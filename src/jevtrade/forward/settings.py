"""Settings for reading the forward log and replaying Jev's paper account."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _utc_ms(ts: datetime | None) -> int | None:
    """A config time as UTC milliseconds; a time without a zone is read as UTC."""
    if ts is None:
        return None
    return int((ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp() * 1000)


class ForwardLogConfig(BaseModel):
    """Where the API reads the hourly collector's JSONL (the `data-log` branch)."""

    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly
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
        return _utc_ms(self.start)


class JevPaperConfig(BaseModel):
    """Costs for the dashboard's Jev portfolio (jevtrade.forward.portfolio), Coinbase Advanced by default.

    Each side pays `fee_bps` plus a spread from the mid: `spread_bps`, plus up
    to `thin_extra_bps` for coins with little Coinbase volume (in full at 100x
    below `ref_volume_usd` of hourly dollar volume, the median of the coin's
    last 24 logged hours, scaled on a log scale in between).
    """

    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly
    # The portfolio's own fresh start (UTC candle open): it replays only lines
    # from here on, at its full balance, while the accuracy stats keep the whole
    # log from forward_log.start. None: start with the log.
    start: datetime | None = None
    initial_equity: float = Field(10_000, gt=0)
    fee_bps: float = Field(60.0, ge=0, lt=10_000)       # per side, on notional (taker fee)
    slippage_bps: float = Field(0.0, ge=0, lt=10_000)   # beyond the spread
    spread_bps: float = Field(2.0, ge=0, lt=10_000)     # per side, deep markets
    thin_extra_bps: float = Field(30.0, ge=0, lt=10_000)
    ref_volume_usd: float = Field(5_000_000, gt=0)
    volume_bars: int = Field(24, ge=1)
    # A buy never exceeds this share of the coin's median hourly dollar volume;
    # null = no cap. Hours with no volume logged are not capped.
    max_volume_frac: float | None = Field(0.01, gt=0, le=1)
    # Skill gate: buy only while Jev's recent buy signals would have paid. A
    # signal is any answer that met the policy's buy thresholds, bought or not;
    # it resolves decision.horizon_bars later. New buys are allowed only while
    # the signals of the last `gate_lookback_hours` that have resolved returned
    # more than their round-trip cost on average, and at least
    # `gate_min_signals` of them have. null = no gate.
    gate_lookback_hours: int | None = Field(None, ge=1)
    gate_min_signals: int = Field(30, ge=1)

    def start_ms(self) -> int | None:
        return _utc_ms(self.start)

    def spread_for(self, dollar_volume: float | None) -> float:
        """Per-side spread in bps for a coin trading `dollar_volume` an hour; unknown pays the most."""
        if dollar_volume is None or dollar_volume <= 0:
            return self.spread_bps + self.thin_extra_bps
        thin = min(max(math.log10(self.ref_volume_usd / dollar_volume) / 2, 0.0), 1.0)
        return self.spread_bps + self.thin_extra_bps * thin
