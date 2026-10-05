"""Deterministic offline models for tests and pipeline runs."""

from __future__ import annotations

import hashlib
import json
import time

from ..state.builder import MarketState
from .base import Decision, QuestionSpec


def _unit_floats(seed: str, n: int) -> list[float]:
    digest = hashlib.sha256(seed.encode()).digest()
    return [int.from_bytes(digest[4 * i: 4 * i + 4], "big") / 2**32 for i in range(n)]


class MockModel:
    """Same input → same output. Probabilities derive from a hash of the state.

    Set `fixed` to return the given probabilities for every call (useful for
    policy tests), or `abstain_rate` to make a share of calls abstain.
    """

    name = "mock"

    def __init__(
        self,
        abstain_rate: float = 0.0,
        fixed: dict[str, dict[str, float]] | None = None,
        version: str = "mock-1",
    ):
        self.abstain_rate = abstain_rate
        self.fixed = fixed
        self.version = version

    def decide(self, state: MarketState, questions: list[QuestionSpec]) -> Decision:
        t0 = time.perf_counter()
        h = state.input_hash
        if self.fixed is not None:
            probs = self.fixed
        else:
            probs = {}
            for q in questions:
                opts = list(q.options)
                w = [u + 0.05 for u in _unit_floats(f"{h}:{q.id}", len(opts))]
                s = sum(w)
                probs[q.id] = {o: wi / s for o, wi in zip(opts, w)}
        abstain = _unit_floats(f"{h}:abstain", 1)[0] < self.abstain_rate
        return Decision(
            model=self.name,
            model_version=self.version,
            input_hash=h,
            probs={} if abstain else probs,
            abstain=abstain,
            abstain_reason="mock abstain" if abstain else None,
            raw_output=json.dumps({"probs": probs, "abstain": abstain}),
            latency_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=state.est_tokens,
            cost_usd=0.0,
        )
