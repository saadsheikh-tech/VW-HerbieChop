"""S1: Visual Dialogue. "Hey Herbie, what's that building over there?"

Pipeline:
  1. pointing offset relative to the car (cabin camera gesture, or "left"/"right" in speech)
  2. world bearing = vehicle heading + pointing offset
  3. POI candidates inside a cone around that bearing (local map cache or OpenStreetMap)
  4. score = angle fit + closeness + prominence; ask back when the top two are too close to call
  5. answer from grounded facts, phrased in Herbie's voice

Camera confirmation with a vision model is the planned next step for (4); the scorer
already returns the shortlist it would verify.
"""
from __future__ import annotations

import logging
import os
import re

from .. import geo
from ..context import ContextSnapshot
from ..personality import narrate, say
from .base import Reply, Services, Skill, Suggestion, load_json

log = logging.getLogger("herbie.visual")

RADIUS_M = 600.0
CONE_DEG = 30.0
AMBIGUITY_MARGIN = 0.08
FAST_SPEED_KMH = 80.0
W_ANGLE, W_DIST, W_PROMINENCE = 0.5, 0.3, 0.2
MERGE_RADIUS_M = 60.0
SMALL_MEMORIALS = {"plaque", "stolperstein", "stone", "blue_plaque"}


def side_word(offset: float) -> str:
    o = geo.angle_diff(offset, 0)
    if abs(o) <= 30:
        return "ahead of you"
    if abs(o) >= 150:
        return "behind you"
    return "on your left" if o < 0 else "on your right"


class LocalPOIs:
    """Bundled map cache. Doubles as the offline fallback for the online provider."""

    name = "local cache"

    def __init__(self) -> None:
        self.pois = load_json("berlin_pois.json")

    def near(self, lat: float, lon: float, radius: float) -> list[dict]:
        # Tall landmarks (TV Tower, cathedral domes) can be seen and pointed at from much further away.
        return [p for p in self.pois if geo.haversine_m(lat, lon, p["lat"], p["lon"]) <= max(radius, p.get("visible_m", 0))]


class OverpassPOIs:
    """Live OpenStreetMap lookup (free, no key). Used when HERBIE_POI_SOURCE=overpass."""

    name = "OpenStreetMap"
    URL = "https://overpass-api.de/api/interpreter"

    def near(self, lat: float, lon: float, radius: float) -> list[dict]:
        import httpx

        q = (f"[out:json][timeout:5];(nwr(around:{radius:.0f},{lat},{lon})[historic][name];"
             f"nwr(around:{radius:.0f},{lat},{lon})[tourism~\"attraction|museum\"][name];"
             f"nwr(around:{radius:.0f},{lat},{lon})[amenity=place_of_worship][name];);out center tags 40;")
        r = httpx.post(self.URL, data={"data": q}, timeout=6.0,
                       headers={"User-Agent": "HerbieHub/0.1 (VW HerbieChop student prototype)"})
        r.raise_for_status()
        out = []
        for el in r.json().get("elements", []):
            tags = el.get("tags", {})
            c = el.get("center", el)
            if "lat" not in c or tags.get("memorial") in SMALL_MEMORIALS:
                continue  # plaques and Stolpersteine aren't something you point at from a car
            kind = tags.get("historic") or tags.get("tourism") or tags.get("building") or "landmark"
            if tags.get("amenity") == "place_of_worship":
                kind = tags.get("building") if tags.get("building") not in (None, "yes") else "place of worship"
            facts = ", ".join(f"{k}: {tags[k]}" for k in ("start_date", "architect", "heritage", "description") if k in tags)
            prominence = 0.4 + 0.4 * ("wikipedia" in tags or "wikidata" in tags) + 0.2 * (el["type"] != "node")
            out.append({
                "id": f"osm-{el['id']}", "name": tags.get("name:en") or tags["name"], "type": kind.replace("_", " "),
                "lat": c["lat"], "lon": c["lon"], "prominence": round(prominence, 2),
                "short": facts or f"A {kind.replace('_', ' ')} listed in OpenStreetMap.", "story": facts,
            })
        # OSM maps features inside buildings separately (a memorial hall inside a church).
        # Keep only the most prominent feature per ~60 m so one building is one answer.
        kept: list[dict] = []
        for p in sorted(out, key=lambda p: p["prominence"], reverse=True):
            if all(geo.haversine_m(p["lat"], p["lon"], k["lat"], k["lon"]) > MERGE_RADIUS_M for k in kept):
                kept.append(p)
        return kept


