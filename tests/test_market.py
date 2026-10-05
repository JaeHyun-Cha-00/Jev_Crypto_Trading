import json

import pytest
from fastapi.testclient import TestClient

from jevtrade.api.app import create_app
from jevtrade.forward.log import ForwardLog
from jevtrade.data.market import Market, MarketError
from jevtrade.data.market import MarketConfig
from jevtrade.forward.settings import ForwardLogConfig
from jevtrade.config import AppConfig

SYMS = ["BTC/USD", "ETH/USD"]

STATS = {
    "BTC-USD": {"stats_24hour": {"open": "100", "high": "112", "low": "95", "last": "110", "volume": "3"},
                "stats_30day": {"volume": "90"}},
    "ETH-USD": {"stats_24hour": {"open": "50", "high": "51", "low": "40", "last": "45", "volume": "10"}},
    "DOGE-USD": {"stats_24hour": {"open": "1", "last": "1"}},
}
CANDLES = [[1_700_000_060, 9, 12, 10, 11, 5.0], [1_700_000_000, 8, 11, 9, 10, 2.0]]   # newest first
BOOK = {"bids": [["109.5", "1.0", 2], ["109", "2", 1]], "asks": [["110.5", "0.5", 1], ["111", "3", 1]]}
TRADES = [{"trade_id": 2, "side": "buy", "size": "0.1", "price": "110", "time": "t2"},
          {"trade_id": 1, "side": "sell", "size": "0.2", "price": "109", "time": "t1"}]
CURRENCIES = [{"id": "BTC", "name": "Bitcoin"}, {"id": "ETH", "name": "Ethereum"}]


class FakeCoinbase:
    def __init__(self):
        self.calls: list[str] = []
        self.down = False

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.down:
            raise OSError("network is down")
        path = url.split("coinbase.com", 1)[1]
        if path == "/products/stats":
            return json.dumps(STATS).encode()
        if path == "/currencies":
            return json.dumps(CURRENCIES).encode()
        if "/candles" in path:
            return json.dumps(CANDLES).encode()
        if "/book" in path:
            return json.dumps(BOOK).encode()
        if "/trades" in path:
            return json.dumps(TRADES).encode()
        raise AssertionError(url)


class Clock:
    def __init__(self):
        self.t = 1_700_000_100.0

    def __call__(self):
        return self.t


def market(**cfg):
    cb, clock = FakeCoinbase(), Clock()
    m = Market(SYMS, MarketConfig(min_interval_s=0, **cfg), http_get=cb, clock=clock, background=False)
    return m, cb, clock


def test_tickers_compute_change_and_dollar_volume():
    m, _, _ = market()
    t = m.tickers()
    assert t["error"] is None and [c["symbol"] for c in t["coins"]] == SYMS   # only tracked coins
    btc, eth = t["coins"]
    assert btc["name"] == "Bitcoin" and btc["price"] == 110
    assert btc["change_24h"] == 10 and btc["change_pct_24h"] == pytest.approx(0.10)
    assert btc["volume_usd_24h"] == 330 and btc["volume_30d"] == 90
    assert eth["change_pct_24h"] == pytest.approx(-0.10) and eth["low_24h"] == 40


def test_stats_are_cached_and_last_good_prices_survive_an_outage():
    m, cb, clock = market(stats_seconds=5)
    m.tickers()
    m.tickers()
    assert sum(u.endswith("/products/stats") for u in cb.calls) == 1
    clock.t += 6
    cb.down = True
    t = m.tickers()
    assert t["coins"][0]["price"] == 110 and "network is down" in t["error"]


def test_no_data_yet_is_empty_rows_not_a_crash():
    m, cb, _ = market()
    cb.down = True
    t = m.tickers()
    assert t["updated_at"] is None and t["coins"][0]["price"] is None and t["coins"][0]["name"] == "BTC"


def test_sparklines_are_closes_oldest_first():
    m, _, _ = market()
    m.refresh_spark()
    assert m.tickers()["coins"][0]["spark"] == [10.0, 11.0]


def test_candles_book_and_trades():
    m, _, _ = market()
    c = m.candles("BTC/USD", "1m")
    assert c == [{"ts": 1_700_000_000_000, "open": 9, "high": 11, "low": 8, "close": 10, "volume": 2.0},
                 {"ts": 1_700_000_060_000, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 5.0}]
    b = m.book("BTC/USD", depth=1)
    assert b["bids"] == [[109.5, 1.0]] and b["asks"] == [[110.5, 0.5]]
    assert b["mid"] == 110 and b["spread"] == 1 and b["spread_pct"] == pytest.approx(1 / 110)
    tr = m.trades("BTC/USD")
    assert [t["side"] for t in tr] == ["sell", "buy"]   # taker side: Coinbase reports the maker's
    with pytest.raises(KeyError):
        m.book("DOGE/USD")   # not a tracked coin: never proxied


def test_routes():
    cb = FakeCoinbase()
    cfg = AppConfig.model_validate({"data": {"symbols": SYMS}})
    m = Market(SYMS, MarketConfig(min_interval_s=0), http_get=cb, background=False)
    c = TestClient(create_app(cfg, forward_log=ForwardLog(ForwardLogConfig(source="off")), market=m))
    assert c.get("/api/market/tickers").json()["coins"][1]["name"] == "Ethereum"
    assert len(c.get("/api/market/candles", params={"symbol": "BTC/USD", "timeframe": "1h"}).json()) == 2
    assert c.get("/api/market/candles", params={"symbol": "BTC/USD", "timeframe": "2h"}).status_code == 422
    assert c.get("/api/market/book", params={"symbol": "ETH/USD"}).json()["mid"] == 110
    assert c.get("/api/market/trades", params={"symbol": "SHIB/USD"}).status_code == 404
    cb.down = True
    r = c.get("/api/market/book", params={"symbol": "BTC/USD"})
    assert r.status_code == 502 and "network is down" in r.json()["detail"]
    assert c.post("/api/market/tickers").status_code == 405


def test_market_can_be_turned_off():
    cfg = AppConfig.model_validate({"data": {"symbols": SYMS}, "market": {"enabled": False}})
    m = Market(SYMS, cfg.market, http_get=FakeCoinbase(), background=False)
    c = TestClient(create_app(cfg, forward_log=ForwardLog(ForwardLogConfig(source="off")), market=m))
    assert c.get("/api/market/tickers").status_code == 404


def test_market_error_message_has_no_query_string():
    m, cb, _ = market()
    cb.down = True
    with pytest.raises(MarketError) as e:
        m.candles("BTC/USD", "1h", end=1_700_000_000)
    assert "?" not in str(e.value)
