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
    deep_history: bool = False,
) -> SyncResult:
    """Bring the store up to the last *closed* candle.

    The still-forming candle is never stored: everything downstream assumes
    stored candles are final.

    `deep_history` marks a source whose `since` reaches old candles (Coinbase,
    not Kraken). Pagination then steps over empty windows, and if the store
    starts after `start_ms` (for example, `start` was moved earlier) the
    missing head is backfilled.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    until = last_closed_open_ms(now_ms, tf_ms)

    last = store.last_ts(exchange, symbol, timeframe)
    since = start_ms if last is None else last + tf_ms
    inserted = 0
    if since <= until:
        candles = fetch_range(source, symbol, timeframe, since, until, tf_ms, page_limit,
                              skip_empty=deep_history)
        inserted += store.upsert(exchange, symbol, timeframe, candles)

    first = store.first_ts(exchange, symbol, timeframe)
    if deep_history and first is not None and first > start_ms:
        candles = fetch_range(source, symbol, timeframe, start_ms, first - tf_ms, tf_ms,
                              page_limit, skip_empty=True)
        inserted += store.upsert(exchange, symbol, timeframe, candles)

    # One backfill attempt per gap, ever; anything left is a real exchange gap
    # (e.g. maintenance, or an hour with no trades) and is reported, not
    # invented. Tried gaps are remembered so a thin coin's hundreds of empty
    # hours are not re-fetched on every sync.
    gaps = detect_gaps(store.timestamps(exchange, symbol, timeframe), tf_ms)
    tried = store.tried_gaps(exchange, symbol, timeframe)
    todo = [g for g in gaps if (g.after_ts, g.before_ts) not in tried]
    for g in todo:
        candles = fetch_range(
            source, symbol, timeframe, g.after_ts + tf_ms, g.before_ts - tf_ms, tf_ms, page_limit,
            skip_empty=deep_history,
        )
        inserted += store.upsert(exchange, symbol, timeframe, candles)
    if todo:
        gaps = detect_gaps(store.timestamps(exchange, symbol, timeframe), tf_ms)
        store.mark_gaps_tried(exchange, symbol, timeframe, [(g.after_ts, g.before_ts) for g in gaps])
        new = {(g.after_ts, g.before_ts) for g in todo}
        for g in gaps:
            if (g.after_ts, g.before_ts) in new:
                log.warning("%s %s: %d missing candles after ts=%d", symbol, timeframe, g.missing, g.after_ts)

    return SyncResult(symbol, inserted, store.last_ts(exchange, symbol, timeframe), gaps)
