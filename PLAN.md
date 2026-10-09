# VW HerbieChop — Project Plan

The Car Brain Challenge "Re-inCARnate Herbie", built as a cloud prototype.
No fixed dates yet. Milestones are in order, and real dates get added once the CODE email dates are confirmed.

---

## Status

| Milestone | State |
|---|---|
| M0 Foundation (repo, container, simulator, cockpit UI) | ✅ built; Docker Compose not yet run on a machine with Docker |
| M1 Core brain (context, memory, orchestrator, personality, safety gate) | ✅ |
| M2 S1 Visual Dialogue | ✅ (vision-model confirmation still open) |
| M3 S2 Adaptive Personalization | ✅ |
| M4 S3 Diagnostics & Booking | ✅ basic |
| M5 S4 Meeting Assistant | ✅ basic (recorded transcript instead of live speech-to-text) |
| M6 S5 Trip Planner | ⏳ waiting for the definition |
| M7 Hardening | 🟡 AC1–AC10 automated; latency/resource measurements still open |
| M8 Docs & deck | 🟡 README covers architecture and run instructions |

Where the build differs from this plan (on purpose, for an easy-to-run prototype): one modular FastAPI service instead of separate services on MQTT, SQLite instead of Postgres, and a plain-JS cockpit instead of React. See README → Design decisions.

---

## 1. Vision in one line

Herbie is one AI companion with a single personality and a single memory. It notices what's happening in and around the car, remembers what you like, and acts by itself, but only inside safe limits. Every feature below is a *skill* of that one brain, not a separate app.

This framing matters for the judges. The brief says to *"prioritize technical substance and a coherent implementation over breadth of features"* and asks for a *"clean and generic"* context model that can grow. A shared brain with plug-in skills gives us both.

## 2. Feature set (skills)

| # | Skill | What the user experiences | Herbie traits shown |
|---|-------|---------------------------|---------------------|
| S1 | **Visual Dialogue ("What's that?")** | Point at something and ask "Hey Herbie, what's that building?". Herbie names it and tells a short story about it | Perception, social skills, personality |
| S2 | **Adaptive Personalization** | After work, when tired and hungry: softer suspension, seat reclines, calm music, a food suggestion. Saturday night with friends: Herbie *offers* sport mode | Memory, emotional intelligence, autonomy |
| S3 | **Self-Diagnosis & Auto Service Booking** | Herbie spots a fault, explains it in plain words, rates how urgent it is, checks your calendar and books a workshop slot | Autonomy, self-awareness |
| S4 | **Work / Study Assistant** | Join a Meet/Teams call by voice while driving. Herbie transcribes it, summarizes it and pulls out action items | Social skills, memory |
| S5 | **Smart Trip Planner** | *Needs a definition from the Phase 1 material, see open questions* | Autonomy, memory |

## 3. Architecture

```
┌──────────────────────────── IN-VEHICLE (edge) — mocked in the prototype ────────────────────────────┐
│  Vehicle Simulator (VSS signals: GPS, heading, speed, DTCs, suspension, seat, drive mode, HVAC)    │
│  Cabin sensors (mocked): driver camera → gesture + fatigue, mic → wake word + STT                  │
│  Safety Gate (deterministic rules, ALWAYS edge) ◄── every actuation passes through here           │
│  Edge fallback: small local intent model + cached profiles for offline operation                   │
└────────────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                             │  event bus (MQTT)
┌────────────────────────────────────────────▼──────────────── CLOUD ───────────────────────────────┐
│  Context Engine  ── builds a live "context snapshot" (user, vehicle, environment, interaction,     │
│                     system) from the event stream                                                │
│  Memory Service  ── episodic (what happened), semantic (preferences/facts), routine patterns       │
│  Herbie Orchestrator (LLM agent + tool calling)                                                  │
│     ├─ Personality layer (persona, tone, how to say "I can't")                                  │
│     ├─ Decision/Policy engine (proactive suggestions: when to speak, when to stay quiet)        │
│     └─ Skills: S1 Visual Dialogue · S2 Personalization · S3 Diagnostics · S4 Meetings · S5 Trip │
│  External adapters: Maps/Places, Wikipedia, Calendar, Workshop booking (mock), Meeting audio     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
                                             │
                      Web "Cockpit" UI + Scenario Player (for the demo and the jury)
```

### Key design decisions (to defend in docs and presentation)

1. **The LLM proposes, deterministic rules decide on actuation.** The LLM never sets suspension, drive mode or seats directly. It emits an *intent*, and the Safety Gate checks it against speed, road type, weather, passengers and driver state. This answers the "safety / cloud-edge separation" criterion.
2. **Vehicle data follows the COVESA Vehicle Signal Specification (VSS)** (e.g. `Vehicle.CurrentLocation.Heading`, `Vehicle.Chassis.Axle...`). It's the industry standard, VW is involved in it, and it makes a later move to a real car (Phase 3) much easier. Optional: run Eclipse Kuksa databroker as the vehicle signal server.
3. **Edge vs cloud split:**
   - Edge: wake word, safety gate, actuation, cached profile, offline fallback.
   - Cloud: LLM reasoning, VLM, maps/POI lookups, meeting summarization, memory store.
   - Every cloud skill gets a defined *degraded mode* for when the network is lost.
