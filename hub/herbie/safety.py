"""Safety Gate: deterministic rules between "Herbie wants to" and "the car does".

Neither the LLM nor any skill writes vehicle signals directly. They emit Actions;
the gate allows, clamps or blocks each one against the current context. In the
target architecture this module runs on the in-vehicle edge computer, so it keeps
working without the cloud and can be certified independently of the AI parts.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .context import ContextSnapshot

MAX_RECLINE_WHILE_MOVING = 30.0  # degrees; beyond this the belt geometry stops being effective
SPORT_MIN_SPEED_LIMIT = 50
SPORT_MAX_FATIGUE = 0.6
COMFORT_TEMP_RANGE = (16.0, 28.0)


class Action(BaseModel):
    path: str
    value: Any
    label: str  # human-readable, e.g. "softer suspension"
    requires_confirmation: bool = False  # True for anything Herbie may not do on its own
    confirmed: bool = False


class GateDecision(BaseModel):
    action: Action
    allowed: bool
    final_value: Any = None
    reasons: list[str] = []


def check(action: Action, ctx: ContextSnapshot) -> GateDecision:
    reasons: list[str] = []
    value = action.value
    p = action.path

    if action.requires_confirmation and not action.confirmed:
        return GateDecision(action=action, allowed=False, reasons=["needs the driver's confirmation"])

    if p == "Vehicle.Powertrain.Transmission.PerformanceMode" and value == "SPORT":
        if ctx.environment.road_wet:
            reasons.append(f"the road is wet ({ctx.environment.weather})")
        if ctx.user.fatigue > SPORT_MAX_FATIGUE:
            reasons.append("you seem tired")
        if ctx.environment.zone in ("school_zone", "residential") or ctx.environment.speed_limit < SPORT_MIN_SPEED_LIMIT:
            reasons.append(f"we're in a {ctx.environment.speed_limit} km/h {ctx.environment.zone.replace('_', ' ')}")
        if ctx.quality.get("speed") != "ok":
            reasons.append("I can't trust the speed signal right now")
        if reasons:
            return GateDecision(action=action, allowed=False, reasons=reasons)

    if p == "Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline":
        if ctx.vehicle.moving and value > MAX_RECLINE_WHILE_MOVING:
            reasons.append(f"recline limited to {MAX_RECLINE_WHILE_MOVING:.0f}° while driving")
            value = MAX_RECLINE_WHILE_MOVING

    if p == "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature":
        lo, hi = COMFORT_TEMP_RANGE
        if not lo <= value <= hi:
            value = min(hi, max(lo, value))
            reasons.append(f"temperature clamped to {value:.1f}°C")

    if p == "Vehicle.ADAS.Autopilot.IsEngaged" and value:
        if not ctx.gps_ok:
            reasons.append("I've lost GPS, so I can't see where I'm going")
        if ctx.quality.get("speed") != "ok":
            reasons.append("my speed signal is unreliable")
        if ctx.environment.weather == "snow":
            reasons.append("it's snowing")
        if reasons:
            return GateDecision(action=action, allowed=False, reasons=reasons)

    if p == "Vehicle.Cabin.Infotainment.Meeting.VideoActive" and value and ctx.vehicle.moving:
        return GateDecision(action=action, allowed=False, reasons=["video is only allowed while parked"])

    return GateDecision(action=action, allowed=True, final_value=value, reasons=reasons)
