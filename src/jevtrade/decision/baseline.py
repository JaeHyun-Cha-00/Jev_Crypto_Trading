"""Moving-average crossover baseline, expressed through the same interface."""

from __future__ import annotations

import json
import time

from ..state.builder import MarketState
from .base import Decision, QuestionSpec


class BaselineModel:
    """Fast SMA above slow SMA → {"up": 1}, otherwise {"down": 1}.

    Uses the `dist_ma_{w}` features (close / SMA_w - 1).
    SMA_fast > SMA_slow  ⇔  dist_ma_fast < dist_ma_slow.
    """

    name = "baseline"

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast, self.slow = fast, slow
        self.version = f"ma-crossover-{fast}-{slow}"

    def decide(self, state: MarketState, questions: list[QuestionSpec]) -> Decision:
        t0 = time.perf_counter()
        f = state.features
        bullish = f[f"dist_ma_{self.fast}"] < f[f"dist_ma_{self.slow}"]
        probs: dict[str, dict[str, float]] = {}
        for q in questions:
            if q.id == "direction":
                probs[q.id] = {o: 0.0 for o in q.options}
                probs[q.id]["up" if bullish else "down"] = 1.0
        return Decision(
            model=self.name,
            model_version=self.version,
            input_hash=state.input_hash,
            probs=probs,
            raw_output=json.dumps({"bullish": bool(bullish)}),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
