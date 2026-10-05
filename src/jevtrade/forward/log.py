"""Read-only view of the hourly forward log (the collector's `data-log` branch).

The collect workflow writes `decisions/YYYY-MM-DD.jsonl` and
`outcomes/YYYY-MM-DD.jsonl` (see jevtrade.collect.collector); it never
reads `state/`, the gzip files of state text. This module
loads them from a local directory or from GitHub over HTTPS, joins each
decision with its outcome, and scores Jev's direction calls against naive
baselines. It only reads: no writes, no model calls, no exchange calls.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .. import net
from ..net import HttpGet, NotFound, redact
from .settings import ForwardLogConfig

log = logging.getLogger(__name__)

DIRECTIONS = ("up", "flat", "down")
FOLDERS = ("decisions", "outcomes")
# Confidence bins for the calibration table: [lo, hi).
CAL_EDGES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
_DAYFILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
_DROP = ("state", "raw_response")   # large, and not for the browser
_EPS = 1e-6
_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"


def parse_jsonl(text: str) -> list[dict]:
    """One record per non-empty line, without the large `state`/`raw_response` fields."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            log.warning("skipping unreadable forward-log line")
            continue
        if isinstance(rec, dict) and "symbol" in rec and "candle_ts" in rec:
            for k in _DROP:
                rec.pop(k, None)
            out.append(rec)
    return out


# --------------------------------------------------------------- join


def _final_decisions(decisions: list[dict]) -> dict[tuple[str, int], dict]:
    """One decision per (symbol, candle_ts). An `error` line is superseded by its retry."""
    final: dict[tuple[str, int], dict] = {}
    for d in sorted(decisions, key=lambda r: str(r.get("called_at") or "")):
        key = (d["symbol"], d["candle_ts"])
        prev = final.get(key)
        if prev is None or prev.get("status") == "error" or d.get("status") != "error":
            final[key] = d
    return final


def _confidence(ans: dict) -> float | None:
    if ans.get("confidence") is not None:
        return float(ans["confidence"])
    probs = ans.get("probabilities") or {}
    return float(max(probs.values())) if probs else None


def join(decisions: list[dict], outcomes: list[dict]) -> list[dict]:
    """Decision rows with `outcome` (None until the horizon closes) and a scored `hit`."""
    outs = {(o["symbol"], o["candle_ts"]): o for o in outcomes}
    rows = []
    for key, d in _final_decisions(decisions).items():
        answers = d.get("answers") or {}
        direction = answers.get("direction") or {}
        o = outs.get(key)
        pred = direction.get("choice") if d.get("status") == "answered" else None
        if pred not in DIRECTIONS:
            pred = None
        realized = o.get("direction") if o else None
        row = {k: v for k, v in d.items() if k not in _DROP}
        row.update(
            predicted=pred,
            confidence=_confidence(direction) if pred else None,
            regime=(answers.get("regime") or {}).get("choice") if pred else None,
            p_adverse=(answers.get("adverse_move") or {}).get("noul") if pred else None,
            outcome=o,
            hit=(pred == realized) if pred and realized in DIRECTIONS else None,
        )
        rows.append(row)
    rows.sort(key=lambda r: (-r["candle_ts"], r["symbol"]))
    return rows


# --------------------------------------------------------------- metrics


def _norm(probs: dict, labels) -> list[float] | None:
    vals = [max(float(probs.get(k) or 0.0), 0.0) for k in labels]
    s = sum(vals)
    return [v / s for v in vals] if s > 0 else None


def _rate(k: int, n: int) -> float | None:
    return k / n if n else None


