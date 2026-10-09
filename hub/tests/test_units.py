import pytest

from herbie import geo, intents, safety
from herbie.context import ContextEngine
from herbie.memory import Memory
from herbie.safety import Action
from herbie.sim import World


def test_bearing_and_angle_diff():
    assert geo.bearing_deg(52.5, 13.4, 52.51, 13.4) == pytest.approx(0, abs=0.01)
    assert geo.bearing_deg(52.5, 13.4, 52.5, 13.41) == pytest.approx(90, abs=0.1)
    assert geo.angle_diff(350, 10) == -20
    assert geo.angle_diff(10, 350) == 20
    assert geo.normalize(90 - 90) == 0


def test_destination_roundtrip():
    lat, lon = geo.destination(52.5, 13.4, 90, 1000)
    assert geo.haversine_m(52.5, 13.4, lat, lon) == pytest.approx(1000, rel=1e-3)


@pytest.mark.parametrize("text,name,slots", [
    ("Hey Herbie, what's that building over there?", "identify_poi", {}),
    ("what is that on the right", "identify_poi", {"pointing_offset_deg": 90.0}),
    ("Herbie, sport mode!", "request_mode", {"mode": "sport"}),
    ("I'm so tired", "request_mode", {"mode": "comfort"}),
    ("no, put it back", "undo", {}),
    ("join my teams call", "join_meeting", {}),
    ("end the meeting", "end_meeting", {}),
    ("book a service appointment", "book_service", {}),
    ("yes please", "confirm", {}),
    ("hey herbie", "greeting", {}),
    ("what's the meaning of life", "unknown", {"text": "what's the meaning of life"}),
])
def test_rule_intents(text, name, slots):
    i = intents.parse(text)
    assert i.name == name
    assert i.slots == slots


def ctx_for(**world_kw):
    w = World(**world_kw)
    w.signals["Vehicle.Speed"] = 50.0  # moving
    return ContextEngine(w, Memory(":memory:"), lambda: False).snapshot()


def test_gate_clamps_recline_while_moving():
    d = safety.check(Action(path="Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline", value=45, label="x"), ctx_for())
    assert d.allowed and d.final_value == safety.MAX_RECLINE_WHILE_MOVING


def test_gate_clamps_temperature():
    d = safety.check(Action(path="Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", value=35, label="x"), ctx_for())
    assert d.final_value == 28.0


def test_gate_requires_confirmation():
    a = Action(path="Vehicle.Powertrain.Transmission.PerformanceMode", value="SPORT", label="x", requires_confirmation=True)
    assert not safety.check(a, ctx_for()).allowed
    assert safety.check(a.model_copy(update={"confirmed": True}), ctx_for()).allowed


def test_gate_blocks_sport_when_tired_or_in_school_zone():
    a = Action(path="Vehicle.Powertrain.Transmission.PerformanceMode", value="SPORT", label="x", confirmed=True)
    assert not safety.check(a, ctx_for(fatigue=0.9)).allowed
    d = safety.check(a, ctx_for(zone="school_zone", speed_limit=30))
    assert not d.allowed and "school zone" in d.reasons[0]


def test_context_flags_missing_gps():
    w = World(gps_ok=False)
    snap = ContextEngine(w, Memory(":memory:"), lambda: True).snapshot()
    assert snap.quality["gps"] == "missing" and snap.vehicle.lat is None


def test_context_marks_llm_unavailable_when_offline():
    snap = ContextEngine(World(cloud_online=False), Memory(":memory:"), lambda: True).snapshot()
    assert snap.system.llm_available is False
