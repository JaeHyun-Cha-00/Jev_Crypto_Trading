from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    """Missing candles strictly between two stored candles."""

    after_ts: int   # last present candle before the gap
    before_ts: int  # first present candle after the gap
    missing: int


def detect_gaps(timestamps: list[int], tf_ms: int) -> list[Gap]:
    """Find holes in a sorted list of candle open times."""
    gaps: list[Gap] = []
    for prev, cur in zip(timestamps, timestamps[1:]):
        step = cur - prev
        if step > tf_ms:
            gaps.append(Gap(after_ts=prev, before_ts=cur, missing=step // tf_ms - 1))
    return gaps
