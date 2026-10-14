import pytest

from jevtrade.decision.base import QuestionSpec


def test_question_needs_two_options():
    with pytest.raises(ValueError):
        QuestionSpec(id="q", instructions="?", options={"only": "x"})


def test_noul_question_needs_true_false():
    with pytest.raises(ValueError):
        QuestionSpec(id="q", type="noul", instructions="?", options={"yes": "a", "no": "b"})
