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

Once a decision's horizon has closed, the realized outcome (close-to-close
change, direction label, deepest dip below the decision close) goes to
`<out>/outcomes/<YYYY-MM-DD>.jsonl`, keyed the same way. Nothing here trades
or simulates positions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..backtest.engine import state_window
from ..data.fetcher import OHLCVSource, fetch_range, has_deep_history, page_limit_for
from ..data.timeframes import last_closed_open_ms, ms_to_iso, timeframe_ms
from ..decision.base import Decision, DecisionModel, QuestionSpec
from ..features.compute import compute_features
from ..state.builder import build_state

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Extra bars before the state window so the recursive RSI has settled.
_WARMUP_BARS = 200

README = """# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Nothing here trades or simulates positions.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC). `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response; that
  candle is asked again on a later run). `answers` holds every answer as Jev
  returned it; a Noul's `noul` is P(yes).
- `outcomes/YYYY-MM-DD.jsonl`: the realized outcome of each decision once its
  horizon has closed, keyed by `symbol` and `candle_ts`.
"""


@dataclass
class CollectResult:
    called: list[tuple[str, int]] = field(default_factory=list)   # (symbol, candle_ts)
    errors: list[tuple[str, int]] = field(default_factory=list)
    outcomes: list[tuple[str, int]] = field(default_factory=list)
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


def candles_frame(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.astype(float)


def decision_record(symbol: str, ts: int, close: float, d: Decision, qhash: str,
                    state_text: str, called_at_ms: int) -> dict:
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
        "state": state_text,
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
        self.window = state_window(app_cfg.state, app_cfg.features)

    def fetch(self, symbol: str, latest: int) -> pd.DataFrame:
        d = self.cfg.data
        lookback = self.window + self.max_backfill + _WARMUP_BARS
        rows = fetch_range(self.source, symbol, d.timeframe, latest - lookback * self.tf_ms,
                           latest, self.tf_ms, page_limit_for(d.exchange, d.page_limit),
                           skip_empty=has_deep_history(d.exchange))
        return candles_frame(rows) if rows else candles_frame([])

    def run(self, now_ms: int | None = None) -> CollectResult:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        latest = last_closed_open_ms(now_ms, self.tf_ms)
        first = latest - (self.max_backfill - 1) * self.tf_ms
        horizon = self.cfg.decision.horizon_bars
        self.out.mkdir(parents=True, exist_ok=True)
        readme = self.out / "README.md"
        if not readme.exists():
            readme.write_text(README)

        # Decisions old enough to still be waiting on an outcome are re-read too.
        oldest = first - (horizon + self.max_backfill) * self.tf_ms
        logged = self.decisions.read(oldest, latest)
        done = {(r["symbol"], r["candle_ts"]) for r in logged if r.get("status") != "error"}
        have_outcome = {(r["symbol"], r["candle_ts"]) for r in self.outcomes.read(oldest, latest)}

        res = CollectResult()
        for sym in self.cfg.data.symbols:
            df = self.fetch(sym, latest)
            if df.empty:
                log.warning("%s: no candles returned", sym)
                continue
            feats = compute_features(df, self.cfg.features)
            pos = {int(ix.value // 1_000_000): i for i, ix in enumerate(df.index)}
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
                rec = decision_record(sym, ts, float(df["close"].iloc[i]), d, self.qhash,
                                      state.text, int(time.time() * 1000))
                self.decisions.append(rec)
                logged.append(rec)
                res.called.append((sym, ts))
                res.cost_usd += d.cost_usd
                if rec["status"] == "error":
                    res.errors.append((sym, ts))
                log.info("%s %s: %s", sym, rec["candle_open"], rec["status"])

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
        return res
