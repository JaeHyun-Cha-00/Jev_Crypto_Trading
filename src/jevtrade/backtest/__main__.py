"""CLI: walk-forward backtest over the stored candles.

    python -m jevtrade.backtest [--model mock|baseline|jev] [--start ISO] [--end ISO]
                                [--symbols BTC/USD ETH/USD] [--config PATH]

Reads candles from the configured SQLite store (run `python -m jevtrade.data`
first), runs the configured decision model through the policy, logs every
decision under the run id, and writes summary.json, summary.md, trades.csv
and equity.csv to `backtest.output_dir/<run id>/`.

`--model jev` makes one paid API call per symbol per bar, so it also needs
`--allow-live-model`.
"""

from __future__ import annotations

import argparse
import logging

from ..config import load_config
from ..data.store import CandleStore, connect
from ..data.timeframes import timeframe_ms
from ..decision.factory import build_model
from ..decision.log import DecisionLog
from .engine import Backtester
from .metrics import format_summary, summarize, write_outputs


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a walk-forward backtest on stored candles")
    ap.add_argument("--config", default=None)
    ap.add_argument("--model", default=None, help="mock | baseline | jev (default: decision.model)")
    ap.add_argument("--start", default=None, help="first decision bar, ISO-8601 UTC")
    ap.add_argument("--end", default=None, help="last bar, ISO-8601 UTC")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--allow-live-model", action="store_true",
                    help="required for --model jev, which calls the paid API on every bar")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    cfg = load_config(args.config)
    if args.model:
        cfg.decision.model = args.model
    if args.start:
        cfg.backtest.start = args.start
    if args.end:
        cfg.backtest.end = args.end
    if cfg.decision.model == "jev" and not args.allow_live_model:
        raise SystemExit("--model jev calls the paid Jev API once per symbol per bar; "
                         "pass --allow-live-model to run it")

    conn = connect(cfg.storage.sqlite_path)
    store = CandleStore(conn)
    symbols = args.symbols or cfg.data.symbols
    candles = {s: store.load(cfg.data.exchange, s, cfg.data.timeframe) for s in symbols}
    empty = [s for s, df in candles.items() if df.empty]
    if empty:
        raise SystemExit(f"no stored candles for {empty}; run `python -m jevtrade.data` first")

    tf_ms = timeframe_ms(cfg.data.timeframe)
    bt = Backtester(
        build_model(cfg.decision), cfg.decision.resolved_questions(cfg.data.timeframe), cfg.policy,
        cfg.backtest, tf_ms, cfg.data.timeframe, cfg.decision.horizon_bars,
        cfg.features, cfg.state, DecisionLog(conn), run_id=args.run_id,
    )
    res = bt.run(candles)
    summary = summarize(res, tf_ms)
    out = write_outputs(res, summary, cfg.backtest.output_dir)
    print(format_summary(summary))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
