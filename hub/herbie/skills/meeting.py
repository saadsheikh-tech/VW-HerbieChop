"""S4: Work / study assistant. Join a call from the car, record with consent, summarise.

Prototype boundary (documented, not hidden): the meeting runs on the phone, the car
provides audio, and speech-to-text is simulated with a recorded transcript. The
consent and safety logic, and the summarisation, are real.

Rules:
  * audio only while moving: video goes through the safety gate and is blocked
  * nothing is recorded until the driver confirms everyone has been told
    (secretly recording a private conversation is a criminal offence in Germany, §201 StGB)
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..context import ContextSnapshot
from ..safety import Action
from .base import DATA_DIR, Reply, Services, Skill, Suggestion

P_AUDIO = "Vehicle.Cabin.Infotainment.Meeting.AudioActive"
P_VIDEO = "Vehicle.Cabin.Infotainment.Meeting.VideoActive"
P_REC = "Vehicle.Cabin.Infotainment.Meeting.Recording"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "task": {"type": "string"}},
            "required": ["owner", "task"], "additionalProperties": False}},
    },
    "required": ["summary", "decisions", "action_items"],
    "additionalProperties": False,
}


def heuristic_summary(transcript: str) -> dict:
    """Edge fallback: cheap pattern-based extraction when no LLM is reachable."""
    lines = [m for m in (re.match(r"^([^:\[]{1,30}):\s*(.+)$", l) for l in transcript.splitlines()) if m]
    speakers = sorted({m.group(1).strip() for m in lines})
    decisions, actions, topic = [], [], None
    for m in lines:
        who, said = m.group(1).strip(), m.group(2).strip()
        if topic is None and re.search(r"\btoday\b", said, re.I):
            topic = next(x for x in re.split(r"(?<=[.!?])\s+", said) if "today" in x.lower())
        if re.search(r"\b(decided|agreed)\b", said, re.I):
            decisions.append(said)
        hw = re.search(r"\bhomework:\s*(.+)", said, re.I)
        if hw:
            actions.append({"owner": "You", "task": hw.group(1).split(". ")[0].strip(" .")})
        elif re.search(r"\b(I will|I'll)\b", said):
            task = re.split(r"\bI will\b|\bI'll\b", said, maxsplit=1)[1].strip(" .")
            actions.append({"owner": who, "task": task})
    summary = f"With {', '.join(speakers)}."
    if topic:
        summary = f"{topic.rstrip('.')}. " + summary
    return {"summary": summary, "decisions": decisions, "action_items": actions}


class MeetingAssistant(Skill):
    name = "meeting"
    intents = {"join_meeting", "end_meeting", "calendar_query"}

    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        if intent.name == "calendar_query":
            return self.agenda(ctx, svc)
        return self.join(ctx, svc) if intent.name == "join_meeting" else self.end(ctx, svc)

    def agenda(self, ctx: ContextSnapshot, svc: Services) -> Reply:
        now = ctx.environment.time
        end = now.replace(hour=23, minute=59)
        evs = [e for e in svc.memory.events_between(ctx.user.user_id, now, end) if e["kind"] != "work"]
        if not evs:
            return Reply(text="Nothing else on your calendar today.", skill=self.name)
        listing = "; ".join(f"{e['title']} at {e['start']:%H:%M}" for e in evs[:4])
        return Reply(text=f"Still to come today: {listing}.", skill=self.name,
                     trace=[f"{len(evs)} calendar events after {now:%H:%M}"])

    def join(self, ctx: ContextSnapshot, svc: Services) -> Reply:
        now = ctx.environment.time
        events = [e for e in svc.memory.events_between(ctx.user.user_id, now - timedelta(minutes=15), now + timedelta(hours=1))
                  if e["link"]]
        if not events:
            return Reply(text="I don't see a call on your calendar in the next hour.", skill=self.name,
                         trace=["no calendar event with a meeting link within the next hour"])
        ev = events[0]
        platform = ctx.user.preferences.get("meeting_platform", "your meeting app")
        trace = [f"next call: {ev['title']} at {ev['start']:%H:%M} ({ev['link']})"]
        applied, blocked, _ = svc.apply([
            Action(path=P_AUDIO, value=True, label="joined with car audio"),
            Action(path=P_VIDEO, value=True, label="camera on"),
        ], ctx)
        if blocked:
            trace.append("video: " + "; ".join(r for b in blocked for r in b["reasons"]))
        svc.world.set(P_REC, False)
        s = Suggestion.new(skill=self.name, kind="meeting", subject="recording_consent", payload={"event": ev["title"]})
        video_note = " Audio only while we're driving." if blocked else ""
        text = (f"Joining {ev['title']} on {platform} through your phone.{video_note} "
                "Want me to record it for a summary? I'll tell everyone first.")
        return Reply(text=text, skill=self.name, trace=trace, applied=applied, blocked=blocked, suggestion=s)

    def offer_join(self, ev: dict, ctx: ContextSnapshot, svc: Services, lead: str = "") -> Reply:
        """Proactive: offer to join a specific calendar event virtually and keep a summary (one consent)."""
        kind = "class" if "class" in ev["title"].lower() or "course" in ev["title"].lower() else "meeting"
        s = Suggestion.new(skill=self.name, kind="meeting", subject="join_and_record",
                           payload={"event": ev["title"], "start": ev["start"].isoformat()})
        return Reply(text=f"{lead}Your {ev['title']} starts at {ev['start']:%H:%M}. Would you like me to join it virtually, "
                          f"audio only, and keep a summary of the {kind} for you?",
                     skill=self.name, suggestion=s, proactive=True,
                     trace=[f"calendar: {ev['title']} {ev['start']:%H:%M}–{ev['end']:%H:%M} ({ev.get('link') or 'no link'})"])

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        if s.subject == "join_and_record":
            if not accepted:
                return Reply(text="Okay, I'll leave that one to you.", skill=self.name)
            svc.apply([Action(path=P_AUDIO, value=True, label="joined with car audio")], ctx)
            svc.world.set(P_REC, True)
            svc.world.meeting_title = s.payload["event"]
            return Reply(text=f"Okay, joining {s.payload['event']} in the background, audio only. I've told everyone I'm "
                              "taking notes, and you'll get the summary when we arrive.",
                         skill=self.name, trace=["joined via phone (simulated); recording announced to participants"])
        if not accepted:
            svc.world.set(P_REC, False)
            return Reply(text="Okay, no recording. I'm just your speakerphone this time.", skill=self.name,
                         trace=["consent not given: recording stays off"])
        svc.world.set(P_REC, True)
        return Reply(
            text=f"Recording. I've announced: \"Herbie, {ctx.user.name}'s assistant, is recording this call to take notes.\"",
            skill=self.name, trace=["driver confirmed; recording announcement played to all participants"],
        )

    def end(self, ctx: ContextSnapshot, svc: Services) -> Reply:
        was_recording = ctx.interaction.meeting_recording
        title = svc.world.meeting_title or ""
        if not ctx.interaction.meeting_audio:
            return Reply(text="We're not in a call right now.", skill=self.name)
        svc.world.set(P_AUDIO, False)
        svc.world.set(P_VIDEO, False)
        svc.world.set(P_REC, False)
        svc.world.meeting_title = None
        if not was_recording:
            return Reply(text="Left the call. Nothing was recorded, so there's no summary this time.", skill=self.name,
                         trace=["recording was never consented: no summary"])
        transcript_file = "sample_german_class.txt" if "german" in title.lower() else "sample_meeting.txt"
        transcript = (DATA_DIR / transcript_file).read_text()
        trace = ["transcript: simulated speech-to-text of cabin audio"]
        result = None
        if ctx.system.llm_available:
            result = svc.llm.complete(
                "You write meeting notes in English. Be factual; only use what is in the transcript. "
                "Keep the summary to two short sentences suitable for reading aloud.",
                f"Summarise this session.\n\n{transcript}", schema=SUMMARY_SCHEMA, effort="medium")
        if isinstance(result, dict):
            trace.append("summary by LLM (structured output)")
        else:
            result = heuristic_summary(transcript)
            trace.append("summary by on-device heuristic (LLM unavailable)")
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "meeting_summary", None, None, None, result)
        mine = [a["task"] for a in result["action_items"]
                if a["owner"].lower().startswith(ctx.user.name.lower()) or a["owner"].lower() in ("you", "everyone", "all")]
        spoken = f"{title or 'The call'} has ended. {result['summary']}"
        if mine:
            spoken += f" Your to-do: {mine[0]}."
        spoken += " I've saved the full notes for you."
        return Reply(text=spoken, skill=self.name, trace=trace, data={"notes": result})
