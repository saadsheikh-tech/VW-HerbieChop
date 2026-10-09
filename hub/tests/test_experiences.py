"""Experiences, trip planner, music taste, driver mood and Google Calendar import."""
from datetime import date, datetime

import pytest

import herbie.orchestrator as orch
from herbie import calendar_sync, emotion
from herbie.llm import LLM
from herbie.memory import Memory
from herbie.orchestrator import Orchestrator
from herbie.sim import World


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(orch.time, "monotonic", clock)
    w = World()
    brain = Orchestrator(w, Memory(":memory:"), LLM())
    brain.director.clock = clock
    return w, brain, clock


def play(w, brain, clock, exp, answers=(), max_steps=20000, scale=8.0):
    """Run an experience; answer each question ~3 s after it's asked. Returns Herbie's lines."""
    brain.director.start(exp)
    w.time_scale = scale
    answers, said = list(answers), []
    for _ in range(max_steps):
        clock.t += 0.25
        for ev, payload in w.tick(0.25):
            said += [r.text for r in brain.event(ev, payload)]
        for r in brain.director.tick():
            said.append(r.text)
        pending = [sid for sid, s in brain.open.items() if not s.applied]
        if pending and clock.t - max(brain.open_times[sid] for sid in pending) > 3:
            r = brain.feedback(pending[-1], answers.pop(0) if answers else True)
            said.append(r.text)
        if "arrival" in brain.director.fired:
            break
    return said


def test_drive_to_work_full_story(rig):
    w, brain, clock = rig
    said = play(w, brain, clock, "drive_to_work", answers=[True, False, True, True, True])
    text = " | ".join(said)
    assert "welcome back" in said[0].lower()
    assert "service due in one week" in text and "Volkswagen" in text and "tomorrow" in text
    assert "morning coffee" in text and "Pariser Platz" in text
    assert "German class" in text and "keep a summary" in text
    assert "run low on your way back" in text and "rush hour" in text and "Holzmarktstraße" in text
    assert "Full tank" in text
    assert "write ten sentences" in text  # class summary at arrival (on-device fallback)
    assert w.drive_mode == "parked" and w.get("Vehicle.Powertrain.FuelSystem.RelativeLevel") > 90
    order = ["self_check", "coffee", "calendar", "depart", "fuel_plan", "arrival"]
    assert all(b in brain.director.fired for b in order)


def test_one_question_at_a_time(rig):
    w, brain, clock = rig
    brain.director.start("drive_to_work")
    for _ in range(400):  # nobody answers for 100 s
        clock.t += 0.25
        brain.director.tick()
        assert sum(1 for s in brain.open.values() if not s.applied) <= 1 or clock.t > 35
    assert "coffee" not in brain.director.fired or clock.t >= 35


def test_drive_home_remembers_the_morning(rig):
    w, brain, clock = rig
    play(w, brain, clock, "drive_to_work", answers=[True, True, True, False])
    said = play(w, brain, clock, "drive_home", answers=[True, True, True])
    text = " | ".join(said)
    assert "service is booked for Thursday" in text
    assert "you said you'd" in text or "this morning" in text
    assert "Fuel's getting low" in text


def test_friday_night_offers_sport_after_destination(rig):
    w, brain, clock = rig
    said = play(w, brain, clock, "friday_night", answers=[True, True])
    text = " | ".join(said)
    assert "RAW-Gelände" in text and "Friday night with the crew" in text and "taxi" in text
    assert w.get("Vehicle.Powertrain.Transmission.PerformanceMode") == "SPORT"


def test_music_taste_is_remembered(rig):
    w, brain, clock = rig
    brain.director.start("friday_night")
    w.tick(0.25)  # playlist_changed → first track
    for e, p in [("playlist_changed", {})]:
        brain.event(e, p)
    genre = w.get("Vehicle.Cabin.Infotainment.Media.Track")["genre"]
    r = brain.utterance("Hmm, I don't like it that much but I'd like to see more music you could recommend")[0]
    assert r.skill == "music" and r.data["recommendations"]
    assert genre in brain.memory.get_preferences("saad")["disliked_genres"]
    assert w.get("Vehicle.Cabin.Infotainment.Media.Track")["genre"] != genre


