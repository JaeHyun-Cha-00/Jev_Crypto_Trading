from __future__ import annotations

from datetime import datetime, timezone

import ccxt


def timeframe_ms(timeframe: str) -> int:
    return int(ccxt.Exchange.parse_timeframe(timeframe)) * 1000


def iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_closed_open_ms(now_ms: int, tf_ms: int) -> int:
    """Open time of the most recent candle that has fully closed at `now_ms`."""
    return (now_ms // tf_ms) * tf_ms - tf_ms
