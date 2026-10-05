"""Typed configuration loaded from YAML.

Secrets never live in YAML: anything sensitive is read from environment
variables at the point of use. Each stage adds its own section here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .decision.base import DecisionConfig
from .features.compute import FeatureConfig
from .policy.engine import PolicyConfig
from .state.builder import StateConfig

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


class DataConfig(BaseModel):
    exchange: str = "coinbase"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframe: str = "1h"
    # First candle to fetch on an empty store (ISO-8601, UTC).
    start: str = "2024-01-01T00:00:00Z"
    # Max candles per request; exchanges cap this (coinbase: 300).
    page_limit: int = 300

    @field_validator("symbols")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one symbol is required")
        return v


class StorageConfig(BaseModel):
    sqlite_path: str = "data/jevtrade.sqlite"


class AppConfig(BaseModel):
    data: DataConfig = Field(default_factory=DataConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return AppConfig.model_validate(raw or {})
