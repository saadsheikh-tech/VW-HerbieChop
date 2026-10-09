"""Context model and Context Engine.

The engine fuses raw world/sensor data and long-term memory into one immutable
ContextSnapshot with five dimensions (user, vehicle, environment, interaction,
system) plus the trip that links them. Every skill decides on a snapshot, never on
raw signals, so bad or missing inputs are handled in exactly one place: here.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .sim import PLACES, World


class UserContext(BaseModel):
    user_id: str
    name: str
    fatigue: float = Field(ge=0, le=1)
    hunger: float = Field(ge=0, le=1)
    preferences: dict[str, Any] = {}


class VehicleContext(BaseModel):
    lat: float | None
    lon: float | None
    heading_deg: float | None
    speed_kmh: float
    moving: bool
    performance_mode: str
    damping_mode: str
    seat_recline_deg: float
    cabin_temp_c: float
    playlist: str
    dtcs: list[str]


class EnvironmentContext(BaseModel):
    time: datetime
    day_type: Literal["weekday", "weekend"]
    time_band: Literal["morning", "midday", "evening", "night"]
    weather: str
    road_wet: bool
    zone: str
    speed_limit: int


class InteractionContext(BaseModel):
    passengers: int
    pointing_offset_deg: float | None
    meeting_audio: bool
    meeting_recording: bool


class SystemContext(BaseModel):
    cloud_online: bool
    llm_available: bool


class TripContext(BaseModel):
    origin: str | None
    destination: str | None
    kind: str  # e.g. "work->home", "other"


class ContextSnapshot(BaseModel):
    user: UserContext
    vehicle: VehicleContext
    environment: EnvironmentContext
    interaction: InteractionContext
    system: SystemContext
    trip: TripContext
    # Data-quality flags, e.g. {"gps": "missing", "speed": "invalid"}. Skills read
    # these instead of re-validating raw values.
    quality: dict[str, str] = {}

    @property
    def gps_ok(self) -> bool:
        return self.quality.get("gps") == "ok"

    def summary(self) -> str:
        """Compact, human-readable context line used for LLM prompts and traces."""
        v, e, u = self.vehicle, self.environment, self.user
        loc = f"{v.lat:.5f},{v.lon:.5f} heading {v.heading_deg:.0f}°" if self.gps_ok else "location unknown"
        return (
            f"{e.time:%A %H:%M} ({e.day_type} {e.time_band}), {e.weather}, {e.zone} zone. "
            f"Car: {v.speed_kmh:.0f} km/h, {loc}, mode {v.performance_mode}, suspension {v.damping_mode}. "
            f"Driver fatigue {u.fatigue:.2f}, hunger {u.hunger:.2f}, passengers {self.interaction.passengers}. "
            f"Trip: {self.trip.kind}."
        )


def time_band(t: datetime) -> str:
    h = t.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 16:
        return "midday"
    if 16 <= h < 21:
        return "evening"
    return "night"


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _clamp01(x: Any, default: float) -> float:
    n = _num(x)
    return default if n is None else min(1.0, max(0.0, n))


class ContextEngine:
    def __init__(self, world: World, memory, llm_available: callable):
        self.world = world
        self.memory = memory
        self._llm_available = llm_available

    def snapshot(self, user_id: str = "saad") -> ContextSnapshot:
        w, s = self.world, self.world.signals
        quality: dict[str, str] = {}

        lat, lon = _num(s["Vehicle.CurrentLocation.Latitude"]), _num(s["Vehicle.CurrentLocation.Longitude"])
        heading = _num(s["Vehicle.CurrentLocation.Heading"])
        gps_valid = w.gps_ok and lat is not None and lon is not None and heading is not None
        quality["gps"] = "ok" if gps_valid else "missing"
        if not gps_valid:
            lat = lon = heading = None
        else:
            heading %= 360

        speed = _num(s["Vehicle.Speed"])
        if speed is None or speed < 0 or speed > 400:
            quality["speed"] = "invalid"
            speed = 0.0  # treat as unknown; safety gate is told via quality flag
        else:
            quality["speed"] = "ok"

        profile = self.memory.get_user(user_id)
        t = w.clock
        cloud = w.cloud_online
        return ContextSnapshot(
            user=UserContext(
                user_id=user_id,
                name=profile["name"],
                fatigue=_clamp01(w.fatigue, 0.0),
                hunger=_clamp01(w.hunger, 0.0),
                preferences=self.memory.get_preferences(user_id),
            ),
            vehicle=VehicleContext(
                lat=lat, lon=lon, heading_deg=heading,
                speed_kmh=speed,
                moving=speed > 0,
                performance_mode=s["Vehicle.Powertrain.Transmission.PerformanceMode"],
                damping_mode=s["Vehicle.Chassis.Suspension.DampingMode"],
                seat_recline_deg=_num(s["Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline"]) or 20.0,
                cabin_temp_c=_num(s["Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature"]) or 21.0,
                playlist=str(s["Vehicle.Cabin.Infotainment.Media.Playlist"]),
                dtcs=list(s["Vehicle.OBD.DTCList"] or []),
            ),
            environment=EnvironmentContext(
                time=t,
                day_type="weekend" if t.weekday() >= 5 else "weekday",
                time_band=time_band(t),
                weather=w.weather,
                road_wet=w.weather in ("rain", "snow"),
                zone=w.zone,
                speed_limit=w.speed_limit,
            ),
            interaction=InteractionContext(
                passengers=max(0, int(w.passengers or 0)),
                pointing_offset_deg=_num(w.pointing_offset_deg),
                meeting_audio=bool(s["Vehicle.Cabin.Infotainment.Meeting.AudioActive"]),
                meeting_recording=bool(s["Vehicle.Cabin.Infotainment.Meeting.Recording"]),
            ),
            system=SystemContext(cloud_online=cloud, llm_available=cloud and self._llm_available()),
            trip=TripContext(origin=w.origin, destination=w.destination, kind=trip_kind(w.origin, w.destination)),
            quality=quality,
        )


def trip_kind(origin: str | None, destination: str | None) -> str:
    if origin in PLACES and destination in PLACES:
        return f"{origin}->{destination}"
    return "other"
