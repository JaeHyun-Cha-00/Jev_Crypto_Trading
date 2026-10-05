"""API settings, importable without FastAPI installed."""

from __future__ import annotations

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
