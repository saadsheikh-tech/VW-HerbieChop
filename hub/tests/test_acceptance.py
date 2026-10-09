"""Acceptance criteria AC1-AC10 from PLAN.md, verified end-to-end through the HTTP API."""
import math
from datetime import datetime

MODE = "Vehicle.Powertrain.Transmission.PerformanceMode"
DAMP = "Vehicle.Chassis.Suspension.DampingMode"


def test_ac1_point_left_names_the_church(run, say):
    run("point_left_church")
    r = say("Hey Herbie, what's that building over there?")
    assert r["data"]["poi"]["id"] == "kwg"
    assert "Kaiser Wilhelm Memorial Church" in r["text"]
    assert any("bearing 0°" in t for t in r["trace"])
    assert r["data"]["latency_ms"] < 3000


def test_ac1_direction_word_overrides_missing_gesture(client, run, say):
    run("point_left_church")
    client.post("/api/gesture", json={"offset_deg": None})
    r = say("what's that on my left?")
    assert r["data"]["poi"]["id"] == "kwg"


def test_ac1_story_follow_up(run, say):
    run("point_left_church")
    say("what's that over there?")
    assert "hollow tooth" in say("yes")["text"]


def test_ac2_ambiguous_asks_then_resolves(run, say):
    run("museum_island_ambiguous")
    r = say("What's that over there?")
    assert r["clarify"] == ["Berlin Cathedral", "Altes Museum"]
    assert "poi" not in r["data"]
    r2 = say("the museum")
    assert r2["data"]["poi"]["id"] == "altes_museum"


def test_ac3_comfort_applied_automatically_with_reasons(run, signal):
    replies = run("after_work_tired")
    comfort = next(r for r in replies if r["suggestion"] and r["suggestion"]["subject"] == "comfort_wind_down")
    assert comfort["suggestion"]["applied"] is True
    assert any("learned score" in t for t in comfort["trace"])
    assert signal(DAMP) == "SOFT"
    food = next(r for r in replies if r["suggestion"] and r["suggestion"]["kind"] == "food")
    assert "(sponsored)" in food["text"]


def test_ac3_undo_restores_previous_settings(run, say, signal):
    run("after_work_tired")
    say("undo")
    assert signal(DAMP) == "NORMAL"


def test_ac4_sport_refused_on_wet_road(run, say, signal):
    run("saturday_night_rain")
    r = say("Herbie, sport mode!")
    assert r["blocked"] and "wet" in r["blocked"][0]["reasons"][0]
    assert r["suggestion"]["kind"] == "alternative"
    assert signal(MODE) == "NORMAL"
    say("yes")
    assert signal("Vehicle.Cabin.Infotainment.Media.Playlist") == "Saturday Night Drive"
    assert signal(MODE) == "NORMAL"


def test_sport_is_offered_never_automatic(client, run, signal):
    replies = run("saturday_night")
    sport = next(r for r in replies if r["suggestion"] and r["suggestion"]["subject"] == "sport_night")
    assert sport["suggestion"]["applied"] is False
    assert signal(MODE) == "NORMAL"
    client.post("/api/feedback", json={"id": sport["suggestion"]["id"], "accept": True})
    assert signal(MODE) == "SPORT"


def comfort_of(replies):
    return next((r for r in replies if r["suggestion"] and r["suggestion"]["subject"] == "comfort_wind_down"), None)


def test_ac5_three_declines_mute_the_profile(client, run, signal):
    applied_flags = []
    for _ in range(3):
        c = comfort_of(run("after_work_tired"))
        applied_flags.append(c["suggestion"]["applied"])
        client.post("/api/feedback", json={"id": c["suggestion"]["id"], "accept": False})
    # Confidence drops as declines accumulate: Herbie stops acting alone before it goes quiet.
    assert applied_flags == [True, True, False]
    replies = run("after_work_tired")
    assert signal(DAMP) == "NORMAL"
    assert not any(r["suggestion"] and r["suggestion"]["subject"] == "comfort_wind_down" for r in replies)
    assert any(any("muted" in t for t in r["trace"]) for r in replies)


