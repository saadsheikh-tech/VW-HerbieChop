"""Intent recognition: fast rule-based parser first, LLM only for what it can't parse.

The rule parser runs in microseconds with no network, which is what an in-car edge
fallback needs. When it returns "unknown" and the cloud is reachable, the LLM
classifies the utterance into the same intent set (and may answer small talk).
"""
from __future__ import annotations

import re

from pydantic import BaseModel

INTENTS = [
    "identify_poi", "confirm", "decline", "undo", "request_mode", "join_meeting", "end_meeting",
    "vehicle_health", "book_service", "speed_query", "hungry", "navigate", "stop_driving", "where_am_i",
    "greeting", "chat",
]

DIRECTIONS = {"left": -90.0, "right": 90.0, "ahead": 0.0, "front": 0.0, "behind": 180.0}

WAKE = re.compile(r"^\s*(hey|hi|ok|okay)?\s*,?\s*herbie\b[\s,!.?]*", re.I)


class Intent(BaseModel):
    name: str
    slots: dict = {}
    source: str = "rules"  # rules | llm
    reply: str | None = None  # LLM small-talk answer for name == "chat"


_RULES: list[tuple[str, re.Pattern]] = [
    ("undo", re.compile(r"\b(undo|put it back|change it back|revert|back to normal|turn that off)\b")),
    ("end_meeting", re.compile(r"\b(end|leave|hang up|stop|finish)\b.*\b(meeting|call|stand-?up|recording)\b")),
    ("join_meeting", re.compile(r"\b(join|dial into|start|connect)\b.*\b(meeting|call|stand-?up|teams|meet)\b")),
    ("look_at_me", re.compile(r"\b(how do i look|look at me|can you see me|what do you see)\b")),
    ("music_dislike", re.compile(r"\b(i (don'?t|do not) (really )?(like|love|enjoy) (this|it|that)|not (a fan|into this|my thing)|hate (this|it)|this song (is bad|sucks))")),
    ("music_like", re.compile(r"\b(i (really )?(like|love|enjoy) (this|it|that)|great (song|track)|more (songs? )?like this|what a (song|banger))\b")),
    ("music_next", re.compile(r"\b(next (song|track)|skip( this)?( song| track)?|play something else|change the (song|music))\b")),
    ("music_recommend", re.compile(r"\b(recommend|suggest)\b.*\b(music|songs?|tracks?|something)\b|\bshow me (similar|more) (songs|music|tracks)\b")),
    ("music_now", re.compile(r"\b(what('?s| is) (this song|playing|this track)|who (is this|sings this))\b")),
    ("music_calm", re.compile(r"\b((calm|calming|relaxing|chill|soft) (music|songs)|something (calm|relaxing|chill))\b")),
    ("music_party", re.compile(r"\b((party|upbeat|dance) (music|songs)|turn it up|pump it up)\b")),
    ("fuel_query", re.compile(r"\b(how much (fuel|gas|petrol)|fuel level|what'?s my range|how far can (we|i) (go|drive))\b")),
    ("find_fuel", re.compile(r"\b(need (fuel|gas|petrol)|(gas|petrol|fuel) station|fill (up|her up)|refuel)\b")),
    ("coffee", re.compile(r"\b(coffee|starbucks|latte|cappuccino|espresso)\b")),
    ("stop_driving", re.compile(r"\b(stop (the car|driving|here)|pull over|park here)\b")),
    ("where_am_i", re.compile(r"\b(where are we|where am i|what street|which street)\b")),
    ("request_mode", re.compile(r"\b(sport|sports|comfort|relax|chill|normal|eco) mode\b")),
    ("navigate", re.compile(r"\b(drive|take|bring|navigate|head|go)\b( me| us)? (to|towards) (?P<dest>.+)"
                            r"|\b(start|begin|continue|resume)\b.*\b(journey|tour|drive|route|driving)\b|\bkeep (going|driving)\b")),
    ("identify_poi", re.compile(r"\b(what('?s| is) (that|this)|which (building|place)|what am i looking at|tell me about (that|this))\b")),
    ("book_service", re.compile(r"\b(book|schedule|make)\b.*\b(service|workshop|appointment|repair|garage)\b")),
    ("calendar_query", re.compile(r"\b(meetings?|calendar|schedule|appointments?|what'?s (on )?(my day|today))\b")),
    ("vehicle_health", re.compile(r"\b(how are you( doing| feeling)?|any (problems|issues|faults)|what'?s wrong|check (the car|yourself)|warning light|your health)\b")),
    ("speed_query", re.compile(r"\bhow fast\b|\bmy speed\b")),
    ("hungry", re.compile(r"\b(i'?m (hungry|starving)|get (some )?food|something to eat)\b")),
    ("request_mode", re.compile(r"\b(sport|sports|comfort|relax|chill|normal|eco) mode\b|\bi'?m (so )?(tired|exhausted)\b|\bmake it (sporty|comfy|cozy)\b")),
    ("confirm", re.compile(r"^(yes|yeah|yep|sure|ok|okay|do it|go ahead|please do|sounds good|let'?s go|keep it)\b")),
    ("decline", re.compile(r"^(no|nope|nah|not now|don'?t|cancel|maybe later|no thanks)\b")),
    ("greeting", re.compile(r"^(hey|hi|hello|good (morning|evening))\b.{0,12}$")),
]


