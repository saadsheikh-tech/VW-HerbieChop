"""S2: Adaptive AI-powered personalization.

Herbie learns which "mood profile" the driver wants in which situation and either
applies it on its own or offers it, depending on how sure it is and how much is at
stake:

  * context bucket   = day type | time band | trip kind | tired/fresh | solo/group
  * learned score    = Laplace-smoothed acceptance rate over the last 10 episodes
                       in that bucket (explainable, no black box)
  * muting           = 3 declines in a row in a bucket silences that profile there
  * autonomy policy  = comfort may auto-apply when score >= 0.7 (n >= 3), always
                       announced with one-tap undo; sport is ALWAYS only offered and
                       still has to pass the safety gate

A contextual bandit can replace the scorer later without changing this interface.
"""
from __future__ import annotations

from typing import Callable

from .. import geo, safety
from ..context import ContextSnapshot
from ..personality import human_join, narrate, say
from ..safety import Action
from .base import Reply, Services, Skill, Suggestion, load_json

AUTO_APPLY_SCORE = 0.7
SUGGEST_SCORE = 0.4
MIN_SAMPLES_FOR_AUTO = 3
MUTE_AFTER_DECLINES = 3
HISTORY = 10
HUNGER_THRESHOLD = 0.6

P_MODE = "Vehicle.Powertrain.Transmission.PerformanceMode"
P_DAMP = "Vehicle.Chassis.Suspension.DampingMode"
P_SEAT = "Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline"
P_TEMP = "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature"
P_MUSIC = "Vehicle.Cabin.Infotainment.Media.Playlist"
P_LIGHT = "Vehicle.Cabin.Light.AmbientMood"
P_WAYPOINT = "Vehicle.Cabin.Infotainment.Navigation.Waypoint"


def bucket(ctx: ContextSnapshot) -> str:
    return "|".join([
        ctx.environment.day_type,
        ctx.environment.time_band,
        ctx.trip.kind,
        "tired" if ctx.user.fatigue > 0.6 else "fresh",
        "group" if ctx.interaction.passengers >= 2 else "solo",
    ])


def comfort_actions(prefs: dict) -> list[Action]:
    recline = prefs.get("comfort_seat_recline", 28)
    temp = prefs.get("comfort_cabin_temp", 22.5)
    music = prefs.get("music_wind_down", "Calm Evening")
    return [
        Action(path=P_DAMP, value="SOFT", label="softened the suspension"),
        Action(path=P_SEAT, value=recline, label=f"eased your seat back to {recline}°"),
        Action(path=P_MUSIC, value=music, label=f"put on {music}"),
        Action(path=P_TEMP, value=temp, label=f"warmed the cabin to {temp}°C"),
        Action(path=P_LIGHT, value="WARM", label="dimmed the lights to warm"),
    ]


def sport_actions(prefs: dict) -> list[Action]:
    music = prefs.get("music_night_out", "Saturday Night Drive")
    return [
        Action(path=P_MODE, value="SPORT", label="switched to sport mode", requires_confirmation=True),
        Action(path=P_DAMP, value="FIRM", label="firmed up the suspension"),
        Action(path=P_SEAT, value=15, label="brought your seat upright"),
        Action(path=P_MUSIC, value=music, label=f"put on {music}"),
        Action(path=P_LIGHT, value="VIBRANT", label="turned the ambient light up"),
    ]


def normal_actions() -> list[Action]:
    return [
        Action(path=P_MODE, value="NORMAL", label="back to normal mode"),
        Action(path=P_DAMP, value="NORMAL", label="normal suspension"),
        Action(path=P_SEAT, value=20, label="seat back to your default"),
        Action(path=P_LIGHT, value="NEUTRAL", label="neutral lighting"),
    ]


PROFILES: dict[str, dict] = {
    "comfort_wind_down": {
        "auto_allowed": True,
        "actions": comfort_actions,
        "trigger": lambda c: c.environment.time_band in ("evening", "night") and c.user.fatigue > 0.6,
        "pitch": "Long day? Want me to switch to your wind-down setup?",
    },
    "sport_night": {
        "auto_allowed": False,
        "actions": sport_actions,
        "trigger": lambda c: (c.environment.time.weekday() >= 4 and c.environment.time_band == "night"  # Fri–Sun nights
                              and c.interaction.passengers >= 1 and c.user.fatigue <= 0.6),
        "pitch": "{day} night with the crew! Shall I go sport mode: firmer suspension, upright seat and your night-out playlist?",
    },
}


