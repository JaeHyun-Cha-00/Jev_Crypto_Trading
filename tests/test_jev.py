"""JevModel tests. All replay recorded fixtures; none touch the network."""

import json
from pathlib import Path

import pytest

from jevtrade.config import load_config
from jevtrade.decision.base import JevConfig, default_questions
from jevtrade.decision.jev import HttpResponse, JevError, JevModel, build_payload
from jevtrade.state.builder import MarketState, estimate_tokens

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "btc_live"
PINNED = "typesafe/jev-1.13-20260917"
QS = default_questions(24, 0.5, 3.0)


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
    assert isinstance(p["state"], dict) and p["state"]["horizon_bars"] == 24
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
    assert d.probs["direction"] == {"up": 0.53, "flat": 0.12, "down": 0.35}
    assert d.p("regime", "trend_up") == 0.98
    assert d.probs["adverse_move"] == pytest.approx({"true": 0.54, "false": 0.46})
    assert d.probs["clear_signal"] == pytest.approx({"true": 0.59, "false": 0.41})
    assert d.input_tokens == 1920
    assert d.cost_usd == pytest.approx(8.064e-05)
    assert d.extra["estimated_cost_usd"] == pytest.approx(1920 * 0.042e-6)
    assert d.extra["served_model"] == PINNED and d.extra["requested_model"] == PINNED
    assert d.extra["confidence"]["direction"] == 0.3
    assert json.loads(d.raw_output)["id"] == "gen-dec-1791168980-wL4vIZGwKNDNpCpH5z4g"


def test_call_is_logged_with_version_latency_tokens_cost(caplog):
    m, _, _ = _model(HttpResponse(200, _body()))
    with caplog.at_level("INFO", logger="jevtrade.decision.jev"):
        m.decide(_state(), QS)
    line = caplog.records[-1].getMessage()
    for part in (f"model={PINNED}", f"served={PINNED}", "latency_ms=", "input_tokens=1920",
                 "est_cost_usd=0.00008064"):
        assert part in line


def test_cost_falls_back_to_estimate_without_reported_cost():
    body = json.loads(_body())
    del body["usage"]["cost"]
    m, _, _ = _model(HttpResponse(200, json.dumps(body)))
    d = m.decide(_state(), QS)
    assert d.cost_usd == pytest.approx(1920 * 0.042e-6)


def test_served_version_drift_abstains():
    body = json.loads(_body())
    body["model"] = "typesafe/jev-1.14-20261101"
    m, _, _ = _model(HttpResponse(200, json.dumps(body)))
    d = m.decide(_state(), QS)
    assert d.abstain and "differs from pinned" in d.abstain_reason
    assert d.probs == {} and d.input_tokens == 1920


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
