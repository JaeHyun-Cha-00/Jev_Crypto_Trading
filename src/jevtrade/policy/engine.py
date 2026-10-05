"""Deterministic policy: model probabilities + account state → one action.

All risk limits live here as code. The model only supplies probabilities;
it cannot open, size, or keep a position open against a limit. v1 is
long-only spot (no shorting or leverage).

Timing contract: `evaluate` runs on the close of bar t. The executor fills
any resulting order at the open of bar t+1, with one exception: a stop-loss
exit carries `fill_price` and fills inside bar t (see `stop_fill`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..decision.base import Decision


class PolicyConfig(BaseModel):
    question: str = "direction"
    up_option: str = "up"
    down_option: str = "down"
    entry_threshold: float = Field(0.55, gt=0, le=1)   # min p(up) to enter
    min_edge: float = Field(0.10, ge=0, le=1)          # min p(up) - p(down)
    exit_threshold: float = Field(0.55, gt=0, le=1)    # p(down) that triggers a model exit
    risk_per_trade: float = Field(0.01, gt=0, le=1)    # equity lost if the stop is hit
    stop_loss_pct: float = Field(0.03, gt=0, lt=1)
    stop_slippage_bps: float = Field(5.0, ge=0, lt=10_000)  # adverse slippage on stop fills
    max_position_frac: float = Field(0.25, gt=0, le=1)  # per-symbol notional / equity
    max_gross_exposure: float = Field(0.50, gt=0, le=1)  # all positions / equity
    min_trade_frac: float = Field(0.01, gt=0, le=1)
    max_daily_loss_pct: float = Field(0.03, gt=0, lt=1)  # vs equity at UTC day start
    cooldown_after_losses: int = Field(3, ge=1)
    cooldown_bars: int = Field(24, ge=0)
    max_holding_bars: int = Field(24, ge=1)

    @model_validator(mode="after")
    def _caps(self) -> "PolicyConfig":
        if self.max_position_frac > self.max_gross_exposure:
            raise ValueError("max_position_frac cannot exceed max_gross_exposure")
        return self


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_ts: int
    stop_price: float


@dataclass
class RiskState:
    day_start_equity: float
    day: int  # UTC day number (ts_ms // 86_400_000)
    consecutive_losses: int = 0
    cooldown_until_ts: int = 0


@dataclass
class AccountView:
    equity: float
    positions: dict[str, Position]
    marks: dict[str, float]  # latest close per symbol
    risk: RiskState

    def gross_exposure_frac(self) -> float:
        if self.equity <= 0:
            return float("inf")
        notional = sum(abs(p.qty) * self.marks.get(s, p.entry_price) for s, p in self.positions.items())
        return notional / self.equity


ActionKind = Literal["enter", "exit", "hold", "skip"]


@dataclass
class Action:
    kind: ActionKind
    symbol: str
    reason: str
    size_frac: float = 0.0          # target notional / equity for "enter"
    stop_price: float | None = None  # set relative to the reference close; executor re-anchors to fill
    fill_price: float | None = None  # stop exits only: fill inside this bar at this price, not next open
    passed_threshold: bool = False
    details: dict = field(default_factory=dict)


DAY_MS = 86_400_000


def stop_fill(stop_price: float, bar_open: float, bar_low: float, slippage_bps: float) -> float | None:
    """Fill price for a long stop hit during a bar, or None if the bar never reached it.

    The stop triggers on the bar's low, not its close. It fills at the stop
    price, or at the open when the bar gapped through the stop, then slippage
    is applied against the seller.
    """
    if bar_low > stop_price:
        return None
    base = bar_open if bar_open <= stop_price else stop_price
    return base * (1 - slippage_bps / 10_000)


def roll_day(risk: RiskState, ts_ms: int, equity: float) -> None:
    """Reset the daily-loss baseline when a new UTC day starts."""
    day = ts_ms // DAY_MS
    if day != risk.day:
        risk.day = day
        risk.day_start_equity = equity


def register_trade_result(risk: RiskState, pnl: float, exit_ts: int, tf_ms: int, cfg: PolicyConfig) -> None:
    """Update loss streak and cooldown after a round trip closes."""
    if pnl < 0:
        risk.consecutive_losses += 1
        if risk.consecutive_losses >= cfg.cooldown_after_losses:
            risk.cooldown_until_ts = exit_ts + cfg.cooldown_bars * tf_ms
            risk.consecutive_losses = 0
    else:
        risk.consecutive_losses = 0


class Policy:
    def __init__(self, cfg: PolicyConfig, tf_ms: int):
        self.cfg = cfg
        self.tf_ms = tf_ms

    def evaluate(
        self, symbol: str, bar_ts: int, close: float, decision: Decision | None, acct: AccountView,
        *, bar_open: float | None = None, bar_low: float | None = None,
    ) -> Action:
        """Decide what to do at the close of bar `bar_ts`.

        Backtest and paper loops must pass `bar_open` and `bar_low` so stops
        trigger on the bar's low. Without them the bar is treated as having
        no range beyond its close.
        """
        c = self.cfg
        usable = decision is not None and not decision.abstain
        p_up = decision.p(c.question, c.up_option) if usable else 0.0
        p_down = decision.p(c.question, c.down_option) if usable else 0.0
        probs = {"p_up": round(p_up, 4), "p_down": round(p_down, 4)}

        pos = acct.positions.get(symbol)
        if pos is not None:
            # Risk exits come first and do not depend on the model.
            low = min(close, bar_low if bar_low is not None else close)
            opened = bar_open if bar_open is not None else close
            fill = stop_fill(pos.stop_price, opened, low, c.stop_slippage_bps)
            if fill is not None:
                how = "gap open" if opened <= pos.stop_price else "stop"
                return Action("exit", symbol,
                              f"stop_loss: low {low:.6g} <= stop {pos.stop_price:.6g}, "
                              f"fill {fill:.6g} at {how} less {c.stop_slippage_bps:g}bps",
                              fill_price=fill, details=probs)
            held = (bar_ts - pos.entry_ts) // self.tf_ms + 1
            if held >= c.max_holding_bars:
                return Action("exit", symbol, f"max_holding: held {held} bars >= {c.max_holding_bars}",
                              details=probs)
            if usable and p_down >= c.exit_threshold:
                return Action("exit", symbol, f"model_exit: p_down {p_down:.3f} >= {c.exit_threshold}",
                              passed_threshold=True, details=probs)
            why = "model abstained" if not usable else f"p_down {p_down:.3f} < {c.exit_threshold}"
            return Action("hold", symbol, f"hold: {why}", details=probs)

        # Flat: check every gate and record the first one that blocks.
        if decision is None:
            return Action("skip", symbol, "no_decision: model unavailable", details=probs)
        if decision.abstain:
            return Action("skip", symbol, f"abstain: {decision.abstain_reason or 'model abstained'}",
                          details=probs)

        passed = p_up >= c.entry_threshold and (p_up - p_down) >= c.min_edge
        if not passed:
            return Action("skip", symbol,
                          f"below_threshold: p_up {p_up:.3f} (need {c.entry_threshold}), "
                          f"edge {p_up - p_down:.3f} (need {c.min_edge})", details=probs)

        day_pnl = acct.equity / acct.risk.day_start_equity - 1 if acct.risk.day_start_equity > 0 else 0
        if day_pnl <= -c.max_daily_loss_pct:
            return Action("skip", symbol, f"max_daily_loss: day pnl {day_pnl:.2%} <= -{c.max_daily_loss_pct:.2%}",
                          passed_threshold=True, details=probs)
        if bar_ts < acct.risk.cooldown_until_ts:
            return Action("skip", symbol, f"cooldown: until ts {acct.risk.cooldown_until_ts}",
                          passed_threshold=True, details=probs)

        size = min(c.risk_per_trade / c.stop_loss_pct, c.max_position_frac)
        room = c.max_gross_exposure - acct.gross_exposure_frac()
        size = min(size, room)
        if size < c.min_trade_frac:
            return Action("skip", symbol, f"no_capacity: gross exposure room {room:.3f} < {c.min_trade_frac}",
                          passed_threshold=True, details=probs)

        return Action("enter", symbol,
                      f"entry: p_up {p_up:.3f} >= {c.entry_threshold}, edge {p_up - p_down:.3f}; "
                      f"size {size:.3f} of equity",
                      size_frac=size, stop_price=close * (1 - c.stop_loss_pct),
                      passed_threshold=True, details=probs)
