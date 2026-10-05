import pytest

from jevtrade.decision.base import Decision, DecisionConfig, QuestionSpec, default_questions
from jevtrade.decision.baseline import BaselineModel
from jevtrade.decision.factory import build_model
from jevtrade.decision.log import DecisionLog
from jevtrade.decision.mock import MockModel
from jevtrade.data.store import connect
from jevtrade.state.builder import MarketState

QS = default_questions(24, 0.5)


def _state(text="{}", **feats):
    return MarketState(text=text, features=feats, est_tokens=len(text) // 4)


def test_mock_is_deterministic_and_normalized():
    m = MockModel()
    a, b = m.decide(_state("x"), QS), m.decide(_state("x"), QS)
    assert a.probs == b.probs and a.input_hash == b.input_hash
    assert sum(a.probs["direction"].values()) == pytest.approx(1.0)
    assert m.decide(_state("y"), QS).probs != a.probs


def test_mock_abstain_rate():
    m = MockModel(abstain_rate=0.3)
    ds = [m.decide(_state(str(i)), QS) for i in range(500)]
    rate = sum(d.abstain for d in ds) / len(ds)
    assert 0.2 < rate < 0.4
    assert all(d.probs == {} for d in ds if d.abstain)


def test_mock_fixed():
    fixed = {"direction": {"up": 0.7, "flat": 0.2, "down": 0.1}}
    d = MockModel(fixed=fixed).decide(_state(), QS)
    assert d.p("direction", "up") == 0.7


def test_baseline_crossover():
    m = BaselineModel(20, 50)
    # SMA20 > SMA50  <=>  dist_ma_20 < dist_ma_50
    bull = m.decide(_state(dist_ma_20=0.01, dist_ma_50=0.03), QS)
    bear = m.decide(_state(dist_ma_20=0.03, dist_ma_50=0.01), QS)
    assert bull.p("direction", "up") == 1.0 and bear.p("direction", "down") == 1.0


def test_question_needs_two_options():
    with pytest.raises(ValueError):
        QuestionSpec(id="q", instructions="?", options={"only": "x"})


def test_factory():
    assert build_model(DecisionConfig(model="mock")).name == "mock"
    assert build_model(DecisionConfig(model="baseline")).name == "baseline"
    with pytest.raises(NotImplementedError):
        build_model(DecisionConfig(model="jev"))


def test_decision_log_roundtrip():
    conn = connect(":memory:")
    log = DecisionLog(conn)
    d = Decision("mock", "mock-1", "abc", {"direction": {"up": 1.0}}, latency_ms=3.2, input_tokens=100)
    i = log.record("paper", "BTC/USDT", 123, d, "{}")
    log.annotate(i, True, "enter", "entry: ...")
    row = conn.execute("SELECT model_version, input_hash, latency_ms, input_tokens, passed_threshold,"
                       " policy_action FROM decisions WHERE id=?", (i,)).fetchone()
    assert row == ("mock-1", "abc", 3.2, 100, 1, "enter")
