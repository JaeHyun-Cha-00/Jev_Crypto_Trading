"""Read-only HTTP API behind the dashboard.

Every route is a GET. The forward-log routes only read the collector's JSONL,
locally or from GitHub (jevtrade.forward.log), and /api/forward/paper replays
Jev's logged answers through the simulator (jevtrade.forward.portfolio). The
market routes proxy public Coinbase data (jevtrade.data.market). Nothing here
calls the model or places an order.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from ..data.timeframes import timeframe_ms
from ..forward.log import ForwardLog
from ..forward.portfolio import detail_curve, replay
from ..data.market import TIMEFRAMES, Market, MarketError
from .settings import ApiConfig  # noqa: F401  (re-exported)


def create_app(app_cfg, forward_log: ForwardLog | None = None, market: Market | None = None) -> FastAPI:
    """`app_cfg` is a jevtrade.config.AppConfig; `forward_log` and `market` override the
    loaders built from `app_cfg.forward_log` and `app_cfg.market` (tests inject them)."""
    fwd = forward_log or ForwardLog(app_cfg.forward_log)
    mkt = market or Market(app_cfg.data.symbols, app_cfg.market)
    app = FastAPI(title="jevtrade (read-only)", version="1",
                  description="Jev's forward log, its replayed portfolio and live market data. GET only.")
    app.add_middleware(CORSMiddleware, allow_origins=app_cfg.api.cors_origins,
                       allow_methods=["GET"], allow_headers=["*"])
    # The dashboard pulls the replay (hundreds of KB of JSON) every minute.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "time": time.time()}

    @app.get("/api/config")
    def config():
        """Non-secret settings: symbols, the direction question, and the Jev portfolio's sizing and limits."""
        p = app_cfg.policy.for_model("jev")
        return {
            "exchange": app_cfg.data.exchange,
            "symbols": app_cfg.data.symbols,
            "timeframe": app_cfg.data.timeframe,
            "jev_model": app_cfg.decision.jev.model,
            "horizon_bars": app_cfg.decision.horizon_bars,
            "flat_band_pct": app_cfg.decision.flat_band_pct,
            "sizing": p.describe_sizing(app_cfg.jev_paper.initial_equity),
            "policy": p.model_dump(),
        }

    @app.get("/api/forward/summary")
    def forward_summary():
        """Jev's hourly forward log (data-log branch) scored against realized outcomes."""
        return fwd.summary()

    @app.get("/api/forward/rows")
    def forward_rows(symbol: str | None = None, limit: int = Query(100, ge=1, le=2_000)):
        """Recent decisions, newest first, each with its outcome (null while pending)."""
        return fwd.rows(symbol, limit)

    paper_cache: dict = {}

    def paper_replay() -> dict:
        snap = fwd.snapshot()
        if paper_cache.get("snap") is not snap:   # replay once per forward-log refresh
            # The portfolio may restart later than the log (jev_paper.start); the later one wins.
            starts = [t for t in (app_cfg.forward_log.start_ms(), app_cfg.jev_paper.start_ms()) if t is not None]
            start = max(starts) if starts else None
            rows = snap.rows if start is None else [r for r in snap.rows if r["candle_ts"] >= start]
            out = replay(rows, app_cfg, timeframe_ms(app_cfg.data.timeframe))
            out["tracking_since"] = start if start is not None else (out["curve"][0]["bar_ts"] if out["curve"] else None)
            paper_cache.update(snap=snap, out=out)
        return paper_cache["out"]

    @app.get("/api/forward/paper")
    def forward_paper(actions: int = Query(200, ge=0, le=5_000), hours: int = Query(168, ge=0, le=5_000)):
        """Jev's simulated account: the policy and simulator replayed over the forward log's
        answers and closes from forward_log.start on. No model or exchange calls;
        fills are simulated. `actions` and `hours` cap the newest calls and hours returned."""
        out = dict(paper_replay())
        out["actions"] = out["actions"][:actions]
        out["hours"] = out["hours"][:hours]
        return out

    @app.get("/api/forward/paper/detail")
    def forward_paper_detail(hours: float | None = Query(None, gt=0, le=24 * 366,
                                                         description="newest hours to cover; default all")):
        """The simulated account at 5-minute (up to 50 hours), 15-minute (up to a week) or hourly
        steps: each hour's cash and coins valued at Coinbase candle closes."""
        out = paper_replay()
        curve = out["curve"]
        tf_ms = timeframe_ms(app_cfg.data.timeframe)
        if not curve:
            return {"step_ms": None, "points": []}
        end = int(time.time() * 1000)
        first = out["tracking_since"] or curve[0]["bar_ts"]
        start = max(first, end - int(hours * 3_600_000)) if hours else first
        step = next((g for g in (300, 900, 3600) if (end - start) / (g * 1000) <= 600), 3600) * 1000
        tf = {300_000: "5m", 900_000: "15m", 3_600_000: "1h"}[step]
        # Coins held at any close in the window, or going into it.
        closes = [p["bar_ts"] + tf_ms for p in curve]
        held = {s for p, c in zip(curve, closes) if c > start - tf_ms for s in p.get("holdings") or {}}
        candles: dict[str, list[dict]] = {}
        if app_cfg.market.enabled:
            g = step // 1000
            for sym in sorted(held):
                rows, e = [], (end // step + 1) * g   # aligned, so repeat calls hit the cache
                try:
                    while e * 1000 > start:
                        page = mkt.candles(sym, tf, e)
                        if not page:
                            break
                        rows += page
                        e -= g * 300
                except (KeyError, MarketError):
                    pass   # that coin falls back to its hourly price
                candles[sym] = rows
        return {"step_ms": step, "points": detail_curve(curve, candles, start, end, step, tf_ms)}

    # -- live market (public Coinbase data; jevtrade.data.market)

    def market_call(fn, *args):
        if not app_cfg.market.enabled:
            raise HTTPException(404, "market data is off (market.enabled in config)")
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(404, f"{args[0]!r} is not a tracked coin")
        except MarketError as e:
            raise HTTPException(502, f"Coinbase: {e}")

    @app.get("/api/market/tickers")
    def market_tickers():
        """Price, 24-hour change, range and volume for every tracked coin, plus a 24-hour sparkline."""
        return market_call(mkt.tickers)

    @app.get("/api/market/candles")
    def market_candles(symbol: str, timeframe: str = Query("1h", pattern="^(" + "|".join(TIMEFRAMES) + ")$"),
                       end: int | None = Query(None, description="UTC seconds; default now")):
        """Up to 300 Coinbase candles, oldest first; ts is the candle open in UTC ms."""
        return market_call(mkt.candles, symbol, timeframe, end)

    @app.get("/api/market/book")
    def market_book(symbol: str, depth: int = Query(15, ge=1, le=50)):
        """Best bids and asks (aggregated by price level)."""
        return market_call(mkt.book, symbol, depth)

    @app.get("/api/market/trades")
    def market_trades(symbol: str, limit: int = Query(40, ge=1, le=100)):
        """Recent fills, newest first; side is the taker's."""
        return market_call(mkt.trades, symbol, limit)

    return app
