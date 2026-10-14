"""Hourly forward collection: Jev trades its paper account, one closed candle at a time.

Each run fetches recent public candles for every coin, then walks the closed
candles not yet asked about, oldest first. For each hour it replays the
account from the log (jevtrade.forward.agent), shows Jev each coin's market
state with the account and that coin's position, and asks the fixed
question that fits (jevtrade.decision.agent): `entry` for a coin the account
doesn't hold, `manage` for one it does. Then it executes the hour's answers,
so the next hour's position is what Jev just chose. One JSON line per call
goes to `<out>/decisions/<YYYY-MM-DD>.jsonl` (the day of the candle's open
time, UTC) as soon as its call returns, so a run that dies midway keeps
everything it paid for.

An hour's answers are final: a call that failed (`status: "error"`) or
abstained does nothing and is not asked again, and a run never asks about an
hour older than the newest one logged, because later positions were shown to
Jev on top of it. The newest hour can still be completed for coins a dead
run never reached.

The state text Jev was shown (a few KB per call, most of a decision line's
size) goes to `<out>/state/<YYYY-MM-DD>.jsonl.gz` instead, keyed by `symbol`,
`candle_ts` and `input_hash`. Each run appends one gzip member, so
the file only grows at the end and git stores each hour as a small delta.

Once a decision's horizon has closed, the realized outcome (close-to-close
change, direction label, deepest dip below the decision close) goes to
`<out>/outcomes/<YYYY-MM-DD>.jsonl`, keyed the same way, to score the call.

With a `prices` source, each run also reads every coin's Coinbase price once,
before asking Jev, and logs it on that run's newest-candle lines (`cb_price`,
`cb_price_at`): the account fills that hour's trades at it. Simulated only:
nothing here places an order.
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
from typing import Callable

import pandas as pd

from ..data.fetcher import OHLCVSource, fetch_range
from ..data.timeframes import last_closed_open_ms, ms_to_iso, timeframe_ms
from ..decision import agent
from ..decision.base import Decision, DecisionModel, QuestionSpec
from ..forward import agent as account
from ..features.compute import compute_features
from ..state.builder import build_state, state_window

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2   # 2: Jev chooses the trade (question, shown, prompt_version)
# Extra bars before the state window so the recursive RSI has settled.
_WARMUP_BARS = 200

README = """# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Jev trades a paper account here: each line is its choice for one coin and
hour. Nothing places real orders.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC), with that candle's `open`, `high`,
  `low`, `close` and `volume` (base units) and `called_at`, when the call was made.
  From schema 2 on, `question` is the question asked (`entry` when the account
  didn't hold the coin, `manage` when it did), `shown` is the account and
  position Jev was shown, and `prompt_version` names the prompt's wording.
  Lines for the newest candle of a run also carry the coin's Coinbase price at
  the start of that run (`cb_price`, `cb_price_at`; absent when Coinbase couldn't
  be read): that hour's trades fill at it. `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response); either
  of the last two does nothing. `answers` holds every answer as Jev returned it.
  Schema 1 lines (before the agent prompt) answered the old direction questions.
- `state/YYYY-MM-DD.jsonl.gz`: the state text Jev was shown for each call,
  keyed by `symbol`, `candle_ts` and `input_hash` (gzip; `zcat` reads it).
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

    def read_all(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        names = sorted(p.name for p in self.root.glob("*.jsonl"))
        if not names:
            return []
        day_ms = lambda n: int(datetime.fromisoformat(n[:10]).replace(tzinfo=timezone.utc).timestamp() * 1000)  # noqa: E731
        return self.read(day_ms(names[0]), day_ms(names[-1]) + 86_400_000 - 1)

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
                    called_at_ms: int, candle: dict | None = None, price: dict | None = None,
                    question: str | None = None, shown: dict | None = None) -> dict:
    """One decision line; the state text goes to `StateLog` (see `state_record`).

    `candle` adds the candle's open, high, low and volume beside `close`, so the
    account can track a position's range and estimate the coin's spread.
    `price` ({"price", "at"}) adds the coin's Coinbase price when the run asked,
    as `cb_price` and `cb_price_at`. `question` and `shown` are the question
    asked and the account and position Jev was shown.
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
        **({"cb_price": price["price"], "cb_price_at": ms_to_iso(price["at"])} if price else {}),
        "status": status,
        "abstain_reason": d.abstain_reason,
        "question": question,
        "shown": shown,
        "prompt_version": agent.PROMPT_VERSION,
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
                 max_backfill: int = 24, prices: Callable[[list[str]], dict[str, float]] | None = None):
        self.cfg = app_cfg
        self.prices = prices
        self.model = model
        self.source = source
        self.out = Path(out_dir)
        self.max_backfill = max_backfill
        self.tf_ms = timeframe_ms(app_cfg.data.timeframe)
        self.questions, self.side_cost = agent.configured(app_cfg)
        self.qhash = questions_hash(list(self.questions.values()))
        self.decisions = JsonlLog(self.out / "decisions")
        self.outcomes = JsonlLog(self.out / "outcomes")
        self.states = StateLog(self.out / "state")
        self.window = state_window(app_cfg.state, app_cfg.features)

    def fetch(self, symbol: str, latest: int) -> pd.DataFrame:
        d = self.cfg.data
        lookback = lookback_bars(self.cfg, self.max_backfill)
        rows = fetch_range(self.source, symbol, d.timeframe, latest - lookback * self.tf_ms,
                           latest, self.tf_ms, d.page_limit)
        return candles_frame(rows) if rows else candles_frame([])

    def run(self, now_ms: int | None = None) -> CollectResult:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        latest = last_closed_open_ms(now_ms, self.tf_ms)
        first = latest - (self.max_backfill - 1) * self.tf_ms
        start = self.cfg.forward_log.start_ms()
        if start is not None:   # fresh start: never ask about an earlier candle
            first = max(first, start)
        self.out.mkdir(parents=True, exist_ok=True)
        readme = self.out / "README.md"
        if not readme.exists() or readme.read_text() != README:
            readme.write_text(README)

        # Every call since the fresh start: the account is replayed from them.
        logged = [r for r in self.decisions.read_all() if start is None or r["candle_ts"] >= start]
        if logged:   # never ask about an hour older than the newest one asked
            first = max(first, max(r["candle_ts"] for r in logged))
        done = {(r["symbol"], r["candle_ts"]) for r in logged}
        acct = account.run(logged, self.cfg.jev_paper, self.tf_ms, until_ts=first)

        res = CollectResult()
        prices: dict[str, dict] = {}
        if self.prices is not None:
            at = int(time.time() * 1000)
            try:
                prices = {s: {"price": p, "at": at} for s, p in self.prices(list(self.cfg.data.symbols)).items()}
            except Exception as e:  # noqa: BLE001 - trades then fill at the candle's close
                log.warning("Coinbase prices unavailable: %s", e)

        frames: dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[int, int]]] = {}
        for sym in self.cfg.data.symbols:
            # One coin's exchange error (delisted, rate limit) must not cost the
            # rest of the pass; that coin sits the hour out.
            try:
                df = self.fetch(sym, latest)
            except Exception as e:  # noqa: BLE001 - ccxt raises many types
                log.warning("%s: candle fetch failed: %s", sym, e)
                res.failed_symbols.append(sym)
                continue
            if df.empty:
                log.warning("%s: no candles returned", sym)
                continue
            frames[sym] = (df, compute_features(df, self.cfg.features),
                           {int(ix.value // 1_000_000): i for i, ix in enumerate(df.index)})

        # One gzip member per run: a member per call compresses about 2.5x worse.
        states: list[dict] = []
        try:
            for ts in range(first, latest + 1, self.tf_ms):
                self._hour(ts, latest, frames, acct, done, logged, states, res, prices)
        finally:   # whatever was asked before a failure keeps its state
            self.states.append(states)
        self._outcomes(latest, frames, logged, res)
        return res

    def _hour(self, ts: int, latest: int, frames: dict, acct: account.Account, done: set, logged: list[dict],
              states: list[dict], res: CollectResult, prices: dict[str, dict]) -> None:
        """Ask Jev about every coin of hour `ts` not asked yet, then execute the hour on `acct`.

        `price` goes only on the `latest` candle's lines: older candles being
        backfilled were decided on hours ago, at other prices.
        """
        rows = [r for r in logged if r["candle_ts"] == ts]
        todo = []
        for sym, (df, feats, pos) in frames.items():
            if (sym, ts) in done or ts not in pos:
                continue
            i = pos[ts]
            lo = max(0, i + 1 - self.window)
            market = build_state(df.iloc[lo:i + 1], feats.iloc[lo:i + 1], self.cfg.decision.horizon_bars,
                                 self.cfg.data.timeframe, self.cfg.state, self.cfg.features)
            if market is None:
                log.warning("%s %s: not enough history for a state", sym, ms_to_iso(ts))
                continue
            bar = df.iloc[i]
            todo.append((sym, market, {k: float(bar[k]) for k in ("open", "high", "low", "close", "volume")}))
        acct.mark(rows + [{"symbol": s, "candle_ts": ts, **c} for s, _, c in todo])
        for sym, market, candle in todo:
            view = acct.view(sym, ts)
            q = "manage" if view.position is not None else "entry"
            state = agent.agent_state(market, view, self.side_cost)
            d = self.model.decide(state, [self.questions[q]])
            rec = decision_record(sym, ts, candle["close"], d, self.qhash, int(time.time() * 1000),
                                  candle, prices.get(sym) if ts == latest else None, q, view.as_dict())
            self.decisions.append(rec)
            states.append(state_record(rec, state.text))
            logged.append(rec)
            rows.append(rec)
            done.add((sym, ts))
            res.called.append((sym, ts))
            res.cost_usd += d.cost_usd
            if rec["status"] == "error":
                res.errors.append((sym, ts))
            log.info("%s %s: %s %s", sym, rec["candle_open"], q,
                     ((rec.get("answers") or {}).get(q) or {}).get("choice") or rec["status"])
        acct.apply(ts, rows)

    def _outcomes(self, latest: int, frames: dict, logged: list[dict], res: CollectResult) -> None:
        """Log the outcome of every recent call whose horizon has closed."""
        horizon = self.cfg.decision.horizon_bars
        oldest = latest - (horizon + self.max_backfill) * self.tf_ms
        have = {(r["symbol"], r["candle_ts"]) for r in self.outcomes.read(oldest, latest)}
        for rec in logged:
            key = (rec["symbol"], rec["candle_ts"])
            if rec["candle_ts"] < oldest or key in have or rec.get("status") == "error" or rec["symbol"] not in frames:
                continue
            out = outcome_record(rec, frames[rec["symbol"]][0], self.tf_ms, horizon,
                                 self.cfg.decision.flat_band_pct, self.cfg.decision.adverse_move_pct)
            if out is not None:
                self.outcomes.append(out)
                have.add(key)
                res.outcomes.append(key)
