"""Jev paper account replayed from the forward log.

Runs the same policy and simulator as the backtest and paper loop over the
answers Jev already gave in the hourly forward log (jevtrade.forward.log).
It makes no model calls and no exchange calls: prices come from the log too.

Orders fill when the collect run that logged the deciding row actually ran
(its `called_at`), not at the next candle's open: GitHub's hourly schedule
often starts tens of minutes late, sometimes hours. The fill price is
interpolated between the open and close of the hour that contains
`called_at`, by how far into the hour it falls; until that hour is logged the
order stays pending. Rows without `called_at` fill at the next open. While a
buy waits, that coin is treated as flat, and a newer answer that also says buy
replaces the waiting order.

Rows logged since 2026-10-05 carry their candle's open, high, low and
volume, so stops trigger on the hour's real low. Older rows carry only the
close, and their bar is approximated as: open = the previous hour's logged
close (this candle's own close when that hour is missing), low = min(open,
close), so their stops only see closes.

Costs follow `jev_paper` in the config, Robinhood-like by default: no fee,
and every fill pays a spread from the mid. That spread is Robinhood's own: the
bid and ask the collect run logged for the coin (`rh_bid`, `rh_ask`), the
latest one at or before the fill's hour. A coin with no logged quote yet pays
the configured estimate instead, which is wider for coins with little
Coinbase volume. Stops pay it too. A buy is also capped at `max_volume_frac`
of the coin's median hourly dollar volume, so a thin coin gets a small
position however confident Jev is.

Within an hour, symbols are evaluated in `Simulator.symbols_by_priority`, the
same order the backtest and paper loop use: held coins first, then by edge
(p_up - p_down), highest first, so when the exposure caps leave room for only
a few entries the slots go to the coins Jev was most confident about, not the
alphabetically first ones.

With `jev_paper.gate_lookback_hours` set, a skill gate holds back new buys
while Jev's recent buy signals would not have paid (`SkillGate`). Held coins
still sell on Jev's answer and the stop.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from itertools import accumulate
from statistics import median
from typing import Callable

from ..decision.base import Decision
from ..policy.engine import Action, PolicyConfig
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
    volume: float | None = None   # base units; None on rows logged before it was recorded


def _called_ms(row: dict | None) -> int | None:
    """When the collect run asked Jev about this row, in ms; None if unknown."""
    at = (row or {}).get("called_at")
    if not at:
        return None
    try:
        return int(datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _fill_bar(bar: Bar, ts: int, due: int, tf_ms: int) -> Bar:
    """The rest of `bar` from `due` on, within the hour [ts, ts + tf).

    The price is assumed to move in a straight line from the open to the
    close, so the remainder opens part way along. The hour's dip below that
    line may have come before or after `due`; it is scaled by the share of
    the hour left, so a fill late in the hour sees little of it. At
    `due <= ts` this is the whole bar.
    """
    frac = min(max((due - ts) / tf_ms, 0.0), 1.0)
    if frac == 0.0:
        return bar
    px = bar.open + (bar.close - bar.open) * frac
    line_low = min(px, bar.close)
    low = line_low - max(0.0, min(bar.open, bar.close) - bar.low) * (1 - frac)
    high = max(px, bar.close) + max(0.0, bar.high - max(bar.open, bar.close)) * (1 - frac)
    return Bar(px, high, low, bar.close, bar.volume)


def _num(v) -> float | None:
    return None if v is None else float(v)


def _bars(rows: list[dict], tf_ms: int) -> dict[str, dict[int, Bar]]:
    logged: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        if r.get("close") is not None:
            logged[r["symbol"]][r["candle_ts"]] = r
    out: dict[str, dict[int, Bar]] = {}
    for sym, by_ts in logged.items():
        out[sym] = {}
        for ts, r in by_ts.items():
            close = float(r["close"])
            o, h, lo = _num(r.get("open")), _num(r.get("high")), _num(r.get("low"))
            if o is None or h is None or lo is None:   # close-only row: open at the previous close
                prev = by_ts.get(ts - tf_ms)
                o = float(prev["close"]) if prev else close
                h, lo = max(o, close), min(o, close)
            out[sym][ts] = Bar(o, max(h, o, close), min(lo, o, close), close, _num(r.get("volume")))
    return out


def _quoted_spreads(rows: list[dict]) -> dict[str, tuple[list[int], list[float]]]:
    """Per coin, the hours with a logged Robinhood quote and its per-side spread in bps, oldest first."""
    by_sym: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        bid, ask = _num(r.get("rh_bid")), _num(r.get("rh_ask"))
        if bid is None or ask is None or not 0 < bid <= ask:
            continue
        by_sym[r["symbol"]].append((r["candle_ts"], (ask - bid) / (ask + bid) * 10_000))
    out = {}
    for sym, pts in by_sym.items():
        pts.sort()
        out[sym] = ([t for t, _ in pts], [b for _, b in pts])
    return out


def _dollar_volumes(bars: dict[str, dict[int, Bar]], n: int) -> dict[str, dict[int, float | None]]:
    """Median hourly dollar volume of each coin's last `n` logged hours, as of each hour."""
    out: dict[str, dict[int, float | None]] = {}
    for sym, by_ts in bars.items():
        out[sym], recent = {}, deque(maxlen=n)
        for ts in sorted(by_ts):
            b = by_ts[ts]
            if b.volume is not None:
                recent.append(b.volume * b.close)
            out[sym][ts] = median(recent) if recent else None
    return out


