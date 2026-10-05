"""Incremental candle sync with gap detection and one backfill pass."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .fetcher import OHLCVSource, fetch_range
from .gaps import Gap, detect_gaps
from .store import CandleStore
from .timeframes import last_closed_open_ms

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    symbol: str
    inserted: int
    last_ts: int | None
    gaps: list[Gap] = field(default_factory=list)


def sync_symbol(
    source: OHLCVSource,
    store: CandleStore,
    exchange: str,
    symbol: str,
    timeframe: str,
    tf_ms: int,
    start_ms: int,
    now_ms: int | None = None,
    page_limit: int = 1000,
) -> SyncResult:
    """Bring the store up to the last *closed* candle.

    The still-forming candle is never stored: everything downstream assumes
    stored candles are final.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    until = last_closed_open_ms(now_ms, tf_ms)

    last = store.last_ts(exchange, symbol, timeframe)
    since = start_ms if last is None else last + tf_ms
    inserted = 0
    if since <= until:
        candles = fetch_range(source, symbol, timeframe, since, until, tf_ms, page_limit)
        inserted += store.upsert(exchange, symbol, timeframe, candles)

    # One backfill attempt per gap; anything left is a real exchange gap
    # (e.g. maintenance) and is reported, not invented.
    gaps = detect_gaps(store.timestamps(exchange, symbol, timeframe), tf_ms)
    for g in gaps:
        candles = fetch_range(
            source, symbol, timeframe, g.after_ts + tf_ms, g.before_ts - tf_ms, tf_ms, page_limit
        )
        inserted += store.upsert(exchange, symbol, timeframe, candles)
    if gaps:
        gaps = detect_gaps(store.timestamps(exchange, symbol, timeframe), tf_ms)
        for g in gaps:
            log.warning("%s %s: %d missing candles after ts=%d", symbol, timeframe, g.missing, g.after_ts)

    return SyncResult(symbol, inserted, store.last_ts(exchange, symbol, timeframe), gaps)
