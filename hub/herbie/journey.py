"""A pre-computed driving route (real OSRM geometry) the simulated car can follow.

Position along the route is a single number, metres from the start; everything else
(lat/lon, heading, street name, next landmark) is derived from it.
"""
from __future__ import annotations

import bisect
import json
from functools import lru_cache
from pathlib import Path

from . import geo

DATA = Path(__file__).resolve().parent / "data"
LOOKAHEAD_M = 15.0  # heading is taken towards a point slightly ahead, so turns look smooth


class Journey:
    def __init__(self, data: dict):
        self.id, self.name, self.about = data["id"], data["name"], data["about"]
        self.points: list[list[float]] = data["points"]
        self.cum: list[float] = data["cum_m"]
        self.streets: list[dict] = data["streets"]
        self.stops: list[dict] = data["stops"]
        self.amenities: list[dict] = data.get("amenities", [])
        self.start, self.end = data.get("start", "the start"), data.get("end", "the end of the route")
        self.length = self.cum[-1]

    def position(self, s: float) -> tuple[float, float]:
        s = min(max(s, 0.0), self.length)
        i = max(0, bisect.bisect_right(self.cum, s) - 1)
        if i >= len(self.points) - 1:
            return tuple(self.points[-1])
        seg = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg <= 0 else (s - self.cum[i]) / seg
        (a_lat, a_lon), (b_lat, b_lon) = self.points[i], self.points[i + 1]
        return a_lat + (b_lat - a_lat) * t, a_lon + (b_lon - a_lon) * t

    def heading(self, s: float) -> float:
        a = self.position(min(s, self.length - LOOKAHEAD_M))
        b = self.position(min(s, self.length - LOOKAHEAD_M) + LOOKAHEAD_M)
        return geo.bearing_deg(*a, *b)

    def street(self, s: float) -> str | None:
        name = None
        for st in self.streets:
            if st["from_m"] <= s:
                name = st["name"]
        return name

    def stop(self, poi_id: str) -> dict | None:
        return next((st for st in self.stops if st["poi_id"] == poi_id), None)

    def next_stop(self, s: float) -> dict | None:
        return next((st for st in self.stops if st["at_m"] > s + 5), None)

    def amenity_ahead(self, kind: str, s: float, min_ahead_m: float = 250.0) -> dict | None:
        return next((a for a in self.amenities if a["kind"] == kind and a["at_m"] >= s + min_ahead_m), None)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "about": self.about, "length_m": round(self.length),
                "start": self.start, "end": self.end, "points": self.points, "stops": self.stops,
                "streets": self.streets, "amenities": self.amenities}


@lru_cache(maxsize=None)
def load(journey_id: str = "berlin") -> Journey:
    return Journey(json.loads((DATA / f"journey_{journey_id}.json").read_text()))