4. **Skills are plug-ins with one shared interface** (`can_handle(context, intent)`, `run(...)`, declared tools and permissions). This is the extensibility story.
5. **Everything runs on Docker Compose with one command.** The brief strongly recommends containers, and judges must be able to run it themselves.
6. **Explainability:** Every proactive action carries a "why" trace, e.g. "suggested comfort mode because: weekday 18:10, leaving Work, fatigue score 0.72, accepted 8 of the last 10 times". The UI shows it. This helps both the jury and user trust.

## 4. Context & data model (draft)

Five context dimensions, all timestamped, with a source and a confidence value:

| Dimension | Examples | Source in prototype |
|-----------|----------|---------------------|
| **User** | identity, preferences, routines, mood/fatigue estimate, calendar | Memory + mocked driver monitoring |
| **Vehicle** | location, heading, speed, DTCs, battery/fuel, suspension/seat/mode state | Vehicle simulator (VSS) |
| **Environment** | time, day, weather, nearby POIs, traffic | APIs (OSM/Google, weather) |
| **Interaction** | current conversation, gesture, gaze, passengers present | Cabin mock + STT |
| **System** | connectivity, active skills, permissions/consent | Internal |

**Relationships:** a *Trip* links User + Vehicle + Environment over time. *Episodes* (e.g. "accepted comfort mode") are stored per Trip. *Routine patterns* come from repeated Episodes in similar context buckets.

**Flow:** sensor events → Context Engine (normalize, fuse) → snapshot → Orchestrator / Policy engine → action → Safety Gate → actuator, then the outcome and user feedback go back into Memory.

**Storage:** Postgres for structured data and pgvector for semantic memory recall. Redis/MQTT for live signals.

## 5. Skill designs

### S1 — Visual Dialogue (flagship demo candidate)
1. Wake word plus the question go to STT, and intent = `identify_poi`.
2. **Pointing direction:** a cabin camera (MediaPipe hand landmarks on recorded video, or a simulated gesture) gives the arm angle relative to the car, e.g. left = −90°.
3. **World bearing** = vehicle heading + pointing offset. *Example: heading east (90°) and pointing left (−90°) gives a bearing of 0°, so north.*
4. **Candidate search:** POIs inside a cone (bearing ±25°, about 50–500 m) from Google Places or OpenStreetMap Overpass (free). Rank by angular distance, size/prominence, "historic/landmark" tags and line of sight.
5. **Optional confirmation:** send the matching exterior camera frame to a VLM ("does this frame show a church?") to settle between close candidates.
6. **Answer:** fetch a Wikipedia summary, then the personality layer turns it into one or two spoken sentences. Herbie offers more ("Want the story behind it?").
7. **Edge cases:** no clear winner → Herbie asks back ("The church or the museum next to it?"). No GPS → Herbie admits it in character. Highway speed → shorter answer.

### S2 — Adaptive Personalization
- **Profiles** are bundles of settings: `comfort_wind_down` (soft suspension, reclined seat, calm playlist, warm cabin) and `sport_night` (stiff suspension, upright seat, sport drive mode, upbeat music).
- **Learning:** begin with an explainable model, i.e. frequency counts per context bucket (weekday/weekend × time band × origin→destination × fatigue level × passengers), then upgrade to a contextual bandit that learns from accept/decline. No black box at first.
- **Autonomy levels:**
  - Comfort settings: Herbie applies them and announces it ("Rough day? I've softened things up").
  - **Sport mode is always offered, never switched on automatically.** It also gets blocked if the driver looks tired, the road is wet, it's late and fatigue is high, or there's a speed-restricted zone. This is a strong safety point for the jury.
