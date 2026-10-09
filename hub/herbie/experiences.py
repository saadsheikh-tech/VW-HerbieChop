"""Experiences: complete drives that bring every skill together, paced like a conversation.

An experience is a setup (where, when, fuel, calendar, mood of the day) plus "beats":
small moments Herbie can start, each with a condition (time, position on the route, an
earlier answer). The Director plays them one at a time:

  * never talks over the driver (quiet for a few seconds after any input)
  * one open question at a time: it waits for the answer (or ~35 s) before moving on
  * beats tied to the route fire where they make sense ("coming up on your left…")

Nothing here is a fake recording: each beat calls the real skills (diagnostics, trip
planner, meeting assistant, personalization…), so answers change what happens next.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import calendar_sync, geo
from .sim import World, at
from .skills.base import Reply, Suggestion

GAP_S = 6.0                # min real seconds between two things Herbie brings up
ANSWER_TIMEOUT_S = 35.0    # stop waiting for an answer after this
QUIET_AFTER_INPUT_S = 4.0  # don't start talking right after the driver did
TOUR_LANDMARKS = ("victory_column", "brandenburg_gate", "reichstag", "berliner_dom", "tv_tower")


@dataclass
class Beat:
    id: str
    when: Callable[["Director"], bool]
    run: Callable[["Director"], list]
    blocking: bool = False


@dataclass
class Experience:
    id: str
    title: str
    blurb: str
    setup: Callable[["Director"], None]
    beats: list[Beat] = field(default_factory=list)


class Director:
    name = "experience"  # also acts as the skill that receives answers to its own questions
    intents: set = set()
    events: set = set()

    def __init__(self, brain, clock: Callable[[], float] = time.monotonic):
        self.brain, self.clock = brain, clock
        self.exp: Experience | None = None
        self.fired: set[str] = set()
        self.flags: dict = {}
        self.waiting_since: float | None = None
        self.last = -1e9

    @property
    def world(self) -> World:
        return self.brain.world

    def skill(self, name: str):
        return self.brain.by_name[name]

    def ctx(self):
        return self.brain.engine.snapshot()

    # ---- lifecycle -------------------------------------------------------
    def start(self, exp_id: str) -> None:
        self.exp = EXPERIENCES[exp_id]
        self.fired, self.flags, self.waiting_since, self.last = set(), {}, None, -1e9
        self.brain.reset()
        self.world.__dict__.update(World().__dict__)
        self.world.experience_id = exp_id
        self.exp.setup(self)
        self.brain.by_name["music"].queue = []

    def stop(self) -> None:
        self.exp = None
        self.world.experience_id = None

    def on_world_event(self, event: str, payload: dict) -> None:
        if event == "arrived":
            self.flags["arrived"] = True

    def tick(self) -> list[Reply]:
        if not self.exp:
            return []
        now = self.clock()
        if self.brain.has_pending_question(now, ANSWER_TIMEOUT_S):
            return []
        if now - self.last < GAP_S or now - self.brain.last_input < QUIET_AFTER_INPUT_S:
            return []
        for beat in self.exp.beats:
            if beat.id in self.fired or not beat.when(self):
                continue
            self.fired.add(beat.id)
            replies = [r for r in (beat.run(self) or []) if r and r.text]
            if not replies:
                continue
            self.last = now
            return self.brain.deliver(replies)
        return []

    # ---- answers to the director's own questions ---------------------------
    def on_feedback(self, s: Suggestion, accepted: bool, ctx, svc) -> Reply:
        self.flags[s.subject] = accepted
        if s.subject == "destination_ok":
            if accepted:
                return depart(self, "Off we go! I'll get you there in style.")[0]
            return Reply(text="No problem. Tell me where to, or take the wheel with W A S D.", skill=self.name)
        return Reply(text="Okay!", skill=self.name)


# ---- building blocks ---------------------------------------------------------
def done(*ids: str) -> Callable[[Director], bool]:
    return lambda d: all(i in d.fired for i in ids)


def at_route(m: float) -> Callable[[Director], bool]:
    return lambda d: d.world.drive_mode in ("autopilot", "stopped") and d.world.route_pos_m >= m


def arrived(d: Director) -> bool:
    return bool(d.flags.get("arrived"))


def say(text: str, proactive: bool = True) -> Callable[[Director], list]:
    return lambda d: [Reply(text=text, skill="experience", proactive=proactive)]


def depart(d: Director, text: str) -> list[Reply]:
    w = d.world
    w.drive_mode, w.route_target_m, w.target_name = "autopilot", None, w.journey().end
    return [Reply(text=text, skill="experience", proactive=True, trace=[f"autopilot engaged → {w.journey().end}"])]


def tour_beats(journey_ids: tuple[str, ...]) -> list[Beat]:
    """'Coming up on your left: …' for a few headline landmarks, at the right spot on the route."""
    from .journey import load

    beats, seen = [], set()
    for jid in journey_ids:
        j = load(jid)
        for st in j.stops:
            if st["poi_id"] not in TOUR_LANDMARKS or st["poi_id"] in seen:
                continue
            seen.add(st["poi_id"])

            def run(d: Director, st=st):
                poi = next(p for p in d.brain.visual.local.pois if p["id"] == st["poi_id"])
                w = d.world
                lat, lon = w.get("Vehicle.CurrentLocation.Latitude"), w.get("Vehicle.CurrentLocation.Longitude")
                rel = geo.angle_diff(geo.bearing_deg(lat, lon, poi["lat"], poi["lon"]), w.get("Vehicle.CurrentLocation.Heading"))
                side = "ahead" if abs(rel) < 25 else ("on your left" if rel < 0 else "on your right")
                return [Reply(text=f"Coming up {side}: the {poi['name']}. Point at it and ask me anything!",
                              skill="experience", proactive=True, trace=[f"tour hint at {w.route_pos_m:.0f} m"])]

            beats.append(Beat(f"tour_{st['poi_id']}",
                              lambda d, st=st: d.world.journey() is not None and d.world.journey().id in journey_ids
                              and d.world.drive_mode == "autopilot"
                              and st["at_m"] - 320 <= d.world.route_pos_m <= st["at_m"] - 60, run))
    return beats


def prepare(d: Director, journey: str, when: datetime, fuel: float, *, fatigue=0.3, hunger=0.3, passengers=0,
            playlist="Herbie Radio", origin=None, destination=None, legs=(), service_days=60) -> None:
    w = d.world
    w.journey_id, w.clock = journey, when
    w.place_on_route(0.0)
    w.drive_mode, w.time_scale, w.cruise_kmh = "parked", 2.0, 40.0
    w.set_fuel(fuel)
    w.fatigue, w.hunger, w.passengers = fatigue, hunger, passengers
    w.origin, w.destination = origin, destination
    w.signals["Vehicle.Cabin.Infotainment.Media.Playlist"] = playlist
    w.signals["Vehicle.Service.TimeToService"] = service_days * 86400
    w.planned_legs = [dict(leg) for leg in legs]


def demo_day(d: Director, day: datetime, events: list[tuple[str, int, int, int, str | None]]) -> str:
    """Today's calendar: real Google Calendar if configured, otherwise the demo events."""
    mem, uid = d.brain.memory, "saad"
    real = calendar_sync.fetch_today()
    if real is not None:
        n = calendar_sync.map_onto(mem, uid, real, day)
        return f"Google Calendar ({n} events today)"
    day0 = day.replace(hour=0, minute=0)
    for title, h, m, minutes, link in events:
        start = day0.replace(hour=h, minute=m)
        if not any(e["title"] == title for e in mem.events_between(uid, start, start + timedelta(minutes=1))):
            mem.add_event(uid, title, start, start + timedelta(minutes=minutes), link, "meeting" if link else "personal")
    return "demo calendar"


