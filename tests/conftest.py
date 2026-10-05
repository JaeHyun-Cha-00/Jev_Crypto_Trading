from __future__ import annotations

import numpy as np
import pytest

H = 3_600_000  # 1h in ms
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def synthetic_candles(n: int, start_ms: int = T0, tf_ms: int = H, seed: int = 0) -> list[list[float]]:
    """Deterministic geometric random-walk OHLCV."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, n))
    vol = rng.lognormal(10, 0.5, n)
    return [[start_ms + i * tf_ms, open_[i], high[i], low[i], close[i], vol[i]] for i in range(n)]


class FakeSource:
    """In-memory OHLCV source mimicking ccxt.fetch_ohlcv semantics."""

    def __init__(self, candles: list[list[float]], missing: set[int] | None = None):
        self.candles = candles
        self.missing = missing or set()
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        rows = [c for c in self.candles if (since is None or c[0] >= since) and c[0] not in self.missing]
        return rows[: limit or 500]


@pytest.fixture
def candles_500():
    return synthetic_candles(500)
