"""The Berlin landmark drive: autopilot, voice navigation, pointing while moving, manual takeover."""
from herbie import scenarios
from herbie.llm import LLM
from herbie.memory import Memory
from herbie.orchestrator import Orchestrator
from herbie.sim import World


def setup():
    w = World()
    brain = Orchestrator(w, Memory(":memory:"), LLM())
    scenarios.apply(w, "berlin_drive")
    return w, brain


def drive(w, brain, seconds, scale=8.0):
    w.time_scale = scale
    events = []
    for _ in range(int(seconds / 0.25)):
        for ev, payload in w.tick(0.25):
            events.append(ev)
            brain.event(ev, payload)
    return events


def test_autopilot_follows_the_route_and_turns():
    w, brain = setup()
    assert w.drive_mode == "autopilot"
    drive(w, brain, 30)
    d = w.public_state()["drive"]
    assert d["route_pos_m"] > 1000 and d["street"]
    assert w.get("Vehicle.Speed") > 20


def test_voice_destination_and_arrival():
    w, brain = setup()
    r = brain.utterance("Herbie, drive to the Berlin Cathedral")[0]
    assert r.skill == "navigation" and "Berlin Cathedral" in r.text
    events = drive(w, brain, 120)
    assert "arrived" in events
    assert w.drive_mode == "parked" and w.get("Vehicle.Speed") == 0
    assert any("Here we are: Berlin Cathedral" in e.get("reply", {}).get("text", "") for e in brain.log)


def test_point_left_on_unter_den_linden_while_driving():
    w, brain = setup()
    w.place_on_route(3700)  # next to Neue Wache / Zeughaus, heading east
    w.pointing_offset_deg = -90
    r = brain.utterance("Hey Herbie, what's that?")[0]
    assert r.data.get("poi", {}).get("id") in {"neue_wache", "zeughaus"} or r.clarify


def test_manual_takeover_and_resume():
    w, brain = setup()
    w.control("accelerate")
    assert w.drive_mode == "manual"
    drive(w, brain, 2, scale=1)
    r = brain.utterance("keep going")[0]
    assert w.drive_mode == "autopilot" and "Alexanderplatz" in r.text


def test_passed_destination_and_unknown_place():
    w, brain = setup()
    w.place_on_route(4500)
    assert "already passed" in brain.utterance("take me to the Reichstag")[0].text
    assert "don't know" in brain.utterance("drive to Hogwarts")[0].text


def test_autopilot_refused_without_gps():
    w, brain = setup()
    w.drive_mode = "manual"
    w.gps_ok = False
    r = brain.utterance("drive to the TV Tower")[0]
    assert r.blocked and w.drive_mode == "manual"
