"""Public OHLCV fetching via ccxt.

This module only ever calls `fetch_ohlcv`, a public endpoint. Exchanges are
constructed without credentials, and construction fails loudly if any are
present, so nothing here can authenticate or place orders.
"""

from __future__ import annotations

from typing import Protocol

import ccxt


class OHLCVSource(Protocol):
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]: ...


_CREDENTIAL_FIELDS = ("apiKey", "secret", "password", "uid", "privateKey", "walletAddress")

# Exchanges whose public OHLCV serves only the latest N candles, whatever
# `since` is. Kraken's OHLC endpoint returns at most the latest 720.
CAPPED_HISTORY: dict[str, int] = {"kraken": 720}

# Max candles per request. Coinbase Exchange (api.exchange.coinbase.com)
# returns at most 300 candles per window but pages back through full history.
PAGE_CAPS: dict[str, int] = {"coinbaseexchange": 300, **CAPPED_HISTORY}


def has_deep_history(exchange_id: str) -> bool:
    """True if `since` can reach arbitrarily old candles on this exchange."""
    return exchange_id not in CAPPED_HISTORY


def page_limit_for(exchange_id: str, configured: int) -> int:
    """Clamp the configured page size to what the exchange serves per request.

    Empty-window skipping in `fetch_range` advances by one page, so a page
    larger than the exchange's window would jump over real candles.
    """
    cap = PAGE_CAPS.get(exchange_id)
    return min(configured, cap) if cap else configured


def public_exchange(exchange_id: str) -> ccxt.Exchange:
    """Build an unauthenticated ccxt exchange client."""
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"unknown ccxt exchange: {exchange_id}")
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    for field in _CREDENTIAL_FIELDS:
        if getattr(ex, field, None):
            raise RuntimeError(f"refusing exchange client with credential field {field!r} set")
    if not ex.has.get("fetchOHLCV"):
        raise ValueError(f"{exchange_id} does not support public OHLCV")
    return ex


def fetch_range(
    source: OHLCVSource,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    tf_ms: int,
    page_limit: int = 1000,
    skip_empty: bool = False,
) -> list[list[float]]:
    """Fetch candles with open time in [since_ms, until_ms], paginating forward.

    With `skip_empty`, an empty page is treated as a window with no trades
    (an outage, or time before the market listed) and the cursor moves on by
    one page instead of stopping. Only use it for exchanges whose `since`
    reaches full history; on a capped-history exchange an empty page means
    there is nothing newer, and stepping through windows would only waste
    requests.
    """
    out: list[list[float]] = []
    cursor = since_ms
    while cursor <= until_ms:
        page = source.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page_limit)
        page = [c for c in page if cursor <= c[0] <= until_ms]
        if not page:
            if skip_empty:
                cursor += page_limit * tf_ms
                continue
            break
        out.extend(page)
        nxt = int(page[-1][0]) + tf_ms
        if nxt <= cursor:  # defensive: never loop on a stuck cursor
            break
        cursor = nxt
    return out
