"""Turn features into a compact, anonymized market-state text.

The state contains no dates, prices or ticker names. Every value is either
relative (a return or a distance from a moving average) or normalized (a
z-score, RSI, or a percentile within the trailing window), and is rounded so
that the exact series is harder to match against memorized history. The
output is a JSON object, because TypeSafe's guidance prefers named fields
when state has several parts.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..features.settings import FeatureConfig
from .core import HARD_CONTEXT_LIMIT_TOKENS, MarketState, StateConfig, estimate_tokens

__all__ = ["MarketState", "StateConfig", "build_state", "estimate_tokens", "state_window"]


def state_window(state_cfg: StateConfig, feature_cfg: FeatureConfig) -> int:
    """Trailing rows build_state needs to give the same text as full history."""
    return state_cfg.percentile_window + state_cfg.history_bars + feature_cfg.volume_z_window + 1


def _pct(x: float) -> float:
    return round(float(x) * 100, 2)


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


def _percentile_rank(series: pd.Series, window: int) -> float | None:
    tail = series.dropna().iloc[-window:]
    if len(tail) < 20:
        return None
    return round(float((tail <= tail.iloc[-1]).mean()) * 100)


def build_state(
    candles: pd.DataFrame,
    features: pd.DataFrame,
    horizon_bars: int,
    bar_label: str,
    cfg: StateConfig | None = None,
    feature_cfg: FeatureConfig | None = None,
) -> MarketState | None:
    """Build state for the last row of `features`. Returns None if not ready.

    `candles` and `features` must already be truncated at the decision time.
    This function only reads their last rows. `feature_cfg` must be the
    config `features` was computed with, so per-bar volume_z matches it.
    """
    cfg = cfg or StateConfig()
    feature_cfg = feature_cfg or FeatureConfig()
    last = features.iloc[-1]
    if last.isna().any():
        return None

    close = candles["close"]
    logret = np.log(close).diff()
    lv = np.log(candles["volume"].where(candles["volume"] > 0))
    vz_w = feature_cfg.volume_z_window
    vol_z = (lv - lv.rolling(vz_w, min_periods=vz_w).mean()) / lv.rolling(
        vz_w, min_periods=vz_w
    ).std().replace(0, np.nan)

    hist_n = cfg.history_bars
    recent = []
    for i in range(hist_n, 0, -1):
        row = candles.iloc[-i]
        rng = (row.high - row.low) / row.open
        recent.append({
            "bars_ago": i - 1,
            "ret_pct": _pct(logret.iloc[-i]),
            "range_pct": _pct(rng),
            "volume_z": _r(vol_z.iloc[-i], 1) if pd.notna(vol_z.iloc[-i]) else None,
        })

    current = {
        name: (_r(v) if name.startswith(("rsi", "volume_z")) else _pct(v))
        for name, v in last.items()
    }
    vol_cols = [c for c in features.columns if c.startswith("rvol_")]
    percentiles = {
        f"{c}_pctile": _percentile_rank(features[c], cfg.percentile_window) for c in vol_cols
    }
    percentiles["ret_24_pctile"] = (
        _percentile_rank(features["ret_24"], cfg.percentile_window) if "ret_24" in features else None
    )

    state = {
        "description": (
            f"Anonymized state of one liquid crypto asset on {bar_label} bars, ending at the most "
            "recently closed bar. Returns and distances are percent; volume_z is a z-score of log "
            f"volume against the trailing {vz_w} bars; *_pctile is the percentile rank (0-100) of the "
            f"current value within the trailing {cfg.percentile_window} bars."
        ),
        "horizon_bars": horizon_bars,
        "current": current,
        "percentiles": percentiles,
        "recent_bars_oldest_first": recent,
    }
    text = json.dumps(state, separators=(",", ":"))
    tokens = estimate_tokens(text)
    while tokens > cfg.target_max_tokens and len(state["recent_bars_oldest_first"]) > 4:
        state["recent_bars_oldest_first"] = state["recent_bars_oldest_first"][2:]
        text = json.dumps(state, separators=(",", ":"))
        tokens = estimate_tokens(text)
    if tokens > HARD_CONTEXT_LIMIT_TOKENS:
        raise ValueError(f"state is {tokens} tokens, over the hard {HARD_CONTEXT_LIMIT_TOKENS} limit")

    return MarketState(text=text, features={k: float(v) for k, v in last.items()}, est_tokens=tokens)
