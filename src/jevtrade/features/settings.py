"""Feature settings, apart from compute.py so that loading the config doesn't import pandas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeatureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly

    return_windows: list[int] = Field(default_factory=lambda: [1, 4, 24, 72])
    vol_windows: list[int] = Field(default_factory=lambda: [24, 72])
    volume_z_window: int = 24
    rsi_period: int = 14
    ma_windows: list[int] = Field(default_factory=lambda: [20, 50, 200])
