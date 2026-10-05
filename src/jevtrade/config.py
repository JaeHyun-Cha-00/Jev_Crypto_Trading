"""Typed configuration loaded from YAML.

Secrets never live in YAML: anything sensitive is read from environment
variables at the point of use. Each stage adds its own section here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

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


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return AppConfig.model_validate(raw or {})
