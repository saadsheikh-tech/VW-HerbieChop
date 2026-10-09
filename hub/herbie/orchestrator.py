"""Herbie Orchestrator: one brain, many skills.

Routes utterances and vehicle events to skills, keeps the short-term dialogue state
(open suggestions, a pending clarification question) and logs every turn with its
decision trace so the cockpit can show *why* Herbie did something.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Callable

from . import intents as intent_mod
from .context import ContextEngine
from .llm import LLM
from .memory import Memory
from .personality import PERSONA, say
from .sim import World
from .skills.base import Reply, Services, Skill, Suggestion
from .skills.diagnostics import Diagnostics
from .skills.meeting import MeetingAssistant
from .skills.music import Music
from .skills.navigation import Navigation
from .skills.trip_planner import TripPlanner
from .skills.wellbeing import Wellbeing
from .skills.personalization import Personalization
from .skills.visual_dialogue import VisualDialogue

log = logging.getLogger("herbie.orchestrator")
MAX_OPEN_SUGGESTIONS = 10
MAX_LOG = 200


class Orchestrator:
    def __init__(self, world: World, memory: Memory, llm: LLM):
        self.world, self.memory, self.llm = world, memory, llm
        self.engine = ContextEngine(world, memory, llm.available)
        self.svc = Services(world, memory, llm, self.engine)
        self.visual = VisualDialogue()
        music, planner = Music(), TripPlanner()
        self.skills: list[Skill] = [self.visual, Personalization(), Diagnostics(), MeetingAssistant(),
                                    Navigation(self.visual.local.pois), music, planner, Wellbeing(music, planner)]
        self.by_name = {s.name: s for s in self.skills}
        from .experiences import Director

        self.director = Director(self)
        self.by_name["experience"] = self.director
        self.open_times: dict[str, float] = {}
        self.last_input = -1e9
        self.open: OrderedDict[str, Suggestion] = OrderedDict()
        self.pending_clarify: dict | None = None
        self.log: list[dict] = []
        self.listeners: list[Callable[[dict], None]] = []

    # ---- bookkeeping -------------------------------------------------------
    def _emit(self, entry: dict) -> None:
        self.log.append(entry)
        del self.log[:-MAX_LOG]
        for fn in self.listeners:
            fn(entry)

    def _finish(self, replies: list[Reply], started: float) -> list[Reply]:
        ms = round((time.perf_counter() - started) * 1000)
        for r in replies:
            if r.suggestion:
                self.open[r.suggestion.id] = r.suggestion
                self.open_times[r.suggestion.id] = time.monotonic()
                while len(self.open) > MAX_OPEN_SUGGESTIONS:
                    self.open.popitem(last=False)
            if r.clarify:
                self.pending_clarify = {"options": r.data["options"], "offset": r.data["offset"]}
            r.data.setdefault("latency_ms", ms)
            self._emit({"role": "herbie", "reply": r.model_dump(mode="json"), "time": self.world.clock.isoformat()})
        return replies

    def deliver(self, replies: list[Reply]) -> list[Reply]:
        """Publish replies produced outside a driver turn (experience beats, sensor reactions)."""
        return self._finish(replies, time.perf_counter())

    def has_pending_question(self, now: float, timeout: float) -> bool:
        """Is Herbie still waiting for an answer it asked recently? (auto-applied changes don't count)"""
        return any(not s.applied and now - self.open_times.get(sid, -1e9) < timeout for sid, s in self.open.items())

    def reset(self) -> None:
        self.open.clear()
        self.open_times.clear()
        self.pending_clarify = None
        self.log.clear()

    # ---- entry points ------------------------------------------------------
    def utterance(self, text: str) -> list[Reply]:
        started = time.perf_counter()
        text = (text or "").strip()[:500]
        self.last_input = time.monotonic()
        self._emit({"role": "driver", "text": text, "time": self.world.clock.isoformat()})
        ctx = self.engine.snapshot()

        if self.pending_clarify:
            pending, self.pending_clarify = self.pending_clarify, None
            r = self.visual.resolve(text, pending["options"], pending["offset"], ctx, self.svc)
            if r:
                return self._finish([r], started)

        intent = intent_mod.parse(text)
        if intent.name == "unknown" and ctx.system.llm_available:
            intent = intent_mod.parse_with_llm(self.llm, PERSONA, text, ctx.summary()) or intent
        reply = self.route(intent, ctx)
        reply.intent = f"{intent.name} ({intent.source})"
        return self._finish([reply], started)

    def route(self, intent: intent_mod.Intent, ctx) -> Reply:
        n = intent.name
        if n in ("confirm", "decline"):
            if not self.open:
                return Reply(text="There's nothing waiting for a yes or no right now.", skill="orchestrator")
            sid = next(reversed(self.open))
            return self._feedback(sid, n == "confirm", ctx)
        if n == "undo":
            sid = next((k for k in reversed(self.open) if self.open[k].applied), None)
            if not sid:
                return Reply(text="There's nothing for me to undo.", skill="orchestrator")
            return self._feedback(sid, False, ctx)
        if n == "greeting":
            return Reply(text=say("greeting", name=ctx.user.name), skill="orchestrator")
        if n == "speed_query":
            text = (f"We're doing {ctx.vehicle.speed_kmh:.0f} km/h; the limit here is {ctx.environment.speed_limit}."
                    if ctx.quality.get("speed") == "ok" else "My speed signal looks unreliable right now, so check the cluster.")
            return Reply(text=text, skill="orchestrator", trace=[f"speed quality: {ctx.quality.get('speed')}"])
        if n == "chat" and intent.reply:
            return Reply(text=intent.reply, skill="orchestrator", trace=["small talk answered by LLM"])
        for skill in self.skills:
            if n in skill.intents:
                return skill.handle(intent, ctx, self.svc)
        trace = ["no rule matched" + ("" if ctx.system.llm_available else "; LLM unavailable, so no second opinion")]
        return Reply(text=say("unknown"), skill="orchestrator", trace=trace)

    def event(self, name: str, payload: dict | None = None) -> list[Reply]:
        started = time.perf_counter()
        payload = payload or {}
        ctx = self.engine.snapshot()
        if name in ("playlist_changed", "track_end"):  # silent housekeeping, not part of the dialogue log
            self.by_name["music"].on_event(name, payload, ctx, self.svc)
            return []
        self.director.on_world_event(name, payload)
        self._emit({"role": "event", "text": f"{name} {payload or ''}".strip(), "time": self.world.clock.isoformat()})
        replies: list[Reply] = []
        for skill in self.skills:
            if name in skill.events:
                out = skill.on_event(name, payload, ctx, self.svc)
                replies.extend(out if isinstance(out, list) else [out] if out else [])
        return self._finish(replies, started)

    def feedback(self, suggestion_id: str, accepted: bool) -> Reply | None:
        if suggestion_id not in self.open:
            return None
        started = time.perf_counter()
        self.last_input = time.monotonic()
        self._emit({"role": "driver", "text": "✓ yes" if accepted else "✗ no", "time": self.world.clock.isoformat()})
        return self._finish([self._feedback(suggestion_id, accepted, self.engine.snapshot())], started)[0]

    def _feedback(self, sid: str, accepted: bool, ctx) -> Reply:
        s = self.open.pop(sid)
        self.open_times.pop(sid, None)
        return self.by_name[s.skill].on_feedback(s, accepted, ctx, self.svc)
