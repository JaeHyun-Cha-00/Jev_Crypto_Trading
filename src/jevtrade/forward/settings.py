"""Settings for reading the forward log and replaying Jev's paper account."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ForwardLogConfig(BaseModel):
    """Where the API reads the hourly collector's JSONL (the `data-log` branch)."""

    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly
    source: Literal["github", "local", "off"] = "github"
    repo: str = "JaeHyun-Cha-00/jev_crypto_trading"   # owner/name on github.com
    branch: str = "data-log"
    local_dir: str = "data-log"     # for source: local, e.g. a `git worktree` of the branch
    refresh_seconds: int = Field(300, ge=10)
    # Newest day files to load. The account is replayed from forward_log.start,
    # so this must reach back that far.
    max_days: int = Field(400, ge=1)
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
    """Jev's paper account (jevtrade.forward.agent): buy sizes and Coinbase Advanced costs.

    Each side pays `fee_bps` plus a spread from the mid: `spread_bps`, plus up
    to `thin_extra_bps` for coins with little Coinbase volume (in full at 100x
    below `ref_volume_usd` of hourly dollar volume, the median of the coin's
    last 24 logged hours, scaled on a log scale in between).
    """

    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly
    initial_equity: float = Field(10_000, gt=0)
    fee_bps: float = Field(60.0, ge=0, lt=10_000)       # per side, on notional (taker fee)
    slippage_bps: float = Field(0.0, ge=0, lt=10_000)   # beyond the spread
    spread_bps: float = Field(2.0, ge=0, lt=10_000)     # per side, deep markets
    thin_extra_bps: float = Field(30.0, ge=0, lt=10_000)
    ref_volume_usd: float = Field(5_000_000, gt=0)
    volume_bars: int = Field(24, ge=1)
    # The share of equity each of Jev's buy sizes spends (jevtrade.decision.agent);
    # a buy is cut to the cash left.
    sizes: dict[str, float] = Field(default_factory=lambda: {"small": 0.05, "normal": 0.10, "large": 0.20})
    min_order_usd: float = Field(1.0, ge=0)   # a buy smaller than this (cash all but gone) is skipped

    @field_validator("sizes")
    @classmethod
    def _three_sizes(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {"small", "normal", "large"} or not all(0 < f <= 1 for f in v.values()):
            raise ValueError("sizes needs small, normal and large, each a share of equity in (0, 1]")
        return v

    def typical_side_cost(self) -> float:
        """One fill's cost as a fraction, at a mid-depth coin's spread: what Jev's mandate quotes."""
        return round((self.fee_bps + self.slippage_bps + self.spread_bps + self.thin_extra_bps / 2) / 10_000, 4)

    def spread_for(self, dollar_volume: float | None) -> float:
        """Per-side spread in bps for a coin trading `dollar_volume` an hour; unknown pays the most."""
        if dollar_volume is None or dollar_volume <= 0:
            return self.spread_bps + self.thin_extra_bps
        thin = min(max(math.log10(self.ref_volume_usd / dollar_volume) / 2, 0.0), 1.0)
        return self.spread_bps + self.thin_extra_bps * thin