- **Food suggestions** (e.g. McDonald's offer): only with opt-in, labelled when sponsored, and based on stated favourites from memory.

### S3 — Self-Diagnosis & Auto Service Booking
- The simulator injects OBD-II style fault codes (DTCs), e.g. `P0301` misfire, low tyre pressure, brake pad wear.
- A rule table assigns severity: *stop now / book soon / monitor*. The LLM only *explains* the fault, it doesn't decide severity.
- Herbie checks the calendar (mock or Google Calendar API), asks the mock workshop API for slots, proposes one, books it after the user confirms, and saves the booking to memory.

### S4 — Work / Study Assistant
- **Prototype route:** the meeting runs on the phone and the car mic picks up the cabin audio. Speech-to-text (Whisper) feeds a summary with action items, which gets emailed or saved afterwards.
- **Production route, documented and not built:** a meeting bot via the platform APIs (Teams/Graph, Google Meet media API) or a provider such as Recall.ai.
- **Legal/safety point:** recording needs consent from everyone in the call (in Germany, secretly recording a private conversation is a criminal offence under §201 StGB). So Herbie announces the recording and confirms consent. **Audio only while driving**, and video or screen content only when parked.

### S5 — Smart Trip Planner
To be defined from the Phase 1 material. Likely parts: multi-stop routing, calendar-aware departure times, charging/fuel stops, preferred stops from memory, and handing over to S2 when the trip ends.

## 6. Acceptance criteria (the brief asks for "If X, then Y" hypotheses)

| ID | If… | …then Herbie demonstrably… |
|----|-----|------------------------------|
| AC1 | heading east, pointing left, asking "what's that?" with a landmark north within 300 m | names that landmark within 3 s |
| AC2 | two landmarks are equally likely | asks a clarifying question instead of guessing |
| AC3 | weekday evening, leaving work, fatigue > 0.6, and the pattern was accepted before | proposes or applies the comfort profile and shows the reason |
| AC4 | Saturday night, user asks for sport mode, road wet | refuses in character, explains why, and offers an alternative |
| AC5 | the user declines the comfort profile 3 times in that context | stops suggesting it there (the model updates) |
| AC6 | a "book soon" DTC appears | explains it, offers a slot that fits the calendar, books it on confirmation |
| AC7 | a "stop now" DTC appears | gives an immediate safety warning and does no booking dialogue while driving |
| AC8 | a meeting starts with no consent confirmed | doesn't record |
| AC9 | cloud connection drops | the edge fallback still handles basic commands, and Herbie says what's unavailable |
| AC10 | malformed / missing sensor data (no GPS, NaN speed) | no crash; context is marked low-confidence and the reply is graceful |

These become automated tests plus scripted scenarios in the Scenario Player.

## 7. Proposed tech stack

- **Backend:** Python + FastAPI, one service per component, all in Docker Compose
- **Bus:** MQTT (Mosquitto), which is close to real in-car messaging
- **Data:** Postgres + pgvector, Redis
- **LLM:** the provider is still to be chosen (VW's Azure or a direct API). A large model handles orchestration, a small fast model handles intent classification, and a VLM handles S1 confirmation
- **Speech:** Whisper (STT), any TTS
- **Maps:** OpenStreetMap/Overpass (free, no key), with Google Places as an option
- **Frontend:** React web cockpit: map, car state, Herbie chat/voice, a "why" panel and the scenario player
- **Tests:** pytest, with scenario replays as integration tests

## 8. Milestones (in order, no dates)

| M | Deliverable | Done when |
|---|-------------|-----------|
| M0 | Repo, Docker Compose skeleton, CI, vehicle simulator with VSS signals | `docker compose up` shows a car driving a recorded route in the cockpit UI |
| M1 | Context Engine + Memory + Orchestrator + Personality + Safety Gate | Herbie can chat with context ("how fast am I going?") and remember a preference |
| M2 | **S1 Visual Dialogue** | AC1, AC2, AC10 pass |
| M3 | **S2 Adaptive Personalization** | AC3–AC5 pass |
| — | **Intermediate progress presentation** | Architecture + M1/M2 demo |
| M4 | S3 Diagnostics & Booking | AC6, AC7 pass |
| M5 | S4 Meeting Assistant | AC8 passes and a summary comes out of a recorded call |
| M6 | S5 Trip Planner | criteria still to be written |
| M7 | Hardening: AC9, fault injection, resource measurements (latency, tokens, memory) | Edge-case suite is green |
| M8 | Documentation (the four required chapters) + final deck + demo video | Submitted |

**Scope advice:** go deep on S1 and S2 first, since they're the most novel and show the most Herbie traits. Then add S3, which is low risk. S4 and S5 can be lighter if time runs short. A finished core plus three excellent skills scores better than five half-built ones.

## 9. Documentation mapping (required chapters)

1. **Technical Architecture & System Structure:** section 3, plus the fine-tuning candidates: intent classifier (in-car vocabulary), personality/tone model (consistent Herbie voice), POI ranking, and the fault-explanation model.
2. **Context & Data Model:** section 4.
3. **Technical Implementation & Artefacts:** repo, Compose file, README with run steps, `.env.example`.
4. **Contextualization & Further Development:** assumptions (mocked sensors, simulated actuators), limits, how it scales, and how the concept evolved since Phase 1.

## 10. Open questions

1. **Smart Trip Planner:** the attached PDFs don't describe it. The VW Task 2 brief and the CODE kickoff slides are both generic. Please share the Phase 1 submission that defines it.
2. **Real dates** from the CODE email.
3. **Team:** size, skills, roles. This decides how many skills we build in depth.
4. **Cloud:** VW Azure, CODE resources or your own? This decides the LLM provider.
5. **Wording check:** "self-conscious repair" is written here as *self-diagnosis*, meaning Herbie being aware of its own health. Correct?
