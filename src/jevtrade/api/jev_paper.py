"""Jev paper account replayed from the forward log.

Runs the same policy and simulator as the backtest and paper loop over the
answers Jev already gave in the hourly forward log (jevtrade.api.forward).
It makes no model calls and no exchange calls: prices come from the log too.

Each logged row carries its candle's close, so a bar is approximated as:
open = the previous hour's logged close (this candle's own close when the
previous hour is missing), low = min(open, close). Stops therefore trigger
on closes, not on intrabar dips.

Within an hour, symbols are evaluated by edge (p_up - p_down), highest first,
so when the exposure caps leave room for only a few entries the slots go to
the coins Jev was most confident about, not the alphabetically first ones.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..decision.base import Decision
from ..sim import SimState, Simulator, Trade


def _decision(row: dict) -> Decision | None:
    """The policy's input for one logged row; None when the call failed."""
    status = row.get("status")
    if status == "error":
        return None
    probs = {q: {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
             for q, a in (row.get("answers") or {}).items() if isinstance(a, dict)}
    return Decision(model="jev", model_version=row.get("served_model") or row.get("requested_model") or "",
                    input_hash=row.get("input_hash") or "", probs=probs,
                    abstain=status != "answered", abstain_reason=row.get("abstain_reason"))


@dataclass(frozen=True)
class Bar:
    """The fields Simulator reads from a candle; lighter than a pandas row for many symbols."""
    open: float
    high: float
    low: float
    close: float


def _bars(rows: list[dict], tf_ms: int) -> dict[str, dict[int, Bar]]:
    closes: dict[str, dict[int, float]] = defaultdict(dict)
    for r in rows:
        if r.get("close") is not None:
            closes[r["symbol"]][r["candle_ts"]] = float(r["close"])
    out: dict[str, dict[int, Bar]] = {}
    for sym, by_ts in closes.items():
        out[sym] = {}
        for ts, close in by_ts.items():
            opened = by_ts.get(ts - tf_ms, close)
            out[sym][ts] = Bar(opened, max(opened, close), min(opened, close), close)
    return out


def _edge(pcfg, d: Decision | None) -> float:
    """p_up - p_down as the policy reads it; -inf when there is no usable answer."""
    if d is None or d.abstain:
        return float("-inf")
    return d.p(pcfg.question, pcfg.up_option) - d.p(pcfg.question, pcfg.down_option)


def _trade(t: Trade) -> dict:
    return {"symbol": t.symbol, "entry_ts": t.entry_ts, "entry_price": t.entry_price, "exit_ts": t.exit_ts,
            "exit_price": t.exit_price, "qty": t.qty, "fees": t.fees, "pnl": t.pnl, "ret": t.ret,
            "bars_held": t.bars_held, "exit_reason": t.exit_reason, "entry_reason": t.entry_reason}


def replay(rows: list[dict], app_cfg, tf_ms: int) -> dict:
    """Simulated Jev account after every logged hour in `rows` (any order)."""
    pcfg = app_cfg.policy.for_model("jev")
    paper = app_cfg.paper
    initial = paper.initial_equity
    base = {"initial_equity": initial, "fee_bps": paper.fee_bps, "slippage_bps": paper.slippage_bps,
            "policy": {"entry_threshold": pcfg.entry_threshold, "min_edge": pcfg.min_edge,
                       "exit_threshold": pcfg.exit_threshold, "stop_loss_pct": pcfg.stop_loss_pct,
                       "max_holding_bars": pcfg.max_holding_bars}}
    bars = _bars(rows, tf_ms)
    timeline = sorted({ts for b in bars.values() for ts in b})
    if not timeline:
        return {**base, "equity": initial, "cash": initial, "total_return": 0.0, "drawdown": 0.0,
                "last_bar_ts": None, "trades": [], "positions": [], "pending": [], "curve": [], "actions": [],
                "buys": [], "calls": 0, "counts": {}, "per_symbol": {}}

    decisions = {(r["symbol"], r["candle_ts"]): r for r in rows}
    sim = Simulator(pcfg, tf_ms, paper.fee_bps, paper.slippage_bps)
    st = SimState.new(initial, timeline[0])
    trades: list[Trade] = []
    curve, actions = [], []
    counts: dict[str, int] = defaultdict(int)
    peak = initial

    for ts in timeline:
        sim.begin_bar(st, ts)
        now = {s: b[ts] for s, b in bars.items() if ts in b}
        trades += sim.fill_pending(st, ts, now)
        hour = {}
        for sym in now:
            row = decisions.get((sym, ts))
            hour[sym] = (row, _decision(row) if row else None)
        for sym in sorted(hour, key=lambda s: (-_edge(pcfg, hour[s][1]), s)):
            row, d = hour[sym]
            action, trade = sim.on_close(st, sym, ts, now[sym], d)
            counts[action.kind] += 1
            if trade is not None:
                trades.append(trade)
            actions.append({"symbol": sym, "bar_ts": ts, "close": float(now[sym].close),
                            "status": row.get("status") if row else None,
                            "p_up": action.details.get("p_up"), "p_down": action.details.get("p_down"),
                            "action": action.kind, "reason": action.reason, "size_frac": action.size_frac})
        eq = st.equity()
        peak = max(peak, eq)
        curve.append({"bar_ts": ts, "equity": eq, "cash": st.cash})

    eq = st.equity()
    per_symbol = {}
    for sym in sorted(bars):
        ts_ = [t for t in trades if t.symbol == sym]
        per_symbol[sym] = {"trades": len(ts_), "pnl": sum(t.pnl for t in ts_),
                           "buys": sum(1 for a in actions if a["symbol"] == sym and a["action"] == "enter")}
    return {
        **base,
        "equity": eq, "cash": st.cash, "total_return": eq / initial - 1, "drawdown": eq / peak - 1,
        "last_bar_ts": timeline[-1],
        "trades": [_trade(t) for t in sorted(trades, key=lambda t: -t.exit_ts)],
        "positions": [{"symbol": s, "qty": o.pos.qty, "entry_price": o.pos.entry_price, "entry_ts": o.pos.entry_ts,
                       "stop_price": o.pos.stop_price, "mark": st.marks.get(s, o.pos.entry_price),
                       "notional": o.pos.qty * st.marks.get(s, o.pos.entry_price),
                       "unrealized_pnl": o.pos.qty * (st.marks.get(s, o.pos.entry_price) - o.pos.entry_price),
                       "entry_reason": o.entry_reason} for s, o in sorted(st.open.items())],
        # Decided on the last logged close; fills at the next open once that hour is logged.
        "pending": [{"symbol": s, "kind": p.action.kind, "reason": p.action.reason, "size_frac": p.action.size_frac,
                     "ref_close": p.ref_close} for s, p in sorted(st.pending.items())],
        "curve": curve,
        "actions": actions[::-1],   # newest first
        "buys": [a for a in reversed(actions) if a["action"] == "enter"],   # every buy, newest first
        "calls": len(actions),
        "counts": dict(counts),
        "per_symbol": per_symbol,
    }
