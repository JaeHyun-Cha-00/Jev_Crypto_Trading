"""Guardrails: v1 is paper-only. No order placement, no exchange credentials."""

import re
from pathlib import Path

import pytest

from jevtrade.data.fetcher import public_exchange

SRC = Path(__file__).resolve().parents[1] / "src"

FORBIDDEN = [
    r"create_order", r"createOrder", r"create_\w*_order", r"cancel_order", r"cancelOrder",
    r"edit_order", r"withdraw", r"fetch_balance", r"fetchBalance", r"\bapiKey\s*=", r"private_\w+\(",
]


def test_no_order_placement_code():
    hits = []
    for f in SRC.rglob("*.py"):
        text = f.read_text()
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text):
                hits.append(f"{f.relative_to(SRC)}: {m.group(0)}")
    assert not hits, f"forbidden trading/auth calls found: {hits}"


def test_public_exchange_has_no_credentials():
    ex = public_exchange("coinbase")
    assert not ex.apiKey and not ex.secret


def test_unknown_exchange_rejected():
    with pytest.raises(ValueError):
        public_exchange("not_an_exchange")
