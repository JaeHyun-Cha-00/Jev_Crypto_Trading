"""Live coin list and Robinhood quotes (jevtrade.data.live), from canned responses."""

import json

from jevtrade.config import DataConfig
from jevtrade.data.live import (COINBASE_PRODUCTS_URL, ROBINHOOD_PAIRS_URL, live_symbols, resolve_symbols,
                                robinhood_quotes)


def pair(code, pid, tradable=True, stable=False, quote="USD"):
    return {"asset_currency": {"code": code}, "quote_currency": {"code": quote}, "id": pid,
            "tradability": "tradable" if tradable else "untradable", "display_only": False,
            "is_stablecoin": stable}


PAIRS = {"next": None, "results": [
    pair("BTC", "id-btc"), pair("ETH", "id-eth"), pair("SOL", "id-sol"), pair("USDC", "id-usdc", stable=True),
    pair("PAXG", "id-paxg"), pair("DOGE", "id-doge", tradable=False), pair("AAA", "id-aaa"),
    pair("ZZZ", "id-zzz"), pair("RHONLY", "id-rh"), pair("BTC", "id-btc-eur", quote="EUR")]}
PRODUCTS = [{"base_currency": c, "quote_currency": "USD", "status": "online", "trading_disabled": False}
            for c in ("BTC", "ETH", "SOL", "USDC", "PAXG", "DOGE", "AAA", "ZZZ")]
PRODUCTS.append({"base_currency": "OFF", "quote_currency": "USD", "status": "delisted"})


def http(routes):
    calls = []

    def get(url, headers, timeout_s):
        calls.append(url)
        for prefix, body in routes.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return json.dumps(body).encode()
        raise AssertionError(url)
    get.calls = calls
    return get


def test_live_list_keeps_configured_order_and_appends_new_listings():
    get = http({ROBINHOOD_PAIRS_URL: PAIRS, COINBASE_PRODUCTS_URL: PRODUCTS})
    out = live_symbols(["ETH/USD", "DOGE/USD", "BTC/USD"], ["PAXG/USD"], get)
    # DOGE untradable on Robinhood, USDC a stablecoin, PAXG excluded, RHONLY not on Coinbase.
    assert out == ["ETH/USD", "BTC/USD", "AAA/USD", "SOL/USD", "ZZZ/USD"]


def test_resolve_falls_back_to_the_configured_list_on_any_error():
    cfg = DataConfig(symbols=["BTC/USD"], symbols_live=True)
    assert resolve_symbols(cfg, http({ROBINHOOD_PAIRS_URL: OSError("down")})) == ["BTC/USD"]
    assert cfg.symbols == ["BTC/USD"]


def test_resolve_is_off_unless_asked():
    cfg = DataConfig(symbols=["BTC/USD"])
    get = http({})
    assert resolve_symbols(cfg, get) == ["BTC/USD"] and get.calls == []


def test_resolve_updates_the_config():
    cfg = DataConfig(symbols=["BTC/USD"], symbols_live=True, exclude=["PAXG/USD"])
    resolve_symbols(cfg, http({ROBINHOOD_PAIRS_URL: PAIRS, COINBASE_PRODUCTS_URL: PRODUCTS}))
    assert cfg.symbols[0] == "BTC/USD" and "PAXG/USD" not in cfg.symbols and len(cfg.symbols) == 5


def test_quotes_map_pair_ids_back_to_symbols_in_one_call():
    quotes = {"results": [
        {"id": "id-btc", "bid_price": "99", "ask_price": "101"},
        {"id": "id-eth", "bid_price": "0", "ask_price": "5"},      # unusable
        None]}
    get = http({ROBINHOOD_PAIRS_URL: PAIRS, "https://api.robinhood.com/marketdata/forex/quotes/": quotes})
    out = robinhood_quotes(["BTC/USD", "ETH/USD", "NOPE/USD"], get)
    assert out == {"BTC/USD": {"bid": 99.0, "ask": 101.0}}
    assert sum("quotes" in u for u in get.calls) == 1