@pytest.mark.parametrize("shape,mood", [
    ({"mouthSmileLeft": 0.8, "mouthSmileRight": 0.7}, "happy"),
    ({"browDownLeft": 0.6, "browDownRight": 0.5}, "tense"),
    ({"eyeBlinkLeft": 0.9, "eyeBlinkRight": 0.9}, "tired"),
    ({"browInnerUp": 0.6, "mouthFrownLeft": 0.3, "mouthFrownRight": 0.3}, "sad"),
    ({}, "neutral"),
])
def test_classify(shape, mood):
    assert emotion.classify(shape) == mood


def test_mood_needs_to_be_stable_and_has_cooldown():
    clock = Clock()
    t = emotion.MoodTracker(clock=clock)
    tense = {"browDownLeft": 0.6, "browDownRight": 0.6}
    reacts = []
    for i in range(10):
        clock.t += 1.5
        reacts.append(t.update(tense if i % 3 else {})[2])
    assert reacts.count("tense") == 1  # reacted once, after it became the dominant mood
    for _ in range(10):  # neutral again, then tense again within the cooldown: no second remark
        clock.t += 1.5
        t.update({})
    assert all(t.update(tense)[2] is None for _ in range(10))


def test_tense_driver_gets_calming_offer(rig):
    w, brain, clock = rig
    brain.director.start("drive_to_work")
    r = brain.event("mood", {"mood": "tense"})[0]
    assert "calming" in r.text and "visually" not in r.text.lower()
    brain.feedback(r.suggestion.id, True)
    assert w.get("Vehicle.Cabin.Infotainment.Media.Playlist") == "Calm Drive"


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART;TZID=Europe/Berlin:20261012T110000
DTEND;TZID=Europe/Berlin:20261012T114500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
SUMMARY:Meeting with Joshua
DESCRIPTION:Join: https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART:20261014T060000Z
DTEND:20261014T064500Z
SUMMARY:German class
END:VEVENT
END:VCALENDAR
"""


def test_google_calendar_ics_with_recurrence_and_timezones():
    evs = calendar_sync.events_on(ICS, date(2026, 10, 14))  # a Wednesday
    assert [(e["title"], e["start"].strftime("%H:%M")) for e in evs] == [("German class", "08:00"), ("Meeting with Joshua", "11:00")]
    assert evs[1]["link"] == "https://meet.google.com/abc-defg-hij"
    mem = Memory(":memory:")
    calendar_sync.map_onto(mem, "saad", evs, datetime(2026, 1, 7, 7, 40))
    day = mem.events_between("saad", datetime(2026, 1, 7), datetime(2026, 1, 8))
    assert [e["title"] for e in day] == ["German class", "Meeting with Joshua"]


def test_experience_and_camera_api(client):
    assert {e["id"] for e in client.get("/api/experiences").json()} == {"drive_to_work", "drive_home", "friday_night"}
    assert client.post("/api/experience/drive_to_work").status_code == 200
    st = client.get("/api/state").json()
    assert st["world"]["experience"] == "drive_to_work" and st["world"]["drive"]["mode"] == "parked"
    assert client.post("/api/driver/face", json={"blendshapes": {"mouthSmileLeft": 0.9}}).status_code == 409
    client.post("/api/camera", json={"on": True})
    r = client.post("/api/driver/face", json={"blendshapes": {"mouthSmileLeft": 0.9, "mouthSmileRight": 0.9}}).json()
    assert r["mood"] in ("unknown", "happy")
    assert client.post("/api/camera/snapshot", json={"image": "not base64!!"}).status_code == 422
    assert client.post("/api/experience/nope").status_code == 404
