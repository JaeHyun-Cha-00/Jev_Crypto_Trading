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

    def __init__(self, candles: list[list[float]], missing: set[int] | None = None,
                 max_history: int | None = None):
        self.candles = candles
        self.missing = missing or set()
        self.max_history = max_history  # e.g. Kraken: only the latest 720 candles exist
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        pool = self.candles[-self.max_history:] if self.max_history else self.candles
        rows = [c for c in pool if (since is None or c[0] >= since) and c[0] not in self.missing]
        return rows[: limit or 500]


class WindowedSource(FakeSource):
    """Coinbase-style source: each request returns only candles inside the
    window [since, since + (limit - 1) * tf], capped at `cap` candles. Full
    history is reachable, and a window with no trades comes back empty."""

    def __init__(self, candles, missing=None, cap: int = 300, tf_ms: int = H):
        super().__init__(candles, missing)
        self.cap = cap
        self.tf_ms = tf_ms

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        n = min(limit or self.cap, self.cap)
        end = since + (n - 1) * self.tf_ms
        return [c for c in self.candles if since <= c[0] <= end and c[0] not in self.missing]


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    """The forward-log loader must never reach the network in tests."""
    def refuse(url, headers, timeout_s):
        raise OSError(f"network disabled in tests: {url}")
    monkeypatch.setattr("jevtrade.api.forward._http_get", refuse)