def todays_events(d: Director) -> list[dict]:
    t = d.world.clock
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return [e for e in d.brain.memory.events_between("saad", t, day0 + timedelta(days=1)) if e["kind"] != "work"]


# ---- Drive to work -------------------------------------------------------------
WED = 2


def setup_work(d: Director) -> None:
    when = at(WED, 7, 40)
    prepare(d, "to_work", when, fuel=24.0, fatigue=0.45, hunger=0.3, playlist="Morning Boost", origin="home",
            destination="work", service_days=7,
            legs=[{"name": "drive home", "km": 13.9, "at": datetime(2000, 1, 1, 17, 45).time()}])
    d.flags["calendar_source"] = demo_day(d, when, [
        ("German class (B1, online)", 7, 50, 45, "https://meet.google.com/herbie-deutsch"),
        ("Meeting with Joshua", 11, 0, 45, "https://meet.google.com/herbie-joshua"),
    ])


def briefing(d: Director) -> list[Reply]:
    evs = todays_events(d)
    if not evs:
        return [Reply(text="Your calendar's clear today. Enjoy it!", skill="experience", proactive=True)]
    listing = ", then ".join(f"{e['title']} at {e['start']:%H:%M}" for e in evs[:3])
    lead = f"Here's your day: {listing}. "
    soon = next((e for e in evs if e["link"] and e["start"] <= d.world.clock + timedelta(minutes=60)), None)
    if soon:
        return [d.skill("meeting").offer_join(soon, d.ctx(), d.brain.svc, lead=lead)]
    return [Reply(text=lead.strip(), skill="experience", proactive=True,
                  trace=[f"source: {d.flags.get('calendar_source')}"])]


