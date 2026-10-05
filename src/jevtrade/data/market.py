"""Live market data for the dashboard's coin list and coin pages.

Public Coinbase Exchange REST endpoints only (api.exchange.coinbase.com):
24-hour stats for every product in one call, candles, the level-2 order book
and recent trades. No credentials, no orders. Everything is cached briefly so
many open dashboards cost Coinbase the same few requests, and calls are
spaced to stay under the public rate limit (10 requests/second).

Sparklines (the last 24 hours in 15-minute closes) need one candle request
per coin, so a background thread refreshes them every `spark_seconds`; the
price list itself never waits on them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

from pydantic import BaseModel, Field

from .. import net
from ..net import HttpGet, redact


class MarketConfig(BaseModel):
    """Live prices for the dashboard's coin list (jevtrade.data.market): public Coinbase data only."""

    enabled: bool = True
    base_url: str = "https://api.exchange.coinbase.com"
    stats_seconds: float = Field(5, ge=1)        # re-read 24-hour stats for all coins at most this often
    spark_seconds: float = Field(300, ge=30)     # refresh the 24-hour sparklines this often
    book_seconds: float = Field(2, ge=0.5)       # order book and trades cache
    min_interval_s: float = Field(0.15, ge=0)    # spacing between Coinbase calls (public limit: 10/s)
    timeout_s: float = Field(10, gt=0)

log = logging.getLogger(__name__)

# Dashboard timeframe -> Coinbase candle granularity (seconds). Coinbase serves only these.
TIMEFRAMES: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
MAX_CANDLES = 300          # one page; Coinbase serves about this many per request
SPARK_GRANULARITY = 900    # 15-minute closes
SPARK_POINTS = 96          # 24 hours


class MarketError(Exception):
    """Coinbase could not be reached or answered with something unusable."""


def product_id(symbol: str) -> str:
    """BTC/USD -> BTC-USD."""
    return symbol.replace("/", "-")


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def coin_row(symbol: str, stats: dict | None, spark: list[float] | None, name: str | None = None) -> dict:
    """One line of the price list from Coinbase's 24-hour stats."""
    s = (stats or {}).get("stats_24hour") or {}
    last, open_ = _f(s.get("last")), _f(s.get("open"))
    vol = _f(s.get("volume"))
    change = last - open_ if last is not None and open_ else None
    return {
        "symbol": symbol,
        "name": name or symbol.split("/")[0],
        "price": last,
        "open_24h": open_,
        "high_24h": _f(s.get("high")),
        "low_24h": _f(s.get("low")),
        "change_24h": change,
        "change_pct_24h": change / open_ if change is not None else None,
        "volume_24h": vol,                                        # base currency
        "volume_usd_24h": vol * last if vol is not None and last is not None else None,
        "volume_30d": _f(((stats or {}).get("stats_30day") or {}).get("volume")),
        "spark": spark or [],
    }


