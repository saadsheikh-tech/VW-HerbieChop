# VW HerbieChop: Herbie Hub

The cloud prototype of **Herbie**, an AI companion for the car, built for the Volkswagen Car Brain Challenge "Re-inCARnate Herbie".
Herbie has one personality and one memory, and several skills plug into that one brain:

| Skill | What it does |
|---|---|
| **Visual Dialogue** | "Hey Herbie, what's that building over there?" Herbie combines your pointing direction with the car's heading and position, finds the landmark and tells you about it |
| **Adaptive Personalization** | Learns your routines. Tired after work: soft suspension, reclined seat, calm music, a food suggestion. Saturday night with friends: *offers* sport mode |
| **Self-Diagnosis & Service Booking** | Explains fault codes in plain words, rates urgency with fixed rules and books a workshop slot that fits your calendar |
| **Work / Study Assistant** | Joins your call audio-only while driving, records **only with consent**, and summarises it with action items |
| **Navigation** | "Herbie, drive to the TV Tower": autopilot along a real Berlin route, with arrival announcements |
| **Smart Trip Planner** | Plans fuel over your whole day (incl. rush hour on the way back), adds fuel and coffee stops on the real route |
| **Emotional intelligence** | Driver camera → mood → calming music, "more like this?", fatigue care; "how do I look?" on request |
| **Music** | Simulated player that remembers what you like and dislike |

## Quick start

**With Docker** (recommended for evaluation):

```bash
docker compose up --build        # the build also runs the test suite
open http://localhost:8000
```

**Without Docker** (Python 3.10+):

```bash
cd hub
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn herbie.main:app --reload
open http://localhost:8000
```

**Optional LLM:** put `GEMINI_API_KEY` (default model `gemini-3.5-flash-lite`, about 0.5–1.8 s per reply) or `ANTHROPIC_API_KEY` in `.env` at the project root (see `.env.example`). Without a key, everything still works on rule-based fallbacks and templates. That's deliberate, see *Design decisions*.

**Optional Street View:** add `GOOGLE_MAPS_API_KEY` (Google Maps Platform, with the *Street View Static API* enabled) to `.env`.

**Maps:** the Navigation tab uses Stadia Maps tiles, which need **no key on localhost**. To host the cockpit somewhere else, create a free account at stadiamaps.com, add your domain, and set `STADIA_API_KEY`.

**Rebuilding the world data:** `python tools/build_world.py` re-fetches the routes (OSRM) and places (OpenStreetMap); the results are committed in `herbie/data/`.

**Tests:** `cd hub && python -m pytest -q`. There are 62 tests: one per acceptance criterion (AC1–AC10), all three experiences end to end, mood, music memory, calendar parsing, journey, unit and LLM-path tests (the LLM tests use a fake client).

## Experiences (start here)

Opening `http://localhost:8000` shows a picker. Each experience is a full drive that brings every skill together, paced like a conversation. Herbie brings up one thing at a time, waits for your answer, never talks over you, and ties moments to where the car is on a real route.

| Experience | What happens |
|---|---|
| **Drive to work** (Wed 07:40, home in Charlottenburg → CODE University) | Welcome back → self-check: service due in a week, nearest real **Volkswagen service centre** (OpenStreetMap), slot that fits your calendar → *"Had your morning coffee?"* → your **favourite Starbucks at Pariser Platz** added as a real stop → calendar briefing (Google Calendar or demo) → *join the German class virtually and keep a summary?* → drive → **fuel plan**: "you'll run low on the way back, and that's rush hour: fuel up now at Aral Holzmarktstraße" → the car pulls in, refuels and continues → tour hints at the landmarks → arrival with the class summary and your next meeting |
| **Drive back from work** (Wed 17:40) | Welcome back with **memories from the morning** (service booking, homework) → comfort profile applied automatically and a food offer → low fuel at 11%, station on the route → tour → home |
| **Friday night out** (Fri 22:30, 3 passengers) | "Where to? RAW-Gelände like last Friday?" → **sport mode offered** (never automatic; safety-gated) → party playlist → taxi reminder → tour → arrival |
| **Free drive** | The Berlin landmark tour; just point and ask |

Fuel drains as you drive (4× faster than real so it shows on a 14 km trip; the planner uses the same numbers). Music is simulated: fictional tracks, no audio. Say "I don't like this, recommend me something else" and Herbie remembers the genre.

### Driver camera (📷, bottom right)

- **What it does:** MediaPipe Face Landmarker runs **in your browser**. Only ~12 facial-expression scores are sent to the hub every 1.5 s, never images.
- **How Herbie reacts:** the hub smooths the scores into a mood (happy / neutral / tense / sad / tired). A mood has to dominate ~15 s before Herbie reacts, with a cooldown so it never nags:
  - tense or sad → it offers calming music and a smoother ride
  - happy while music plays → "want more like this?"
  - tired → cooler cabin and a coffee stop; tiredness also blocks sport mode
