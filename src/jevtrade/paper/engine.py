"""Live paper loop: process each newly closed candle exactly once.

Designed to run unattended on your own machine or a small VM (see docs/stages.md),
not in a hosted notebook or chat session. Every step:

1. Syncs public candles up to the last closed bar (public endpoints only).
2. Finds bars after the last processed one. A bar is ready once every
   symbol it holds (or has an order queued for) has a candle at or after it,
   or `stall_grace_bars` have passed. A flat coin with no candle yet is not
   waited for: Coinbase publishes no candle for an hour without trades, and
   a coin the account doesn't hold has nothing to protect that hour.
3. Processes each ready bar through the same `Simulator` the backtest uses,
   in one SQLite transaction: fills, decisions, trades, equity and state
   commit together. A crash mid-bar rolls the whole bar back and the next
   step redoes it, so restarts never double-count or skip a candle.

If the loop was down for longer than `max_catchup_bars`, the older missed
bars are processed risk-only: stops and holding limits still apply to open
positions, but the model is not called and nothing new is opened.

Paper only: fills are simulated from candles. Nothing here can place,
cancel or query an order on an exchange.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

from ..backtest.engine import state_window
from ..data.fetcher import OHLCVSource, has_deep_history, page_limit_for
from ..data.store import CandleStore
from ..data.sync import sync_symbol
from ..data.timeframes import iso_to_ms, last_closed_open_ms, timeframe_ms
from ..decision.base import Decision, DecisionModel
from ..decision.log import DecisionLog
from ..features.compute import compute_features
from ..sim import SimState, Simulator, Trade
from ..state.builder import MarketState, build_state
from .store import PaperStore

log = logging.getLogger(__name__)


class PaperConfig(BaseModel):
    run_id: str = "paper"             # rows in decisions / paper_* are keyed by this
    initial_equity: float = Field(10_000.0, gt=0)
    fee_bps: float = Field(10.0, ge=0, lt=10_000)
    slippage_bps: float = Field(2.0, ge=0, lt=10_000)
    poll_delay_s: float = Field(30.0, ge=0)   # wait after a candle closes before syncing
    retry_s: float = Field(60.0, gt=0)        # wait after a failed step
    max_catchup_bars: int = Field(48, ge=1)   # missed bars beyond this are processed risk-only
    stall_grace_bars: int = Field(1, ge=0)    # process a bar without a lagging held symbol after this
    log_inputs: bool = True                   # store each state text with its decision
    daily_report: bool = True                 # write the report for each UTC day as it ends
    lock_path: str | None = None              # default: <sqlite_path>.paper.lock


class PaperLockError(RuntimeError):
    pass


class PaperLock:
    """Exclusive file lock so only one paper loop writes a store at a time."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "PaperLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise PaperLockError(f"another paper loop holds {self.path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


@dataclass
class StepResult:
    processed: list[int] = field(default_factory=list)   # bar timestamps committed this step
    risk_only: list[int] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)  # symbols whose latest candle is late
    trades: list[Trade] = field(default_factory=list)
    equity: float | None = None


