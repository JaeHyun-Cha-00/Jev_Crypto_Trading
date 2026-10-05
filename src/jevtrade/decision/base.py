"""DecisionModel interface shared by Jev, Mock and Baseline models.

Models answer a fixed set of questions about a MarketState with a
probability distribution over each question's options. Models never see
portfolio state and never return actions; the policy layer owns that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from ..state.builder import MarketState


class QuestionSpec(BaseModel):
    id: str            # used by code only
    instructions: str  # the full meaning of the question
    options: dict[str, str]  # option id -> criterion text

    @model_validator(mode="after")
    def _at_least_two(self) -> "QuestionSpec":
        if len(self.options) < 2:
            raise ValueError(f"question {self.id} needs at least two options")
        return self


def default_questions(horizon_bars: int, flat_band_pct: float) -> list[QuestionSpec]:
    return [
        QuestionSpec(
            id="direction",
            instructions=(
                f"Given the market state, where will this asset's close price be {horizon_bars} "
                "bars after the most recent closed bar, relative to that bar's close?"
            ),
            options={
                "up": f"Higher by more than {flat_band_pct}%.",
                "flat": f"Within ±{flat_band_pct}%.",
                "down": f"Lower by more than {flat_band_pct}%.",
            },
        )
    ]


class DecisionConfig(BaseModel):
    model: str = "mock"  # mock | baseline | jev
    horizon_bars: int = 24
    flat_band_pct: float = 0.5
    questions: list[QuestionSpec] | None = None
    mock_abstain_rate: float = Field(0.1, ge=0, le=1)
    baseline_fast: int = 20
    baseline_slow: int = 50

    def resolved_questions(self) -> list[QuestionSpec]:
        return self.questions or default_questions(self.horizon_bars, self.flat_band_pct)


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
