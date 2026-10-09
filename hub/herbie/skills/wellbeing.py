"""Emotional intelligence: react to the driver's mood, and look when asked.

Mood comes from the driver camera (see emotion.py). Herbie never comments on how the
driver *looks*; it responds to how they seem to feel, with an offer they can decline:

  tense / sad → calming music and a smoother ride
  happy (music on) → "enjoying this one? want more like it?"
  tired → cooler cabin + coffee stop offer (and it feeds the safety gate's fatigue rule)

"How do I look?" sends exactly one camera frame to the vision model, only on request.
"""
from __future__ import annotations

from ..context import ContextSnapshot
from ..personality import PERSONA
from ..safety import Action
from .base import Reply, Services, Skill, Suggestion

P_PLAYLIST = "Vehicle.Cabin.Infotainment.Media.Playlist"


class Wellbeing(Skill):
    name = "wellbeing"
    intents = {"look_at_me"}
    events = {"mood"}

    def __init__(self, music, planner) -> None:
        self.music, self.planner = music, planner

    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        mood = payload["mood"]
        trace = [f"camera mood stable for ~15 s: {mood}"]
        if mood in ("tense", "sad"):
            dest = "to work" if ctx.trip.kind == "home->work" else "home" if ctx.trip.kind == "work->home" else ""
            text = ("Rough morning? " if ctx.environment.time_band == "morning" else "Long day? ") + \
                   f"Want me to put on something calming and keep the ride{' ' + dest if dest else ''} nice and smooth?"
            s = Suggestion.new(skill=self.name, kind="mood", subject="calm_down", payload={"mood": mood})
            return [Reply(text=text, skill=self.name, trace=trace, suggestion=s, proactive=True)]
        if mood == "happy":
            t = svc.world.get("Vehicle.Cabin.Infotainment.Media.Track")
            if not t:
                return []
            s = Suggestion.new(skill="music", kind="music", subject="more_like_this")
            return [Reply(text=f"You seem to be enjoying {t['title']}! Do you like this kind of {t['genre']}? "
                               "Want me to line up more like it?",
                          skill=self.name, trace=trace + [f"current track genre: {t['genre']}"], suggestion=s, proactive=True)]
        if mood == "tired":
            svc.world.fatigue = max(svc.world.fatigue, 0.7)
            s = Suggestion.new(skill=self.name, kind="mood", subject="tired")
            return [Reply(text="You seem a bit tired. Shall I cool the cabin down and find a coffee stop on the way?",
                          skill=self.name, trace=trace + ["fatigue raised to ≥ 0.7 (sport mode now blocked)"],
                          suggestion=s, proactive=True)]
        return []

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        if not accepted:
            return Reply(text="Okay, I'm here if you need me.", skill=self.name)
        if s.subject == "calm_down":
            applied, _, _ = svc.apply([
                Action(path=P_PLAYLIST, value="Calm Drive", label="put on Calm Drive"),
                Action(path="Vehicle.Chassis.Suspension.DampingMode", value="SOFT", label="softened the suspension"),
                Action(path="Vehicle.Cabin.Light.AmbientMood", value="WARM", label="warmed the lights"),
            ], ctx)
            t = self.music.set_genres(self.music.playlists["Calm Drive"], svc, ctx)
            svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "mood_support", "calm_down", None, "accepted")
            return Reply(text=f"There we go: {t['title']} by {t['artist']}, softer suspension and warmer light. Breathe out.",
                         skill=self.name, applied=applied)
        applied, _, _ = svc.apply([Action(path="Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", value=19.0,
                                          label="cooled the cabin to 19°C")], ctx)
        coffee = self.planner.offer_coffee(ctx, svc, ["tired → coffee stop"], ask=False)
        text = "Cooled the cabin to 19 degrees. " + (coffee.text if coffee else "No café on our route, but I'll keep an eye on you.")
        return Reply(text=text, skill=self.name, applied=applied, suggestion=coffee.suggestion if coffee else None)

    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        if not svc.world.camera_on:
            return Reply(text="My camera's off. Turn it on with the camera button and ask me again.", skill=self.name)
        if not ctx.system.llm_available:
            return Reply(text="I need my cloud brain for a proper look, and it's not reachable right now.", skill=self.name)
        return Reply(text="Let me have a look…", skill=self.name, data={"request_snapshot": True},
                     trace=["asking the cockpit for exactly one camera frame"])

    def describe(self, jpeg: bytes, question: str, ctx: ContextSnapshot, svc: Services) -> Reply:
        prompt = (f"The driver asked: {question!r}. You are looking at one frame from the in-car driver camera. "
                  "Answer warmly in one or two short spoken sentences about their apparent mood or energy and offer "
                  "something helpful. Never comment on attractiveness, body, age, ethnicity or identity.")
        text = svc.llm.vision(PERSONA, prompt, jpeg)
        if not text:
            return Reply(text="Hmm, I couldn't get a good look just now. Try again in a moment?", skill=self.name,
                         trace=[f"vision call failed: {svc.llm.last_error}"])
        return Reply(text=text, skill=self.name, trace=["one frame sent to the vision model; not stored"])