class PaperTrader:
    def __init__(
        self,
        app_cfg,                       # jevtrade.config.AppConfig
        conn: sqlite3.Connection,
        model: DecisionModel,
        source: OHLCVSource | None,    # None: candles are synced by something else
    ):
        self.cfg = app_cfg
        self.pcfg: PaperConfig = app_cfg.paper
        self.conn = conn
        self.model = model
        self.source = source
        self.tf_ms = timeframe_ms(app_cfg.data.timeframe)
        self.candles = CandleStore(conn)
        self.store = PaperStore(conn, self.pcfg.run_id)
        self.dlog = DecisionLog(conn, commit=False)
        self.questions = app_cfg.decision.resolved_questions(app_cfg.data.timeframe)
        self.sim = Simulator(app_cfg.policy.for_model(model.name), self.tf_ms,
                             self.pcfg.fee_bps, self.pcfg.slippage_bps)

    # -- data ------------------------------------------------------------------

    def sync(self, now_ms: int) -> None:
        if self.source is None:
            return
        d = self.cfg.data
        for sym in d.symbols:
            sync_symbol(self.source, self.candles, d.exchange, sym, d.timeframe, self.tf_ms,
                        iso_to_ms(d.start), now_ms=now_ms,
                        page_limit=page_limit_for(d.exchange, d.page_limit),
                        deep_history=has_deep_history(d.exchange))

    def _load(self) -> dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[int, int]]]:
        d = self.cfg.data
        out = {}
        for sym in d.symbols:
            df = self.candles.load(d.exchange, sym, d.timeframe)
            if df.empty:
                continue
            # Full history: RSI is recursive, so a truncated history would drift
            # from the backtest's state text.
            rows = {int(ix.value // 1_000_000): i for i, ix in enumerate(df.index)}
            out[sym] = (df, compute_features(df, self.cfg.features), rows)
        return out

    # -- main step -------------------------------------------------------------

    def _init_state(self, latest: int) -> SimState:
        loaded = self.store.load()
        if loaded is None:
            # A fresh run starts trading at the latest closed bar; history is not replayed.
            st = SimState.new(self.pcfg.initial_equity, latest)
            st.last_bar_ts = latest - self.tf_ms
            return st
        st, model, version = loaded
        if (model, version) != (self.model.name, self.model.version):
            raise SystemExit(
                f"paper run {self.pcfg.run_id!r} was started with {model} ({version}), not "
                f"{self.model.name} ({self.model.version}); set paper.run_id to start a new run")
        return st

    def step(self, now_ms: int | None = None) -> StepResult:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        self.sync(now_ms)
        latest = last_closed_open_ms(now_ms, self.tf_ms)
        data = self._load()
        res = StepResult()
        if not data:
            log.warning("no stored candles yet")
            return res
        st = self._init_state(latest)

        timeline = sorted({t for _, _, rows in data.values() for t in rows if st.last_bar_ts < t <= latest})
        newest = {s: max(rows) for s, (_, _, rows) in data.items()}
        for n, ts in enumerate(timeline):
            # Only coins with a position or a queued order are worth waiting for.
            late = [s for s in self.cfg.data.symbols
                    if newest.get(s, -1) < ts and (s in st.open or s in st.pending)]
            if late and latest - ts < self.pcfg.stall_grace_bars * self.tf_ms:
                res.waiting_on = late
                break  # wait for the lagging candle rather than skip that symbol's bar
            if self.store.processed(ts):  # defensive: the ledger wins over the state blob
                st = self.store.load()[0]
                continue
            decide = len(timeline) - n <= self.pcfg.max_catchup_bars
            res.trades += self._process_bar(st, ts, data, decide)
            res.processed.append(ts)
            if not decide:
                res.risk_only.append(ts)
        if self.store.load() is not None:
            self.store.heartbeat()
        res.equity = st.equity()
        return res

    def _process_bar(self, st: SimState, ts: int, data, decide: bool) -> list[Trade]:
        """One bar, one transaction. On any error the bar is rolled back."""
        bars = {s: df.iloc[rows[ts]] for s, (df, _, rows) in data.items() if ts in rows}
        before = st.to_json()
        trades: list[Trade] = []
        try:
            with self.conn:
                self.sim.begin_bar(st, ts)
                trades += self.sim.fill_pending(st, ts, bars)
                window = state_window(self.cfg.state, self.cfg.features)
                # Ask the model about every symbol first, then act in the same
                # order as the backtest: held first, then strongest edge.
                states: dict[str, MarketState | None] = {}
                decisions: dict[str, Decision | None] = {}
                for sym, (df, feats, rows) in data.items():
                    i = rows.get(ts)
                    if i is None:
                        continue
                    decision: Decision | None = None
                    state = None
                    if decide:
                        lo = max(0, i + 1 - window)
                        state = build_state(df.iloc[lo:i + 1], feats.iloc[lo:i + 1],
                                            self.cfg.decision.horizon_bars, self.cfg.data.timeframe,
                                            self.cfg.state, self.cfg.features)
                        if state is not None:
                            decision = self.model.decide(state, self.questions)
                    if state is None and sym not in st.open and decide:
                        self.sim.mark(st, sym, bars[sym])
                        continue  # warm-up: nothing to decide and nothing to protect
                    states[sym], decisions[sym] = state, decision
                for sym in self.sim.symbols_by_priority(st, decisions):
                    state, decision = states[sym], decisions[sym]
                    action, trade = self.sim.on_close(st, sym, ts, bars[sym], decision)
                    if trade is not None:
                        trades.append(trade)
                    if decision is not None:
                        did = self.dlog.record(self.pcfg.run_id, sym, ts, decision,
                                               state.text if self.pcfg.log_inputs else None)
                        self.dlog.annotate(did, action.passed_threshold, action.kind, action.reason)
                    if action.kind in ("enter", "exit"):
                        log.info("%s %s %s: %s", pd.Timestamp(ts, unit="ms", tz="UTC"), sym,
                                 action.kind, action.reason)
                st.last_bar_ts = ts
                self.store.commit_bar(st, self.model.name, self.model.version, ts,
                                      "full" if decide else "risk_only", trades)
        except BaseException:
            restored = SimState.from_json(before)
            st.__dict__.update(restored.__dict__)
            raise
        return trades


def seconds_until_next_close(now_ms: int, tf_ms: int, delay_s: float) -> float:
    """Seconds until the current candle closes, plus `delay_s` for the exchange to publish it."""
    next_close = (now_ms // tf_ms + 1) * tf_ms
    return max(0.0, (next_close - now_ms) / 1000 + delay_s)
