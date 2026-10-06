import pytest

from jevtrade.decision.base import QuestionSpec, default_questions

QS = default_questions(24, 0.5)


def test_question_needs_two_options():
    with pytest.raises(ValueError):
        QuestionSpec(id="q", instructions="?", options={"only": "x"})


def test_noul_question_needs_true_false():
    with pytest.raises(ValueError):
        QuestionSpec(id="q", type="noul", instructions="?", options={"yes": "a", "no": "b"})


def test_default_questions_are_the_four_from_jev_prompt():
    assert [q.id for q in QS] == ["direction", "regime", "adverse_move", "clear_signal"]
    assert [q.type for q in QS] == ["choice", "choice", "noul", "noul"]
