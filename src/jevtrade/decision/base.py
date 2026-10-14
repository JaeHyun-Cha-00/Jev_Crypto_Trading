"""DecisionModel interface and the question format.

A model answers questions about a MarketState with a probability
distribution over each question's options. The questions Jev trades with,
and the account and position it is shown, are in jevtrade.decision.agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..state.builder import MarketState


class QuestionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly

    id: str            # used by code only
    instructions: str  # the full meaning of the question
    options: dict[str, str]  # option id -> criterion text
    # choice: one of `options`. noul: yes/no, options must be exactly true/false.
    type: Literal["choice", "noul"] = "choice"

    @model_validator(mode="after")
    def _at_least_two(self) -> "QuestionSpec":
        if len(self.options) < 2:
            raise ValueError(f"question {self.id} needs at least two options")
        if self.type == "noul" and set(self.options) != {"true", "false"}:
            raise ValueError(f"noul question {self.id} needs exactly 'true' and 'false' options")
        return self


class JevConfig(BaseModel):
    """JevModel settings. The API key is read from the environment, never from YAML."""

    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly
    base_url: str = "https://openrouter.ai/api/v1"
    # A dated snapshot, so answers (and tuned thresholds) cannot drift silently.
    model: str = "typesafe/jev-1.13-20260917"
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_s: float = 30.0
    max_retries: int = Field(2, ge=0)
    # USD per input token; output tokens are free. Used for the cost estimate.
    price_per_input_token_usd: float = 0.042 / 1_000_000

    @field_validator("model")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if any(alias in v for alias in ("latest", "preview", "router")) or v.startswith("~"):
            raise ValueError(f"decision.jev.model must be a pinned version, not an alias: {v!r}")
        return v


class DecisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelled key fails loudly

    # How each call is scored once it is old enough (the collector's outcomes):
    # the close `horizon_bars` later, labelled up / flat / down at `flat_band_pct`,
    # and whether the price dipped more than `adverse_move_pct` on the way.
    horizon_bars: int = Field(24, ge=1)   # 24 bars = 24 hours on 1h candles
    flat_band_pct: float = 3.0
    adverse_move_pct: float = 8.0
    jev: JevConfig = Field(default_factory=JevConfig)


@dataclass
class Decision:
    model: str
    model_version: str
    input_hash: str
    probs: dict[str, dict[str, float]]  # question id -> option -> probability
    abstain: bool = False
    abstain_reason: str | None = None
    raw_output: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)

    def p(self, question: str, option: str) -> float:
        return self.probs.get(question, {}).get(option, 0.0)


class DecisionModel(Protocol):
    name: str
    version: str

    def decide(self, state: MarketState, questions: list[QuestionSpec]) -> Decision: ...
