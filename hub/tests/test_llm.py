"""LLM integration paths, with a fake Anthropic client (no network, no credentials)."""
import json
from types import SimpleNamespace as NS

from herbie import scenarios
from herbie.llm import LLM
from herbie.memory import Memory
from herbie.orchestrator import Orchestrator
from herbie.sim import World


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_llm(*responses) -> tuple[LLM, FakeMessages]:
    llm = LLM()
    llm.enabled, llm.provider, llm.model = True, "anthropic", "claude-opus-5-5"
    msgs = FakeMessages(responses)
    llm._client = NS(beta=NS(messages=msgs))
    return llm, msgs


def text_response(text, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=text)])


def test_request_shape_and_structured_output():
    llm, msgs = fake_llm(text_response(json.dumps({"a": 1})))
    assert llm.complete("sys", "hi", schema={"type": "object"}) == {"a": 1}
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in call and "temperature" not in call


def test_refusal_returns_none():
    llm, _ = fake_llm(text_response("", stop="refusal"))
    assert llm.complete("sys", "hi") is None


def test_unknown_utterance_is_classified_by_llm_and_still_gated():
    classified = {"intent": "request_mode", "direction": "none", "mode": "sport", "destination": "", "reply": ""}
    llm, _ = fake_llm(text_response(json.dumps(classified)))
    w = World()
    brain = Orchestrator(w, Memory(":memory:"), llm)
    scenarios.apply(w, "saturday_night_rain")
    r = brain.utterance("let's make this ride spicy")[0]
    assert r.intent == "request_mode (llm)"
    assert r.blocked and w.get("Vehicle.Powertrain.Transmission.PerformanceMode") == "NORMAL"


def test_llm_small_talk_and_offline_bypass():
    chat = {"intent": "chat", "direction": "none", "mode": "none", "destination": "",
            "reply": "Always happy to drive you, Saad."}
    llm, msgs = fake_llm(text_response(json.dumps(chat)))
    w = World()
    brain = Orchestrator(w, Memory(":memory:"), llm)
    assert brain.utterance("do you like me?")[0].text == "Always happy to drive you, Saad."
    w.cloud_online = False
    brain.utterance("do you like me?")
    assert len(msgs.calls) == 1  # offline: no cloud call attempted
