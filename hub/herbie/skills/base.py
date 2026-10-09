"""Shared skill interface and the services every skill gets.

A skill is a plug-in: it declares the intents and events it handles and returns a
Reply. Adding a capability (e.g. the trip planner) means adding one module and
registering it in the orchestrator; nothing else changes.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import safety
from ..context import ContextEngine, ContextSnapshot
from ..llm import LLM
from ..memory import Memory
from ..safety import Action
from ..sim import World

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_json(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text())


class Suggestion(BaseModel):
    id: str
    skill: str
    kind: str  # profile | food | booking | meeting | poi_story | alternative
    subject: str  # what is being learned about, e.g. "comfort_wind_down"
    bucket: str | None = None
    actions: list[Action] = []
    applied: bool = False  # already auto-applied; declining means undo
    undo: list[tuple[str, Any]] = []
    payload: dict = {}

    @staticmethod
    def new(**kw) -> "Suggestion":
        return Suggestion(id=uuid.uuid4().hex[:8], **kw)


class Reply(BaseModel):
    text: str
    skill: str
    trace: list[str] = []  # the "why": every decision step, shown in the cockpit
    applied: list[dict] = []
    blocked: list[dict] = []
    suggestion: Suggestion | None = None
    clarify: list[str] = []
    data: dict = {}
    proactive: bool = False
    intent: str | None = None


class Services:
    def __init__(self, world: World, memory: Memory, llm: LLM, engine: ContextEngine):
        self.world, self.memory, self.llm, self.engine = world, memory, llm, engine

    def apply(self, actions: list[Action], ctx: ContextSnapshot) -> tuple[list[dict], list[dict], list[tuple[str, Any]]]:
        """Run every action through the safety gate; write only what it allows."""
        applied, blocked, undo = [], [], []
        for a in actions:
            d = safety.check(a, ctx)
            if d.allowed:
                undo.append((a.path, self.world.set(a.path, d.final_value)))
                applied.append({"path": a.path, "value": d.final_value, "label": a.label, "notes": d.reasons})
            else:
                blocked.append({"path": a.path, "value": a.value, "label": a.label, "reasons": d.reasons})
        return applied, blocked, undo

    def revert(self, undo: list[tuple[str, Any]]) -> None:
        for path, old in reversed(undo):
            self.world.set(path, old)


class Skill:
    name: str = "skill"
    intents: set[str] = set()
    events: set[str] = set()

    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply | None:
        return None

    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> Reply | None:
        return None

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        raise NotImplementedError
