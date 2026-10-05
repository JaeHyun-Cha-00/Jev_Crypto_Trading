"""Typed configuration loaded from YAML.

Secrets never live in YAML: anything sensitive is read from environment
variables at the point of use. Each stage adds its own section here.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .api.settings import ApiConfig
from .data.market import MarketConfig
from .forward.settings import ForwardLogConfig, JevPaperConfig
from .backtest.engine import BacktestConfig
from .decision.base import DecisionConfig
from .features.compute import FeatureConfig
from .paper.engine import PaperConfig
from .policy.engine import PolicyConfig
from .report.daily import ReportConfig
from .state.builder import StateConfig

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def default_config_path() -> Path:
    """$JEVTRADE_CONFIG, else the repo's config/default.yaml: next to the source in a
    checkout or editable install, else under the working directory (a regular
    `pip install .` puts this file in site-packages, as in the Docker images)."""
    env = os.environ.get("JEVTRADE_CONFIG")
    if env:
        return Path(env)
    cwd = Path.cwd() / "config" / "default.yaml"
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() or not cwd.exists() else cwd


class DataConfig(BaseModel):
    # ccxt exchange id. coinbaseexchange is api.exchange.coinbase.com, which
    # pages back through full hourly history; kraken serves only ~30 days.
    exchange: str = "coinbaseexchange"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    # Track every Robinhood-tradable, non-stablecoin coin with an online Coinbase
    # USD market instead, read at the start of each run (jevtrade.data.live);
    # `symbols` is then the fallback and sets the order of the coins it names.
    symbols_live: bool = False
    exclude: list[str] = Field(default_factory=list)   # never tracked, even when listed live
    timeframe: str = "1h"
    # Earliest candle to keep (ISO-8601, UTC). Deep-history exchanges backfill to it.
    start: str = "2025-01-01T00:00:00Z"
    # Max candles per request; clamped to the exchange cap (coinbase 300, kraken 720).
    page_limit: int = 300
    # Honour HTTPS_PROXY / REQUESTS_CA_BUNDLE env vars (ccxt ignores them by default).
    requests_trust_env: bool = True

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
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    forward_log: ForwardLogConfig = Field(default_factory=ForwardLogConfig)
    jev_paper: JevPaperConfig = Field(default_factory=JevPaperConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)

    @model_validator(mode="after")
    def _holding_follows_horizon(self) -> "AppConfig":
        # Unless set explicitly, positions are held no longer than the horizon
        # the direction question asks about, so the two stay in sync.
        if "max_holding_bars" not in self.policy.model_fields_set:
            self.policy.max_holding_bars = self.decision.horizon_bars
        return self


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else default_config_path()
    if not p.exists():
        if path or os.environ.get("JEVTRADE_CONFIG"):
            raise FileNotFoundError(f"config file not found: {p}")
        warnings.warn(f"no config at {p}; using built-in defaults (BTC/ETH only)", stacklevel=2)
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return AppConfig.model_validate(raw or {})