def metrics(rows: list[dict], cost_usd: float = 0.0) -> dict:
    """Counts, hit rate vs. baselines, confusion matrix, calibration and scores for `rows`."""
    counts = Counter(r.get("status") for r in rows)
    scored = [r for r in rows if r["hit"] is not None]
    n = len(scored)
    realized = Counter(r["outcome"]["direction"] for r in scored)
    # Most common realized class, in hindsight; ties go to flat, the usual majority.
    majority = max(DIRECTIONS, key=lambda c: (realized[c], c == "flat")) if n else None

    confusion = {p: {a: 0 for a in DIRECTIONS} for p in DIRECTIONS}
    for r in scored:
        confusion[r["predicted"]][r["outcome"]["direction"]] += 1

    calibration = []
    for lo, hi in zip(CAL_EDGES, CAL_EDGES[1:]):
        b = [r for r in scored if r["confidence"] is not None
             and lo <= r["confidence"] < (hi if hi < 1 else math.inf)]
        calibration.append({"lo": lo, "hi": hi, "n": len(b),
                            "mean_confidence": sum(r["confidence"] for r in b) / len(b) if b else None,
                            "hit_rate": _rate(sum(r["hit"] for r in b), len(b))})

    # Direction: multi-class Brier (sum over classes) and log loss, next to the
    # Brier of always forecasting the realized base rates.
    briers, losses = [], []
    freq = [realized[c] / n for c in DIRECTIONS] if n else None
    base_briers = []
    for r in scored:
        p = _norm(((r.get("answers") or {}).get("direction") or {}).get("probabilities") or {}, DIRECTIONS)
        if p is None:
            continue
        y = [1.0 if c == r["outcome"]["direction"] else 0.0 for c in DIRECTIONS]
        briers.append(sum((pi - yi) ** 2 for pi, yi in zip(p, y)))
        losses.append(-math.log(max(p[y.index(1.0)], _EPS)))
        base_briers.append(sum((fi - yi) ** 2 for fi, yi in zip(freq, y)))

    # adverse_move is a Noul: `noul` is P(yes).
    adv = [(float(r["p_adverse"]), bool(r["outcome"]["adverse_move"])) for r in rows
           if r["predicted"] and r["outcome"] and r["p_adverse"] is not None
           and r["outcome"].get("adverse_move") is not None]
    adv_rate = _rate(sum(y for _, y in adv), len(adv))

    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return {
        "decisions": len(rows),
        "answered": counts.get("answered", 0),
        "abstain": counts.get("abstain", 0),
        "error": counts.get("error", 0),
        "scored": n,
        "pending": sum(1 for r in rows if r["predicted"] and r["outcome"] is None),
        "hits": sum(r["hit"] for r in scored),
        "hit_rate": _rate(sum(r["hit"] for r in scored), n),
        "realized": {c: realized[c] for c in DIRECTIONS},
        "baselines": {
            "majority": {"label": majority, "hit_rate": _rate(realized[majority], n) if n else None},
            "always_flat": {"label": "flat", "hit_rate": _rate(realized["flat"], n)},
        },
        "confusion": {"labels": list(DIRECTIONS),   # rows: predicted, columns: realized
                      "matrix": [[confusion[p][a] for a in DIRECTIONS] for p in DIRECTIONS]},
        "calibration": calibration,
        "direction_scores": {"n": len(briers), "brier": mean(briers), "log_loss": mean(losses),
                             "brier_base_rate": mean(base_briers)},
        "adverse_move_scores": {
            "n": len(adv),
            "brier": mean([(p - y) ** 2 for p, y in adv]),
            "log_loss": mean([-math.log(max(p if y else 1 - p, _EPS)) for p, y in adv]),
            "base_rate": adv_rate,
            "brier_base_rate": adv_rate * (1 - adv_rate) if adv_rate is not None else None,
        },
        "cost_usd": cost_usd,
    }


# --------------------------------------------------------------- source


@dataclass
class Snapshot:
    rows: list[dict] = field(default_factory=list)
    cost_by_symbol: dict[str, float] = field(default_factory=dict)
    files: int = 0
    fetched_at: float | None = None    # last successful load


