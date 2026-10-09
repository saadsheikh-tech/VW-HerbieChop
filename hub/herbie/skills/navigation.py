"""S5 (precursor): Navigation along the demo journey, voice-controlled.

"Herbie, drive to the TV Tower" picks the matching landmark on the route and engages
the (simulated) autopilot. Engaging goes through the safety gate like any other
actuation. This is the seed of the Smart Trip Planner: routing between real stops
with memory of preferences comes on top once its Phase 1 definition is in.
"""
from __future__ import annotations

import re

from .. import safety
from ..context import ContextSnapshot
from ..safety import Action
from .base import Reply, Services, Skill

P_AUTOPILOT = "Vehicle.ADAS.Autopilot.IsEngaged"
END_WORDS = re.compile(r"\b(end|alexanderplatz|alex|finish|all the way|the tour|the journey|the route)\b")
STOP_WORDS = {"the", "to", "at", "of", "on", "a", "drive", "me", "take", "please", "herbie", "go", "head", "navigate"}


def match_stop(text: str, stops: list[dict], pois: dict[str, dict]) -> dict | None:
    for st in stops:  # an exact name always wins
        if st["name"].lower() in text.lower():
            return st
    words = set(re.findall(r"[a-zäöüß.']{3,}", text.lower())) - STOP_WORDS
    best, best_score = None, 0
    for st in stops:
        poi = pois.get(st["poi_id"], {})
        names = f"{st['name']} {poi.get('type', '')}".lower()
        score = sum(2 if w in st["name"].lower() else 1 for w in words if w in names)
        if score > best_score:
            best, best_score = st, score
    return best


class Navigation(Skill):
    name = "navigation"
    intents = {"navigate", "stop_driving", "where_am_i"}
    events = {"arrived"}

    def __init__(self, pois: list[dict]) -> None:
        self.pois = {p["id"]: p for p in pois}

    def drive_to(self, stop: dict | None, ctx: ContextSnapshot, svc: Services, trace: list[str]) -> Reply:
        w = svc.world
        j = w.journey()
        if w.drive_mode == "manual" or w.route_pos_m == 0:
            w.snap_to_route()
        target_m = j.length if stop is None else stop["at_m"]
        name = j.end if stop is None else stop["name"]
        if stop is None:
            name = j.end
        if target_m <= w.route_pos_m + 5:
            trace.append(f"{name} is at {target_m} m, we're at {w.route_pos_m:.0f} m")
            return Reply(text=f"We've already passed {name}. Want me to take you back to the start of the route?",
                         skill=self.name, trace=trace)
        decision = safety.check(Action(path=P_AUTOPILOT, value=True, label="engaged autopilot"), ctx)
        if not decision.allowed:
            trace.append("safety gate: " + "; ".join(decision.reasons))
            return Reply(text=f"I can't drive myself right now: {'; '.join(decision.reasons)}.", skill=self.name,
                         trace=trace, blocked=[{"label": "autopilot", "reasons": decision.reasons}])
        w.route_target_m, w.target_name, w.drive_mode = float(target_m), name, "autopilot"
        dist = target_m - w.route_pos_m
        mins = max(1, round(dist / (w.cruise_kmh / 3.6) / 60))
        trace.append(f"target {name} at {target_m} m along the route; {dist:.0f} m to go")
        return Reply(text=f"Off we go to {name}: {dist / 1000:.1f} km, about {mins} minute{'s' if mins > 1 else ''}. "
                          "Ask me about anything you see on the way.",
                     skill=self.name, trace=trace, applied=[{"label": f"autopilot to {name}", "path": P_AUTOPILOT,
                                                             "value": True, "notes": []}])

    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        w = svc.world
        j = w.journey()
        if j is None:
            return Reply(text="I only know the Berlin landmark route for now. Load the 'Berlin landmark drive' and I'll take you.",
                         skill=self.name, trace=["no journey loaded"])
        if intent.name == "stop_driving":
            w.drive_mode = "manual"
            w.signals["Vehicle.Speed"] = 0.0
            w.route_target_m = w.target_name = None
            return Reply(text="Stopping here. Take the wheel with W A S D whenever you like.", skill=self.name,
                         trace=["autopilot disengaged, speed 0"])
        if intent.name == "where_am_i":
            nxt = j.next_stop(w.route_pos_m)
            text = f"We're on {j.street(w.route_pos_m) or 'the route'}."
            if nxt:
                text += f" Next up: {nxt['name']}, in about {round(nxt['at_m'] - w.route_pos_m, -1):.0f} metres."
            return Reply(text=text, skill=self.name, trace=[f"route position {w.route_pos_m:.0f} m"])
        dest = intent.slots.get("destination", "")
        trace = [f"destination phrase: '{dest}'"]
        if not dest or END_WORDS.search(dest.lower()):
            return self.drive_to(None, ctx, svc, trace)
        stop = match_stop(dest, j.stops, self.pois)
        if stop is None:
            names = ", ".join(s["name"] for s in j.stops[1:6])
            return Reply(text=f"I don't know '{dest}' on this route. I can take you to places like {names}.",
                         skill=self.name, trace=trace + ["no stop matched"])
        trace.append(f"matched stop: {stop['name']}")
        return self.drive_to(stop, ctx, svc, trace)

    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        if svc.world.experience_id:
            return []  # the running experience narrates its own arrival
        return [Reply(text=f"Here we are: {payload['name']}. Want to keep going, or take the wheel?",
                      skill=self.name, proactive=True, trace=["autopilot reached its target"])]
