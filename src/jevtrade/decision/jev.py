"""JevModel: TypeSafe's Jev System One model, called through OpenRouter.

One request per decision: the market state plus every question, answered in
parallel (POST {base_url}/systemone). Every call is logged with the pinned and
served model version, latency, input tokens and estimated cost.

The API key is read from the environment at call time and is only ever put in
the Authorization header. When the variable is unset, the request goes out
without one, which works behind a proxy that injects the credential.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ..state.builder import MarketState
from .base import Decision, JevConfig, QuestionSpec

log = logging.getLogger(__name__)

# Configuration problems, not market signals: raise instead of abstaining.
_FATAL_STATUSES = {400, 401, 402, 403, 404, 413, 422}
_MAX_BACKOFF_S = 10.0


@dataclass
class HttpResponse:
    status: int
    body: str
    retry_after: float | None = None


# (url, json payload, headers, timeout_s) -> HttpResponse
Transport = Callable[[str, dict, dict, float], HttpResponse]


class JevError(RuntimeError):
    """A Jev call failed in a way that retrying or abstaining won't fix."""


def urllib_transport(url: str, payload: dict, headers: dict, timeout_s: float) -> HttpResponse:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return HttpResponse(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        ra = e.headers.get("retry-after") if e.headers else None
        try:
            retry_after = float(ra) if ra is not None else None
        except ValueError:
            retry_after = None
        return HttpResponse(e.code, e.read().decode(errors="replace"), retry_after)


def build_payload(model: str, state: MarketState, questions: list[QuestionSpec]) -> dict:
    try:
        state_value = json.loads(state.text)
    except json.JSONDecodeError:
        state_value = state.text
    return {
        "model": model,
        "state": state_value,
        "questions": {
            q.id: {"type": q.type, "instructions": q.instructions, "criteria": dict(q.options)}
            for q in questions
        },
    }


def noul_confidence(p: float) -> float:
    """TypeSafe's confidence-style number for a Noul: distance from 0.5, scaled to 0..1.

    Jev returns no `confidence` for a Noul; `noul` itself is P(yes).
    See https://docs.typesafe.ai/confidence.md#noul.
    """
    return abs(2.0 * p - 1.0)


def parse_answers(
    answers: dict, questions: list[QuestionSpec]
) -> tuple[dict[str, dict[str, float]], dict[str, dict], list[str]]:
    """Map Jev answers to {question: {option: p}}, a per-question record, and missing ids.

    A Noul's `noul` value is the probability that the answer is yes, so it
    becomes {"true": p, "false": 1 - p}. Each record keeps the answer as Jev
    returned it plus `gate_confidence`: Jev's `confidence` for a Choice, and
    |2p - 1| for a Noul (which has no model-reported confidence).
    """
    probs: dict[str, dict[str, float]] = {}
    records: dict[str, dict] = {}
    missing: list[str] = []
    for q in questions:
        a = answers.get(q.id)
        if not isinstance(a, dict) or a.get("type") != q.type:
            missing.append(q.id)
            continue
        if q.type == "noul":
            p = float(a["noul"])
            probs[q.id] = {"true": p, "false": 1.0 - p}
            records[q.id] = {"type": "noul", "choice": "true" if p >= 0.5 else "false",
                             "noul": p, "confidence": None, "gate_confidence": noul_confidence(p),
                             "probabilities": probs[q.id]}
        else:
            raw = a.get("probabilities") or {}
            probs[q.id] = {o: float(raw.get(o, 0.0)) for o in q.options}
            conf = float(a["confidence"]) if "confidence" in a else None
            records[q.id] = {"type": "choice", "choice": a.get("choice"), "noul": None,
                             "confidence": conf, "gate_confidence": conf,
                             "probabilities": probs[q.id]}
    return probs, records, missing


class JevModel:
    name = "jev"

    def __init__(
        self,
        cfg: JevConfig | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg or JevConfig()
        self.version = self.cfg.model
        self.transport = transport or urllib_transport
        self.sleep = sleep

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        key = os.environ.get(self.cfg.api_key_env)
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _abstain(self, state: MarketState, reason: str, raw: str, latency_ms: float,
                 extra: dict) -> Decision:
        log.warning("jev abstain model=%s reason=%s latency_ms=%.0f", self.version, reason,
                    latency_ms)
        return Decision(
            model=self.name, model_version=self.version, input_hash=state.input_hash, probs={},
            abstain=True, abstain_reason=reason, raw_output=raw, latency_ms=latency_ms,
            extra=extra,
        )

    def decide(self, state: MarketState, questions: list[QuestionSpec]) -> Decision:
        url = self.cfg.base_url.rstrip("/") + "/systemone"
        payload = build_payload(self.cfg.model, state, questions)
        extra: dict = {"requested_model": self.cfg.model}

        t0 = time.perf_counter()
        resp: HttpResponse | None = None
        error = ""
        for attempt in range(self.cfg.max_retries + 1):
            extra["attempts"] = attempt + 1
            try:
                resp = self.transport(url, payload, self._headers(), self.cfg.timeout_s)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                resp, error = None, f"{type(e).__name__}: {e}"
            if resp is not None:
                if resp.status == 200:
                    break
                error = f"HTTP {resp.status}"
                if resp.status in _FATAL_STATUSES:
                    log.error("jev call failed model=%s status=%d body=%s", self.version,
                              resp.status, resp.body[:500])
                    raise JevError(f"Jev request failed with HTTP {resp.status}: {resp.body[:500]}")
            if attempt < self.cfg.max_retries:
                wait = resp.retry_after if resp and resp.retry_after else 2.0 ** attempt
                self.sleep(min(wait, _MAX_BACKOFF_S))
        latency_ms = (time.perf_counter() - t0) * 1000

        if resp is None or resp.status != 200:
            raw = resp.body if resp is not None else ""
            return self._abstain(state, f"request failed: {error}", raw, latency_ms, extra)

        try:
            body = json.loads(resp.body)
            answers = body["answers"]
            served = body["model"]
            usage = body.get("usage") or {}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return self._abstain(state, f"malformed response: {e}", resp.body, latency_ms, extra)

        input_tokens = int(usage.get("input_tokens", 0))
        est_cost = input_tokens * self.cfg.price_per_input_token_usd
        reported_cost = usage.get("cost")
        extra.update(
            served_model=served,
            provider=body.get("provider"),
            generation_id=body.get("id"),
            output_tokens=usage.get("output_tokens"),
            estimated_cost_usd=est_cost,
            reported_cost_usd=reported_cost,
        )
        cost = float(reported_cost) if reported_cost is not None else est_cost
        log.info(
            "jev call model=%s served=%s latency_ms=%.0f input_tokens=%d est_cost_usd=%.8f "
            "reported_cost_usd=%s attempts=%d",
            self.version, served, latency_ms, input_tokens, est_cost, reported_cost,
            extra["attempts"],
        )

        def _with_usage(d: Decision) -> Decision:
            d.input_tokens, d.cost_usd = input_tokens, cost
            return d

        if served != self.cfg.model:
            return _with_usage(self._abstain(
                state, f"served model {served!r} differs from pinned {self.cfg.model!r}",
                resp.body, latency_ms, extra))
        try:
            probs, records, missing = parse_answers(answers, questions)
        except (KeyError, TypeError, ValueError) as e:
            return _with_usage(self._abstain(state, f"malformed answer: {e}", resp.body,
                                             latency_ms, extra))
        if missing:
            return _with_usage(self._abstain(state, f"missing answers: {', '.join(missing)}",
                                             resp.body, latency_ms, extra))
        extra["answers"] = records

        return Decision(
            model=self.name,
            model_version=served,
            input_hash=state.input_hash,
            probs=probs,
            raw_output=resp.body,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            cost_usd=cost,
            extra=extra,
        )
