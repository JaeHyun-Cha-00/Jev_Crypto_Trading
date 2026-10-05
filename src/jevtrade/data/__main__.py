"""CLI: python -m jevtrade.data [--config PATH]"""

from __future__ import annotations

import argparse
import logging

from ..config import load_config
from .fetcher import has_deep_history, page_limit_for, public_exchange
from .store import CandleStore, connect
from .sync import sync_symbol
from .timeframes import iso_to_ms, ms_to_iso, timeframe_ms


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync public OHLCV candles into SQLite")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config(args.config)
    ex = public_exchange(cfg.data.exchange)
    store = CandleStore(connect(cfg.storage.sqlite_path))
    tf_ms = timeframe_ms(cfg.data.timeframe)
    page_limit = page_limit_for(cfg.data.exchange, cfg.data.page_limit)
    deep = has_deep_history(cfg.data.exchange)
    for symbol in cfg.data.symbols:
        r = sync_symbol(ex, store, cfg.data.exchange, symbol, cfg.data.timeframe, tf_ms,
                        iso_to_ms(cfg.data.start), page_limit=page_limit, deep_history=deep)
        last = ms_to_iso(r.last_ts) if r.last_ts else "-"
        print(f"{symbol}: +{r.inserted} candles, last={last}, unresolved gaps={len(r.gaps)}")


if __name__ == "__main__":
    main()
