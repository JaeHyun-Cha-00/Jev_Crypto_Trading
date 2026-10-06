"""Coinbase (api.exchange.coinbase.com), the only exchange: windowed
pagination over deep history."""

import ccxt

from jevtrade.config import load_config
import pytest
from pydantic import ValidationError

from jevtrade.data.fetcher import PAGE_CAP, fetch_range, public_exchange

from conftest import H, T0, WindowedSource, synthetic_candles

EX, SYM, TF = "coinbaseexchange", "BTC/USD", "1h"
YEAR = 366 * 24


def test_default_config_is_coinbase_usd():
    cfg = load_config()
    assert cfg.data.exchange == "coinbaseexchange"
    syms = cfg.data.symbols
    assert syms[:2] == ["BTC/USD", "ETH/USD"]
    assert len(syms) == len(set(syms)) and all(s.endswith("/USD") for s in syms)
    assert not {"USDC/USD", "USDT/USD", "PAXG/USD"} & set(syms)  # no pegged coins
    assert cfg.data.page_limit == PAGE_CAP == 300


@pytest.mark.parametrize("yaml_text", ["data:\n  exchange: kraken\n", "data:\n  page_limit: 720\n"])
def test_only_coinbase_and_its_page_size_are_accepted(tmp_path, yaml_text):
    p = tmp_path / "c.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ValidationError):
        load_config(p)


def test_public_coinbase_client_hits_exchange_api_without_credentials():
    ex = public_exchange("coinbaseexchange")
    assert ex.implode_hostname(ex.urls["api"]["public"]) == "https://api.exchange.coinbase.com"
    assert not ex.apiKey and not ex.secret


def test_backfills_a_year_through_300_candle_windows():
    n = YEAR + 50
    src = WindowedSource(synthetic_candles(n))
    out = fetch_range(src, SYM, TF, T0, T0 + (n - 1) * H, H, 300)
    ts = [c[0] for c in out]
    assert len(ts) == n and ts[0] == T0 and ts[-1] == T0 + (n - 1) * H
    assert src.calls == -(-n // 300)  # one request per window


def test_empty_window_does_not_stop_pagination():
    """A no-trade stretch longer than one window (an outage) must be stepped
    over, never ended on or filled in."""
    n = 2000
    missing = {T0 + i * H for i in range(500, 1200)}  # 700 candles > 300 window
    src = WindowedSource(synthetic_candles(n), missing)
    out = fetch_range(src, SYM, TF, T0, T0 + (n - 1) * H, H, 300)
    assert out[-1][0] == T0 + (n - 1) * H
    assert len(out) == n - 700


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
    out = fetch_range(ex, "BTC/USD", "1h", T0, T0 + 699 * H, H, 300)

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


def test_a_misspelled_config_key_fails_loudly(tmp_path):
    p = tmp_path / "typo.yaml"
    p.write_text("policy:\n  max_gross_exposre: 1.0\n")
    with pytest.raises(ValidationError, match="max_gross_exposre"):
        load_config(p)
