"""Candle timeframes and limits, without ccxt, so the API server can use them cheaply."""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Coinbase Exchange (api.exchange.coinbase.com) returns at most 300 candles per
# request, but pages back through full history.
PAGE_CAP = 300

# The same units and lengths as ccxt.Exchange.parse_timeframe ("M" is 30 days).
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
_TIMEFRAME = re.compile(r"^(\d+)([smhdwMy])$")


def timeframe_ms(timeframe: str) -> int:
    m = _TIMEFRAME.match(timeframe)
    if not m:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    return int(m.group(1)) * _UNIT_S[m.group(2)] * 1000


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_closed_open_ms(now_ms: int, tf_ms: int) -> int:
    """Open time of the most recent candle that has fully closed at `now_ms`."""
    return (now_ms // tf_ms) * tf_ms - tf_ms
