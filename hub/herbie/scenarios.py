"""Scripted demo scenarios. Each sets up the mocked world and suggests what to say.

They double as acceptance tests (tests/test_scenarios.py), so the demo the jury sees
is exactly what CI verifies.
"""
from __future__ import annotations

from .sim import World, at

SCENARIOS: dict[str, dict] = {
    "berlin_drive": {
        "title": "Berlin landmark drive",
        "about": "Sunday afternoon. Herbie drives you from the Victory Column down Unter den Linden to Alexanderplatz. "
                 "Click a landmark (or the side windows) to point, then ask.",
        "time": at(6, 15, 0), "journey": "berlin", "drive_mode": "autopilot", "fatigue": 0.3, "passengers": 1,
        "script": ["Hey Herbie, what's that?", "where are we?", "drive to the TV Tower", "stop the car"],
        "acceptance": "Live journey: Herbie answers about whatever you point at while driving",
    },
    "point_left_church": {
        "title": "\"What's that building?\" (pointing left)",
        "about": "Monday morning, driving east a few blocks south of Breitscheidplatz. Point left and ask.",
        "time": at(0, 10, 30), "pos": (52.5030, 13.3352, 90.0), "speed": 30, "pointing": -90.0,
        "script": ["Hey Herbie, what's that building over there?", "yes"],
        "acceptance": "AC1: names the Kaiser Wilhelm Memorial Church",
    },
    "museum_island_ambiguous": {
        "title": "Two landmarks, one gesture",
        "about": "Pointing between Berlin Cathedral and the Altes Museum. Herbie should ask, not guess.",
        "time": at(0, 11, 0), "pos": (52.5170, 13.4000, 90.0), "speed": 20, "pointing": -92.0,
        "script": ["What's that over there?", "the cathedral"],
        "acceptance": "AC2: asks a clarifying question",
    },
    "after_work_tired": {
        "title": "Tired after work",
        "about": "Monday 18:10, leaving work, tired and hungry. Herbie knows this routine.",
        "time": at(0, 18, 10), "pos": (52.4930, 13.4460, 300.0), "speed": 20,
        "fatigue": 0.75, "hunger": 0.8, "origin": "work", "destination": "home",
        "events": [("trip_start", {})],
        "script": ["undo", "comfort mode"],
        "acceptance": "AC3: comfort profile applied automatically with reasons; AC5: three undos mute it",
    },
    "saturday_night": {
        "title": "Saturday night out",
        "about": "Saturday 23:40 with three friends, fresh, dry roads.",
        "time": at(5, 23, 40), "pos": (52.5058, 13.4497, 270.0), "speed": 25, "fatigue": 0.3, "passengers": 3,
        "events": [("trip_start", {})],
        "script": ["yes"],
        "acceptance": "Sport mode is offered, never switched on automatically",
    },
    "saturday_night_rain": {
        "title": "Saturday night, wet roads",
        "about": "Same night out, but it's raining.",
        "time": at(5, 23, 40), "pos": (52.5058, 13.4497, 270.0), "speed": 25, "fatigue": 0.3, "passengers": 3,
        "weather": "rain",
        "events": [("trip_start", {})],
        "script": ["Herbie, sport mode!"],
        "acceptance": "AC4: refuses sport in character and offers an alternative",
    },
    "engine_misfire": {
        "title": "Self-diagnosis: misfire",
        "about": "Tuesday afternoon. A misfire code appears.",
        "time": at(1, 16, 0), "pos": (52.5100, 13.4100, 0.0), "speed": 40, "dtcs": ["P0301"],
        "events": [("dtc", {"code": "P0301"})],
        "script": ["yes", "how are you doing?"],
        "acceptance": "AC6: explains, proposes a slot that fits the calendar, books on confirmation",
    },
    "brake_failure": {
        "title": "Self-diagnosis: critical brake fault",
        "about": "A stop-now fault while driving.",
        "time": at(0, 12, 0), "pos": (52.5100, 13.4100, 0.0), "speed": 50, "dtcs": ["C0040"],
        "events": [("dtc", {"code": "C0040"})],
        "script": [],
        "acceptance": "AC7: immediate safety warning, no booking dialogue",
    },
    "morning_meeting": {
        "title": "Stand-up from the car",
        "about": "Monday 08:25 on the way to work. Join the stand-up, record with consent, get the summary.",
        "time": at(0, 8, 25), "pos": (52.5070, 13.3040, 90.0), "speed": 40, "fatigue": 0.4,
        "origin": "home", "destination": "work",
        "script": ["Hey Herbie, join my meeting", "yes", "end the meeting"],
        "acceptance": "AC8: audio only while moving; nothing recorded without consent",
    },
    "tunnel_no_gps": {
        "title": "No GPS (tunnel)",
        "about": "GPS lost. Herbie must not guess.",
        "time": at(2, 14, 0), "pos": (52.5030, 13.3352, 90.0), "speed": 50, "gps_ok": False,
        "script": ["what's that on my left?"],
        "acceptance": "AC10: graceful, honest answer, no crash",
    },
    "offline": {
        "title": "Cloud connection lost",
        "about": "No connectivity. Edge fallbacks keep Herbie useful.",
        "time": at(3, 19, 0), "pos": (52.5030, 13.3352, 90.0), "speed": 30, "cloud_online": False, "pointing": -90.0,
        "script": ["what's that on my left?", "comfort mode", "how are you doing?"],
        "acceptance": "AC9: basic commands still work; Herbie says what is degraded",
    },
}


def apply(world: World, name: str) -> list[tuple[str, dict]]:
    """Reset the world to the scenario. Memory is kept on purpose: Herbie keeps learning."""
    sc = SCENARIOS[name]
    fresh = World()
    world.__dict__.update(fresh.__dict__)
    if "journey" in sc:
        world.journey_id = sc["journey"]
        world.place_on_route(0.0)
        world.drive_mode = sc.get("drive_mode", "autopilot")
    else:
        lat, lon, heading = sc["pos"]
        world.signals["Vehicle.CurrentLocation.Latitude"] = lat
        world.signals["Vehicle.CurrentLocation.Longitude"] = lon
        world.signals["Vehicle.CurrentLocation.Heading"] = heading
    world.signals["Vehicle.Speed"] = float(sc.get("speed", 0))
    world.signals["Vehicle.OBD.DTCList"] = list(sc.get("dtcs", []))
    world.clock = sc["time"]
    for key in ("fatigue", "hunger", "passengers", "weather", "origin", "destination", "gps_ok", "cloud_online"):
        if key in sc:
            setattr(world, key, sc[key])
    world.pointing_offset_deg = sc.get("pointing")
    return list(sc.get("events", []))