@dataclass(frozen=True)
class Gate:
    """Whether new buys are allowed at one hour, and why."""
    open: bool
    signals: int                 # resolved signals in the lookback window
    avg_net: float | None        # their mean return after a round trip of costs
    avg_excess: float | None     # their mean return minus the average coin's over the same hours
    reason: str


class SkillGate:
    """Jev's recent buy signals, scored by what they went on to return.

    A signal is a logged answer that met the policy's buy thresholds, whether
    or not the account bought it. It resolves `horizon_bars` later at that
    hour's logged close. Its net return pays a round trip of the spread,
    slippage and fee the coin had when the signal was given; its excess
    return is its return minus the average logged coin's over the same hours,
    so a market-wide move doesn't count as Jev's skill.

    At hour `ts` the gate is open when the signals given in the last
    `gate_lookback_hours` that have resolved by then number at least
    `gate_min_signals`, made money after costs on average, and beat the
    average coin on average.
    """

    def __init__(self, decisions: dict, bars: dict[str, dict[int, Bar]], pcfg: PolicyConfig, paper,
                 spread: Callable[[str, int], float], horizon_bars: int, tf_ms: int):
        self.lookback_ms = paper.gate_lookback_hours * 3_600_000
        self.hours = paper.gate_lookback_hours
        self.min_signals = paper.gate_min_signals
        self.horizon_ms = horizon_bars * tf_ms

        def ret(sym: str, t: int) -> float | None:
            start, end = bars[sym].get(t), bars[sym].get(t + self.horizon_ms)
            return None if start is None or end is None else end.close / start.close - 1

        by_hour: dict[int, list[float]] = defaultdict(list)
        for sym in bars:
            for t in bars[sym]:
                r = ret(sym, t)
                if r is not None:
                    by_hour[t].append(r)
        market = {t: sum(rs) / len(rs) for t, rs in by_hour.items()}

        signals = []
        for (sym, t), row in decisions.items():
            d = _decision(row)
            if d is None or d.abstain or sym not in bars:
                continue
            p_up, p_down = d.p(pcfg.question, pcfg.up_option), d.p(pcfg.question, pcfg.down_option)
            r = ret(sym, t)
            if p_up < pcfg.entry_threshold or p_up - p_down < pcfg.min_edge or r is None:
                continue
            side = (spread(sym, t) + paper.slippage_bps + paper.fee_bps) / 10_000
            signals.append((t, r - 2 * side, r - market[t]))
        signals.sort()
        self.ts = [t for t, _, _ in signals]
        self.cum_net = [0.0, *accumulate(n for _, n, _ in signals)]
        self.cum_excess = [0.0, *accumulate(x for _, _, x in signals)]

    def at(self, ts: int) -> Gate:
        lo = bisect_left(self.ts, ts - self.lookback_ms)
        hi = bisect_right(self.ts, ts - self.horizon_ms)   # resolved by this hour's close
        n = max(hi - lo, 0)
        if n < self.min_signals:
            return Gate(False, n, None, None, f"skill_gate: {n} of {self.min_signals} buy signals from "
                                              f"the last {self.hours}h resolved so far")
        net = (self.cum_net[hi] - self.cum_net[lo]) / n
        excess = (self.cum_excess[hi] - self.cum_excess[lo]) / n
        return Gate(net > 0 and excess > 0, n, net, excess,
                    f"skill_gate: last {self.hours}h of buy signals averaged {net:+.2%} after costs, "
                    f"{excess:+.2%} vs the average coin ({n} signals)")


