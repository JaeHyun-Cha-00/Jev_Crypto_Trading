import numpy as np
import pandas as pd
import pytest

from jevtrade.features.compute import FeatureConfig, compute_features, max_lookback, rsi

from conftest import synthetic_candles


def _df(n: int, seed: int = 0) -> pd.DataFrame:
    rows = synthetic_candles(n, seed=seed)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df


@pytest.mark.parametrize("cut", [210, 260, 399])
def test_features_unchanged_when_future_appended(cut):
    """Look-ahead guard: features at t must not change when data after t is added."""
    full = _df(400)
    f_full = compute_features(full)
    f_cut = compute_features(full.iloc[:cut])
    pd.testing.assert_frame_equal(f_cut, f_full.iloc[:cut], check_exact=False, rtol=1e-12)


def test_features_unchanged_when_future_perturbed():
    """Stronger guard: wildly different future data must not leak backwards."""
    a = _df(400)
    b = a.copy()
    b.iloc[300:] = b.iloc[300:] * np.linspace(0.2, 5.0, 100)[:, None]
    fa, fb = compute_features(a), compute_features(b)
    pd.testing.assert_frame_equal(fa.iloc[:300], fb.iloc[:300], check_exact=False, rtol=1e-12)
    assert not np.allclose(fa.iloc[300:].dropna(), fb.iloc[300:].dropna())


def test_all_features_defined_after_lookback():
    f = compute_features(_df(400))
    lb = max_lookback()
    assert f.iloc[lb:].notna().all().all()
    assert f.iloc[: lb - 1].isna().any(axis=1).all()


def test_expected_columns():
    cols = set(compute_features(_df(300)).columns)
    assert {"ret_1", "ret_24", "rvol_24", "volume_z_24", "rsi_14", "dist_ma_200"} <= cols


def test_return_values():
    df = _df(50)
    f = compute_features(df, FeatureConfig(return_windows=[1], vol_windows=[2], ma_windows=[3]))
    assert f["ret_1"].iloc[10] == pytest.approx(np.log(df.close.iloc[10] / df.close.iloc[9]))


def test_rsi_bounds_and_monotone_series():
    up = pd.Series(np.arange(1.0, 60.0))
    assert rsi(up, 14).dropna().eq(100).all()
    r = rsi(pd.Series(100 * np.exp(np.random.default_rng(1).normal(0, 0.01, 300).cumsum())), 14)
    assert r.dropna().between(0, 100).all()
