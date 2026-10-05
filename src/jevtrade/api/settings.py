"""API settings, importable without FastAPI installed."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])  # vite dev server


class ForwardLogConfig(BaseModel):
    """Where the API reads the hourly collector's JSONL (the `data-log` branch)."""

    source: Literal["github", "local", "off"] = "github"
    repo: str = "JaeHyun-Cha-00/jev_crypto_trading"   # owner/name on github.com
    branch: str = "data-log"
    local_dir: str = "data-log"     # for source: local, e.g. a `git worktree` of the branch
    refresh_seconds: int = Field(300, ge=10)
    max_days: int = Field(30, ge=1)  # newest day files to load
    token_env: str = "GITHUB_TOKEN"  # optional; private repos and higher rate limits
    timeout_s: float = Field(20, gt=0)
    # Fresh-start cutoff (UTC candle open). The collector never asks Jev about an
    # earlier candle, and the API ignores earlier lines (stats and portfolio).
    # None: no cutoff.
    start: datetime | None = None

    def start_ms(self) -> int | None:
        if self.start is None:
            return None
        ts = self.start if self.start.tzinfo else self.start.replace(tzinfo=timezone.utc)
        return int(ts.timestamp() * 1000)


class JevPaperConfig(BaseModel):
    """Costs for the dashboard's Jev portfolio (jevtrade.api.jev_paper), Robinhood-like by default.

    Robinhood charges no commission on crypto; the cost is its quote spread.
    Each side pays `spread_bps` from the mid, plus up to `thin_extra_bps` for
    coins with little Coinbase volume: in full at 100x below
    `ref_volume_usd` of hourly dollar volume (median of the coin's last 24
    logged hours), scaled on a log scale in between.
    """

    initial_equity: float = Field(10_000, gt=0)
    fee_bps: float = Field(0.0, ge=0, lt=10_000)        # per side, on notional
    slippage_bps: float = Field(0.0, ge=0, lt=10_000)   # beyond the quoted spread
    spread_bps: float = Field(95.0, ge=0, lt=10_000)    # per side, deep markets
    thin_extra_bps: float = Field(20.0, ge=0, lt=10_000)
    ref_volume_usd: float = Field(5_000_000, gt=0)
    volume_bars: int = Field(24, ge=1)
    # A buy never exceeds this share of the coin's median hourly dollar volume;
    # null = no cap. Hours with no volume logged are not capped.
    max_volume_frac: float | None = Field(0.01, gt=0, le=1)

    def spread_for(self, dollar_volume: float | None) -> float:
        """Per-side spread in bps for a coin trading `dollar_volume` an hour; unknown pays the most."""
        if dollar_volume is None or dollar_volume <= 0:
            return self.spread_bps + self.thin_extra_bps
        thin = min(max(math.log10(self.ref_volume_usd / dollar_volume) / 2, 0.0), 1.0)
        return self.spread_bps + self.thin_extra_bps * thin


class MarketConfig(BaseModel):
    """Live prices for the dashboard's coin list (jevtrade.api.market): public Coinbase data only."""

    enabled: bool = True
    base_url: str = "https://api.exchange.coinbase.com"
    stats_seconds: float = Field(5, ge=1)        # re-read 24-hour stats for all coins at most this often
    spark_seconds: float = Field(300, ge=30)     # refresh the 24-hour sparklines this often
    book_seconds: float = Field(2, ge=0.5)       # order book and trades cache
    min_interval_s: float = Field(0.15, ge=0)    # spacing between Coinbase calls (public limit: 10/s)
    timeout_s: float = Field(10, gt=0)
