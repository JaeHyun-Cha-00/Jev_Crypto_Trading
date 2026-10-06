"""JevModel tests. All replay recorded fixtures; none touch the network."""

import json
from pathlib import Path

import pytest

from jevtrade.config import load_config
from jevtrade.decision.base import JevConfig, default_questions
from jevtrade.decision.jev import HttpResponse, JevError, JevModel, build_payload
from jevtrade.policy.engine import AccountView, Policy, PolicyConfig, RiskState
from jevtrade.state.builder import MarketState, estimate_tokens

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "btc_live"
PINNED = "typesafe/jev-1.13-20260917"
QS = default_questions(4, 1.0, 3.0, "1h")


def _state() -> MarketState:
    text = (FIXTURE / "state.json").read_text().strip()
    return MarketState(text=text, features={}, est_tokens=estimate_tokens(text))


def _body() -> str:
    return (FIXTURE / "response.json").read_text().strip()


class Recorder:
    """Transport stub that replays responses in order and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout_s):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _model(*responses, **cfg):
    t = Recorder(*responses)
    sleeps = []
    return JevModel(JevConfig(**cfg), transport=t, sleep=sleeps.append), t, sleeps


def test_payload_matches_system_one_schema():
    p = build_payload(PINNED, _state(), QS)
    assert p["model"] == PINNED
    assert isinstance(p["state"], dict) and p["state"]["horizon_bars"] == 4
    assert set(p["questions"]) == {"direction", "regime", "adverse_move", "clear_signal"}
    assert p["questions"]["direction"]["type"] == "choice"
    assert set(p["questions"]["direction"]["criteria"]) == {"up", "flat", "down"}
    assert p["questions"]["adverse_move"]["type"] == "noul"
    assert set(p["questions"]["adverse_move"]["criteria"]) == {"true", "false"}


def test_recorded_response_is_parsed():
    m, t, _ = _model(HttpResponse(200, _body()))
    d = m.decide(_state(), QS)
    assert t.calls[0]["url"] == "https://openrouter.ai/api/v1/systemone"
    assert not d.abstain
    assert d.model == "jev" and d.model_version == PINNED
    assert d.input_hash == _state().input_hash
    assert d.probs["direction"] == {"up": 0.22, "flat": 0.59, "down": 0.19}
    assert d.p("regime", "trend_up") == 0.98
    assert d.probs["adverse_move"] == pytest.approx({"true": 0.32, "false": 0.68})
    assert d.probs["clear_signal"] == pytest.approx({"true": 0.52, "false": 0.48})
    assert d.input_tokens == 1951
    assert d.cost_usd == pytest.approx(8.1942e-05)
    assert d.extra["estimated_cost_usd"] == pytest.approx(1951 * 0.042e-6)
    assert d.extra["served_model"] == PINNED and d.extra["requested_model"] == PINNED
    assert d.extra["answers"]["direction"]["confidence"] == 0.38
    assert json.loads(d.raw_output)["id"] == "gen-dec-1791169343-sLo1dzWs6ws8qa3Ica4W"


def test_call_is_logged_with_version_latency_tokens_cost(caplog):
    m, _, _ = _model(HttpResponse(200, _body()))
    with caplog.at_level("INFO", logger="jevtrade.decision.jev"):
        m.decide(_state(), QS)
    line = caplog.records[-1].getMessage()
    for part in (f"model={PINNED}", f"served={PINNED}", "latency_ms=", "input_tokens=1951",
                 "est_cost_usd=0.00008194"):
        assert part in line


def test_cost_falls_back_to_estimate_without_reported_cost():
    body = json.loads(_body())
    del body["usage"]["cost"]
    m, _, _ = _model(HttpResponse(200, json.dumps(body)))
    d = m.decide(_state(), QS)
    assert d.cost_usd == pytest.approx(1951 * 0.042e-6)


def test_served_version_drift_abstains():
    body = json.loads(_body())
    body["model"] = "typesafe/jev-1.14-20261101"
    m, _, _ = _model(HttpResponse(200, json.dumps(body)))
    d = m.decide(_state(), QS)
    assert d.abstain and "differs from pinned" in d.abstain_reason
    assert d.probs == {} and d.input_tokens == 1951


def test_missing_answer_abstains():
    body = json.loads(_body())
    del body["answers"]["clear_signal"]
    m, _, _ = _model(HttpResponse(200, json.dumps(body)))
    d = m.decide(_state(), QS)
    assert d.abstain and d.abstain_reason == "missing answers: clear_signal"


def test_malformed_body_abstains():
    m, _, _ = _model(HttpResponse(200, "<html>oops</html>"))
    d = m.decide(_state(), QS)
    assert d.abstain and d.abstain_reason.startswith("malformed response")


def test_rate_limit_retries_honoring_retry_after():
    m, t, sleeps = _model(HttpResponse(429, '{"error":{"code":429}}', retry_after=1.5),
                          HttpResponse(200, _body()))
    d = m.decide(_state(), QS)
    assert not d.abstain and len(t.calls) == 2 and sleeps == [1.5]
    assert d.extra["attempts"] == 2


def test_persistent_server_errors_abstain_after_retries():
    m, t, sleeps = _model(*[HttpResponse(502, '{"error":{"code":502}}')] * 3, max_retries=2)
    d = m.decide(_state(), QS)
    assert d.abstain and d.abstain_reason == "request failed: HTTP 502"
    assert len(t.calls) == 3 and sleeps == [1.0, 2.0]


def test_network_error_abstains():
    m, _, _ = _model(TimeoutError("timed out"), max_retries=0)
    d = m.decide(_state(), QS)
    assert d.abstain and "TimeoutError" in d.abstain_reason


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_config_and_auth_errors_raise(status):
    m, t, _ = _model(HttpResponse(status, '{"error":{"code":%d}}' % status))
    with pytest.raises(JevError, match=f"HTTP {status}"):
        m.decide(_state(), QS)
    assert len(t.calls) == 1


def test_api_key_only_from_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    m, t, _ = _model(HttpResponse(200, _body()), HttpResponse(200, _body()))
    m.decide(_state(), QS)
    assert "Authorization" not in t.calls[0]["headers"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    m.decide(_state(), QS)
    assert t.calls[1]["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize("alias", ["typesafe/jev-latest", "~typesafe/jev-latest",
                                   "jev-preview", "typesafe/jev-router"])
def test_config_rejects_aliases(alias):
    with pytest.raises(ValueError, match="pinned version"):
        JevConfig(model=alias)


def test_default_config_pins_a_dated_snapshot():
    jev = load_config().decision.jev
    assert jev.model == PINNED
    assert jev.price_per_input_token_usd == pytest.approx(0.042e-6)


def test_direction_question_is_generated_from_config():
    q = load_config().decision.resolved_questions("1h")[0]
    assert q.id == "direction"
    assert "24 hours (24 bars of 1h)" in q.instructions
    assert "more than 3% above" in q.options["up"] and "+3%" in q.options["up"]
    assert "between -3% and +3%" in q.options["flat"]
    assert "more than 3% below" in q.options["down"] and "-3%" in q.options["down"]
    q2 = default_questions(6, 0.25, 2.0, "15m")[0]
    assert "90 minutes (6 bars of 15m)" in q2.instructions and "+0.25%" in q2.options["up"]


def _with_nouls(adverse: float, clear: float) -> str:
    body = json.loads(_body())
    body["answers"]["adverse_move"]["noul"] = adverse
    body["answers"]["clear_signal"]["noul"] = clear
    return json.dumps(body)


def test_noul_is_the_yes_probability_with_derived_gate_confidence():
    # docs.typesafe.ai/primitives/noul: `noul` is P(yes); a Noul has no separate
    # confidence, and |2p - 1| is the documented confidence-style gate.
    m, _, _ = _model(HttpResponse(200, _body()))
    a = m.decide(_state(), QS).extra["answers"]["adverse_move"]
    assert a["noul"] == 0.32 and a["probabilities"]["true"] == 0.32
    assert a["choice"] == "false" and a["confidence"] is None
    assert a["gate_confidence"] == pytest.approx(0.36)


def test_policy_reads_direction_probabilities_not_noul_values():
    acct = AccountView(equity=10_000.0, positions={}, marks={},
                       risk=RiskState(day_start_equity=10_000.0, day=0, cooldown_until_ts=0))
    pol = Policy(PolicyConfig(), 3_600_000)
    actions = []
    for adverse, clear in [(0.0, 0.0), (1.0, 1.0), (0.99, 0.01)]:
        m, _, _ = _model(HttpResponse(200, _with_nouls(adverse, clear)))
        d = m.decide(_state(), QS)
        a = pol.evaluate("BTC/USDT", 0, 100.0, d, acct)
        assert a.details == {"p_up": 0.22, "p_down": 0.19}
        actions.append((a.kind, a.reason))
    assert len(set(actions)) == 1  # noul answers do not move the policy


def test_jev_prompt_doc_matches_default_questions():
    doc = (Path(__file__).parents[1] / "docs" / "jev_prompt.md").read_text()
    for q in load_config().decision.resolved_questions("1h"):
        assert f"`{q.id}` ({q.type.capitalize()})" in doc
        assert q.instructions in doc
        for k, v in q.options.items():
            assert f"- `{k}`: {v}" in doc


def test_max_holding_bars_follows_direction_horizon(tmp_path):
    cfg = load_config()
    assert cfg.decision.horizon_bars == 24 and cfg.policy.max_holding_bars == 24
    p = tmp_path / "c.yaml"
    p.write_text("decision: {horizon_bars: 6}\n")
    assert load_config(p).policy.max_holding_bars == 6
    p.write_text("decision: {horizon_bars: 6}\npolicy: {max_holding_bars: 10}\n")
    assert load_config(p).policy.max_holding_bars == 10
