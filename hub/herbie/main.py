"""Herbie Hub HTTP/WebSocket API and the cockpit web UI.

Run:  uvicorn herbie.main:app --reload   (from the hub/ directory)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import experiences, scenarios
from .emotion import MoodTracker
from .llm import LLM
from .memory import Memory
from .orchestrator import Orchestrator
from .sim import World
from .skills.personalization import bucket

HUB_DIR = Path(__file__).resolve().parent.parent


def load_dotenv() -> None:
    """Minimal .env support (project root or hub/). Real environment variables win."""
    for path in (HUB_DIR.parent / ".env", HUB_DIR / ".env"):
        if path.exists():
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
logging.basicConfig(level=os.environ.get("HERBIE_LOG", "INFO"))
WEB_DIR = HUB_DIR / "web"
TICK_SECONDS = 0.25
STREETVIEW_URL = "https://maps.googleapis.com/maps/api/streetview"


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class FeedbackIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    accept: bool


class GestureIn(BaseModel):
    offset_deg: float | None = Field(default=None, ge=-180, le=180)


class SimIn(BaseModel):
    fatigue: float | None = Field(default=None, ge=0, le=1)
    hunger: float | None = Field(default=None, ge=0, le=1)
    passengers: int | None = Field(default=None, ge=0, le=8)
    weather: Literal["dry", "rain", "snow"] | None = None
    zone: Literal["city", "highway", "residential", "school_zone"] | None = None
    speed_limit: int | None = Field(default=None, ge=5, le=250)
    speed: float | None = Field(default=None, ge=0, le=250)
    heading: float | None = Field(default=None, ge=0, lt=360)
    gps_ok: bool | None = None
    cloud_online: bool | None = None
    playing: bool | None = None
    time_scale: float | None = Field(default=None, ge=0.25, le=16)
    cruise_kmh: float | None = Field(default=None, ge=5, le=60)
    route_pos_m: float | None = Field(default=None, ge=0, le=20000)  # dev: jump along the journey


class DriveIn(BaseModel):
    action: Literal["accelerate", "brake", "left", "right"]


class AutopilotIn(BaseModel):
    stop_id: str | None = Field(default=None, max_length=64)  # None = drive to the end of the route
    restart: bool = False


class CameraIn(BaseModel):
    on: bool


class FaceIn(BaseModel):
    # MediaPipe blendshape scores (0..1), computed in the browser. No image data.
    blendshapes: dict[str, float] = Field(max_length=60)


class SnapshotIn(BaseModel):
    image: str = Field(max_length=2_000_000)  # data:image/jpeg;base64,...
    question: str = Field(default="How do I look?", max_length=300)


class DtcIn(BaseModel):
    code: str = Field(pattern=r"^[PCBU][0-9A-F]{4}$")


def create_app(db_path: str | None = None) -> FastAPI:
    world = World()
    memory = Memory(db_path or os.environ.get("HERBIE_DB", "data/herbie.db"))
    llm = LLM()
    brain = Orchestrator(world, memory, llm)
    mood = MoodTracker()
    lock = threading.RLock()
    sockets: set[asyncio.Queue] = set()
    state = {"loop": None, "scenario": None}

    def broadcast(msg: dict) -> None:
        loop = state["loop"]
        if loop is None:
            return
        for q in list(sockets):
            loop.call_soon_threadsafe(q.put_nowait, msg)

    brain.listeners.append(lambda entry: broadcast({"type": "log", "entry": entry}))

    def snapshot() -> dict:
        ctx = brain.engine.snapshot()
        return {
            "world": world.public_state(),
            "context": ctx.model_dump(mode="json"),
            "bucket": bucket(ctx),
            "open_suggestions": [s.model_dump(mode="json") for s in brain.open.values()],
            "scenario": state["scenario"],
            "llm": {"enabled": llm.enabled, "model": llm.model, "last_error": llm.last_error},
            "experience": {"id": world.experience_id, "fired": sorted(brain.director.fired) if brain.director.exp else []},
            "music": {"track": world.get("Vehicle.Cabin.Infotainment.Media.Track"),
                      "up_next": [brain.by_name["music"].track(t) for t in brain.by_name["music"].queue[:3]]},
        }

    def push_state() -> None:
        broadcast({"type": "state", "state": snapshot()})

    def tick_once() -> dict | None:
        with lock:
            if not world.playing or world.drive_mode == "static":
                return None
            for event, payload in world.tick(TICK_SECONDS):
                brain.event(event, payload)
            brain.director.tick()
            return snapshot()

    async def ticker() -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                s = await asyncio.to_thread(tick_once)
            except Exception:  # never let one bad tick kill the simulation loop
                logging.exception("tick failed")
                continue
            if s:
                broadcast({"type": "state", "state": s})

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        state["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title="Herbie Hub", version="0.1.0", lifespan=lifespan)
    app.state.brain, app.state.world, app.state.memory = brain, world, memory

    def replies_out(replies) -> dict:
        push_state()
        return {"replies": [r.model_dump(mode="json") for r in replies]}

    @app.get("/health")
    def health():
        return {"status": "ok", "llm_enabled": llm.enabled, "model": llm.model}

    @app.get("/api/state")
    def get_state():
        with lock:
            return snapshot()

    @app.get("/api/scenarios")
    def list_scenarios():
        return [{"id": k, **{f: v[f] for f in ("title", "about", "script", "acceptance")}} for k, v in scenarios.SCENARIOS.items()]

    @app.post("/api/scenario/{name}")
    def load_scenario(name: str):
        if name not in scenarios.SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name}")
        with lock:
            brain.director.stop()
            brain.reset()
            events = scenarios.apply(world, name)
            state["scenario"] = name
            replies = [r for ev, payload in events for r in brain.event(ev, payload)]
            return replies_out(replies)

    @app.post("/api/say")
    def say(body: SayIn):
        with lock:
            return replies_out(brain.utterance(body.text))

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn):
        with lock:
            r = brain.feedback(body.id, body.accept)
            if r is None:
                raise HTTPException(404, "suggestion no longer open")
            return replies_out([r])

    @app.post("/api/gesture")
    def gesture(body: GestureIn):
        with lock:
            world.pointing_offset_deg = body.offset_deg
            push_state()
            return {"pointing_offset_deg": world.pointing_offset_deg}

    @app.post("/api/sim")
    def update_sim(body: SimIn):
        with lock:
            fields = body.model_dump(exclude_none=True)
            if "speed" in fields:
                world.signals["Vehicle.Speed"] = fields.pop("speed")
            if "heading" in fields:
                world.signals["Vehicle.CurrentLocation.Heading"] = fields.pop("heading")
            if "route_pos_m" in fields:
                pos = fields.pop("route_pos_m")
                if world.journey() is None:
                    raise HTTPException(409, "no journey loaded")
                world.place_on_route(pos)
            for k, v in fields.items():
                setattr(world, k, v)
            push_state()
            return {"ok": True}

    @app.post("/api/drive")
    def drive(body: DriveIn):
        with lock:
            world.control(body.action)
            push_state()
            return {"ok": True}

    @app.post("/api/autopilot")
    def autopilot(body: AutopilotIn):
        with lock:
            if world.journey() is None:
                raise HTTPException(409, "no journey loaded")
            if body.restart:
                world.place_on_route(0.0)
                world.drive_mode = "manual"
            started = time.perf_counter()
            stop = world.journey().stop(body.stop_id) if body.stop_id else None
            if body.stop_id and stop is None:
                raise HTTPException(404, "unknown stop")
            brain._emit({"role": "driver", "text": f"(navigation) drive to {stop['name'] if stop else 'Alexanderplatz'}",
                         "time": world.clock.isoformat()})
            nav = brain.by_name["navigation"]
            reply = nav.drive_to(stop, brain.engine.snapshot(), brain.svc, ["destination picked on the screen"])
            return replies_out(brain._finish([reply], started))

    @app.get("/api/journey")
    def get_journey():
        j = world.journey()
        if j is None:
            from .journey import load

            j = load("berlin")
        return j.public()

    @app.get("/api/config")
    def config():
        stadia = os.environ.get("STADIA_API_KEY")
        tiles = "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png" + (f"?api_key={stadia}" if stadia else "")
        return {"streetview": bool(os.environ.get("GOOGLE_MAPS_API_KEY")), "llm": llm.enabled,
                "llm_provider": llm.provider, "model": llm.model, "map_tiles": tiles,
                "map_attribution": "© Stadia Maps © OpenMapTiles © OpenStreetMap contributors",
                "calendar": "google" if os.environ.get("GOOGLE_CALENDAR_ICS_URL") else "demo"}

    @app.get("/api/experiences")
    def list_experiences():
        return experiences.catalogue()

    @app.post("/api/experience/{exp_id}")
    def start_experience(exp_id: str):
        if exp_id not in experiences.EXPERIENCES:
            raise HTTPException(404, f"unknown experience {exp_id}")
        with lock:
            camera = world.camera_on
            brain.director.start(exp_id)
            world.camera_on = camera
            mood.reset()
            state["scenario"] = exp_id
            push_state()
            return {"ok": True, "experience": exp_id}

    @app.post("/api/camera")
    def camera(body: CameraIn):
        with lock:
            world.camera_on = body.on
            if not body.on:
                world.mood = "unknown"
                mood.reset()
            push_state()
            return {"camera_on": world.camera_on}

    @app.post("/api/driver/face")
    def driver_face(body: FaceIn):
        with lock:
            if not world.camera_on:
                raise HTTPException(409, "camera is off")
            stable, fatigue, react = mood.update(body.blendshapes)
            world.mood = stable
            world.fatigue = round(0.7 * world.fatigue + 0.3 * max(fatigue, 0.15), 3)
            if react and not brain.has_pending_question(time.monotonic(), experiences.ANSWER_TIMEOUT_S) \
                    and time.monotonic() - brain.last_input > experiences.QUIET_AFTER_INPUT_S:
                brain.event("mood", {"mood": react})
            return {"mood": stable, "fatigue": world.fatigue, "reacted": react}

    @app.post("/api/camera/snapshot")
    def camera_snapshot(body: SnapshotIn):
        import base64

        with lock:
            if not world.camera_on:
                raise HTTPException(409, "camera is off")
            try:
                jpeg = base64.b64decode(body.image.split(",", 1)[-1], validate=True)
            except ValueError:
                raise HTTPException(422, "image must be base64 JPEG")
            started = time.perf_counter()
            reply = brain.by_name["wellbeing"].describe(jpeg, body.question, brain.engine.snapshot(), brain.svc)
            return replies_out(brain._finish([reply], started))

    @app.get("/api/streetview")
    def streetview(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                   heading: float = Query(ge=0, lt=360)):
        """Proxy + disk cache for Google Street View, so the key never reaches the browser."""
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            raise HTTPException(404, "Street View not configured")
        lat, lon, heading = round(lat, 4), round(lon, 4), round(heading / 10) * 10 % 360
        cache = Path(os.environ.get("HERBIE_CACHE", HUB_DIR / "data" / "streetview"))
        cache.mkdir(parents=True, exist_ok=True)
        f = cache / f"{lat}_{lon}_{heading}.jpg"
        if not f.exists():
            import httpx

            r = httpx.get(STREETVIEW_URL, timeout=10, params={
                "size": "640x400", "location": f"{lat},{lon}", "heading": heading, "fov": 100, "pitch": 4,
                "source": "outdoor", "return_error_code": "true", "key": key})
            if r.status_code != 200:
                raise HTTPException(404, "no Street View image here")
            f.write_bytes(r.content)
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/dtc")
    def inject_dtc(body: DtcIn):
        with lock:
            codes = world.signals["Vehicle.OBD.DTCList"]
            if body.code not in codes:
                codes.append(body.code)
            return replies_out(brain.event("dtc", {"code": body.code}))

    @app.post("/api/dtc/clear")
    def clear_dtcs():
        with lock:
            world.signals["Vehicle.OBD.DTCList"] = []
            push_state()
            return {"ok": True}

    @app.post("/api/trip/start")
    def trip_start():
        with lock:
            return replies_out(brain.event("trip_start"))

    @app.post("/api/reset")
    def reset():
        with lock:
            brain.director.stop()
            memory.reset()
            brain.reset()
            world.__dict__.update(World().__dict__)
            state["scenario"] = None
            push_state()
            return {"ok": True}

    @app.get("/api/memory")
    def get_memory():
        with lock:
            return {"preferences": memory.get_preferences("saad"), "episodes": memory.recent_episodes("saad", 50)}

    @app.get("/api/pois")
    def get_pois():
        return brain.visual.local.pois

    @app.get("/api/log")
    def get_log():
        return brain.log[-100:]

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        sockets.add(q)
        try:
            with lock:
                first = snapshot()
            await socket.send_json({"type": "state", "state": first})
            while True:
                await socket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sockets.discard(q)

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()
