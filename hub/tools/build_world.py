"""Build the demo world from real open data. Run once (needs internet); outputs are committed.

    python tools/build_world.py

* herbie/data/places.json         fuel stations, cafés, Volkswagen service centres (OpenStreetMap / Overpass)
* herbie/data/journey_<id>.json   real driving routes (OSRM), with landmarks and amenities along them
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from herbie import geo  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "herbie" / "data"
UA = {"User-Agent": "HerbieHub/0.1 (VW HerbieChop student prototype)"}

HOME = (13.3172, 52.5128, "Home (Charlottenburg)")
WORK = (13.4470, 52.4937, "Work (CODE University, Lohmühlenstraße)")
RAW = (13.4545, 52.5075, "RAW-Gelände, Friedrichshain")
LANDMARK_RUN = [(13.3530, 52.5146), (13.3640, 52.5148), (13.3772, 52.5172), (13.3905, 52.5172),
                (13.3965, 52.5174), (13.4045, 52.5191), (13.4115, 52.5226)]

JOURNEYS = {
    "berlin": {"name": "Berlin landmark drive",
               "about": "Victory Column → Straße des 17. Juni → Reichstag → Unter den Linden → Museum Island → Alexanderplatz",
               "start": "Victory Column", "end": "Alexanderplatz", "waypoints": LANDMARK_RUN},
    "to_work": {"name": "Drive to work", "start": HOME[2], "end": WORK[2],
                "about": "Home → Straße des 17. Juni → Unter den Linden → Alexanderplatz → Holzmarktstraße → CODE",
                "waypoints": [HOME[:2], (13.3300, 52.5130)] + LANDMARK_RUN + [(13.4200, 52.5140), WORK[:2]]},
    "to_home": {"name": "Drive home from work", "start": WORK[2], "end": HOME[2],
                "about": "CODE → Holzmarktstraße → Alexanderplatz → Unter den Linden → Straße des 17. Juni → Home",
                "waypoints": [WORK[:2], (13.4200, 52.5140), (13.3965, 52.5176), (13.3640, 52.5150),
                              (13.3300, 52.5132), HOME[:2]]},
    "night_out": {"name": "Friday night out", "start": HOME[2], "end": RAW[2],
                  "about": "Home → Straße des 17. Juni → Unter den Linden → Alexanderplatz → Warschauer Straße → RAW",
                  "waypoints": [HOME[:2], (13.3300, 52.5130)] + LANDMARK_RUN + [(13.4490, 52.5062), RAW[:2]]},
}
LANDMARK_MAX_OFFSET_M = 260
AMENITY_MAX_OFFSET_M = 150

PLACES_QUERY = """[out:json][timeout:25];
(
 nwr["amenity"="fuel"](52.48,13.28,52.535,13.48);
 nwr["brand"="Starbucks"](52.50,13.30,52.53,13.45);
 nwr["shop"~"car|car_repair"]["brand"~"Volkswagen",i](52.40,13.15,52.65,13.70);
);
out center tags;"""


def fetch_places() -> dict:
    r = httpx.post("https://overpass-api.de/api/interpreter", data={"data": PLACES_QUERY}, timeout=60, headers=UA)
    r.raise_for_status()
    return parse_places(r.json()["elements"])


def parse_places(els: list) -> dict:
    out = {"fuel": [], "cafe": [], "vw_service": []}
    for e in els:
        t, c = e.get("tags", {}), e.get("center", e)
        addr = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        item = {"id": f"osm-{e['id']}", "name": t.get("name") or t.get("brand") or "Fuel station",
                "brand": t.get("brand"), "lat": round(c["lat"], 6), "lon": round(c["lon"], 6), "address": addr or None}
        if t.get("amenity") == "fuel":
            out["fuel"].append(item)
        elif t.get("brand") == "Starbucks":
            out["cafe"].append(item)
        elif t.get("shop") in ("car", "car_repair") and "group forum" not in item["name"].lower():
            out["vw_service"].append(item)
    out["source"] = "OpenStreetMap contributors, via Overpass API (ODbL)"
    return out


def route(waypoints) -> dict:
    url = ("https://router.project-osrm.org/route/v1/driving/" + ";".join(f"{lo},{la}" for lo, la in waypoints)
           + "?overview=full&geometries=geojson&steps=true&continue_straight=true")
    return httpx.get(url, timeout=30, headers=UA).json()["routes"][0]


def project(points, cum, lat, lon):
    best = min(range(len(points)), key=lambda i: geo.haversine_m(lat, lon, *points[i]))
    return cum[best], geo.haversine_m(lat, lon, *points[best])


def build_journey(jid: str, spec: dict, pois: list, places: dict) -> dict:
    r = route(spec["waypoints"])
    points = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
    dedup = [points[0]]
    for p in points[1:]:
        if geo.haversine_m(*dedup[-1], *p) > 1.0:
            dedup.append(p)
    points = dedup
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + geo.haversine_m(*a, *b))

    streets, at = [], 0.0
    for leg in r["legs"]:
        for step in leg["steps"]:
            if step["name"] and (not streets or streets[-1]["name"] != step["name"]):
                streets.append({"from_m": round(at), "name": step["name"]})
            at += step["distance"]

    stops = []
    for poi in pois:
        at_m, off = project(points, cum, poi["lat"], poi["lon"])
        if off <= LANDMARK_MAX_OFFSET_M:
            stops.append({"poi_id": poi["id"], "name": poi["name"], "at_m": round(at_m), "offset_m": round(off)})
    stops.sort(key=lambda s: s["at_m"])

    amenities = []
    for kind in ("fuel", "cafe"):
        for p in places[kind]:
            at_m, off = project(points, cum, p["lat"], p["lon"])
            if off <= AMENITY_MAX_OFFSET_M and 300 < at_m < cum[-1] - 300:
                amenities.append({**p, "kind": kind, "at_m": round(at_m), "offset_m": round(off)})
    amenities.sort(key=lambda a: a["at_m"])

    return {"id": jid, "name": spec["name"], "about": spec["about"], "start": spec["start"], "end": spec["end"],
            "source": "OSRM (router.project-osrm.org), OpenStreetMap data © OpenStreetMap contributors",
            "length_m": round(cum[-1]), "points": [[round(a, 6), round(b, 6)] for a, b in points],
            "cum_m": [round(c, 1) for c in cum], "streets": streets, "stops": stops, "amenities": amenities}


def main() -> None:
    places_file = DATA / "places.json"
    try:
        # Optional: python tools/build_world.py saved_overpass_response.json
        places = parse_places(json.loads(Path(sys.argv[1]).read_text())) if len(sys.argv) > 1 else fetch_places()
        places_file.write_text(json.dumps(places, indent=1, ensure_ascii=False))
    except Exception as e:  # Overpass is a shared free service; keep the last good copy
        print(f"Overpass failed ({e}); using existing {places_file.name}")
        places = json.loads(places_file.read_text())
    print({k: len(v) for k, v in places.items() if isinstance(v, list)})
    pois = json.loads((DATA / "berlin_pois.json").read_text())
    for jid, spec in JOURNEYS.items():
        j = build_journey(jid, spec, pois, places)
        (DATA / f"journey_{jid}.json").write_text(json.dumps(j))
        print(f"{jid}: {j['length_m']} m, {len(j['stops'])} landmarks, amenities: "
              + ", ".join(f"{a['kind']}:{a['name']}@{a['at_m']}m" for a in j["amenities"]))
        print("   " + " → ".join(s["name"] for s in j["streets"]))


if __name__ == "__main__":
    main()