def coffee_question(d: Director) -> list[Reply]:
    s = Suggestion.new(skill="trip_planner", kind="question", subject="had_coffee")
    return [Reply(text="Before we go: have you had your morning coffee yet?", skill="experience", proactive=True, suggestion=s)]


def arrival_work(d: Director) -> list[Reply]:
    out = []
    if d.world.get("Vehicle.Cabin.Infotainment.Meeting.Recording"):
        r = d.skill("meeting").end(d.ctx(), d.brain.svc)
        r.text = "We're at work! " + r.text.replace("has ended.", "is wrapping up, so I've handed it to your phone.")
        out.append(r)
    else:
        out.append(Reply(text="Here we are, work sweet work!", skill="experience"))
    nxt = next((e for e in todays_events(d) if "joshua" in e["title"].lower()), None)
    out[-1].text += f" Don't forget: {nxt['title']} at {nxt['start']:%H:%M}. Have a great day!" if nxt else " Have a great day!"
    out[-1].proactive = True
    return out


# ---- Drive home -----------------------------------------------------------------
def setup_home(d: Director) -> None:
    prepare(d, "to_home", at(WED, 17, 40), fuel=11.0, fatigue=0.75, hunger=0.8, playlist="Herbie Radio",
            origin="work", destination="home")


def welcome_home(d: Director) -> list[Reply]:
    mem = d.brain.memory
    eps = mem.recent_episodes("saad", 60)
    recall = []
    booking = next((e for e in eps if e["kind"] == "service_booking"), None)
    if booking:
        slot = datetime.fromisoformat(booking["payload"]["slot"])
        recall.append(f"Your service is booked for {slot:%A at %H:%M}.")
    notes = next((e for e in eps if e["kind"] == "meeting_summary"), None)
    if notes:
        mine = [a["task"] for a in notes["payload"].get("action_items", []) if a["owner"].lower().startswith("saad")]
        if mine:
            recall.append(f"And from this morning's class: you said you'd {mine[0].rstrip('.')}.")
    return [Reply(text=("Welcome back, Saad. " + " ".join(recall)).strip(), skill="experience", proactive=True,
                  trace=["recalled from memory: " + (", ".join(e["kind"] for e in (booking, notes) if e) or "nothing yet")])]


def comfort_and_food(d: Director) -> list[Reply]:
    return d.skill("personalization").on_event("trip_start", {}, d.ctx(), d.brain.svc)


# ---- Friday night ---------------------------------------------------------------
def setup_night(d: Director) -> None:
    prepare(d, "night_out", at(4, 22, 30), fuel=55.0, fatigue=0.25, hunger=0.2, passengers=3,
            playlist="Friday Night")