def test_ac6_booking_fits_calendar(client, run, say):
    replies = run("engine_misfire")
    s = replies[0]["suggestion"]
    assert s["kind"] == "booking"
    slot = datetime.fromisoformat(s["payload"]["slot"])
    r = say("yes")
    assert "Booked" in r["text"]
    mem = client.app.state.memory
    events = mem.events_between("saad", slot, slot.replace(hour=23))
    service = [e for e in events if e["kind"] == "service"]
    assert len(service) == 1
    others = [e for e in events if e["kind"] != "service"]
    assert all(not (e["start"] < service[0]["end"] and service[0]["start"] < e["end"]) for e in others)


def test_ac7_stop_now_warns_without_booking(run):
    replies = run("brake_failure")
    assert len(replies) == 1
    assert "safe place to stop" in replies[0]["text"]
    assert replies[0]["suggestion"] is None


def test_ac8_no_recording_without_consent(run, say, signal):
    run("morning_meeting")
    r = say("Hey Herbie, join my meeting")
    assert r["blocked"] and r["blocked"][0]["path"].endswith("VideoActive")
    assert signal("Vehicle.Cabin.Infotainment.Meeting.AudioActive") is True
    say("no")
    assert signal("Vehicle.Cabin.Infotainment.Meeting.Recording") is False
    assert "Nothing was recorded" in say("end the meeting")["text"]


def test_ac8_summary_with_consent(run, say):
    run("morning_meeting")
    say("join my meeting")
    say("yes")
    r = say("end the meeting")
    owners = {a["owner"] for a in r["data"]["notes"]["action_items"]}
    assert {"Saad", "Jonas"} <= owners
    assert "Your to-do" in r["text"]


def test_ac9_offline_keeps_basic_functions(run, say, signal):
    run("offline")
    r = say("what's that on my left?")
    assert "offline" in r["text"] and r["data"]["poi"]["id"] == "kwg"
    say("comfort mode")
    assert signal(DAMP) == "SOFT"
    assert "feeling great" in say("how are you doing?")["text"]


def test_ac10_no_gps_is_honest(run, say):
    run("tunnel_no_gps")
    r = say("what's that on my left?")
    assert "GPS" in r["text"] and "poi" not in r["data"]


def test_ac10_corrupt_speed_signal(client, run, say, signal):
    run("saturday_night")
    client.app.state.world.signals["Vehicle.Speed"] = math.nan
    state = client.get("/api/state").json()
    assert state["context"]["quality"]["speed"] == "invalid"
    r = say("sport mode")
    assert any("speed signal" in reason for b in r["blocked"] for reason in b["reasons"])
    assert signal(MODE) == "NORMAL"
    assert "unreliable" in say("how fast am I going?")["text"]


def test_ac10_malformed_api_input_is_rejected(client):
    assert client.post("/api/say", json={"text": ""}).status_code == 422
    assert client.post("/api/say", json={}).status_code == 422
    assert client.post("/api/dtc", json={"code": "DROP TABLE"}).status_code == 422
    assert client.post("/api/gesture", json={"offset_deg": 999}).status_code == 422
    assert client.post("/api/sim", json={"fatigue": 7}).status_code == 422
    assert client.post("/api/scenario/nope").status_code == 404
    assert client.post("/api/feedback", json={"id": "gone", "accept": True}).status_code == 404


def test_unknown_dtc_is_handled(client):
    r = client.post("/api/dtc", json={"code": "U1234"}).json()["replies"][0]
    assert r["suggestion"]["kind"] == "booking"


def test_unknown_utterance_without_llm(run, say):
    run("point_left_church")
    r = say("sing me a song about potatoes")
    assert r["skill"] == "orchestrator" and "LLM unavailable" in r["trace"][0]
