"""Event-driven walk-forward backtester.

The loop walks closed bars in time order across all symbols and runs the
same pipeline the paper loop will run:

    candles[:t] ─▶ features ─▶ build_state ─▶ DecisionModel ─▶ Policy.evaluate ─▶ fills

Timing (see policy/engine.py):
- `Policy.evaluate` runs on the close of bar t and only sees data up to t.
- Entries and model or holding exits fill at the open of bar t+1, with
  `slippage_bps` against the trader and `fee_bps` on notional.
- Stop-loss exits fill inside bar t at `action.fill_price` (the policy
  already applied `stop_slippage_bps`); only the fee is added.
- An entry's stop is re-anchored to its actual fill price.

Features are causal (tests/test_features.py), so they are computed once per
symbol and sliced at t. The state builder gets a trailing window that is
long enough to produce byte-identical text to the full history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, Field

from ..decision.base import Decision, DecisionModel, QuestionSpec
from ..decision.log import DecisionLog
from ..features.compute import FeatureConfig, compute_features
from ..policy.engine import (
    AccountView, Action, Policy, PolicyConfig, Position, RiskState, register_trade_result, roll_day,
)
from ..state.builder import StateConfig, build_state


class BacktestConfig(BaseModel):
    initial_equity: float = Field(10_000.0, gt=0)
    fee_bps: float = Field(10.0, ge=0, lt=10_000)       # per side, on notional
    slippage_bps: float = Field(2.0, ge=0, lt=10_000)   # next-open market fills
    start: str | None = None   # first decision bar (ISO-8601 UTC); default: all stored data
    end: str | None = None     # last bar (inclusive)
    output_dir: str = "data/backtests"
    log_decisions: bool = True     # write decisions to the decisions table under the run id
    log_inputs: bool = False       # also store the state text (large: ~2KB per decision)


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    qty: float
    fees: float
    pnl: float           # net of fees on both sides
    ret: float           # pnl / entry notional
    bars_held: int       # bars the position was open for (a next-open exit excludes its bar)
    exit_reason: str
    entry_reason: str


@dataclass
class BacktestResult:
    run_id: str
    model: str
    model_version: str
    symbols: list[str]
    timeframe: str
    initial_equity: float
    trades: list[Trade]
    equity: pd.Series                     # equity at each bar close, UTC index
    benchmark: dict[str, float]           # buy-and-hold return per symbol over the run
    counts: dict[str, int] = field(default_factory=dict)  # decisions, abstains, actions by kind
    contamination: str = "n/a"
    sizing: dict = field(default_factory=dict)  # PolicyConfig.describe_sizing at the run's settings


@dataclass
class _Open:
    pos: Position
    entry_fee: float
    entry_reason: str


@dataclass
class _Pending:
    action: Action
    ref_close: float


def _ts_ms(ix: pd.Timestamp) -> int:
    return int(ix.value // 1_000_000)


def state_window(state_cfg: StateConfig, feature_cfg: FeatureConfig) -> int:
    """Trailing rows build_state needs to give the same text as full history."""
    return state_cfg.percentile_window + state_cfg.history_bars + feature_cfg.volume_z_window + 1


def contamination_label(model_version: str, end_ms: int) -> str:
    """Label a Jev run whose period ends before the pinned snapshot's date.

    Jev may have seen that market history in training (README, Evaluation
    validity). Offline models (mock, baseline) cannot be contaminated.
    """
    m = re.search(r"(\d{8})(?!.*\d{8})", model_version)
    if not m or "jev" not in model_version:
        return "n/a"
    snapshot = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
    if end_ms < snapshot.timestamp() * 1000:
        return f"potentially contaminated: period ends before {snapshot:%Y-%m-%d} snapshot"
    return f"partly or fully after {snapshot:%Y-%m-%d} snapshot; check period start"


class Backtester:
    def __init__(
        self,
        model: DecisionModel,
        questions: list[QuestionSpec],
        policy_cfg: PolicyConfig,
        bt_cfg: BacktestConfig,
        tf_ms: int,
        timeframe: str,
        horizon_bars: int,
        feature_cfg: FeatureConfig | None = None,
        state_cfg: StateConfig | None = None,
        decision_log: DecisionLog | None = None,
        run_id: str | None = None,
    ):
        self.model = model
        self.questions = questions
        # max_holding_bars can differ per model (baseline exits on its own signal).
        policy_cfg = policy_cfg.for_model(model.name)
        self.policy = Policy(policy_cfg, tf_ms)
        self.pcfg = policy_cfg
        self.cfg = bt_cfg
        self.tf_ms = tf_ms
        self.timeframe = timeframe
        self.horizon_bars = horizon_bars
        self.feature_cfg = feature_cfg or FeatureConfig()
        self.state_cfg = state_cfg or StateConfig()
        self.dlog = decision_log
        self.run_id = run_id or (
            f"bt-{model.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        )

    # -- fills -------------------------------------------------------------

    def _buy_fill(self, open_px: float) -> float:
        return open_px * (1 + self.cfg.slippage_bps / 10_000)

    def _sell_fill(self, open_px: float) -> float:
        return open_px * (1 - self.cfg.slippage_bps / 10_000)

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.cfg.fee_bps / 10_000

    # -- main loop -----------------------------------------------------------

    def run(self, candles: dict[str, pd.DataFrame]) -> BacktestResult:
        start_ms = _iso_ms(self.cfg.start)
        end_ms = _iso_ms(self.cfg.end)
        data: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for sym, df in candles.items():
            if end_ms is not None:
                df = df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
            if df.empty:
                continue
            data[sym] = (df, compute_features(df, self.feature_cfg))
        if not data:
            raise ValueError("no candles to backtest")

        timeline = sorted({_ts_ms(ix) for df, _ in data.values() for ix in df.index})
        if start_ms is not None:
            timeline = [t for t in timeline if t >= start_ms]
        if not timeline:
            raise ValueError("no bars in the requested period")
        row_of = {sym: {_ts_ms(ix): i for i, ix in enumerate(df.index)} for sym, (df, _) in data.items()}
        window = state_window(self.state_cfg, self.feature_cfg)

        cash = self.cfg.initial_equity
        open_: dict[str, _Open] = {}
        pending: dict[str, _Pending] = {}
        marks: dict[str, float] = {}
        risk = RiskState(day_start_equity=cash, day=timeline[0] // 86_400_000)
        trades: list[Trade] = []
        curve: list[tuple[int, float]] = []
        counts: dict[str, int] = {"decisions": 0, "abstains": 0, "no_state": 0}
        first_close: dict[str, float] = {}

        def equity() -> float:
            return cash + sum(o.pos.qty * marks.get(s, o.pos.entry_price) for s, o in open_.items())

        def close_position(sym: str, ts: int, px: float, reason: str, at_open: bool) -> None:
            nonlocal cash
            o = open_.pop(sym)
            fee = self._fee(o.pos.qty * px)
            cash += o.pos.qty * px - fee
            cost = o.pos.qty * o.pos.entry_price
            pnl = o.pos.qty * px - cost - fee - o.entry_fee
            bars = (ts - o.pos.entry_ts) // self.tf_ms + (0 if at_open else 1)
            trades.append(Trade(sym, o.pos.entry_ts, o.pos.entry_price, ts, px, o.pos.qty,
                                fee + o.entry_fee, pnl, pnl / cost if cost else 0.0, bars,
                                reason, o.entry_reason))
            register_trade_result(risk, pnl, ts, self.tf_ms, self.pcfg, sym)

        for ts in timeline:
            # The daily-loss baseline is equity at the previous close.
            roll_day(risk, ts, equity())

            # 1. Orders decided at the previous close fill at this bar's open.
            for sym in list(pending):
                i = row_of[sym].get(ts)
                if i is None:
                    continue  # no bar for this symbol yet (exchange gap): keep waiting
                bar = data[sym][0].iloc[i]
                p = pending.pop(sym)
                if p.action.kind == "exit" and sym in open_:
                    close_position(sym, ts, self._sell_fill(bar.open), p.action.reason, at_open=True)
                elif p.action.kind == "enter" and sym not in open_:
                    px = self._buy_fill(bar.open)
                    eq = equity()
                    qty = p.action.size_frac * eq / px
                    fee = self._fee(qty * px)
                    if qty * px + fee > cash:  # never borrow; long-only spot
                        qty = max(0.0, (cash - fee) / px)
                        fee = self._fee(qty * px)
                    if qty <= 0:
                        continue
                    cash -= qty * px + fee
                    stop = px * (1 - self.pcfg.stop_loss_pct)
                    open_[sym] = _Open(Position(sym, qty, px, ts, stop), fee, p.action.reason)
                    marks[sym] = bar.open

            # 2. Decide on this bar's close, symbol by symbol.
            for sym, (df, feats) in data.items():
                i = row_of[sym].get(ts)
                if i is None:
                    continue
                bar = df.iloc[i]
                marks[sym] = bar.close
                first_close.setdefault(sym, bar.close)
                lo = max(0, i + 1 - window)
                state = build_state(df.iloc[lo:i + 1], feats.iloc[lo:i + 1], self.horizon_bars,
                                    self.timeframe, self.state_cfg, self.feature_cfg)
                decision: Decision | None = None
                if state is None:
                    counts["no_state"] += 1
                    if sym not in open_:
                        continue  # warm-up: nothing to decide and nothing to protect
                else:
                    decision = self.model.decide(state, self.questions)
                    counts["decisions"] += 1
                    counts["abstains"] += int(decision.abstain)

                # Pending entries count against gross exposure for later symbols.
                positions = {s: o.pos for s, o in open_.items()}
                for s, p in pending.items():
                    if p.action.kind == "enter":
                        q = p.action.size_frac * equity() / p.ref_close
                        positions[s] = Position(s, q, p.ref_close, ts, p.ref_close)
                acct = AccountView(equity(), positions, dict(marks), risk)
                action = self.policy.evaluate(sym, ts, bar.close, decision, acct,
                                              bar_open=bar.open, bar_low=bar.low)
                counts[action.kind] = counts.get(action.kind, 0) + 1

                if action.kind == "exit" and action.fill_price is not None:
                    # Stop: fills inside this bar at the policy's price, not at the next open.
                    close_position(sym, ts, action.fill_price, action.reason, at_open=False)
                elif action.kind in ("enter", "exit"):
                    pending[sym] = _Pending(action, bar.close)

                if self.dlog is not None and decision is not None and self.cfg.log_decisions:
                    did = self.dlog.record(self.run_id, sym, ts, decision,
                                           state.text if self.cfg.log_inputs else None)
                    self.dlog.annotate(did, action.passed_threshold, action.kind, action.reason)

            curve.append((ts, equity()))

        # Close whatever is still open at the last close so every trade is counted.
        last_ts = timeline[-1]
        for sym in list(open_):
            close_position(sym, last_ts, marks[sym], "end_of_backtest: closed at last close",
                           at_open=False)
        if curve:
            curve[-1] = (last_ts, equity())

        eq = pd.Series([v for _, v in curve],
                       index=pd.to_datetime([t for t, _ in curve], unit="ms", utc=True), name="equity")
        bench = {s: marks[s] / first_close[s] - 1 for s in first_close}
        return BacktestResult(
            run_id=self.run_id, model=self.model.name, model_version=self.model.version,
            symbols=list(data), timeframe=self.timeframe, initial_equity=self.cfg.initial_equity,
            trades=trades, equity=eq, benchmark=bench, counts=counts,
            contamination=contamination_label(self.model.version, last_ts),
            sizing=self.pcfg.describe_sizing(self.cfg.initial_equity),
        )


def _iso_ms(iso: str | None) -> int | None:
    if iso is None:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