def night_destination(d: Director) -> list[Reply]:
    s = Suggestion.new(skill="experience", kind="question", subject="destination_ok")
    return [Reply(text="So where are we headed? RAW-Gelände in Friedrichshain, like last Friday?", skill="experience",
                  proactive=True, suggestion=s)]


def night_sport(d: Director) -> list[Reply]:
    return [r for r in d.skill("personalization").on_event("trip_start", {}, d.ctx(), d.brain.svc)
            if r.suggestion and r.suggestion.subject in ("sport_night", "sport_alternative")]


EXPERIENCES: dict[str, Experience] = {
    "drive_to_work": Experience(
        "drive_to_work", "Drive to work", "Wednesday 07:40 at home. Self-check, coffee, calendar, German class, fuel planning.",
        setup_work, [
            Beat("welcome", lambda d: True, say("Good morning, Saad, welcome back! Give me a second, I'm running my morning self-check.")),
            Beat("self_check", done("welcome"), lambda d: [d.skill("diagnostics").service_check(d.ctx(), d.brain.svc)], blocking=True),
            Beat("coffee", done("self_check"), coffee_question, blocking=True),
            Beat("calendar", done("coffee"), briefing, blocking=True),
            Beat("depart", done("calendar"), lambda d: depart(d, "Alright, seatbelt on, let's go! Next stop: work. "
                                                                "Point at anything outside and ask me about it.")),
            Beat("fuel_plan", at_route(900), lambda d: [r for r in [d.skill("trip_planner").fuel_check(d.ctx(), d.brain.svc, [])] if r],
                 blocking=True),
            Beat("camera_tip", lambda d: at_route(3200)(d) and not d.world.camera_on,
                 say("By the way, switch on my camera and I'll keep an eye on how you're doing. It all stays in the car.")),
            *tour_beats(("to_work",)),
            Beat("arrival", arrived, arrival_work),
        ]),
    "drive_home": Experience(
        "drive_home", "Drive back from work", "Wednesday 17:40 at CODE. Tired, hungry, low on fuel, rush hour.",
        setup_home, [
            Beat("welcome", lambda d: True, welcome_home),
            Beat("comfort", done("welcome"), comfort_and_food, blocking=True),
            Beat("depart", done("comfort"), lambda d: depart(d, "Let's get you home. Sit back, I've got this.")),
            Beat("fuel", at_route(400), lambda d: [r for r in [d.skill("trip_planner").fuel_check(d.ctx(), d.brain.svc, [], urgent=True)] if r],
                 blocking=True),
            *tour_beats(("to_home",)),
            Beat("arrival", arrived, say("Home sweet home. Rest well, Saad. I'll be here in the morning.")),
        ]),
    "friday_night": Experience(
        "friday_night", "Friday night out", "Friday 22:30, three friends in the car, party playlist, sport mode on offer.",
        setup_night, [
            Beat("welcome", lambda d: True, say("Friday night! Hey everyone, full car tonight. I love it.")),
            Beat("where", done("welcome"), night_destination, blocking=True),
            Beat("sport", lambda d: d.flags.get("destination_ok") is not None, night_sport, blocking=True),
            Beat("depart_fallback", lambda d: d.flags.get("destination_ok") is None and "where" in d.fired
                 and d.clock() - d.last > ANSWER_TIMEOUT_S, lambda d: depart(d, "I'll take that as RAW-Gelände. Let's go!")),
            Beat("responsible", at_route(2500),
                 say("Quick one before the fun starts: if anyone's drinking tonight, just say so later and I'll order you "
                     "a taxi home. I'll park myself.")),
            *tour_beats(("night_out",)),
            Beat("arrival", arrived, say("RAW-Gelände! Have an amazing night, everyone. I'll be right here.")),
        ]),
}


def catalogue() -> list[dict]:
    return [{"id": e.id, "title": e.title, "blurb": e.blurb} for e in EXPERIENCES.values()]
