"""Daily Markdown report for a paper (or backtest) run.

Everything is read from the SQLite store: `paper_bars` / `paper_trades` /
`paper_state` for the account, `decisions` for the model, and `candles` for
realized outcomes. A report covers one UTC day and is deterministic, so
rewriting it gives the same file.

Calibration compares the `direction` probabilities with what happened
`horizon_bars` later (up / flat / down against ±`flat_band_pct`), over every
decision of the run whose horizon has elapsed by the end of the day.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel

from ..backtest.metrics import max_drawdown
from ..data.store import CandleStore
from ..decision.log import DecisionLog
from ..paper.store import PaperStore

DAY_MS = 86_400_000


class ReportConfig(BaseModel):
    output_dir: str = "reports/out"   # <output_dir>/<run_id>/<YYYY-MM-DD>.md and .json
    buckets: int = 5                  # reliability buckets for p(up)


def day_bounds(day: date) -> tuple[int, int]:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return start, start + DAY_MS


def realized_direction(close_now: float, close_later: float, band_pct: float) -> str:
    chg = round((close_later / close_now - 1) * 100, 9)  # so a move of exactly the band is flat
    return "up" if chg > band_pct else "down" if chg < -band_pct else "flat"


def calibration(conn: sqlite3.Connection, app_cfg, run_id: str, until_ms: int, buckets: int = 5) -> dict:
    """Score `direction` probabilities against realized moves, decisions before `until_ms`."""
    d, tf = app_cfg.data, app_cfg.decision
    from ..data.timeframes import timeframe_ms

    tf_ms = timeframe_ms(d.timeframe)
    h_ms = tf.horizon_bars * tf_ms
    rows = conn.execute(
        "SELECT symbol, bar_ts, probs FROM decisions WHERE run_id=? AND abstain=0 AND bar_ts + ? + ? <= ?",
        (run_id, h_ms, tf_ms, until_ms),
    ).fetchall()
    store = CandleStore(conn)
    closes: dict[str, dict[int, float]] = {}
    recs = []
    for sym, ts, probs in rows:
        p = json.loads(probs).get("direction")
        if not p:
            continue
        if sym not in closes:
            df = store.load(d.exchange, sym, d.timeframe)
            closes[sym] = {int(ix.value // 1_000_000): float(c) for ix, c in df["close"].items()}
        c0, c1 = closes[sym].get(ts), closes[sym].get(ts + h_ms)
        if c0 is None or c1 is None:
            continue
        recs.append((p, realized_direction(c0, c1, tf.flat_band_pct)))
    if not recs:
        return {"n": 0}

    opts = ["up", "flat", "down"]
    brier = np.mean([sum((p.get(o, 0.0) - (o == y)) ** 2 for o in opts) for p, y in recs])
    logloss = np.mean([-math.log(max(p.get(y, 0.0), 1e-6)) for p, y in recs])
    hit = np.mean([max(opts, key=lambda o: p.get(o, 0.0)) == y for p, y in recs])
    base = {o: float(np.mean([y == o for _, y in recs])) for o in opts}
    # Brier of always forecasting the realized base rates: a model must beat this.
    base_brier = sum(base[o] * (1 - base[o]) for o in opts)
    edges = np.linspace(0, 1, buckets + 1)
    table = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [(p["up"], y) for p, y in recs if lo <= p.get("up", 0.0) < hi or (hi == 1 and p.get("up") == 1)]
        if sel:
            table.append({"p_up": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                          "mean_p_up": round(float(np.mean([s[0] for s in sel])), 3),
                          "realized_up": round(float(np.mean([s[1] == "up" for s in sel])), 3)})
    return {"n": len(recs), "brier": round(float(brier), 4), "base_rate_brier": round(base_brier, 4),
            "log_loss": round(float(logloss), 4), "hit_rate": round(float(hit), 4),
            "base_rates": {o: round(v, 4) for o, v in base.items()}, "reliability": table}


def build_report(conn: sqlite3.Connection, app_cfg, run_id: str, day: date, buckets: int = 5) -> dict:
    lo, hi = day_bounds(day)
    DecisionLog(conn)  # make sure the tables exist on a fresh store
    ps = PaperStore(conn, run_id)
    eq_all = ps.equity()
    loaded = ps.load()
    initial = app_cfg.paper.initial_equity
    prior = [e for t, e in eq_all if t < lo]
    today = [(t, e) for t, e in eq_all if lo <= t < hi]
    upto = [(t, e) for t, e in eq_all if t < hi]
    open_eq = prior[-1] if prior else (initial if today else None)
    close_eq = today[-1][1] if today else (prior[-1] if prior else None)
    series = pd.Series([e for _, e in upto], dtype=float)

    trades = [t for t in ps.trades() if lo <= t.exit_ts < hi]
    dec = conn.execute(
        "SELECT COUNT(*), SUM(abstain), SUM(cost_usd), AVG(latency_ms), SUM(input_tokens)"
        " FROM decisions WHERE run_id=? AND bar_ts>=? AND bar_ts<?", (run_id, lo, hi)).fetchone()
    actions = dict(conn.execute(
        "SELECT policy_action, COUNT(*) FROM decisions WHERE run_id=? AND bar_ts>=? AND bar_ts<?"
        " GROUP BY 1", (run_id, lo, hi)).fetchall())
    skips = dict(conn.execute(
        "SELECT substr(policy_reason, 1, instr(policy_reason, ':') - 1), COUNT(*) FROM decisions"
        " WHERE run_id=? AND bar_ts>=? AND bar_ts<? AND policy_action='skip' GROUP BY 1",
        (run_id, lo, hi)).fetchall())
    models = [r[0] for r in conn.execute(
        "SELECT DISTINCT model_version FROM decisions WHERE run_id=? AND bar_ts>=? AND bar_ts<?",
        (run_id, lo, hi))]

    positions = []
    if loaded is not None:
        st = loaded[0]
        for s, o in sorted(st.open.items()):
            mark = st.marks.get(s, o.pos.entry_price)
            positions.append({"symbol": s, "qty": o.pos.qty, "entry_price": o.pos.entry_price,
                              "entry_ts": o.pos.entry_ts, "stop_price": o.pos.stop_price, "mark": mark,
                              "unrealized_pnl": round(o.pos.qty * (mark - o.pos.entry_price), 2)})

    return {
        "run_id": run_id,
        "day": day.isoformat(),
        "model": loaded[1] if loaded else None,
        "model_version": loaded[2] if loaded else None,
        "bars": len(today),
        "risk_only_bars": conn.execute(
            "SELECT COUNT(*) FROM paper_bars WHERE run_id=? AND bar_ts>=? AND bar_ts<? AND mode='risk_only'",
            (run_id, lo, hi)).fetchone()[0],
        "equity_open": _r(open_eq), "equity_close": _r(close_eq),
        "day_pnl": _r(close_eq - open_eq) if open_eq is not None and close_eq is not None else None,
        "day_return": _r(close_eq / open_eq - 1, 6) if open_eq and close_eq is not None else None,
        "total_return": _r(close_eq / initial - 1, 6) if close_eq is not None else None,
        "max_drawdown": _r(max_drawdown(series), 6) if len(series) else None,
        "trades": [{"symbol": t.symbol, "entry": _iso(t.entry_ts), "exit": _iso(t.exit_ts),
                    "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": round(t.pnl, 2),
                    "ret": round(t.ret, 6), "bars_held": t.bars_held, "exit_reason": t.exit_reason}
                   for t in trades],
        "realized_pnl": round(sum(t.pnl for t in trades), 2),
        "open_positions": positions,
        "decisions": {"count": dec[0] or 0, "abstains": int(dec[1] or 0), "actions": actions,
                      "skip_reasons": skips, "model_versions": models,
                      "cost_usd": round(dec[2] or 0.0, 6), "input_tokens": int(dec[4] or 0),
                      "avg_latency_ms": round(dec[3], 1) if dec[3] is not None else None},
        "calibration": calibration(conn, app_cfg, run_id, hi, buckets),
    }


def _r(x, nd: int = 2):
    return None if x is None else round(float(x), nd)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _money(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def format_report(r: dict) -> str:
    d = r["decisions"]
    out = [
        f"# Paper report {r['day']} ({r['run_id']})",
        "",
        f"Model: {r['model'] or 'n/a'} ({r['model_version'] or 'n/a'}). Paper trading only; no real orders.",
        "",
        "## Account",
        "",
        f"- Equity: {_money(r['equity_open'])} to {_money(r['equity_close'])}, day {_money(r['day_pnl'])} "
        f"({_pct(r['day_return'])})",
        f"- Since start: {_pct(r['total_return'])}, max drawdown {_pct(r['max_drawdown'])}",
        f"- Bars processed: {r['bars']}" + (f" ({r['risk_only_bars']} risk-only after downtime)"
                                           if r["risk_only_bars"] else ""),
        f"- Realized PnL: {_money(r['realized_pnl'])} from {len(r['trades'])} closed trade(s)",
        "",
    ]
    if r["trades"]:
        out += ["| Symbol | Entry | Exit | Bars | Return | PnL | Exit reason |", "|---|---|---|---|---|---|---|"]
        out += [f"| {t['symbol']} | {t['entry']} @ {t['entry_price']:.6g} | {t['exit']} @ {t['exit_price']:.6g} "
                f"| {t['bars_held']} | {_pct(t['ret'])} | {t['pnl']:,.2f} | {t['exit_reason'].split(':')[0]} |"
                for t in r["trades"]]
        out.append("")
    out += ["## Open positions", ""]
    if r["open_positions"]:
        out += ["| Symbol | Qty | Entry | Stop | Mark | Unrealized |", "|---|---|---|---|---|---|"]
        out += [f"| {p['symbol']} | {p['qty']:.6g} | {p['entry_price']:.6g} | {p['stop_price']:.6g} "
                f"| {p['mark']:.6g} | {p['unrealized_pnl']:,.2f} |" for p in r["open_positions"]]
    else:
        out.append("None.")
    acts = ", ".join(f"{k} {v}" for k, v in sorted(d["actions"].items(), key=lambda kv: str(kv[0]))) or "none"
    skips = ", ".join(f"{k} {v}" for k, v in sorted(d["skip_reasons"].items())) or "none"
    out += [
        "",
        "## Decisions",
        "",
        f"- {d['count']} decisions, {d['abstains']} abstained. Actions: {acts}.",
        f"- Skip reasons: {skips}.",
        f"- Cost: ${d['cost_usd']:.4f} for {d['input_tokens']:,} input tokens"
        + (f", average latency {d['avg_latency_ms']:.0f} ms" if d["avg_latency_ms"] is not None else ""),
        "",
        "## Calibration (direction, all resolved decisions so far)",
        "",
    ]
    c = r["calibration"]
    if not c.get("n"):
        out.append("No resolved decisions yet.")
    else:
        out += [
            f"- {c['n']} decisions. Brier {c['brier']} (base-rate forecast {c['base_rate_brier']}; lower is "
            f"better), log loss {c['log_loss']}, top-choice hit rate {c['hit_rate']:.1%}.",
            f"- Realized: up {c['base_rates']['up']:.1%}, flat {c['base_rates']['flat']:.1%}, "
            f"down {c['base_rates']['down']:.1%}.",
            "",
            "| p(up) | n | mean p(up) | realized up |",
            "|---|---|---|---|",
        ]
        out += [f"| {b['p_up']} | {b['n']} | {b['mean_p_up']:.3f} | {b['realized_up']:.3f} |"
                for b in c["reliability"]]
    out.append("")
    return "\n".join(out)


def write_report(conn: sqlite3.Connection, app_cfg, run_id: str, day: date, cfg: ReportConfig) -> Path:
    r = build_report(conn, app_cfg, run_id, day, cfg.buckets)
    d = Path(cfg.output_dir) / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day.isoformat()}.json").write_text(json.dumps(r, indent=2, default=str) + "\n")
    path = d / f"{day.isoformat()}.md"
    path.write_text(format_report(r))
    return path


def previous_utc_day(now_ms: int) -> date:
    return (datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) - timedelta(days=1)).date()