class Personalization(Skill):
    name = "personalization"
    intents = {"request_mode", "hungry"}
    events = {"trip_start"}

    def __init__(self) -> None:
        self.offers = load_json("offers.json")

    # ---- learning ----------------------------------------------------------
    def score(self, svc: Services, ctx: ContextSnapshot, subject: str, b: str) -> tuple[float, int, bool]:
        outcomes = svc.memory.outcomes(ctx.user.user_id, subject, b, HISTORY)
        accepted = sum(1 for o in outcomes if o == "accepted")
        muted = len(outcomes) >= MUTE_AFTER_DECLINES and all(o == "declined" for o in outcomes[:MUTE_AFTER_DECLINES])
        return (accepted + 1) / (len(outcomes) + 2), len(outcomes), muted

    def record(self, svc: Services, ctx: ContextSnapshot, subject: str, b: str | None, outcome: str) -> None:
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "suggestion", subject, b, outcome)

    # ---- proactive ---------------------------------------------------------
    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        replies = []
        b = bucket(ctx)
        for subject, prof in PROFILES.items():
            trace = [f"context bucket: {b}"]
            if not prof["trigger"](ctx):
                continue
            trace.append(f"{subject}: situation matches")
            score, n, muted = self.score(svc, ctx, subject, b)
            trace.append(f"learned score {score:.2f} from {n} past episodes here")
            if muted:
                trace.append(f"muted: declined {MUTE_AFTER_DECLINES}× in a row in this context")
                replies.append(Reply(text="", skill=self.name, trace=trace, proactive=True))
                continue
            if score < SUGGEST_SCORE:
                trace.append(f"score below {SUGGEST_SCORE}: staying quiet")
                replies.append(Reply(text="", skill=self.name, trace=trace, proactive=True))
                continue
            actions = prof["actions"](ctx.user.preferences)
            precheck = [d for a in actions if not (d := safety.check(a.model_copy(update={"confirmed": True}), ctx)).allowed]
            if precheck:
                trace.append("safety gate would block: " + "; ".join(r for d in precheck for r in d.reasons))
                replies.append(self.alternative(ctx, b, precheck, trace, proactive=True))
                continue
            if prof["auto_allowed"] and score >= AUTO_APPLY_SCORE and n >= MIN_SAMPLES_FOR_AUTO:
                trace.append(f"score ≥ {AUTO_APPLY_SCORE} and n ≥ {MIN_SAMPLES_FOR_AUTO}: applying on my own, with undo")
                applied, blocked, undo = svc.apply(actions, ctx)
                what = human_join([a["label"] for a in applied])
                text, src = narrate(svc.llm, f"driver: {ctx.user.name}\napplied: {what}\nsituation: tired, evening, heading home",
                                    "Announce warmly in one or two short sentences what you did and that they can say 'undo'.",
                                    f"Long day, {ctx.user.name}? I've {what}. Say 'undo' if you'd rather not.", ctx.system.llm_available)
                trace.append(f"phrased by {src}")
                s = Suggestion.new(skill=self.name, kind="profile", subject=subject, bucket=b, applied=True, undo=undo)
                replies.append(Reply(text=text, skill=self.name, trace=trace, applied=applied, blocked=blocked,
                                     suggestion=s, proactive=True))
            else:
                trace.append("offering instead of acting (sport is never automatic)" if not prof["auto_allowed"]
                             else "not confident enough to act alone: offering")
                s = Suggestion.new(skill=self.name, kind="profile", subject=subject, bucket=b,
                                   actions=[a.model_copy(update={"confirmed": True}) for a in actions])
                replies.append(Reply(text=prof["pitch"].format(day=ctx.environment.time.strftime("%A")), skill=self.name,
                                     trace=trace, suggestion=s, proactive=True))

        if ctx.user.hunger >= HUNGER_THRESHOLD:
            food = self.food_offer(ctx, svc, [f"hunger estimate {ctx.user.hunger:.2f} ≥ {HUNGER_THRESHOLD}"])
            if food:
                replies.append(food)
        return replies

    def food_offer(self, ctx: ContextSnapshot, svc: Services, trace: list[str]) -> Reply | None:
        prefs = ctx.user.preferences
        if not prefs.get("food_offers_opt_in"):
            trace.append("driver has not opted in to food offers")
            return None
        fav = prefs.get("favourite_food")
        offers = [o for o in self.offers if o["item"] == fav] or self.offers
        offer = offers[0]
        detour, after_trip = "", False
        j = svc.world.journey()
        if j:  # on a journey: is it closer to where we're going than to here?
            end_lat, end_lon = j.points[-1]
            d_end = geo.haversine_m(end_lat, end_lon, offer["lat"], offer["lon"]) / 1000
            if d_end < 3:
                detour, after_trip = f" It's just {d_end:.1f} km from {j.end.split(' (')[0].lower()}.", True
        if not detour and ctx.gps_ok:
            d = geo.haversine_m(ctx.vehicle.lat, ctx.vehicle.lon, offer["lat"], offer["lon"]) / 1000
            detour = f" It's {d:.1f} km away."
        trace.append(f"favourite '{fav}' matched offer {offer['id']} (sponsored={offer['sponsored']}, mock data)")
        label = " (sponsored)" if offer["sponsored"] else ""
        text = f"Also, you sound hungry: {offer['place']} has a deal on your favourite, {offer['deal']}{label}.{detour} Want me to add a stop?"
        s = Suggestion.new(skill=self.name, kind="food", subject="food_offer", bucket=bucket(ctx),
                           actions=[Action(path=P_WAYPOINT, value={"name": offer["place"], "lat": offer["lat"], "lon": offer["lon"]},
                                           label=(f"saved {offer['place']} as your next stop after we arrive" if after_trip
                                                  else f"added a stop at {offer['place']}"), confirmed=True)],
                           payload={"offer": offer})
        return Reply(text=text, skill=self.name, trace=trace, suggestion=s, proactive=True, data={"offer": offer})

    def alternative(self, ctx: ContextSnapshot, b: str, blocked: list, trace: list[str], proactive=False) -> Reply:
        reasons = human_join(sorted({r for d in blocked for r in d.reasons}))
        music = ctx.user.preferences.get("music_night_out", "Saturday Night Drive")
        text = f"I'd love to go sporty, but not tonight: {reasons}. How about your {music} playlist with the chassis kept relaxed?"
        s = Suggestion.new(skill=self.name, kind="alternative", subject="sport_alternative", bucket=b,
                           actions=[Action(path=P_MUSIC, value=music, label=f"put on {music}", confirmed=True),
                                    Action(path=P_LIGHT, value="VIBRANT", label="turned the ambient light up", confirmed=True)])
        trace.append("offering a safe alternative instead")
        return Reply(text=text, skill=self.name, trace=trace, suggestion=s, proactive=proactive,
                     blocked=[{"label": d.action.label, "reasons": d.reasons} for d in blocked])

    # ---- direct requests ---------------------------------------------------
    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        b = bucket(ctx)
        if intent.name == "hungry":
            r = self.food_offer(ctx, svc, ["driver said they're hungry"])
            return r or Reply(text="I'd normally suggest a spot, but you've switched food offers off. Want me to find any restaurant instead?",
                              skill=self.name)
        mode = intent.slots.get("mode", "normal")
        prefs = ctx.user.preferences
        builders: dict[str, Callable[[], list[Action]]] = {
            "sport": lambda: sport_actions(prefs), "comfort": lambda: comfort_actions(prefs), "normal": normal_actions,
        }
        actions = [a.model_copy(update={"confirmed": True}) for a in builders[mode]()]
        trace = [f"driver asked for {mode} mode", f"context bucket: {b}"]
        if mode == "sport":
            blocked = [d for a in actions if not (d := safety.check(a, ctx)).allowed]
            if blocked:
                trace.append("safety gate: " + "; ".join(r for d in blocked for r in d.reasons))
                return self.alternative(ctx, b, blocked, trace)
        applied, blocked, undo = svc.apply(actions, ctx)
        subject = {"sport": "sport_night", "comfort": "comfort_wind_down"}.get(mode)
        if subject:
            self.record(svc, ctx, subject, b, "accepted")
            trace.append(f"remembered: {subject} accepted in this context")
        notes = [n for a in applied for n in a["notes"]]
        text = say("applied", what=human_join([a["label"] for a in applied]))
        if notes:
            text += f" ({human_join(notes)})"
        s = Suggestion.new(skill=self.name, kind="profile", subject=subject or "normal", bucket=b, applied=True, undo=undo)
        return Reply(text=text, skill=self.name, trace=trace, applied=applied, blocked=blocked, suggestion=s)

    # ---- feedback ----------------------------------------------------------
    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        trace = [f"feedback on {s.subject}: {'accepted' if accepted else 'declined'}"]
        if s.kind == "profile" and s.applied:
            if not accepted:
                svc.revert(s.undo)
                if s.subject in PROFILES:
                    self.record(svc, ctx, s.subject, s.bucket, "declined")
                    trace.append("reverted and remembered as declined")
                return Reply(text=say("undone"), skill=self.name, trace=trace)
            return Reply(text="Great, enjoy the ride.", skill=self.name, trace=trace)

        if s.subject in PROFILES or s.kind == "food":
            self.record(svc, ctx, s.subject, s.bucket, "accepted" if accepted else "declined")
        if not accepted:
            return Reply(text=say("declined"), skill=self.name, trace=trace)
        applied, blocked, undo = svc.apply(s.actions, ctx)
        if blocked and not applied:
            reasons = human_join(sorted({r for b in blocked for r in b["reasons"]}))
            return Reply(text=say("blocked", reasons=reasons), skill=self.name, trace=trace, blocked=blocked)
        follow = Suggestion.new(skill=self.name, kind="profile", subject="manual", applied=True, undo=undo)
        return Reply(text=say("applied", what=human_join([a["label"] for a in applied])), skill=self.name,
                     trace=trace, applied=applied, blocked=blocked, suggestion=follow if s.kind != "food" else None)
