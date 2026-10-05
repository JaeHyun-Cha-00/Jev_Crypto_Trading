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
from ..policy.engine import PolicyConfig
from ..sim import SimState, Simulator, Trade
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
        self.pcfg = policy_cfg
        self.cfg = bt_cfg
        self.sim = Simulator(policy_cfg, tf_ms, bt_cfg.fee_bps, bt_cfg.slippage_bps)
        self.tf_ms = tf_ms
        self.timeframe = timeframe
        self.horizon_bars = horizon_bars
        self.feature_cfg = feature_cfg or FeatureConfig()
        self.state_cfg = state_cfg or StateConfig()
        self.dlog = decision_log
        self.run_id = run_id or (
            f"bt-{model.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        )

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

        st = SimState.new(self.cfg.initial_equity, timeline[0])
        trades: list[Trade] = []
        curve: list[tuple[int, float]] = []
        counts: dict[str, int] = {"decisions": 0, "abstains": 0, "no_state": 0}

        for ts in timeline:
            self.sim.begin_bar(st, ts)
            bars = {sym: data[sym][0].iloc[i] for sym in data if (i := row_of[sym].get(ts)) is not None}
            trades += self.sim.fill_pending(st, ts, bars)

            # Decide on this bar's close, symbol by symbol.
            for sym, (df, feats) in data.items():
                i = row_of[sym].get(ts)
                if i is None:
                    continue
                lo = max(0, i + 1 - window)
                state = build_state(df.iloc[lo:i + 1], feats.iloc[lo:i + 1], self.horizon_bars,
                                    self.timeframe, self.state_cfg, self.feature_cfg)
                decision: Decision | None = None
                if state is None:
                    counts["no_state"] += 1
                    if sym not in st.open:
                        self.sim.mark(st, sym, bars[sym])
                        continue  # warm-up: nothing to decide and nothing to protect
                else:
                    decision = self.model.decide(state, self.questions)
                    counts["decisions"] += 1
                    counts["abstains"] += int(decision.abstain)

                action, trade = self.sim.on_close(st, sym, ts, bars[sym], decision)
                counts[action.kind] = counts.get(action.kind, 0) + 1
                if trade is not None:
                    trades.append(trade)

                if self.dlog is not None and decision is not None and self.cfg.log_decisions:
                    did = self.dlog.record(self.run_id, sym, ts, decision,
                                           state.text if self.cfg.log_inputs else None)
                    self.dlog.annotate(did, action.passed_threshold, action.kind, action.reason)

            curve.append((ts, st.equity()))

        # Close whatever is still open at the last close so every trade is counted.
        last_ts = timeline[-1]
        for sym in list(st.open):
            trades.append(self.sim.close_position(st, sym, last_ts, st.marks[sym],
                                                  "end_of_backtest: closed at last close", at_open=False))
        if curve:
            curve[-1] = (last_ts, st.equity())

        eq = pd.Series([v for _, v in curve],
                       index=pd.to_datetime([t for t, _ in curve], unit="ms", utc=True), name="equity")
        bench = {s: st.marks[s] / st.first_close[s] - 1 for s in st.first_close}
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