def strip_wake_word(text: str) -> str:
    return WAKE.sub("", text).strip()


def parse(text: str) -> Intent:
    raw = text.strip()
    t = strip_wake_word(raw).lower() or raw.lower()
    for name, pattern in _RULES:
        if pattern.search(t):
            return Intent(name=name, slots=_slots(name, t))
    if re.fullmatch(r"(hey|hi|hello)?\s*,?\s*herbie[!.?]*", raw.lower()):
        return Intent(name="greeting")
    return Intent(name="unknown", slots={"text": t})


def _slots(name: str, t: str) -> dict:
    slots: dict = {}
    if name == "identify_poi":
        for word, offset in DIRECTIONS.items():
            if re.search(rf"\b{word}\b", t):
                slots["pointing_offset_deg"] = offset
                break
    if name == "navigate":
        m = re.search(r"\b(?:drive|take|bring|navigate|head|go)\b(?: me| us)? (?:to|towards) (.+)", t)
        slots["destination"] = m.group(1).strip(" .!?") if m else ""
    if name == "request_mode":
        if re.search(r"\bsports?\b|sporty", t):
            slots["mode"] = "sport"
        elif re.search(r"comfort|relax|chill|comfy|cozy|tired|exhausted", t):
            slots["mode"] = "comfort"
        else:
            slots["mode"] = "normal"
    return slots


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "direction": {"type": "string", "enum": ["none", "left", "right", "ahead", "behind"]},
        "mode": {"type": "string", "enum": ["none", "sport", "comfort", "normal"]},
        "destination": {"type": "string", "description": "place name for navigate, else empty"},
        "reply": {"type": "string", "description": "Herbie's spoken answer, only used when intent is chat"},
    },
    "required": ["intent", "direction", "mode", "destination", "reply"],
    "additionalProperties": False,
}

LLM_INSTRUCTION = """Classify the driver's utterance into one intent:
identify_poi (asks what a building/place outside is), confirm, decline, undo (revert last change),
request_mode (sport/comfort/normal driving mode), join_meeting, end_meeting, vehicle_health (car condition/faults),
book_service, speed_query, hungry (wants food), navigate (drive somewhere: put the place in 'destination'),
stop_driving, where_am_i, music_like, music_dislike, music_next, music_recommend, music_now, music_calm, music_party,
fuel_query, find_fuel, coffee, look_at_me (driver asks how they look / what you see), calendar_query,
greeting, chat (anything else: answer it in 'reply').
Utterance: """


def parse_with_llm(llm, persona: str, text: str, context_summary: str) -> Intent | None:
    out = llm.complete(persona, f"CONTEXT: {context_summary}\n\n{LLM_INSTRUCTION}{text!r}", schema=LLM_SCHEMA)
    if not isinstance(out, dict) or out.get("intent") not in INTENTS:
        return None
    slots: dict = {}
    if out.get("direction") in DIRECTIONS:
        slots["pointing_offset_deg"] = DIRECTIONS[out["direction"]]
    if out["intent"] == "navigate":
        slots["destination"] = out.get("destination", "")
    if out.get("mode") in ("sport", "comfort", "normal"):
        slots["mode"] = out["mode"]
    return Intent(name=out["intent"], slots=slots, source="llm", reply=out.get("reply") or None)
