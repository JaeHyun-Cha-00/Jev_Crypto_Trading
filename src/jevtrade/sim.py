"""Simulated account behind the dashboard's Jev portfolio (jevtrade.forward.portfolio).

Nothing here talks to an exchange: fills are computed from candles.

Per bar t, in order:
1. `begin_bar`: roll the daily-loss baseline (equity at the previous close).
2. `fill_pending`: orders decided at the close of t-1 fill at t's open, with
   `slippage_bps` (plus the optional per-symbol `spread_bps`) against the
   trader and `fee_bps` on notional.
3. `on_close`, per symbol in `symbols_by_priority`: mark to the close, run
   `Policy.evaluate`, fill a stop inside the bar at `action.fill_price`,
   queue entries and other exits for the next open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .decision.base import Decision
from .policy.engine import (
    AccountView, Action, Policy, PolicyConfig, Position, RiskState, register_trade_result, roll_day,
)

DAY_MS = 86_400_000


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
class OpenPosition:
    pos: Position
    entry_fee: float
    entry_reason: str


@dataclass
class PendingOrder:
    action: Action
    ref_close: float


@dataclass
class SimState:
    cash: float
    risk: RiskState
    open: dict[str, OpenPosition] = field(default_factory=dict)
    pending: dict[str, PendingOrder] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    first_close: dict[str, float] = field(default_factory=dict)
    last_bar_ts: int | None = None   # last bar fully processed

    @classmethod
    def new(cls, cash: float, first_bar_ts: int) -> "SimState":
        return cls(cash=cash, risk=RiskState(day_start_equity=cash, day=first_bar_ts // DAY_MS))

    def equity(self) -> float:
        return self.cash + sum(o.pos.qty * self.marks.get(s, o.pos.entry_price) for s, o in self.open.items())


class Simulator:
    def __init__(self, policy_cfg: PolicyConfig, tf_ms: int, fee_bps: float, slippage_bps: float,
                 spread_bps: Callable[[str, int], float] | None = None,
                 max_notional: Callable[[str, int], float] | None = None):
        self.pcfg = policy_cfg
        self.policy = Policy(policy_cfg, tf_ms)
        self.tf_ms = tf_ms
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        # Per-side spread from the mid for (symbol, bar_ts), on every fill; None = no spread.
        self.spread_bps = spread_bps
        # Largest entry notional the market can absorb for (symbol, bar_ts); None = no cap.
        self.max_notional = max_notional

    def _spread(self, sym: str, ts: int) -> float:
        return self.spread_bps(sym, ts) if self.spread_bps is not None else 0.0

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.fee_bps / 10_000

    def begin_bar(self, st: SimState, ts: int) -> None:
        roll_day(st.risk, ts, st.equity())

    def close_position(self, st: SimState, sym: str, ts: int, px: float, reason: str, at_open: bool) -> Trade:
        o = st.open.pop(sym)
        fee = self._fee(o.pos.qty * px)
        st.cash += o.pos.qty * px - fee
        cost = o.pos.qty * o.pos.entry_price
        pnl = o.pos.qty * px - cost - fee - o.entry_fee
        bars = (ts - o.pos.entry_ts) // self.tf_ms + (0 if at_open else 1)
        register_trade_result(st.risk, pnl, ts, self.tf_ms, self.pcfg, sym)
        return Trade(sym, o.pos.entry_ts, o.pos.entry_price, ts, px, o.pos.qty, fee + o.entry_fee, pnl,
                     pnl / cost if cost else 0.0, bars, reason, o.entry_reason)

    def fill_pending(self, st: SimState, ts: int, bars: dict[str, pd.Series]) -> list[Trade]:
        """Fill orders queued at the previous close at this bar's open.

        A symbol with no bar at `ts` (exchange gap) keeps its order waiting.
        """
        trades: list[Trade] = []
        for sym in list(st.pending):
            bar = bars.get(sym)
            if bar is None:
                continue
            p = st.pending.pop(sym)
            if p.action.kind == "exit" and sym in st.open:
                px = bar.open * (1 - (self.slippage_bps + self._spread(sym, ts)) / 10_000)
                trades.append(self.close_position(st, sym, ts, px, p.action.reason, at_open=True))
            elif p.action.kind == "enter" and sym not in st.open:
                px = bar.open * (1 + (self.slippage_bps + self._spread(sym, ts)) / 10_000)
                qty = p.action.size_frac * st.equity() / px
                if self.max_notional is not None:
                    qty = min(qty, self.max_notional(sym, ts) / px)
                fee = self._fee(qty * px)
                if qty * px + fee > st.cash:  # never borrow; long-only spot
                    qty = max(0.0, (st.cash - fee) / px)
                    fee = self._fee(qty * px)
                if qty <= 0:
                    continue
                st.cash -= qty * px + fee
                stop = px * (1 - self.pcfg.stop_loss_pct)
                st.open[sym] = OpenPosition(Position(sym, qty, px, ts, stop), fee, p.action.reason)
                st.marks[sym] = float(bar.open)
        return trades

    def symbols_by_priority(self, st: SimState, decisions: dict[str, Decision | None]) -> list[str]:
        """The order to call `on_close` in for one bar's symbols.

        Held symbols go first, so a stop or exit frees room before any entry
        is sized. Flat symbols follow by edge (p_up - p_down), highest first,
        so when the exposure caps leave room for only a few entries the slots
        go to the strongest signals, not to whichever coin is listed first.
        Ties, abstains and missing decisions fall back to symbol order.
        """
        c = self.pcfg

        def edge(d: Decision | None) -> float:
            if d is None or d.abstain:
                return float("-inf")
            return d.p(c.question, c.up_option) - d.p(c.question, c.down_option)

        return sorted(decisions, key=lambda s: (s not in st.open, -edge(decisions[s]), s))

    def mark(self, st: SimState, sym: str, bar: pd.Series) -> None:
        st.marks[sym] = float(bar.close)
        st.first_close.setdefault(sym, float(bar.close))

    def on_close(
        self, st: SimState, sym: str, ts: int, bar: pd.Series, decision: Decision | None,
    ) -> tuple[Action, Trade | None]:
        """Mark `sym` to this bar's close and act on the policy's verdict."""
        self.mark(st, sym, bar)
        # Pending entries count against gross exposure for later symbols.
        positions = {s: o.pos for s, o in st.open.items()}
        for s, p in st.pending.items():
            if p.action.kind == "enter":
                q = p.action.size_frac * st.equity() / p.ref_close
                positions[s] = Position(s, q, p.ref_close, ts, p.ref_close)
        acct = AccountView(st.equity(), positions, dict(st.marks), st.risk)
        action = self.policy.evaluate(sym, ts, float(bar.close), decision, acct,
                                      bar_open=float(bar.open), bar_low=float(bar.low))
        trade = None
        if action.kind == "exit" and action.fill_price is not None:
            # Stop: fills inside this bar at the policy's price, not at the next open.
            px = action.fill_price * (1 - self._spread(sym, ts) / 10_000)
            trade = self.close_position(st, sym, ts, px, action.reason, at_open=False)
        elif action.kind in ("enter", "exit"):
            st.pending[sym] = PendingOrder(action, float(bar.close))
        return action, trade
