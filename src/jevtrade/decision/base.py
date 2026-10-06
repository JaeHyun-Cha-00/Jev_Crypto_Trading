"""DecisionModel interface and the questions Jev is asked.

Models answer a fixed set of questions about a MarketState with a
probability distribution over each question's options. Models never see
portfolio state and never return actions; the policy layer owns that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from ..state.builder import MarketState


class QuestionSpec(BaseModel):
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


def _horizon_text(horizon_bars: int, timeframe: str) -> str:
    from ..data.timeframes import timeframe_ms

    minutes = horizon_bars * timeframe_ms(timeframe) // 60_000
    if minutes % 60 == 0:
        hours = minutes // 60
        span = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        span = f"{minutes} minutes"
    return f"{span} ({horizon_bars} bars of {timeframe})"


def default_questions(
    horizon_bars: int,
    flat_band_pct: float,
    adverse_move_pct: float = 3.0,
    timeframe: str = "1h",
) -> list[QuestionSpec]:
    """The four decision questions documented in docs/jev_prompt.md."""
    horizon = _horizon_text(horizon_bars, timeframe)
    band = f"{flat_band_pct:g}%"
    adverse = f"{adverse_move_pct:g}%"
    return [
        QuestionSpec(
            id="direction",
            instructions=(
                f"Given the market state, where will this asset's close price be {horizon} after "
                "the most recent closed bar, relative to that bar's close?"
            ),
            options={
                "up": f"The close is more than {band} above the last close (change above +{band}).",
                "flat": f"The change is between -{band} and +{band} inclusive.",
                "down": f"The close is more than {band} below the last close (change below -{band}).",
            },
        ),
        QuestionSpec(
            id="regime",
            instructions=(
                "Which description best fits this asset's market behaviour over the bars "
                "described in the state?"
            ),
            options={
                "trend_up": "Persistent gains: price above its moving averages, pullbacks shallow.",
                "trend_down": "Persistent losses: price below its moving averages, bounces shallow.",
                "range": "No persistent direction: price near its moving averages, ordinary "
                         "volatility.",
                "volatile": "Large swings in both directions: realized volatility high relative "
                            "to its own history.",
            },
        ),
        QuestionSpec(
            id="adverse_move",
            type="noul",
            instructions=(
                f"Within the next {horizon} after the most recent closed bar, will this "
                f"asset's price at any point trade more than {adverse} below that bar's "
                "close?"
            ),
            options={
                "true": f"Price dips more than {adverse} below the last close at some "
                        "point in the window.",
                "false": f"Price never falls more than {adverse} below the last close "
                         "in the window.",
            },
        ),
        QuestionSpec(
            id="clear_signal",
            type="noul",
            instructions=(
                "Does the state show a clear, coherent directional setup, with returns, "
                "moving-average distances, RSI and volume pointing the same way?"
            ),
            options={
                "true": "The indicators agree on one direction and the move is distinct from "
                        "noise.",
                "false": "The indicators are mixed, contradictory, or too small to distinguish "
                         "from noise.",
            },
        ),
    ]


class JevConfig(BaseModel):
    """JevModel settings. The API key is read from the environment, never from YAML."""

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
    horizon_bars: int = 4       # 4 bars = 4 hours on 1h candles
    flat_band_pct: float = 1.0  # "up" > +1%, "down" < -1%, otherwise "flat"
    adverse_move_pct: float = 3.0
    questions: list[QuestionSpec] | None = None
    jev: JevConfig = Field(default_factory=JevConfig)

    def resolved_questions(self, timeframe: str = "1h") -> list[QuestionSpec]:
        return self.questions or default_questions(
            self.horizon_bars, self.flat_band_pct, self.adverse_move_pct, timeframe
        )


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