def _trade(t: Trade) -> dict:
    return {"symbol": t.symbol, "entry_ts": t.entry_ts, "entry_price": t.entry_price, "exit_ts": t.exit_ts,
            "exit_price": t.exit_price, "qty": t.qty, "fees": t.fees, "pnl": t.pnl, "ret": t.ret,
            "bars_held": t.bars_held, "exit_reason": t.exit_reason, "entry_reason": t.entry_reason}


def replay(rows: list[dict], app_cfg, tf_ms: int) -> dict:
    """Simulated Jev account after every logged hour in `rows` (any order)."""
    pcfg = app_cfg.policy.for_model("jev")
    paper = app_cfg.jev_paper
    initial = paper.initial_equity
    base = {"initial_equity": initial, "fee_bps": paper.fee_bps, "slippage_bps": paper.slippage_bps,
            "spread_bps": {"min": paper.spread_bps, "max": paper.spread_bps + paper.thin_extra_bps},
            "spread_source": "estimate", "quoted_symbols": 0,
            "max_volume_frac": paper.max_volume_frac,
            "policy": {"entry_threshold": pcfg.entry_threshold, "min_edge": pcfg.min_edge,
                       "exit_threshold": pcfg.exit_threshold, "exit_min_edge": pcfg.exit_min_edge,
                       "stop_loss_pct": pcfg.stop_loss_pct, "max_holding_bars": pcfg.max_holding_bars},
            "gate": None if paper.gate_lookback_hours is None else
                    {"lookback_hours": paper.gate_lookback_hours, "min_signals": paper.gate_min_signals,
                     "open": False, "signals": 0, "avg_net": None, "avg_excess": None, "reason": None}}
    bars = _bars(rows, tf_ms)
    timeline = sorted({ts for b in bars.values() for ts in b})
    if not timeline:
        return {**base, "equity": initial, "cash": initial, "total_return": 0.0, "drawdown": 0.0,
                "last_bar_ts": None, "trades": [], "positions": [], "pending": [], "curve": [], "actions": [],
                "buys": [], "calls": 0, "counts": {}, "per_symbol": {}}

    decisions = {(r["symbol"], r["candle_ts"]): r for r in rows}
    volumes = _dollar_volumes(bars, paper.volume_bars)

    def volume(sym: str, ts: int) -> float | None:
        return volumes.get(sym, {}).get(ts)

    quoted = _quoted_spreads(rows)

    def spread(sym: str, ts: int) -> float:
        """Per-side bps: the latest Robinhood quote logged at or before `ts`, else the estimate."""
        q = quoted.get(sym)
        i = bisect_right(q[0], ts) if q else 0
        return q[1][i - 1] if i else paper.spread_for(volume(sym, ts))

    def max_notional(sym: str, ts: int) -> float:
        v = volume(sym, ts)
        return float("inf") if v is None or paper.max_volume_frac is None else v * paper.max_volume_frac

    sim = Simulator(pcfg, tf_ms, paper.fee_bps, paper.slippage_bps,
                    spread_bps=spread, max_notional=max_notional)
    skill = (SkillGate(decisions, bars, pcfg, paper, spread, app_cfg.decision.horizon_bars, tf_ms)
             if paper.gate_lookback_hours is not None else None)
    gate: Gate | None = None
    st = SimState.new(initial, timeline[0])
    trades: list[Trade] = []
    curve, actions = [], []
    counts: dict[str, int] = defaultdict(int)
    peak = initial
    due: dict[str, int] = {}   # pending order -> when the run that queued it happened (ms)

    for ts in timeline:
        gate = skill.at(ts) if skill is not None else None
        sim.begin_bar(st, ts)
        now = {s: b[ts] for s, b in bars.items() if ts in b}
        # An order fills in the hour its run happened, at the price estimated for that moment.
        fills = {s: _fill_bar(now[s], ts, due.get(s, ts), tf_ms) for s in st.pending
                 if s in now and due.get(s, ts) < ts + tf_ms}
        trades += sim.fill_pending(st, ts, fills)
        for s in fills:
            due.pop(s, None)
        # A position bought this hour is stop-checked from its fill price, not the hour's open.
        now.update({s: f for s, f in fills.items() if s in st.open})
        hour = {}
        for sym in now:
            row = decisions.get((sym, ts))
            hour[sym] = (row, _decision(row) if row else None)
        for sym in sim.symbols_by_priority(st, {s: d for s, (_, d) in hour.items()}):
            row, d = hour[sym]
            # A buy still waiting for its run is not a position yet: decide as if flat,
            # and keep the waiting order unless this hour's answer queues a new one.
            waiting = st.pending.pop(sym) if sym in st.pending and sym not in st.open else None
            action, trade = sim.on_close(st, sym, ts, now[sym], d)
            if action.kind == "enter" and gate is not None and not gate.open:
                st.pending.pop(sym, None)   # held back: the buy is never queued
                action = Action("skip", sym, gate.reason, passed_threshold=True, details=action.details)
            if waiting is not None and sym not in st.pending:
                st.pending[sym] = waiting
            counts[action.kind] += 1
            if trade is not None:
                trades.append(trade)
            elif action.kind in ("enter", "exit") and sym in st.pending:
                called = _called_ms(row)
                due[sym] = called if called is not None else ts + tf_ms
            actions.append({"symbol": sym, "bar_ts": ts, "close": float(now[sym].close),
                            "status": row.get("status") if row else None,
                            "p_up": action.details.get("p_up"), "p_down": action.details.get("p_down"),
                            "action": action.kind, "reason": action.reason, "size_frac": action.size_frac})
        eq = st.equity()
        peak = max(peak, eq)
        curve.append({"bar_ts": ts, "equity": eq, "cash": st.cash})

    eq = st.equity()
    latest = [q[1][-1] for q in quoted.values()]
    if latest:   # what the coins pay now, from their latest quotes
        base["spread_bps"] = {"min": min(latest), "max": max(latest)}
        base.update(spread_source="robinhood", quoted_symbols=len(latest))
    per_symbol = {}
    for sym in sorted(bars):
        ts_ = [t for t in trades if t.symbol == sym]
        per_symbol[sym] = {"trades": len(ts_), "pnl": sum(t.pnl for t in ts_),
                           "buys": sum(1 for a in actions if a["symbol"] == sym and a["action"] == "enter")}
    if gate is not None:
        base["gate"].update(open=gate.open, signals=gate.signals, avg_net=gate.avg_net,
                            avg_excess=gate.avg_excess, reason=gate.reason)
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
        # Decided on a logged close; fills once the hour its run happened in is logged.
        "pending": [{"symbol": s, "kind": p.action.kind, "reason": p.action.reason, "size_frac": p.action.size_frac,
                     "ref_close": p.ref_close, "fill_after": due.get(s)} for s, p in sorted(st.pending.items())],
        "curve": curve,
        "actions": actions[::-1],   # newest first
        "buys": [a for a in reversed(actions) if a["action"] == "enter"],   # every buy, newest first
        "calls": len(actions),
        "counts": dict(counts),
        "per_symbol": per_symbol,
    }
