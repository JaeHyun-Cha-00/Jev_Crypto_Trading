import json
import re

import pandas as pd
import pytest

from jevtrade.features.compute import compute_features
from jevtrade.state.builder import StateConfig, build_state

from conftest import H, synthetic_candles


def _df(n=400, scale=1.0, vol_scale=1.0, shift_ms=0, seed=0):
    rows = synthetic_candles(n, seed=seed)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df[["open", "high", "low", "close"]] *= scale
    df["volume"] *= vol_scale
    df.index = pd.to_datetime(df.pop("ts") + shift_ms, unit="ms", utc=True)
    return df


def _state(df, cfg=None):
    return build_state(df, compute_features(df), horizon_bars=24, bar_label="1h", cfg=cfg)


def test_state_invariant_to_price_level_volume_level_and_time():
    """No absolute prices, volumes or dates can leak: rescaling or shifting
    the series in time must yield byte-identical state text."""
    base = _state(_df())
    assert base is not None
    assert _state(_df(scale=437.123)).text == base.text
    assert _state(_df(vol_scale=1e6)).text == base.text
    assert _state(_df(shift_ms=1000 * H)).text == base.text


def test_state_has_no_tickers_or_dates():
    text = _state(_df(scale=43123.57)).text
    assert not re.search(r"BTC|ETH|USDT|USD\b", text)
    assert not re.search(r"\b(19|20)\d{2}-\d{2}", text)
    assert "43123" not in text
    json.loads(text)  # valid JSON


def test_state_within_token_budget():
    s = _state(_df())
    assert s.est_tokens < 2000


def test_state_truncates_history_to_meet_budget():
    big = _state(_df(), StateConfig(history_bars=150, target_max_tokens=10**6))
    small = _state(_df(), StateConfig(history_bars=150, target_max_tokens=2000))
    assert big.est_tokens > 2000 and small.est_tokens <= 2000


def test_state_not_ready_returns_none():
    assert _state(_df(n=150)) is None


def test_state_uses_only_past_data():
    full = _df(400)
    cut = full.iloc[:300]
    a = build_state(cut, compute_features(cut), 24, "1h")
    b_feats = compute_features(full).iloc[:300]
    b = build_state(full.iloc[:300], b_feats, 24, "1h")
    assert a.text == b.text
