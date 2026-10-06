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

# Coinbase Exchange (api.exchange.coinbase.com) returns at most 300 candles per
# request, but pages back through full history.
PAGE_CAP = 300


def public_exchange(exchange_id: str, requests_trust_env: bool = True) -> ccxt.Exchange:
    """Build an unauthenticated ccxt exchange client.

    `requests_trust_env` makes ccxt honour HTTPS_PROXY / REQUESTS_CA_BUNDLE
    like any other HTTP client; ccxt ignores them by default, which breaks
    fetching behind a TLS-intercepting proxy.
    """
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"unknown ccxt exchange: {exchange_id}")
    ex = getattr(ccxt, exchange_id)(
        {"enableRateLimit": True, "requests_trust_env": requests_trust_env}
    )
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
    page_limit: int = PAGE_CAP,
) -> list[list[float]]:
    """Fetch candles with open time in [since_ms, until_ms], paginating forward.

    An empty page is a window with no trades (an outage, or time before the
    market listed), so the cursor moves on by one page instead of stopping.
    `page_limit` must not exceed what the exchange serves per request, or that
    step would jump over real candles.
    """
    out: list[list[float]] = []
    cursor = since_ms
    while cursor <= until_ms:
        page = source.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page_limit)
        page = [c for c in page if cursor <= c[0] <= until_ms]
        if not page:
            cursor += page_limit * tf_ms
            continue
        out.extend(page)
        nxt = int(page[-1][0]) + tf_ms
        if nxt <= cursor:  # defensive: never loop on a stuck cursor
            break
        cursor = nxt
    return out