- **"Herbie, how do I look?":** sends exactly **one** frame to Gemini, only when you ask, and nothing is stored.

### Google Calendar (optional, read-only)

In Google Calendar, go to *Settings → your calendar → Integrate calendar → **Secret address in iCal format***. Copy that link into `.env` as `GOOGLE_CALENDAR_ICS_URL=...`. Today's real events (recurring ones included) then replace the demo day when an experience starts. No password and no OAuth app are needed, and you can revoke access at any time by resetting the secret address. Treat the link like a password.

## The cockpit (main page)

`http://localhost:8000` opens a 2D, interactive Volkswagen-style cockpit: windscreen, steering wheel, instrument cluster, infotainment screen, vents and ambient lighting. Click **Start the drive** and Herbie drives you 4.8 km through central Berlin on a real route: Victory Column → Straße des 17. Juni → Reichstag → Unter den Linden → Museum Island → TV Tower → Alexanderplatz.

- **Point:** click a landmark on the windscreen (it asks automatically), click anywhere to point there, or click a side window. Keyboard: `Q` left, `E` right, `R` ahead, `X` clear.
- **Ask:** the 🎤 on the wheel or screen, hold `Space`, or turn on hands-free "Hey Herbie". You can also type. Herbie answers out loud, and the answer appears as a subtitle on the windscreen.
- **Drive:** `W A S D` (or the on-screen keys) takes over manually. "Keep going" or the Navigation tab hands back to the autopilot. You can pick any landmark on the route as a destination, and switch the sim speed between 1× and 8×.
- **What the windscreen shows:** an illustrated view projected from the car's real position and heading, with real landmark coordinates and real-world heights (the TV Tower is 368 m). AR tags show `?` until Herbie has named a landmark. If `GOOGLE_MAPS_API_KEY` is set, real Street View imagery replaces the illustration (proxied and cached by the server, so the key never reaches the browser).
- **Personalization is visible:** the ambient light strip, cluster badges and climate display change as Herbie applies profiles.

The previous dashboard is still available at `/dev.html` (scenarios, sensors, memory) for testing.

## Using the dev view

- **Left:** demo scenarios. Click one to load it, then click its script lines (or type, or use the 🎤 button) to talk to Herbie.
- **Centre:** map with the car, its heading and the pointing cone, plus the conversation. Every Herbie message has a **"why"** section with its full decision trace and response time.
- **Right:** live vehicle signals, a pointing-gesture pad (stands in for the cabin camera), mocked driver and world sensors (fatigue, weather, GPS, cloud connection…), fault injection, and Herbie's episodic memory.

## Architecture

```
 Mocked world (sim.py)                  Herbie Hub (one FastAPI service)
 ─────────────────────                  ────────────────────────────────────────────────────
 VSS-style vehicle signals ─┐           Context Engine ── 5 context dimensions + quality flags
 driver monitoring (mock)  ─┼──────────▶      │
 cabin camera gesture      ─┤                 ▼
 weather / zone / clock    ─┘           Orchestrator ── intent parser (rules → LLM fallback)
                                              │            personality layer (one voice)
                                              ▼
                              ┌──── Skills: visual_dialogue · personalization · diagnostics · meeting
                              │             (plug-ins with one interface)
                              ▼
                        Safety Gate  ◀── every actuation; deterministic; edge-deployable
                              │
                              ▼
                     vehicle signals written      Memory (SQLite): profile, preferences,
                                                   calendar, episodes (learning data)
 Cockpit UI (web/) ◀── REST + WebSocket ──▶ API (main.py)
```

| Module | Responsibility |
|---|---|
| `herbie/sim.py` | Mocked world: VSS-style signals, environment, driver state, simulated clock |
| `herbie/context.py` | Context model and engine: fuses the world and memory into a validated `ContextSnapshot` |
| `herbie/memory.py` | Long-term memory: preferences, calendar and episodic history (the learning data) |
| `herbie/safety.py` | Safety gate: allows, clamps or blocks every vehicle actuation |
| `herbie/intents.py` | Intent recognition: regex rules first, LLM classification (structured output) only if needed |
| `herbie/personality.py` | Herbie's persona prompt and template voice |
| `herbie/llm.py` | Optional LLM client (Gemini or Claude) with error handling; every caller has a rule-based fallback |
| `herbie/journey.py` | Routes (real OSRM geometry, built by `tools/build_world.py`): position, streets, landmarks, fuel/cafés on the way |
| `herbie/experiences.py` | The Director and the three experiences (beats, pacing, one question at a time) |
| `herbie/emotion.py` | Mood + fatigue from facial expression scores, with smoothing and cooldowns |
| `herbie/calendar_sync.py` | Read-only Google Calendar via the private iCal link (recurring events expanded) |
| `herbie/skills/trip_planner.py` | Fuel forecasting over the whole day (incl. rush hour), fuel and coffee stops |
| `herbie/skills/music.py` / `wellbeing.py` | Simulated player that learns taste / reactions to the driver's mood, "how do I look?" |
| `web/index.html`, `cockpit.*` | The 2D cockpit; `web/dev.html` is the developer dashboard |
| `herbie/orchestrator.py` | Routing, dialogue state (open suggestions, clarifications) and the decision log |
| `herbie/skills/*.py` | The skills |
| `herbie/scenarios.py` | Demo scenarios, which are also the acceptance tests |

