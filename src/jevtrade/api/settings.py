"""API settings, importable without FastAPI installed."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])  # vite dev server
