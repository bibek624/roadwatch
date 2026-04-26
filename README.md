# RoadWatch

> Agentic pavement-condition triage for DOT engineers — a 3-tier multi-agent
> Opus 4.7 fleet that reads street-level Mapillary imagery, grades every
> survey point on a corridor, and turns the evidence into a decision-grade
> priority brief.

🎬 **[3-minute demo video](#)** _(link added on submission)_
🌐 **[Live demo](https://github.com/USER/REPO)** _(GitHub Pages link added after first push)_

Built for the **Anthropic Opus 4.7 hackathon** (April 2026).

---

## What it is

Public works departments need to answer one question every Monday morning:
**which streets need attention first, and what's wrong with them?** Today they
either wait for a $250k windshield survey every 3 years or guess from 311
complaints. The visible answer — the cracks, the patches, the alligator
fatigue, the failed utility cuts — is already in Mapillary's open street-level
imagery. Nobody could read it at scale before, because pavement-condition
grading needs an engineer's eye.

RoadWatch deploys a **3-tier hierarchical Opus 4.7 agent fleet** at every
survey point:

```
            ┌──────────────────┐
            │  Street Captain  │  ← plans, dispatches, synthesizes
            └────────┬─────────┘
                     │  dispatches up to 3 in parallel
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐
  │Surveyor 1│ │Surveyor 2│ │Surveyor 3│  ← one per survey point
  └────┬─────┘ └────┬─────┘ └────┬─────┘
       │            │            │
       │  each spawns up to 2 in parallel
       ▼            ▼            ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐
  │ Year     │ │ Year     │ │ Year     │  ← one per available capture year
  │ Investig.│ │ Investig.│ │ Investig.│
  └──────────┘ └──────────┘ └──────────┘
```

Peak ~6 concurrent Opus calls, communicating through per-point and per-street
JSON blackboards. After the fleet finishes, a single **decision-synthesis
call** turns the raw evidence into a priority brief: Immediate / Scheduled /
Monitor / Healthy bands, each with cited image evidence and an engineer note.

**Validated end-to-end** on West Sunset Boulevard (West Hollywood, CA): 47
survey points, 2 years of imagery, 2,983 trace events, $2.67 synthesis call,
7 priority actions. The full run is bundled into the live demo.

---

## Live demo (no install)

The [GitHub Pages site](#) bundles the West Sunset Boulevard survey as a
fully-static replay. Two views:

1. **Live trace replay** — the agent fleet thinking in real time. Map shows
   the corridor; per-agent cursors pulse at their current survey point;
   chat-style transcript on the right streams the agent's narration as it
   peeks Mapillary panos, zooms into suspected distresses, and grades.
2. **Decisions dashboard** (top-bar button) — the DOT-engineer view. Map
   colors every survey point by tier; right sidebar groups priority actions
   into Immediate / Scheduled / Monitor / Healthy bands; click any point or
   action card to open a detail drawer with the actual viewport image, the
   distress / treatment / safety pills, and the engineer note.

---

## How Claude was used

- **Opus 4.7 drives every reasoning step** — captain planning, surveyor
  decisions, year-investigator yaw selection, corridor synthesis.
- **Skills library** ([.claude/skills/pavement/](.claude/skills/pavement/)) —
  17 markdown files of pavement-engineering knowledge (FHWA Distress
  Identification Manual, ASTM D6433, PASER, FHWA preservation guidance)
  composed into 2 cache-controlled system blocks. Cache-economics: ~90 KB
  costs $0.42 to write once, reads at 0.10× input price (~$0.005/turn). Over
  a 30-turn run, $0.13 cached vs. ~$22.50 uncached.
- **Haiku 4.5** for image-quality prescreens (~$0.001 per call vs. ~$0.08
  for the same call to Opus). Garbage frames rejected before they reach the
  expensive reasoner.
- **Tool-use loop with prompt caching** for surveyors and investigators
  (9 tools: get_position, look_around, find_candidates, peek_candidate, look,
  zoom_into_region, grade, skip_waypoint, done).
- **Single-call structured synthesis** for the decisions dashboard
  (`tool_choice: {"type":"tool","name":"emit_synthesis"}`). One Opus call
  reads the full evidence bundle and emits the priority brief in one shot.
- **Async fan-out** with `asyncio.Semaphore` to run up to 6 Opus calls
  concurrently while respecting rate limits.

See [HANDOFF.md](HANDOFF.md) for the architecture diagram and tool surface
walkthrough; [DECISIONS.md](DECISIONS.md) for the load-bearing architectural
decisions; [PROGRESS.md](PROGRESS.md) for the chronological ship log.

---

## How thinking evolved

The hackathon's own framing rewards depth and pivot discipline. The full
narrative is in [PROGRESS.md](PROGRESS.md); the spine:

1. **Day 1.** 10-category urban-hazard atlas (broken sidewalks, faded
   markings, drainage). Thin signal on first run — Haiku triage agreed with
   nothing in particular.
2. **Day 2 morning.** Pivoted to **pavement-only** triage. Mapillary 360°
   panos, equirectangular → rectilinear viewports, Opus 4.7 5-tier rating.
   Cost: $5/street, OK signal, batch pipeline.
3. **Day 2 afternoon.** Pivoted again — **agentic** instead of batch.
   Opus 4.7 drives a tool-use loop, decides where to look. SoHo Broadway run:
   $8.11 / 58 turns / 9 findings.
4. **Day 3.** Replaced single-loop with **fleet** (Coordinator-Workers
   pattern). $0.14/finding vs $0.90. Live UI color-codes events by worker.
5. **Day 3 afternoon.** Calibrated against LA's PCI dataset (69k segments).
   Got 14% → 20% 5-way accuracy. Diagnosis: PCI includes laser-rutting and
   IRI roughness that **aren't visible in optical imagery**. The model
   wasn't wrong; the ground truth was measuring something else.
6. **Day 3 evening.** Pivoted from "match PCI" to **agentic Mapillary noise
   filtering** — corridor walker that owns its own sampling strategy.
7. **Day 4 morning.** Built the **skills library**. Validated v5 SPRING ST:
   $5.14 for 4 waypoints with 17 looks (10 zoomed) and 4 grades.
8. **Day 4 afternoon.** Tier rubric collapsed 5 → **3 tiers**. Added a
   **deterministic discipline gate** that refuses sloppy grades. Added
   **SHA-256 collision detection** to suppress duplicate-listing artifacts
   that fake a temporal arc.
9. **Day 4 evening.** Pivoted to the **3-tier hierarchical multi-agent**
   architecture. Captain + Surveyors + Year Investigators with blackboard
   communication. SPRING ST 3-pt run: $4.78 budget, 3 distinct tiers, clean
   captain narrative.
10. **Day 5.** Built the **Decisions Dashboard** — the DOT-engineer view that
    turns the agent fleet's narration into structured priority actions with
    image evidence.

The hackathon-internal "Keep Thinking" pattern is real: every pivot here came
from a validation result that contradicted an assumption.

---

## Running it locally

### 1. Prerequisites

- **Python 3.11+**
- A **Mapillary v4 access token** (free at
  [mapillary.com/dashboard/developers](https://www.mapillary.com/dashboard/developers))
- An **Anthropic API key** with access to Claude Opus 4.7 + Haiku 4.5
  ([console.anthropic.com](https://console.anthropic.com))
- Budget: a typical 3-point survey runs ~$5; a 47-point corridor like
  West Sunset runs ~$30 for the agent fleet plus ~$2.70 for the corridor
  synthesis.

### 2. Install

```bash
git clone https://github.com/USER/REPO.git roadwatch
cd roadwatch
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# edit .env, fill in MAPILLARY_TOKEN and ANTHROPIC_API_KEY
```

### 3. Run the web UI

```bash
uvicorn app.main:app --reload --port 8000
```

Then in your browser:

| URL                                             | What you see                                        |
| ----------------------------------------------- | --------------------------------------------------- |
| `http://127.0.0.1:8000/temporal`                | Polygon-draw → pick streets → live agent fleet UI   |
| `http://127.0.0.1:8000/decisions`               | Index of completed corridor surveys                 |
| `http://127.0.0.1:8000/decisions/<run-slug>`    | Per-corridor decision dashboard                     |

The flow:

1. Open `/temporal`. Draw a polygon over a street (works best with major
   urban arterials with multi-year Mapillary coverage — try Sunset Strip in
   West Hollywood).
2. The system finds all named streets in the polygon. Pick one. Click
   **Launch survey**.
3. Watch the fleet live: agent cursors pulse at their points, the chat
   streams every step, the map colors each surveyed point by tier (Good /
   Fair / Poor / unknown).
4. When the captain wraps the corridor narrative, click **View decisions**.
   The Decisions Dashboard renders priority bands with cited image
   evidence.

### 4. Run from the CLI

The web UI is a polling wrapper over a CLI runner. To survey a specific
street headlessly:

```bash
python scripts/run_hierarchy_walker.py \
    --street "Sunset Boulevard" \
    --city "West Hollywood, CA" \
    --spacing 50 \
    --budget 30
```

Output lands in `downloads/walker/<run_slug>/` (excluded from git via
`.gitignore`).

---

## Repo layout

```
roadwatch/
├── README.md, LICENSE, .env.example, .gitignore, requirements.txt
├── PROGRESS.md           — chronological ship log (the pivot story)
├── DECISIONS.md          — 14 load-bearing architectural decisions
├── HANDOFF.md            — fresh-session bootstrap + architecture diagram
├── CLAUDE.md             — repo instructions, auto-loaded by Claude Code
│
├── app/                  — FastAPI app
│   ├── main.py           — routes (/temporal, /decisions, /api/*)
│   ├── decisions.py      — corridor synthesis (single Opus call)
│   ├── claude.py         — pricing + image-block helpers
│   ├── mapillary.py      — Mapillary v4 client (tile + Graph API)
│   ├── osm.py            — Overpass query helper
│   ├── panorama.py       — equirect → rectilinear projection
│   ├── prompts/
│   │   ├── decisions_synthesis.py  — synthesis system prompt + tool schema
│   │   └── pavement.py             — early Haiku validity-visibility prompts
│   └── agent/
│       ├── loop.py, trace.py, walker_state.py, skill_loader.py
│       ├── street_walker.py        — sequential walker (legacy fallback)
│       ├── pano_inspector.py       — viewport rendering primitives
│       └── hierarchy/              — the 3-tier fleet (10 files)
│           ├── runner.py           — shared agent-loop machinery
│           ├── captain.py          — top-tier orchestrator
│           ├── point_surveyor.py   — tier 2: per-point inspector
│           ├── year_investigator.py — tier 3: per-year deep dive
│           ├── blackboard.py       — inter-agent JSON state
│           ├── primitives.py, run_state.py, agent_scratch.py, skills.py
│
├── .claude/skills/pavement/  — 17 engineering-knowledge skill files
│   (distress taxonomy, tier rubric, treatment signatures, climate failure
│    modes, viewport geometry, scan plan, grade discipline, etc.)
│
├── static/
│   ├── temporal_v2.html  — live agent-fleet UI (the hero artifact)
│   └── decisions.html    — DOT-engineer decisions dashboard
│
├── scripts/
│   └── run_hierarchy_walker.py   — CLI entry point for the fleet
│
└── docs/                 — GitHub Pages root (the live static demo)
    ├── index.html        — frozen West Sunset trace replay
    ├── decisions.html    — frozen West Sunset decisions dashboard
    └── data/west_sunset/ — pre-baked run bundle (~36 MB)
```

---

## Honest limitations

- **Mapillary coverage is uneven.** Dense in major US cities, sparse
  rural-ward. RoadWatch surfaces "no coverage" gracefully but cannot
  manufacture imagery.
- **This is triage, not a PCI/PASER survey.** The 3-tier output (Good /
  Fair / Poor / unknown) is plain-language engineer-prioritization input.
  It does not estimate IRI roughness or PCI numerical scores. It cannot see
  subsurface defects (rutting depth, base failure under intact wearing
  course).
- **Distress bounding boxes are approximate.** Loose region-of-interest,
  not surveyor-grade pixel precision.
- **Pavement surface is sensitive to shadows, wetness, and oil stains.**
  The skills library includes a `visual_confusers.md` skill that the agent
  consults; multi-image cross-witness is mandatory for Poor-tier calls.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Author

Built by **Bibek Acharya** (pavement engineer + remote-sensing background)
during the Opus 4.7 hackathon, April 2026.