### Design decisions

1. **The LLM proposes, rules decide.** No skill and no LLM output writes a vehicle signal directly. Everything goes through `safety.check()`. Sport mode needs confirmation and is blocked on wet roads, when the driver is tired, in low-speed zones, or when the speed signal is untrustworthy. Seat recline is capped at 30° while moving, and video calls are blocked while moving.
2. **It works without the cloud.** Every LLM call has a deterministic fallback, so the system degrades instead of failing. This mirrors the in-car split: rules and templates on the edge, LLM quality in the cloud.
3. **Learning is explainable.** Personalization uses a Laplace-smoothed acceptance rate per context bucket instead of a black box. Every decision shows its numbers in the "why" trace.
4. **Autonomy depends on the stakes.** Comfort settings may be applied automatically, always announced and undoable, once Herbie is confident: score ≥ 0.7 from at least 3 past episodes. Sport mode is only ever offered. Three declines in a row mute a profile in that context.
5. **Herbie asks instead of guessing.** If two landmarks score within 0.08 of each other, Herbie asks which one you mean.
6. **Vehicle signals use VSS naming** (COVESA Vehicle Signal Specification), so a real data broker such as Eclipse Kuksa can replace `sim.py` without touching the skills. Signals without a standard VSS path are marked `ext` in `sim.SIGNALS`.
7. **It's one service for now.** The hub is a single service whose modules are cleanly separated, and it can be split into separate services later. One container keeps it easy to run for evaluation. Each module talks only through its interface (snapshot in, Reply/Actions out), so splitting along those seams later is mechanical.

## Acceptance criteria

| ID | If… | …then Herbie | Test |
|---|---|---|---|
| AC1 | heading east, pointing left, landmark 200 m north | names it (< 3 s) | `test_ac1_*` |
| AC2 | two landmarks equally likely | asks a clarifying question | `test_ac2_*` |
| AC3 | weekday evening, leaving work, tired, routine known | applies comfort with reasons and undo | `test_ac3_*` |
| AC4 | sport requested on a wet road | refuses in character, offers an alternative | `test_ac4_*` |
| AC5 | comfort declined 3× in a context | stops suggesting it there | `test_ac5_*` |
| AC6 | "book soon" fault code | offers a slot that fits the calendar, books it | `test_ac6_*` |
| AC7 | "stop now" fault code | gives a safety warning only, no booking dialogue | `test_ac7_*` |
| AC8 | meeting without consent | joins audio-only and records nothing | `test_ac8_*` |
| AC9 | cloud offline | landmarks, modes and status still work | `test_ac9_*` |
| AC10 | no GPS / NaN speed / malformed API input | honest answer, gate stays safe, 422s | `test_ac10_*` |

## What's real and what's mocked

| Real (implemented logic) | Mocked (allowed in Phase 2) |
|---|---|
| Bearing/cone geometry, POI ranking, ambiguity handling | Cabin-camera gesture (buttons in the UI) |
| Live OpenStreetMap lookup (`HERBIE_POI_SOURCE=overpass`) with cache fallback | Bundled Berlin landmark cache with short hand-written facts |
| Context engine, data-quality flags | Vehicle signals, driver fatigue and hunger |
| Personalization learning, muting, autonomy policy | 3 weeks of seeded driving history and the calendar |
| Safety gate | Actuators (signals are just written to the simulator) |
| Severity rules, slot search against the calendar | Workshop and slots; fault codes except P0301/P0420 are illustrative |
| Consent flow, LLM structured summary and heuristic fallback | Meeting audio: a recorded transcript replaces live speech-to-text |
| Food offer matching with opt-in and sponsored labels | Offers data |

## Extending

Add a skill by creating `herbie/skills/<name>.py` with a `Skill` subclass that declares its `intents` and `events`, and register it in `Orchestrator.__init__`. Add a context source by extending `ContextSnapshot` and filling it in `ContextEngine.snapshot()`. Skills that don't read the new field are unaffected.

## Known limitations and next steps

- Trip planner skill (needs the Phase 1 definition).
- Vision-model confirmation of the landmark from an exterior camera frame.
- A contextual bandit to replace the frequency scorer once there is real data.
- Real speech-to-text (Whisper) and a meeting-platform bot instead of the recorded transcript.
- Postgres + pgvector for semantic memory recall; MQTT/Kuksa for vehicle signals.
- Latency budget: the rule path answers in ~1 ms; the LLM path depends on the network and model. Measure before any in-car use.
