from __future__ import annotations

from .base import DecisionConfig, DecisionModel
from .baseline import BaselineModel
from .mock import MockModel


def build_model(cfg: DecisionConfig) -> DecisionModel:
    if cfg.model == "mock":
        return MockModel(abstain_rate=cfg.mock_abstain_rate)
    if cfg.model == "baseline":
        return BaselineModel(cfg.baseline_fast, cfg.baseline_slow)
    if cfg.model == "jev":
        # Deliberately unimplemented until the TypeSafe API docs can be read;
        # the request/response schema must not be guessed.
        raise NotImplementedError("JevModel is not implemented yet (pending TypeSafe API docs)")
    raise ValueError(f"unknown decision model: {cfg.model!r}")
