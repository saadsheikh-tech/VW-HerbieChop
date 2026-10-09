"""Mocked "world": vehicle signals, cabin sensors, environment and a simulated clock.

In a real car these would arrive from the vehicle bus, driver-monitoring camera and
cloud weather/traffic services. The brief explicitly allows mocking external inputs
in Phase 2. Everything downstream (context engine, skills, safety gate) only ever
sees the data through this module, so swapping it for a real VSS data broker
(e.g. Eclipse Kuksa) only touches this file.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import geo

# Signal catalogue. Names follow COVESA VSS conventions; "ext" marks signals that
# have no standard VSS path and are our own extensions.
SIGNALS: dict[str, dict[str, Any]] = {
    "Vehicle.Speed": {"unit": "km/h", "default": 0.0},
    "Vehicle.CurrentLocation.Latitude": {"unit": "deg", "default": 52.4930},
    "Vehicle.CurrentLocation.Longitude": {"unit": "deg", "default": 13.4460},
    "Vehicle.CurrentLocation.Heading": {"unit": "deg", "default": 0.0},
    "Vehicle.Powertrain.Transmission.PerformanceMode": {"allowed": ["NORMAL", "SPORT", "ECONOMY"], "default": "NORMAL"},
    "Vehicle.Chassis.Suspension.DampingMode": {"allowed": ["SOFT", "NORMAL", "FIRM"], "default": "NORMAL", "ext": True},
    "Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline": {"unit": "deg", "default": 20.0},
    "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature": {"unit": "celsius", "default": 21.0},
    "Vehicle.Cabin.Infotainment.Media.Playlist": {"default": "Herbie Radio", "ext": True},
    "Vehicle.Cabin.Light.AmbientMood": {"allowed": ["NEUTRAL", "WARM", "VIBRANT"], "default": "NEUTRAL", "ext": True},
    "Vehicle.Cabin.Infotainment.Navigation.Waypoint": {"default": None, "ext": True},
    "Vehicle.Cabin.Infotainment.Meeting.AudioActive": {"default": False, "ext": True},
    "Vehicle.Cabin.Infotainment.Meeting.VideoActive": {"default": False, "ext": True},
    "Vehicle.Cabin.Infotainment.Meeting.Recording": {"default": False, "ext": True},
    "Vehicle.OBD.DTCList": {"default": []},
    "Vehicle.Powertrain.FuelSystem.RelativeLevel": {"unit": "percent", "default": 60.0},
    "Vehicle.Powertrain.FuelSystem.Range": {"unit": "m", "default": 107000},
    "Vehicle.Service.TimeToService": {"unit": "s", "default": 60 * 86400},
    "Vehicle.Cabin.Infotainment.Media.Track": {"default": None, "ext": True},
}

# Fuel model. Real consumption is ~7 L/100 km; the demo burns DEMO_FUEL_FACTOR times
# faster so a 14 km city drive visibly moves the gauge. Planner maths uses the same
# effective number, so what Herbie predicts is what the gauge then does.
TANK_L = 50.0
CONSUMPTION_L_PER_100KM = 7.0
DEMO_FUEL_FACTOR = 4.0
EFFECTIVE_L_PER_KM = CONSUMPTION_L_PER_100KM * DEMO_FUEL_FACTOR / 100
TRACK_SECONDS = 75.0  # real seconds per (silent, simulated) music track
FUEL_ALERTS = (15.0, 8.0)


def range_m(level_pct: float) -> float:
    return level_pct / 100 * TANK_L / EFFECTIVE_L_PER_KM * 1000


def level_after(level_pct: float, km: float) -> float:
    return level_pct - km * EFFECTIVE_L_PER_KM / TANK_L * 100

# Monday of an arbitrary reference week; scenarios pick a weekday + time inside it.
REFERENCE_MONDAY = datetime(2026, 1, 5)

PLACES = {
    "work": {"label": "Work (CODE University)", "lat": 52.4937, "lon": 13.4470},
    "home": {"label": "Home (Charlottenburg)", "lat": 52.5128, "lon": 13.3172},
}


@dataclass
class World:
    signals: dict[str, Any] = field(default_factory=lambda: {k: copy.deepcopy(v["default"]) for k, v in SIGNALS.items()})
    clock: datetime = field(default_factory=lambda: REFERENCE_MONDAY.replace(hour=10))
    playing: bool = True  # global pause switch for the simulation
    # Driving: "static" = frozen demo frame, "parked" = engine on, standing (clock runs),
    # "autopilot" = follow the journey, "stopped" = at a planned stop, "manual" = WASD
    drive_mode: str = "static"
    experience_id: str | None = None
    route_stops: list = field(default_factory=list)  # planned fuel/coffee stops on the journey
    planned_legs: list = field(default_factory=list)  # later drives today, for the trip planner
    stop_remaining_s: float = 0.0
    time_scale: float = 1.0
    journey_id: str | None = None
    route_pos_m: float = 0.0
    route_target_m: float | None = None
    target_name: str | None = None
    cruise_kmh: float = 35.0
    steer: float = 0.0  # -1..1, drives the steering-wheel animation
    # Environment (would come from weather / map services)
    weather: str = "dry"  # dry | rain | snow
    zone: str = "city"  # city | highway | residential | school_zone
    speed_limit: int = 50
    # Driver monitoring + cabin sensors (mocked camera / seat sensors)
    fatigue: float = 0.2
    hunger: float = 0.2
    passengers: int = 0
    pointing_offset_deg: float | None = None
    camera_on: bool = False
    meeting_title: str | None = None
    mood: str = "unknown"  # from the driver camera: happy | neutral | tense | sad | tired | unknown
    # Media (simulated player, no audio)
    music_playlist: str | None = None
    track_elapsed_s: float = 0.0
    # Trip
    origin: str | None = None
    destination: str | None = None
    # System
    gps_ok: bool = True
    cloud_online: bool = True

    def get(self, path: str) -> Any:
        return self.signals.get(path)

    def set(self, path: str, value: Any) -> Any:
        if path not in SIGNALS:
            raise KeyError(f"unknown signal {path}")
        allowed = SIGNALS[path].get("allowed")
        if allowed and value not in allowed:
            raise ValueError(f"{value!r} not allowed for {path}")
        old = self.signals.get(path)
        self.signals[path] = copy.deepcopy(value)
        return old

    # ---- driving -------------------------------------------------------------
    def journey(self):
        from .journey import load

        return load(self.journey_id) if self.journey_id else None

    def place_on_route(self, pos_m: float) -> None:
        j = self.journey()
        self.route_pos_m = min(max(pos_m, 0.0), j.length)
        lat, lon = j.position(self.route_pos_m)
        self.signals["Vehicle.CurrentLocation.Latitude"] = lat
        self.signals["Vehicle.CurrentLocation.Longitude"] = lon
        self.signals["Vehicle.CurrentLocation.Heading"] = round(j.heading(self.route_pos_m), 1)

    def snap_to_route(self) -> None:
        """After manual driving, continue the journey from the nearest route point."""
        j, lat, lon = self.journey(), self.get("Vehicle.CurrentLocation.Latitude"), self.get("Vehicle.CurrentLocation.Longitude")
        if not (_finite(lat) and _finite(lon)):
            return
        i = min(range(len(j.points)), key=lambda k: geo.haversine_m(lat, lon, *j.points[k]))
        self.place_on_route(j.cum[i])

    def control(self, action: str) -> None:
        """Manual WASD input. Any input takes over from the autopilot."""
        self.drive_mode = "manual"
        speed = self.get("Vehicle.Speed") if _finite(self.get("Vehicle.Speed")) else 0.0
        if action == "accelerate":
            self.signals["Vehicle.Speed"] = min(speed + 4.0, MANUAL_MAX_KMH)
        elif action == "brake":
            self.signals["Vehicle.Speed"] = max(speed - 8.0, 0.0)
        elif action in ("left", "right"):
            sign = -1 if action == "left" else 1
            h = self.get("Vehicle.CurrentLocation.Heading") or 0.0
            self.signals["Vehicle.CurrentLocation.Heading"] = round((h + sign * STEER_STEP_DEG) % 360, 1)
            self.steer = sign

    def tick(self, seconds: float) -> list[tuple[str, dict]]:
        """Advance time and move the car. `seconds` is real time. Returns events such as ("arrived", {...})."""
        if not self.playing or self.drive_mode == "static":
            return []
        events = self._media_tick(seconds)
        dt = seconds * self.time_scale
        self.clock += timedelta(seconds=dt)
        speed = self.get("Vehicle.Speed")
        speed = speed if _finite(speed) else 0.0
        self.steer *= 0.6

        if self.drive_mode == "stopped":
            return events + self._stop_tick(seconds)
        if self.drive_mode == "parked":
            return events

        if self.drive_mode == "autopilot" and self.journey_id:
            j = self.journey()
            target = j.length if self.route_target_m is None else self.route_target_m
            stop = self.next_route_stop()
            if stop and stop["at_m"] <= target:
                target = stop["at_m"]
            remaining = target - self.route_pos_m
            wanted = self.cruise_kmh if remaining > BRAKING_ZONE_M else max(8.0, self.cruise_kmh * remaining / BRAKING_ZONE_M)
            speed = _approach(speed, wanted, ACCEL_KMH_PER_S * dt)
            step = speed / 3.6 * dt
            old_heading = self.get("Vehicle.CurrentLocation.Heading") or 0.0
            if step >= remaining:
                events += self._burn_fuel(max(remaining, 0.0))
                self.place_on_route(target)
                self.signals["Vehicle.Speed"] = 0.0
                if stop and target == stop["at_m"]:
                    self.drive_mode, self.stop_remaining_s = "stopped", stop.get("dwell_s", 8.0)
                    return events + [("stop_reached", dict(stop))]
                self.drive_mode, name = "parked", self.target_name or j.end
                self.route_target_m = self.target_name = None
                return events + [("arrived", {"name": name, "journey": j.id})]
            self.place_on_route(self.route_pos_m + step)
            self.signals["Vehicle.Speed"] = round(speed, 1)
            turn = geo.angle_diff(self.get("Vehicle.CurrentLocation.Heading"), old_heading)
            self.steer = max(-1.0, min(1.0, turn / max(dt, 0.05) / 25))
            return events + self._burn_fuel(step)

        lat, lon = self.get("Vehicle.CurrentLocation.Latitude"), self.get("Vehicle.CurrentLocation.Longitude")
        if speed > 0 and _finite(lat) and _finite(lon):
            step = speed / 3.6 * dt
            lat, lon = geo.destination(lat, lon, self.get("Vehicle.CurrentLocation.Heading"), step)
            self.signals["Vehicle.CurrentLocation.Latitude"] = lat
            self.signals["Vehicle.CurrentLocation.Longitude"] = lon
            events += self._burn_fuel(step)
        return events

    # ---- stops, fuel, media --------------------------------------------------
    def next_route_stop(self) -> dict | None:
        return next((s for s in sorted(self.route_stops, key=lambda s: s["at_m"])
                     if not s.get("done") and s["at_m"] >= self.route_pos_m - 1), None)

    def add_route_stop(self, stop: dict) -> None:
        if not any(s["at_m"] == stop["at_m"] and s["kind"] == stop["kind"] for s in self.route_stops):
            self.route_stops.append({**stop, "done": False})

    def _stop_tick(self, seconds: float) -> list[tuple[str, dict]]:
        stop = self.next_route_stop()
        self.stop_remaining_s -= seconds
        if stop and stop["kind"] == "fuel":
            level = self.get("Vehicle.Powertrain.FuelSystem.RelativeLevel")
            self._set_fuel(min(100.0, level + 100.0 * seconds / max(stop.get("dwell_s", 8.0) * 0.8, 0.5)))
        if self.stop_remaining_s > 0:
            return []
        self.drive_mode = "autopilot"
        if stop:
            stop["done"] = True
            return [("stop_done", dict(stop))]
        return []

    def _set_fuel(self, level: float) -> None:
        level = round(max(0.0, level), 2)
        self.signals["Vehicle.Powertrain.FuelSystem.RelativeLevel"] = level
        self.signals["Vehicle.Powertrain.FuelSystem.Range"] = round(range_m(level))

    def set_fuel(self, level: float) -> None:
        self._set_fuel(level)

    def _burn_fuel(self, metres: float) -> list[tuple[str, dict]]:
        before = self.get("Vehicle.Powertrain.FuelSystem.RelativeLevel")
        after = level_after(before, metres / 1000)
        self._set_fuel(after)
        return [("fuel_low", {"level": round(after, 1), "threshold": t}) for t in FUEL_ALERTS if before >= t > after]

    def _media_tick(self, seconds: float) -> list[tuple[str, dict]]:
        playlist = self.get("Vehicle.Cabin.Infotainment.Media.Playlist")
        if playlist != self.music_playlist:
            self.music_playlist = playlist
            return [("playlist_changed", {"playlist": playlist})]
        if self.get("Vehicle.Cabin.Infotainment.Media.Track") is None:
            return []
        self.track_elapsed_s += seconds
        if self.track_elapsed_s >= TRACK_SECONDS:
            return [("track_end", {})]
        return []

    def _drive_state(self) -> dict[str, Any]:
        j = self.journey()
        out: dict[str, Any] = {"mode": self.drive_mode, "time_scale": self.time_scale, "cruise_kmh": self.cruise_kmh,
                               "steer": round(self.steer, 2), "journey": j.id if j else None,
                               "stops": [s for s in self.route_stops if not s.get("done")]}
        if j:
            nxt = j.next_stop(self.route_pos_m)
            out.update(route_pos_m=round(self.route_pos_m), length_m=round(j.length), street=j.street(self.route_pos_m),
                       target=self.target_name, target_m=self.route_target_m,
                       next_stop=nxt and {**nxt, "in_m": round(nxt["at_m"] - self.route_pos_m)})
        return out

    def public_state(self) -> dict[str, Any]:
        return {
            # Non-finite floats (a corrupt sensor) aren't valid JSON; expose them as null.
            "signals": {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in self.signals.items()},
            "clock": self.clock.isoformat(),
            "weekday": self.clock.strftime("%A"),
            "playing": self.playing,
            "drive": self._drive_state(),
            "environment": {"weather": self.weather, "zone": self.zone, "speed_limit": self.speed_limit},
            "driver": {"fatigue": self.fatigue, "hunger": self.hunger},
            "cabin": {"passengers": self.passengers, "pointing_offset_deg": self.pointing_offset_deg,
                      "camera_on": self.camera_on, "mood": self.mood},
            "media": {"track_elapsed_s": round(self.track_elapsed_s, 1), "track_seconds": TRACK_SECONDS},
            "experience": self.experience_id,
            "trip": {"origin": self.origin, "destination": self.destination},
            "system": {"gps_ok": self.gps_ok, "cloud_online": self.cloud_online},
        }


MANUAL_MAX_KMH = 80.0
STEER_STEP_DEG = 6.0
ACCEL_KMH_PER_S = 9.0  # ~2.5 m/s²
BRAKING_ZONE_M = 80.0


def _approach(value: float, target: float, max_delta: float) -> float:
    return min(target, value + max_delta) if value < target else max(target, value - max_delta)


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def at(weekday: int, hour: int, minute: int = 0) -> datetime:
    """Simulated time on the reference week (0 = Monday)."""
    return REFERENCE_MONDAY + timedelta(days=weekday, hours=hour, minutes=minute)