class ForwardLog:
    """Cached loader. Re-reads at most every `refresh_seconds`; a failed refresh
    keeps serving the last good data and reports the error."""

    def __init__(self, cfg: ForwardLogConfig, http_get: HttpGet | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._get = http_get
        self._clock = clock
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._error: str | None = None
        self._attempt_at: float | None = None
        self._files: dict[tuple[str, str], tuple[str, list[dict]]] = {}   # (folder, name) -> (sha, records)

    # -- public

    def snapshot(self) -> Snapshot:
        if self.cfg.source == "off":
            return self._snap
        with self._lock:
            now = self._clock()
            wait = self.cfg.refresh_seconds if self._error is None else min(self.cfg.refresh_seconds, 60)
            if self._attempt_at is None or now - self._attempt_at >= wait:
                self._attempt_at = now
                try:
                    self._snap = self._load()
                    self._error = None
                except Exception as e:  # keep the last good data
                    log.warning("forward log refresh failed: %s", e)
                    self._error = redact(f"{type(e).__name__}: {e}")[:300]
            return self._snap

    def summary(self) -> dict:
        snap = self.snapshot()
        by_sym: dict[str, list[dict]] = defaultdict(list)
        for r in snap.rows:
            by_sym[r["symbol"]].append(r)
        called = [r["called_at"] for r in snap.rows if r.get("called_at")]
        c = self.cfg
        return {
            "source": c.source,
            "location": (f"{c.repo}@{c.branch}" if c.source == "github"
                         else c.local_dir if c.source == "local" else None),
            "fetched_at": snap.fetched_at,
            "last_called_at": max(called) if called else None,
            "last_candle_ts": snap.rows[0]["candle_ts"] if snap.rows else None,
            "files": snap.files,
            "error": self._error,
            "overall": metrics(snap.rows, sum(snap.cost_by_symbol.values())),
            "per_symbol": {s: metrics(rs, snap.cost_by_symbol.get(s, 0.0)) for s, rs in sorted(by_sym.items())},
        }

    def rows(self, symbol: str | None = None, limit: int = 100) -> list[dict]:
        rows = self.snapshot().rows
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        return rows[:limit]

    # -- loading

    def _load(self) -> Snapshot:
        read = self._read_github if self.cfg.source == "github" else self._read_local
        data, files = {}, 0
        for f in FOLDERS:
            n, data[f] = read(f)
            files += n
        start = self.cfg.start_ms()
        if start is not None:   # nothing before the fresh start is shown
            data = {f: [r for r in recs if r["candle_ts"] >= start] for f, recs in data.items()}
        cost: dict[str, float] = defaultdict(float)
        for d in data["decisions"]:   # every call, retried errors included
            cost[d["symbol"]] += float(d.get("cost_usd") or 0.0)
        return Snapshot(rows=join(data["decisions"], data["outcomes"]), cost_by_symbol=dict(cost),
                        files=files, fetched_at=self._clock())

    def _newest(self, names: list[str]) -> list[str]:
        return sorted(n for n in names if _DAYFILE.match(n))[-self.cfg.max_days:]

    def _read_local(self, folder: str) -> tuple[int, list[dict]]:
        root = Path(self.cfg.local_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"forward_log.local_dir {root} does not exist")
        d = root / folder
        names = self._newest([p.name for p in d.glob("*.jsonl")]) if d.is_dir() else []
        return len(names), [rec for n in names for rec in parse_jsonl((d / n).read_text())]

    def _token(self) -> str | None:
        return os.environ.get(self.cfg.token_env) or None if self.cfg.token_env else None

    def _headers(self, accept: str) -> dict:
        h = {"User-Agent": "jevtrade-api", "Accept": accept}
        token = self._token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _why_missing(self) -> str:
        """Explain a 404 on decisions/: GitHub also answers 404 for a private repo it won't show."""
        c, get = self.cfg, self._get or net.get
        where = f"{c.repo}@{c.branch}"
        try:
            get(f"{_API}/repos/{c.repo}", self._headers("application/vnd.github+json"), c.timeout_s)
        except NotFound:
            if self._token():
                return (f"GitHub can't see {c.repo} with {c.token_env}: give the token access to this repo"
                        " with read-only Contents permission")
            return f"GitHub can't see {c.repo}; if the repo is private, set {c.token_env}"
        try:
            get(f"{_API}/repos/{c.repo}/branches/{quote(c.branch, safe='')}",
                self._headers("application/vnd.github+json"), c.timeout_s)
        except NotFound:
            return f"no branch {c.branch!r} on {c.repo} yet; the collect workflow creates it on its first run"
        return f"no decisions/ on {where} yet"

    def _read_github(self, folder: str) -> tuple[int, list[dict]]:
        get = self._get or net.get
        c = self.cfg
        ref = quote(c.branch, safe="")
        url = f"{_API}/repos/{c.repo}/contents/{folder}?ref={ref}"
        try:
            listing = json.loads(get(url, self._headers("application/vnd.github+json"), c.timeout_s))
        except NotFound:
            if folder == "decisions":
                raise NotFound(self._why_missing()) from None
            listing = []   # no outcomes until the first horizon closes
        if not isinstance(listing, list):
            raise ValueError(f"unexpected GitHub response for {folder}/")
        entries = {e["name"]: e for e in listing if e.get("type") == "file" and "name" in e}
        names = self._newest(list(entries))
        out = []
        for n in names:
            e = entries[n]
            cached = self._files.get((folder, n))
            if cached and e.get("sha") and cached[0] == e["sha"]:
                recs = cached[1]
            else:   # only new or changed files are downloaded
                if self._token():   # works for private repos; raw.githubusercontent may not
                    body = get(f"{_API}/repos/{c.repo}/contents/{folder}/{n}?ref={ref}",
                               self._headers("application/vnd.github.raw"), c.timeout_s)
                else:
                    body = get(f"{_RAW}/{c.repo}/{quote(c.branch)}/{folder}/{n}", self._headers("*/*"), c.timeout_s)
                recs = parse_jsonl(body.decode("utf-8"))
                self._files[(folder, n)] = (e.get("sha") or "", recs)
            out.extend(recs)
        for key in [k for k in self._files if k[0] == folder and k[1] not in names]:
            del self._files[key]
        return len(names), out
