"""Performance metrics and run outputs for a BacktestResult."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import BacktestResult


def _num(x: float | None, nd: int = 6) -> float | None:
    if x is None or math.isnan(x) or math.isinf(x):
        return None
    return round(float(x), nd)


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough fall, as a negative fraction (0 when never below a peak)."""
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1).min())


def summarize(res: BacktestResult, tf_ms: int) -> dict:
    eq = res.equity
    final = float(eq.iloc[-1]) if len(eq) else res.initial_equity
    rets = eq.pct_change().dropna()
    per_year = 365 * 86_400_000 / tf_ms
    years = len(eq) / per_year if len(eq) else 0.0
    sharpe = rets.mean() / rets.std() * math.sqrt(per_year) if len(rets) > 1 and rets.std() > 0 else None

    pnls = np.array([t.pnl for t in res.trades])
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    exits = Counter(t.exit_reason.split(":")[0] for t in res.trades)
    held = sum(t.bars_held for t in res.trades)
    bars = len(eq) * max(1, len(res.symbols))

    return {
        "run_id": res.run_id,
        "model": res.model,
        "model_version": res.model_version,
        "contamination": res.contamination,
        "symbols": res.symbols,
        "timeframe": res.timeframe,
        "start": eq.index[0].isoformat() if len(eq) else None,
        "end": eq.index[-1].isoformat() if len(eq) else None,
        "bars": len(eq),
        "initial_equity": res.initial_equity,
        "final_equity": _num(final, 2),
        "total_return": _num(final / res.initial_equity - 1),
        "cagr": _num((final / res.initial_equity) ** (1 / years) - 1) if years > 0 and final > 0 else None,
        "max_drawdown": _num(max_drawdown(eq)),
        "sharpe": _num(sharpe, 3) if sharpe is not None else None,
        "trades": len(res.trades),
        "win_rate": _num(len(wins) / len(pnls), 4) if len(pnls) else None,
        "profit_factor": _num(wins.sum() / -losses.sum(), 3) if len(losses) and losses.sum() < 0 else None,
        "avg_trade_return": _num(float(np.mean([t.ret for t in res.trades])), 6) if res.trades else None,
        "avg_bars_held": _num(held / len(res.trades), 2) if res.trades else None,
        "exposure": _num(held / bars, 4) if bars else None,  # share of symbol-bars in a position
        "fees_paid": _num(sum(t.fees for t in res.trades), 2),
        "exit_reasons": dict(exits),
        "counts": res.counts,
        "buy_and_hold_return": {s: _num(r) for s, r in res.benchmark.items()},
    }


def write_outputs(res: BacktestResult, summary: dict, out_dir: str | Path) -> Path:
    """Write summary.json, summary.md, trades.csv and equity.csv under out_dir/run_id."""
    d = Path(out_dir) / res.run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    trades = pd.DataFrame([asdict(t) for t in res.trades])
    for col in ("entry_ts", "exit_ts"):
        if col in trades:
            trades[col] = pd.to_datetime(trades[col], unit="ms", utc=True)
    trades.to_csv(d / "trades.csv", index=False)
    res.equity.to_csv(d / "equity.csv", header=True)
    (d / "summary.md").write_text(format_summary(summary))
    return d


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def format_summary(s: dict) -> str:
    bh = ", ".join(f"{k} {_pct(v)}" for k, v in s["buy_and_hold_return"].items())
    exits = ", ".join(f"{k} {v}" for k, v in sorted(s["exit_reasons"].items())) or "none"
    lines = [
        f"# Backtest {s['run_id']}",
        "",
        f"- Model: {s['model']} ({s['model_version']}); contamination: {s['contamination']}",
        f"- Period: {s['start']} to {s['end']}, {s['bars']} {s['timeframe']} bars, {', '.join(s['symbols'])}",
        f"- Equity: {s['initial_equity']:,.2f} to {s['final_equity']:,.2f} ({_pct(s['total_return'])}), "
        f"CAGR {_pct(s['cagr'])}, max drawdown {_pct(s['max_drawdown'])}, Sharpe {s['sharpe']}",
        f"- Trades: {s['trades']}, win rate {_pct(s['win_rate']) if s['win_rate'] is not None else 'n/a'}, "
        f"profit factor {s['profit_factor']}, avg trade {_pct(s['avg_trade_return'])}, "
        f"avg bars held {s['avg_bars_held']}, exposure {_pct(s['exposure'])}, fees {s['fees_paid']:,.2f}",
        f"- Exits: {exits}",
        f"- Buy and hold: {bh}",
        f"- Counts: {json.dumps(s['counts'])}",
        "",
    ]
    return "\n".join(lines)
