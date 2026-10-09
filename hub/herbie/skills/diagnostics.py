"""S3: Self-diagnosis and automatic service booking.

Herbie notices a fault code (DTC), explains it in plain words, and acts on it based
on a fixed severity table. The severity is never decided by the LLM:

  stop_now  → immediate safety warning, no booking dialogue while driving
  book_soon → find a workshop slot that fits the calendar, book on confirmation
  monitor   → remember it and mention it at the next service
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import geo
from ..context import ContextSnapshot
from ..sim import PLACES
from ..personality import narrate
from .base import Reply, Services, Skill, Suggestion, load_json

SLOT_TIMES = [(7, 30), (12, 0), (17, 45), (19, 0)]
SLOT_MINUTES = 45
LOOKAHEAD_DAYS = 5
MIN_LEAD = timedelta(hours=2)
WORKSHOP = "Herbie Partner Workshop Kreuzberg (mock)"


class Diagnostics(Skill):
    name = "diagnostics"
    intents = {"vehicle_health", "book_service"}
    events = {"dtc"}

    def __init__(self) -> None:
        self.catalog = load_json("dtc_catalog.json")
        self.service_centres = load_json("places.json").get("vw_service", [])

    def nearest_centres(self, n: int = 3) -> list[dict]:
        home = PLACES["home"]
        for c in self.service_centres:
            c["km_from_home"] = round(geo.haversine_m(home["lat"], home["lon"], c["lat"], c["lon"]) / 1000, 1)
        return sorted(self.service_centres, key=lambda c: c["km_from_home"])[:n]

    def service_check(self, ctx: ContextSnapshot, svc: Services) -> Reply:
        """Self-check at start-up: DTCs, then the service interval (VSS Vehicle.Service.TimeToService)."""
        days = (svc.world.get("Vehicle.Service.TimeToService") or 0) / 86400
        trace = [f"DTCs: {', '.join(ctx.vehicle.dtcs) or 'none'}", f"time to service: {days:.0f} days"]
        if ctx.vehicle.dtcs:
            return self.on_event("dtc", {"code": ctx.vehicle.dtcs[0]}, ctx, svc)[0]
        if days > 14:
            return Reply(text="All systems healthy, nothing to report.", skill=self.name, trace=trace, proactive=True)
        centres = self.nearest_centres()
        if not centres:
            return Reply(text=f"Your service is due in {days:.0f} days. Shall I find a workshop later?", skill=self.name,
                         trace=trace, proactive=True)
        trace.append("nearest Volkswagen service centres (OpenStreetMap): " +
                     "; ".join(f"{c['name']} ({c['km_from_home']} km)" for c in centres))
        info = {"title": "Scheduled service", "severity": "book_soon"}
        intro = (f"All systems healthy, but you have a service due in {'one week' if 6 <= days <= 8 else f'{days:.0f} days'}. "
                 f"I checked the nearby Volkswagen service centres.")
        return self.offer_booking("SERVICE", info, intro, ctx, svc, trace, workshop=centres[0])

    def lookup(self, code: str) -> dict:
        return self.catalog.get(code) or {"title": f"Unknown fault {code}", "system": "unknown", "severity": "book_soon",
                                          "explanation": "I don't recognise this code, so let's have a workshop look at it to be safe."}

    def free_slot(self, svc: Services, ctx: ContextSnapshot, not_before: datetime | None = None) -> datetime | None:
        now = ctx.environment.time
        not_before = not_before or now + MIN_LEAD
        busy = svc.memory.events_between(ctx.user.user_id, now, now + timedelta(days=LOOKAHEAD_DAYS + 1))
        for day in range(LOOKAHEAD_DAYS + 1):
            d = (now + timedelta(days=day)).replace(second=0, microsecond=0)
            if d.weekday() == 6:  # workshop closed Sundays
                continue
            for h, m in SLOT_TIMES:
                start = d.replace(hour=h, minute=m)
                end = start + timedelta(minutes=SLOT_MINUTES)
                if start < not_before:
                    continue
                if any(e["start"] < end and start < e["end"] for e in busy):
                    continue
                return start
        return None

    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        code = payload["code"]
        info = self.lookup(code)
        trace = [f"DTC {code}: {info['title']} ({info['system']})", f"severity from rule table: {info['severity']}"]
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "dtc", code, None, None, info)
        sev = info["severity"]

        if sev == "stop_now":
            trace.append("stop_now: safety warning only, no booking dialogue while driving")
            text = (f"{ctx.user.name}, please find a safe place to stop. {info['explanation']} "
                    "Put your hazards on. Once we're parked, I can call roadside assistance for you.")
            return [Reply(text=text, skill=self.name, trace=trace, proactive=True, data={"severity": sev, "code": code})]

        explanation, src = narrate(svc.llm, f"fault: {info['title']}\nexplanation: {info['explanation']}",
                                   "Explain this fault to the driver in your own voice in at most two short sentences. Don't change its urgency.",
                                   info["explanation"], ctx.system.llm_available)
        trace.append(f"explanation phrased by {src}")
        if sev == "monitor":
            return [Reply(text=f"Small heads-up: {explanation}", skill=self.name, trace=trace, proactive=True,
                          data={"severity": sev, "code": code})]
        return [self.offer_booking(code, info, explanation, ctx, svc, trace)]

    def offer_booking(self, code: str, info: dict, intro: str, ctx: ContextSnapshot, svc: Services, trace: list[str],
                      workshop: dict | None = None) -> Reply:
        name = WORKSHOP if workshop is None else workshop["name"] + (f", {workshop['address']}" if workshop.get("address") else "")
        # A routine service isn't urgent: start looking from tomorrow morning.
        tomorrow = (ctx.environment.time + timedelta(days=1)).replace(hour=0, minute=0)
        slot_from = tomorrow if code == "SERVICE" else None
        slot = self.free_slot(svc, ctx, slot_from)
        if slot is None:
            trace.append(f"no free slot in the next {LOOKAHEAD_DAYS} days")
            return Reply(text=f"{intro} I couldn't find a free workshop slot this week; want me to call them?",
                         skill=self.name, trace=trace, proactive=True)
        days_ahead = (slot.date() - ctx.environment.time.date()).days
        day = {0: "today", 1: "tomorrow"}.get(days_ahead, f"on {slot:%A}")
        trace.append(f"first slot ≥ {MIN_LEAD} ahead that doesn't clash with the calendar: {slot:%a %H:%M}")
        s = Suggestion.new(skill=self.name, kind="booking", subject=code,
                           payload={"slot": slot.isoformat(), "workshop": name, "title": info["title"]})
        text = f"{intro} {name} has a slot {day} at {slot:%H:%M} that fits around your calendar. Shall I book it?"
        return Reply(text=text, skill=self.name, trace=trace, suggestion=s, proactive=True,
                     data={"severity": info["severity"], "code": code, "slot": slot.isoformat()})

    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        codes = ctx.vehicle.dtcs
        if not codes:
            return Reply(text="I'm feeling great: no fault codes, everything's running smoothly.", skill=self.name,
                         trace=["DTC list empty"])
        infos = [(c, self.lookup(c)) for c in codes]
        worst = max(infos, key=lambda ci: ["monitor", "book_soon", "stop_now"].index(ci[1]["severity"]))
        trace = [f"active DTCs: {', '.join(codes)}; most severe: {worst[0]} ({worst[1]['severity']})"]
        if intent.name == "book_service" and worst[1]["severity"] != "stop_now":
            return self.offer_booking(worst[0], worst[1], f"Let's get the {worst[1]['title'].lower()} looked at.", ctx, svc, trace)
        listing = "; ".join(f"{i['title']}" for _, i in infos)
        return Reply(text=f"I've got {len(codes)} thing{'s' if len(codes) > 1 else ''} on my mind: {listing}. {worst[1]['explanation']}",
                     skill=self.name, trace=trace)

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        if not accepted:
            return Reply(text="Okay. I'll remind you tomorrow; it shouldn't wait too long.", skill=self.name,
                         trace=["booking declined; reminder noted"])
        start = datetime.fromisoformat(s.payload["slot"])
        svc.memory.add_event(ctx.user.user_id, f"Car service: {s.payload['title']}", start,
                             start + timedelta(minutes=SLOT_MINUTES), None, "service")
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "service_booking", s.subject, None, "booked", s.payload)
        return Reply(text=f"Booked for {start:%A at %H:%M} and added to your calendar. I'll remind you the evening before.",
                     skill=self.name, trace=[f"booking stored, calendar event created ({start:%a %H:%M})"],
                     data={"booking": s.payload})
