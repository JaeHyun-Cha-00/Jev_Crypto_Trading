"""Coinbase (api.exchange.coinbase.com) as the default exchange: windowed
pagination over deep history, with Kraken kept as a config option."""

import ccxt

from jevtrade.config import load_config
from jevtrade.data.fetcher import (
    fetch_range, has_deep_history, page_limit_for, public_exchange,
)
from jevtrade.data.store import CandleStore, connect
from jevtrade.data.sync import sync_symbol

from conftest import H, T0, WindowedSource, synthetic_candles

EX, SYM, TF = "coinbaseexchange", "BTC/USD", "1h"
YEAR = 366 * 24


def _store():
    return CandleStore(connect(":memory:"))


def test_default_config_is_coinbase_usd():
    cfg = load_config()
    assert cfg.data.exchange == "coinbaseexchange"
    syms = cfg.data.symbols
    assert syms[:2] == ["BTC/USD", "ETH/USD"]
    assert len(syms) == len(set(syms)) and all(s.endswith("/USD") for s in syms)
    assert not {"USDC/USD", "USDT/USD", "PAXG/USD"} & set(syms)  # no pegged coins
    assert page_limit_for(cfg.data.exchange, cfg.data.page_limit) == 300


def test_kraken_still_selectable(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("data:\n  exchange: kraken\n  page_limit: 720\n")
    cfg = load_config(p)
    assert cfg.data.exchange == "kraken"
    assert not has_deep_history("kraken")
    assert page_limit_for("kraken", 1000) == 720
    assert public_exchange("kraken").id == "kraken"


def test_page_limit_clamped_to_exchange_cap():
    assert page_limit_for("coinbaseexchange", 720) == 300
    assert page_limit_for("coinbaseexchange", 100) == 100
    assert page_limit_for("someexchange", 1000) == 1000
    assert has_deep_history("coinbaseexchange")


def test_public_coinbase_client_hits_exchange_api_without_credentials():
    ex = public_exchange("coinbaseexchange")
    assert ex.implode_hostname(ex.urls["api"]["public"]) == "https://api.exchange.coinbase.com"
    assert not ex.apiKey and not ex.secret


def test_backfills_a_year_through_300_candle_windows():
    n = YEAR + 50
    src = WindowedSource(synthetic_candles(n))
    store = _store()
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + n * H,
                    page_limit=300, deep_history=True)
    ts = store.timestamps(EX, SYM, TF)
    assert r.inserted == n and len(ts) == n
    assert ts[0] == T0 and ts[-1] == T0 + (n - 1) * H
    assert r.gaps == []
    assert src.calls == -(-n // 300)  # one request per window


def test_empty_window_does_not_stop_pagination():
    """A no-trade stretch longer than one window (an outage) must be stepped
    over, then reported as a gap, never ended on or filled in."""
    n = 2000
    missing = {T0 + i * H for i in range(500, 1200)}  # 700 candles > 300 window
    src = WindowedSource(synthetic_candles(n), missing)
    store = _store()
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + n * H,
                    page_limit=300, deep_history=True)
    assert store.last_ts(EX, SYM, TF) == T0 + (n - 1) * H
    assert len(store.timestamps(EX, SYM, TF)) == n - 700
    assert len(r.gaps) == 1 and r.gaps[0].missing == 700


def test_without_deep_history_an_empty_window_stops():
    """The old behaviour, kept for capped-history exchanges like Kraken."""
    missing = {T0 + i * H for i in range(0, 300)}
    out = fetch_range(WindowedSource(synthetic_candles(600), missing), SYM, TF,
                      T0, T0 + 599 * H, H, 300, skip_empty=False)
    assert out == []


def test_head_backfilled_when_start_moves_earlier():
    n = YEAR
    all_c = synthetic_candles(n)
    store = _store()
    store.upsert(EX, SYM, TF, all_c[-100:])  # e.g. a store first synced recently
    src = WindowedSource(all_c)
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + n * H,
                    page_limit=300, deep_history=True)
    assert store.timestamps(EX, SYM, TF)[0] == T0
    assert len(store.timestamps(EX, SYM, TF)) == n
    assert r.inserted == n - 100 and r.gaps == []


def test_ccxt_coinbase_request_shape_and_parsing(monkeypatch):
    """Drive the real ccxt client with the HTTP call mocked: requests carry
    granularity and a start/end window, and Coinbase's newest-first rows in
    epoch seconds come back as ascending ms candles."""
    ex = public_exchange("coinbaseexchange")
    ex.set_markets([{
        "id": "BTC-USD", "symbol": "BTC/USD", "base": "BTC", "quote": "USD",
        "baseId": "BTC", "quoteId": "USD", "type": "spot", "spot": True, "active": True,
    }])
    candles = synthetic_candles(700)
    requests = []

    def fake_candles(params):
        requests.append(params)
        lo = ex.parse8601(params["start"])
        hi = ex.parse8601(params["end"])
        rows = [c for c in candles if lo <= c[0] <= hi]
        # Coinbase: [time_s, low, high, open, close, volume], newest first
        return [[c[0] // 1000, *(float(c[k]) for k in (3, 2, 1, 4, 5))] for c in reversed(rows)]

    monkeypatch.setattr(ex, "publicGetProductsIdCandles", fake_candles)
    out = fetch_range(ex, "BTC/USD", "1h", T0, T0 + 699 * H, H, 300, skip_empty=True)

    assert [c[0] for c in out] == [c[0] for c in candles]
    assert out[0][1:5] == candles[0][1:5]  # open, high, low, close in ccxt order
    assert len(requests) == 3
    assert all(r["id"] == "BTC-USD" and r["granularity"] == 3600 for r in requests)
    assert requests[0]["start"].startswith("2024-01-01T00:00:00")
    assert ex.parse8601(requests[0]["end"]) == T0 + 299 * H
    assert isinstance(ex, ccxt.coinbaseexchange)


def test_requests_trust_env_is_configurable(tmp_path):
    assert load_config().data.requests_trust_env is True
    assert public_exchange("coinbaseexchange").session.trust_env is True
    p = tmp_path / "c.yaml"
    p.write_text("data:\n  requests_trust_env: false\n")
    cfg = load_config(p)
    assert cfg.data.requests_trust_env is False
    ex = public_exchange(cfg.data.exchange, cfg.data.requests_trust_env)
    assert ex.session.trust_env is False