class VisualDialogue(Skill):
    name = "visual_dialogue"
    intents = {"identify_poi"}

    def __init__(self) -> None:
        self.local = LocalPOIs()
        self.online = OverpassPOIs() if os.environ.get("HERBIE_POI_SOURCE") == "overpass" else None

    # ---- ranking -----------------------------------------------------------
    def candidates(self, ctx: ContextSnapshot, offset: float, trace: list[str]) -> list[dict]:
        v = ctx.vehicle
        bearing = geo.normalize(v.heading_deg + offset)
        trace.append(f"heading {v.heading_deg:.0f}° + pointing {offset:+.0f}° = bearing {bearing:.0f}° ({geo.compass_name(bearing)})")
        pois, source = None, self.local.name
        if self.online and ctx.system.cloud_online:
            try:
                pois, source = self.online.near(v.lat, v.lon, RADIUS_M), self.online.name
            except Exception as e:  # network, rate limit, bad JSON: all degrade the same way
                trace.append(f"OpenStreetMap unavailable ({type(e).__name__}), using local cache")
        if pois is None:
            pois = self.local.near(v.lat, v.lon, RADIUS_M)
        trace.append(f"{len(pois)} POIs within {RADIUS_M:.0f} m from {source}")

        scored = []
        for p in pois:
            d = geo.haversine_m(v.lat, v.lon, p["lat"], p["lon"])
            diff = abs(geo.angle_diff(geo.bearing_deg(v.lat, v.lon, p["lat"], p["lon"]), bearing))
            if diff > CONE_DEG:
                continue
            # Same distance scale for everything; far-visible landmarks just get no closeness bonus.
            score = W_ANGLE * (1 - diff / CONE_DEG) + W_DIST * max(0.0, 1 - d / RADIUS_M) + W_PROMINENCE * p.get("prominence", 0.5)
            scored.append({**p, "distance_m": round(d), "angle_off_deg": round(diff, 1), "score": round(score, 3)})
        scored.sort(key=lambda c: c["score"], reverse=True)
        trace.append("in cone: " + (", ".join(f"{c['name']} ({c['score']})" for c in scored[:3]) or "none"))
        return scored

    # ---- skill API ---------------------------------------------------------
    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        trace: list[str] = []
        if not ctx.gps_ok:
            trace.append("GPS quality flag = missing; refusing to guess")
            return Reply(text=say("no_gps"), skill=self.name, trace=trace)

        offset = intent.slots.get("pointing_offset_deg")
        if offset is not None:
            trace.append(f"direction from speech: {offset:+.0f}°")
        elif ctx.interaction.pointing_offset_deg is not None:
            offset = ctx.interaction.pointing_offset_deg
            trace.append(f"direction from cabin camera gesture: {offset:+.0f}°")
        else:
            offset = 0.0
            trace.append("no gesture or direction word; assuming straight ahead")

        cands = self.candidates(ctx, offset, trace)
        if not cands:
            return Reply(text=say("nothing_there", direction=side_word(offset).replace("on your ", "to the ").replace(" of you", "")),
                         skill=self.name, trace=trace)

        if len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < AMBIGUITY_MARGIN:
            a, b = cands[0], cands[1]
            trace.append(f"top two within {AMBIGUITY_MARGIN} → asking instead of guessing")
            return Reply(
                text=f"Do you mean the {a['type']}, the {a['name']}, or the {b['type']} next to it, the {b['name']}?",
                skill=self.name, trace=trace, clarify=[a["name"], b["name"]], data={"options": [a, b], "offset": offset},
            )
        return self.answer(cands[0], offset, ctx, svc, trace)

    def resolve(self, text: str, options: list[dict], offset: float, ctx: ContextSnapshot, svc: Services) -> Reply | None:
        """Follow-up to a clarification question: match by name, type or position."""
        t = text.lower()
        for i, word in enumerate(("first", "second")):
            if re.search(rf"\b{word}\b", t):
                return self.answer(options[i], offset, ctx, svc, [f"clarified: '{word}' option"])
        best, best_hits = None, 0
        for o in options:
            words = set(re.findall(r"[a-zäöüß]{4,}", f"{o['name']} {o['type']}".lower()))
            hits = sum(1 for w in words if w in t)
            if hits > best_hits:
                best, best_hits = o, hits
        if best:
            return self.answer(best, offset, ctx, svc, [f"clarified by name: {best['name']}"])
        return None

    def answer(self, poi: dict, offset: float, ctx: ContextSnapshot, svc: Services, trace: list[str]) -> Reply:
        fast = ctx.vehicle.speed_kmh > FAST_SPEED_KMH
        d = poi["distance_m"]
        where = (f"about {d / 1000:.1f} kilometres {side_word(offset)}" if d >= 1000
                 else f"about {round(d, -1):.0f} metres {side_word(offset)}")
        fallback = f"That's the {poi['name']}, {where}. {poi['short']}"
        facts = f"name: {poi['name']}\ntype: {poi['type']}\nposition: {where}\nfact: {poi['short']}"
        instruction = ("Tell the driver what the building is in ONE short sentence." if fast else
                       "Tell the driver what the building is and share the fact, in two short sentences.")
        text, src = narrate(svc.llm, facts, instruction, fallback, ctx.system.llm_available)
        trace.append(f"answer: {poi['name']} (score {poi['score']}), phrased by {src}")
        if not ctx.system.cloud_online:
            text = f"{say('offline_note')} {text}"
        suggestion = None
        if not fast and poi.get("story") and poi["story"] != poi["short"]:
            text += " Want the story behind it?"
            suggestion = Suggestion.new(skill=self.name, kind="poi_story", subject=poi["id"], payload={"poi": poi})
        elif fast:
            trace.append(f"speed {ctx.vehicle.speed_kmh:.0f} km/h > {FAST_SPEED_KMH:.0f}: short answer, no follow-up offer")
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "poi_answer", poi["id"], None, None,
                               {"name": poi["name"], "lat": ctx.vehicle.lat, "lon": ctx.vehicle.lon})
        return Reply(text=text, skill=self.name, trace=trace, suggestion=suggestion, data={"poi": poi})

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        if not accepted:
            return Reply(text="Sure, eyes on the road.", skill=self.name)
        poi = s.payload["poi"]
        text, src = narrate(svc.llm, f"name: {poi['name']}\nstory: {poi['story']}",
                            "Retell the story conversationally in at most three short sentences.", poi["story"],
                            ctx.system.llm_available)
        return Reply(text=text, skill=self.name, trace=[f"story for {poi['name']} phrased by {src}"])
