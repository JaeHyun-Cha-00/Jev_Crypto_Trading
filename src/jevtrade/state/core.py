"""The state's settings and result type, apart from builder.py so that the API
server (which reads the config and the prompt, but never builds a state) doesn't
import pandas and numpy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

# Rough token estimate (≈4 chars/token for JSON-ish English). Good enough for
# budgeting; the provider's reported usage is logged when available.
CHARS_PER_TOKEN = 4
HARD_CONTEXT_LIMIT_TOKENS = 32_000


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly

    history_bars: int = 24          # recent bars listed individually
    percentile_window: int = 720    # bars used for percentile ranks (~30d at 1h)
    target_max_tokens: int = 2_000


@dataclass(frozen=True)
class MarketState:
    text: str
    features: dict[str, float]      # numeric snapshot for non-LLM models
    est_tokens: int

    @property
    def input_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)
