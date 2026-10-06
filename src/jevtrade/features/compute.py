"""Causal features from closed candles.

Every operation here is a backward-looking rolling window, shift by a
positive lag, or adjust=False EWM. The row at time t therefore depends only
on candles at or before t. `tests/test_features.py` enforces this by
appending and perturbing future data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


class FeatureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly

    return_windows: list[int] = Field(default_factory=lambda: [1, 4, 24, 72])
    vol_windows: list[int] = Field(default_factory=lambda: [24, 72])
    volume_z_window: int = 24
    rsi_period: int = 14
    ma_windows: list[int] = Field(default_factory=lambda: [20, 50, 200])


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI (0–100) using recursive, causal smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # all-gain windows: avg_loss == 0 → RSI 100
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def compute_features(candles: pd.DataFrame, cfg: FeatureConfig | None = None) -> pd.DataFrame:
    """Return a feature frame aligned to `candles.index`.

    Rows without enough history contain NaN. Callers should treat those
    rows as "not ready" rather than filling them.
    """
    cfg = cfg or FeatureConfig()
    close = candles["close"]
    logret = np.log(close).diff()
    out: dict[str, pd.Series] = {}

    for w in cfg.return_windows:
        out[f"ret_{w}"] = np.log(close / close.shift(w))

    for w in cfg.vol_windows:
        out[f"rvol_{w}"] = logret.rolling(w, min_periods=w).std()

    logv = np.log(candles["volume"].where(candles["volume"] > 0))
    w = cfg.volume_z_window
    mu = logv.rolling(w, min_periods=w).mean()
    sd = logv.rolling(w, min_periods=w).std()
    out[f"volume_z_{w}"] = (logv - mu) / sd.replace(0, np.nan)

    out[f"rsi_{cfg.rsi_period}"] = rsi(close, cfg.rsi_period)

    for w in cfg.ma_windows:
        ma = close.rolling(w, min_periods=w).mean()
        out[f"dist_ma_{w}"] = close / ma - 1

    return pd.DataFrame(out, index=candles.index)