class Market:
    def __init__(self, symbols: list[str], cfg: MarketConfig, http_get: HttpGet | None = None,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.symbols = list(symbols)
        self.cfg = cfg
        self._get = http_get or net.get
        self._clock = clock
        self._background = background
        self._lock = threading.Lock()
        self._pace = threading.Lock()
        self._last_call = 0.0
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._stats: tuple[float, dict] | None = None
        self._stats_error: str | None = None
        self._spark: dict[str, list[float]] = {}
        self._spark_at: float | None = None
        self._spark_thread: threading.Thread | None = None
        self._names: dict[str, str] = {}
        self._names_at: float | None = None

    # -- http

    def _json(self, path: str) -> object:
        with self._pace:   # space calls so a burst never trips Coinbase's public limit
            wait = self._last_call + self.cfg.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        url = self.cfg.base_url.rstrip("/") + path
        try:
            body = self._get(url, {"Accept": "application/json", "User-Agent": "jevtrade-dashboard"},
                             self.cfg.timeout_s)
            return json.loads(body)
        except Exception as e:
            raise MarketError(redact(f"{type(e).__name__}: {e}")[:300]) from e

    def _cached(self, key: tuple, ttl: float, fetch: Callable[[], object]) -> object:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fetch()
        with self._lock:
            self._cache[key] = (now, val)
            if len(self._cache) > 500:   # drop the stalest entries; 81 coins x a few views
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                    self._cache.pop(k, None)
        return val

    def _check(self, symbol: str) -> str:
        if symbol not in self.symbols:
            raise KeyError(symbol)
        return product_id(symbol)

    # -- price list

    def _all_stats(self) -> tuple[float | None, dict]:
        now = self._clock()
        with self._lock:
            fresh = self._stats and now - self._stats[0] < self.cfg.stats_seconds
            if fresh:
                return self._stats
        try:
            data = self._json("/products/stats")
            if not isinstance(data, dict):
                raise MarketError("unexpected /products/stats payload")
            with self._lock:
                self._stats = (now, data)
                self._stats_error = None
        except MarketError as e:   # keep serving the last good prices
            log.warning("market stats refresh failed: %s", e)
            with self._lock:
                self._stats_error = str(e)
                if self._stats:   # retry after a short pause, not on every request
                    self._stats = (now - self.cfg.stats_seconds + min(10, self.cfg.stats_seconds), self._stats[1])
        with self._lock:
            return self._stats if self._stats else (None, {})

    def names(self) -> dict[str, str]:
        """Currency id -> full name (BTC -> Bitcoin), re-read daily; empty if Coinbase is down."""
        now = self._clock()
        if self._names_at is not None and now - self._names_at < (86400 if self._names else 300):
            return self._names
        self._names_at = now
        try:
            rows = self._json("/currencies")
            self._names = {r["id"]: r["name"] for r in rows if isinstance(r, dict) and r.get("id") and r.get("name")}
        except (MarketError, TypeError) as e:
            log.info("currency names failed: %s", e)
        return self._names

    def tickers(self) -> dict:
        self._maybe_refresh_spark()
        at, stats = self._all_stats()
        names = self.names()
        with self._lock:
            spark = dict(self._spark)
            err = self._stats_error
            spark_at = self._spark_at
        coins = [coin_row(s, stats.get(product_id(s)), spark.get(s), names.get(s.split("/")[0]))
                 for s in self.symbols]
        return {"updated_at": at, "spark_updated_at": spark_at, "error": err,
                "source": "coinbase", "coins": coins}

    # -- sparklines (background)

    def _maybe_refresh_spark(self) -> None:
        if not self._background:
            return
        now = self._clock()
        with self._lock:
            due = self._spark_at is None or now - self._spark_at >= self.cfg.spark_seconds
            running = self._spark_thread is not None and self._spark_thread.is_alive()
            if not due or running:
                return
            self._spark_thread = threading.Thread(target=self.refresh_spark, name="market-spark", daemon=True)
            self._spark_thread.start()

    def refresh_spark(self) -> None:
        """Fetch 24 hours of 15-minute closes for every coin (one request each)."""
        end = int(self._clock())
        start = end - SPARK_GRANULARITY * (SPARK_POINTS + 1)
        for s in self.symbols:
            try:
                rows = self._json(f"/products/{product_id(s)}/candles?granularity={SPARK_GRANULARITY}"
                                  f"&start={start}&end={end}")
                closes = [float(r[4]) for r in sorted(rows, key=lambda r: r[0])] if isinstance(rows, list) else []
                with self._lock:
                    self._spark[s] = closes[-SPARK_POINTS:]
            except (MarketError, IndexError, TypeError, ValueError) as e:
                log.info("sparkline for %s failed: %s", s, e)
        with self._lock:
            self._spark_at = self._clock()

    # -- one coin

    def candles(self, symbol: str, timeframe: str, end: int | None = None) -> list[dict]:
        """Up to 300 candles, oldest first, ending at `end` (UTC seconds; default now)."""
        pid = self._check(symbol)
        g = TIMEFRAMES[timeframe]
        ttl = min(max(g / 6, 5), 60)

        def fetch():
            q = f"granularity={g}"
            if end is not None:
                q += f"&start={end - g * MAX_CANDLES}&end={end}"
            rows = self._json(f"/products/{pid}/candles?{q}")
            if not isinstance(rows, list):
                raise MarketError("unexpected candles payload")
            # Coinbase: [time, low, high, open, close, volume], newest first.
            return [{"ts": int(r[0]) * 1000, "open": float(r[3]), "high": float(r[2]), "low": float(r[1]),
                     "close": float(r[4]), "volume": float(r[5])} for r in sorted(rows, key=lambda r: r[0])]

        return self._cached(("candles", pid, g, end), ttl, fetch)

    def book(self, symbol: str, depth: int = 15) -> dict:
        """Best `depth` price levels each side, aggregated (level 2)."""
        pid = self._check(symbol)

        def fetch():
            b = self._json(f"/products/{pid}/book?level=2")
            if not isinstance(b, dict):
                raise MarketError("unexpected book payload")
            return b

        b = self._cached(("book", pid), self.cfg.book_seconds, fetch)
        bids = [[float(p), float(s)] for p, s, *_ in (b.get("bids") or [])[:depth]]
        asks = [[float(p), float(s)] for p, s, *_ in (b.get("asks") or [])[:depth]]
        mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else None
        return {"symbol": symbol, "bids": bids, "asks": asks, "mid": mid,
                "spread": asks[0][0] - bids[0][0] if bids and asks else None,
                "spread_pct": (asks[0][0] - bids[0][0]) / mid if mid else None}

    def trades(self, symbol: str, limit: int = 40) -> list[dict]:
        """Recent fills, newest first. `side` is the taker's: Coinbase reports the maker's side."""
        pid = self._check(symbol)

        def fetch():
            rows = self._json(f"/products/{pid}/trades?limit=100")
            if not isinstance(rows, list):
                raise MarketError("unexpected trades payload")
            return rows

        rows = self._cached(("trades", pid), self.cfg.book_seconds, fetch)
        return [{"id": r.get("trade_id"), "time": r.get("time"), "price": float(r["price"]),
                 "size": float(r["size"]), "side": "sell" if r.get("side") == "buy" else "buy"}
                for r in rows[:limit]]
