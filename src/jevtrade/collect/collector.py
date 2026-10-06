"""Hourly forward collection of Jev answers and their realized outcomes.

Each run fetches recent public candles, asks Jev the configured questions once
for every closed candle (per symbol) that has no answer logged yet, within the
last `max_backfill` candles, and appends one JSON line per call to
`<out>/decisions/<YYYY-MM-DD>.jsonl` (the day of the candle's open time, UTC).
Each line is written as soon as its call returns, so a run that dies midway
keeps everything it paid for.

A candle counts as done once Jev answered it, or abstained on a response it
did send. Only calls that got no response at all (network errors, 5xx after
retries) are logged with status "error" and asked again on a later run.

The state text Jev was shown (a few KB per call, most of a decision line's
size) goes to `<out>/state/<YYYY-MM-DD>.jsonl.gz` instead, keyed by `symbol`,
`candle_ts` and `input_hash`. Each run appends one gzip member, so
the file only grows at the end and git stores each hour as a small delta.
Decision lines written before this split still carry `state` inline.

Once a decision's horizon has closed, the realized outcome (close-to-close
change, direction label, deepest dip below the decision close) goes to
`<out>/outcomes/<YYYY-MM-DD>.jsonl`, keyed the same way. Nothing here trades
or simulates positions.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..data.fetcher import OHLCVSource, fetch_range, has_deep_history, page_limit_for
from ..data.timeframes import last_closed_open_ms, ms_to_iso, timeframe_ms
from ..decision.base import Decision, DecisionModel, QuestionSpec
from ..features.compute import compute_features
from ..state.builder import build_state, state_window

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Extra bars before the state window so the recursive RSI has settled.
_WARMUP_BARS = 200

README = """# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Nothing here trades or simulates positions.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC), with that candle's `open`, `high`,
  `low`, `close` and `volume` (base units; older lines have only `close`) and `called_at`, when the call was made.
  Lines from 2026-10-05 and 2026-10-06 may also carry a Robinhood quote
  (`rh_bid`, `rh_ask`, `rh_quote_at`); it is no longer logged. `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response; that
  candle is asked again on a later run). `answers` holds every answer as Jev
  returned it; a Noul's `noul` is P(yes).
- `state/YYYY-MM-DD.jsonl.gz`: the state text Jev was shown for each call,
  keyed by `symbol`, `candle_ts` and `input_hash` (gzip; `zcat` reads it).
  Decision lines from before 2026-10-05 still carry it inline as `state`.
- `outcomes/YYYY-MM-DD.jsonl`: the realized outcome of each decision once its
  horizon has closed, keyed by `symbol` and `candle_ts`.
"""


@dataclass
class CollectResult:
    called: list[tuple[str, int]] = field(default_factory=list)   # (symbol, candle_ts)
    errors: list[tuple[str, int]] = field(default_factory=list)
    outcomes: list[tuple[str, int]] = field(default_factory=list)
    failed_symbols: list[str] = field(default_factory=list)       # candle fetch failed
    cost_usd: float = 0.0


def questions_hash(questions: list[QuestionSpec]) -> str:
    blob = json.dumps([q.model_dump() for q in questions], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class JsonlLog:
    """Day-partitioned JSONL files under one directory, keyed by (symbol, candle_ts)."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, ts_ms: int) -> Path:
        return self.root / f"{_day(ts_ms)}.jsonl"

    def read(self, since_ms: int, until_ms: int) -> list[dict]:
        out = []
        d = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).date()
        last = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).date()
        while d <= last:
            p = self.root / f"{d.isoformat()}.jsonl"
            if p.exists():
                for line in p.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("skipping unreadable line in %s", p)
                        continue
                    if since_ms <= rec.get("candle_ts", -1) <= until_ms:
                        out.append(rec)
            d += timedelta(days=1)
        return out

    def append(self, rec: dict) -> None:
        p = self.path(rec["candle_ts"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


class StateLog:
    """Day-partitioned gzip JSONL of the state text behind each decision.

    Appends a gzip member per batch; readers (`gzip`, `zcat`) see one stream.
    """

    def __init__(self, root: Path):
        self.root = root

    def path(self, ts_ms: int) -> Path:
        return self.root / f"{_day(ts_ms)}.jsonl.gz"

    def append(self, recs: list[dict]) -> None:
        by_day: dict[Path, list[dict]] = {}
        for r in recs:
            by_day.setdefault(self.path(r["candle_ts"]), []).append(r)
        for p, rs in by_day.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rs)
            # mtime=0 keeps the bytes reproducible for the same input.
            with p.open("ab") as f:
                f.write(gzip.compress(body.encode(), mtime=0))

    def read(self, day: str) -> list[dict]:
        p = self.root / f"{day}.jsonl.gz"
        if not p.exists():
            return []
        return [json.loads(line) for line in gzip.decompress(p.read_bytes()).decode().splitlines() if line.strip()]


def state_record(rec: dict, state_text: str) -> dict:
    return {"symbol": rec["symbol"], "candle_ts": rec["candle_ts"], "input_hash": rec["input_hash"],
            "state": state_text}


def lookback_bars(app_cfg, max_backfill: int) -> int:
    """Candles to fetch so the oldest of `max_backfill` decision bars gets the full state window."""
    return state_window(app_cfg.state, app_cfg.features) + max_backfill + _WARMUP_BARS


def candles_frame(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.astype(float)


def decision_record(symbol: str, ts: int, close: float, d: Decision, qhash: str,
                    called_at_ms: int, candle: dict | None = None) -> dict:
    """One decision line; the state text goes to `StateLog` (see `state_record`).

    `candle` adds the candle's open, high, low and volume beside `close`, so the
    dashboard's replay can trigger stops on intrabar lows and size by volume.
    """
    if not d.abstain:
        status = "answered"
    elif (d.abstain_reason or "").startswith("request failed"):
        status = "error"
    else:
        status = "abstain"
    x = d.extra
    return {
        "schema": SCHEMA_VERSION,
        "symbol": symbol,
        "candle_ts": ts,
        "candle_open": ms_to_iso(ts),
        "close": close,
        **{k: (candle or {}).get(k) for k in ("open", "high", "low", "volume")},
        "called_at": ms_to_iso(called_at_ms),
        "status": status,
        "abstain_reason": d.abstain_reason,
        "requested_model": x.get("requested_model"),
        "served_model": x.get("served_model"),
        "questions_hash": qhash,
        "answers": x.get("answers"),
        "probs": d.probs or None,
        "latency_ms": round(d.latency_ms, 1),
        "input_tokens": d.input_tokens,
        "cost_usd": d.cost_usd,
        "attempts": x.get("attempts"),
        "generation_id": x.get("generation_id"),
        "input_hash": d.input_hash,
        # Keep the body only when it explains a failure; answers carry the rest.
        "raw_response": d.raw_output if d.abstain else None,
    }


def outcome_record(dec: dict, df: pd.DataFrame, tf_ms: int, horizon: int,
                   flat_band_pct: float, adverse_move_pct: float) -> dict | None:
    """Realized outcome of one decision, or None until every horizon bar is in `df`."""
    ts = dec["candle_ts"]
    want = [pd.Timestamp(ts + k * tf_ms, unit="ms", tz="UTC") for k in range(1, horizon + 1)]
    if any(t not in df.index for t in want):
        return None
    window = df.loc[want]
    c0 = float(dec["close"])
    ret_pct = (float(window["close"].iloc[-1]) / c0 - 1) * 100
    if ret_pct > flat_band_pct:
        direction = "up"
    elif ret_pct < -flat_band_pct:
        direction = "down"
    else:
        direction = "flat"
    dip_pct = (float(window["low"].min()) / c0 - 1) * 100
    return {
        "schema": SCHEMA_VERSION,
        "symbol": dec["symbol"],
        "candle_ts": ts,
        "candle_open": dec["candle_open"],
        "horizon_bars": horizon,
        "close": c0,
        "close_at_horizon": float(window["close"].iloc[-1]),
        "ret_pct": round(ret_pct, 4),
        "direction": direction,
        "flat_band_pct": flat_band_pct,
        "min_low_pct": round(dip_pct, 4),
        "adverse_move": dip_pct < -adverse_move_pct,
        "adverse_move_pct": adverse_move_pct,
    }


class Collector:
    def __init__(self, app_cfg, model: DecisionModel, source: OHLCVSource, out_dir: str | Path,
                 max_backfill: int = 24):
        self.cfg = app_cfg
        self.model = model
        self.source = source
        self.out = Path(out_dir)
        self.max_backfill = max_backfill
        self.tf_ms = timeframe_ms(app_cfg.data.timeframe)
        self.questions = app_cfg.decision.resolved_questions(app_cfg.data.timeframe)
        self.qhash = questions_hash(self.questions)
        self.decisions = JsonlLog(self.out / "decisions")
        self.outcomes = JsonlLog(self.out / "outcomes")
        self.states = StateLog(self.out / "state")
        self.window = state_window(app_cfg.state, app_cfg.features)

    def fetch(self, symbol: str, latest: int) -> pd.DataFrame:
        d = self.cfg.data
        lookback = lookback_bars(self.cfg, self.max_backfill)
        rows = fetch_range(self.source, symbol, d.timeframe, latest - lookback * self.tf_ms,
                           latest, self.tf_ms, page_limit_for(d.exchange, d.page_limit),
                           skip_empty=has_deep_history(d.exchange))
        return candles_frame(rows) if rows else candles_frame([])

    def run(self, now_ms: int | None = None) -> CollectResult:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        latest = last_closed_open_ms(now_ms, self.tf_ms)
        first = latest - (self.max_backfill - 1) * self.tf_ms
        start = self.cfg.forward_log.start_ms()
        if start is not None:   # fresh start: never ask about an earlier candle
            first = max(first, start)
        horizon = self.cfg.decision.horizon_bars
        self.out.mkdir(parents=True, exist_ok=True)
        readme = self.out / "README.md"
        if not readme.exists() or readme.read_text() != README:
            readme.write_text(README)

        # Decisions old enough to still be waiting on an outcome are re-read too.
        oldest = first - (horizon + self.max_backfill) * self.tf_ms
        logged = self.decisions.read(oldest, latest)
        done = {(r["symbol"], r["candle_ts"]) for r in logged if r.get("status") != "error"}
        have_outcome = {(r["symbol"], r["candle_ts"]) for r in self.outcomes.read(oldest, latest)}

        res = CollectResult()
        # One gzip member per run: a member per call compresses about 2.5x worse.
        states: list[dict] = []
        try:
            for sym in self.cfg.data.symbols:
                # One coin's exchange error (delisted, rate limit) must not cost the
                # rest of the pass; that coin is retried next hour.
                try:
                    df = self.fetch(sym, latest)
                except Exception as e:  # noqa: BLE001 - ccxt raises many types
                    log.warning("%s: candle fetch failed: %s", sym, e)
                    res.failed_symbols.append(sym)
                    continue
                if df.empty:
                    log.warning("%s: no candles returned", sym)
                    continue
                feats = compute_features(df, self.cfg.features)
                pos = {int(ix.value // 1_000_000): i for i, ix in enumerate(df.index)}
                self._decide(sym, df, feats, pos, first, latest, done, logged, states, res)

                for rec in logged:
                    key = (rec["symbol"], rec["candle_ts"])
                    if rec["symbol"] != sym or rec.get("status") == "error" or key in have_outcome:
                        continue
                    out = outcome_record(rec, df, self.tf_ms, horizon, self.cfg.decision.flat_band_pct,
                                         self.cfg.decision.adverse_move_pct)
                    if out is not None:
                        self.outcomes.append(out)
                        have_outcome.add(key)
                        res.outcomes.append(key)
        finally:   # whatever was asked before a failure keeps its state
            self.states.append(states)
        return res

    def _decide(self, sym: str, df: pd.DataFrame, feats, pos: dict[int, int], first: int, latest: int,
                done: set, logged: list[dict], states: list[dict], res: CollectResult) -> None:
        """Ask Jev about every candle of `sym` in [first, latest] not yet answered."""
        horizon = self.cfg.decision.horizon_bars
        for ts in range(first, latest + 1, self.tf_ms):
            if (sym, ts) in done or ts not in pos:
                continue
            i = pos[ts]
            lo = max(0, i + 1 - self.window)
            state = build_state(df.iloc[lo:i + 1], feats.iloc[lo:i + 1], horizon,
                                self.cfg.data.timeframe, self.cfg.state, self.cfg.features)
            if state is None:
                log.warning("%s %s: not enough history for a state", sym, ms_to_iso(ts))
                continue
            d = self.model.decide(state, self.questions)
            bar = df.iloc[i]
            rec = decision_record(sym, ts, float(bar["close"]), d, self.qhash, int(time.time() * 1000),
                                  {k: float(bar[k]) for k in ("open", "high", "low", "volume")})
            self.decisions.append(rec)
            states.append(state_record(rec, state.text))
            logged.append(rec)
            res.called.append((sym, ts))
            res.cost_usd += d.cost_usd
            if rec["status"] == "error":
                res.errors.append((sym, ts))
            log.info("%s %s: %s", sym, rec["candle_open"], rec["status"])
