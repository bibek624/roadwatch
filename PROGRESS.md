# PavTrace — Progress

## 2026-04-23 — Initial scaffold

### Done
- Project structure (`app/`, `static/`, config files)
- `requirements.txt` with pinned versions (FastAPI, httpx, mercantile, vt2geojson, shapely, python-dotenv, pydantic)
- `.env.example` with `MAPILLARY_TOKEN` placeholder
- **Backend**
  - `app/models.py` — Pydantic schemas for all requests/responses
  - `app/osm.py` — Overpass QL builder + GeoJSON converter for drivable highways
  - `app/mapillary.py` — zoom-14 vector-tile fetch, shapely polygon filter, Graph API image detail, handles ms-since-epoch timestamps
  - `app/main.py` — FastAPI app with `/api/analyze`, `/api/images`, `/api/image/{id}`, static mount, `/` serves `index.html`
- **Frontend**
  - `static/index.html` — Leaflet + Leaflet.draw, map + sidebar + toast layout
  - `static/app.js` — polygon-only draw tool, single-polygon state, roads + captures pipeline, viridis-ish palette lerp, year chip filter, image sidebar with date/coords/pano badge + Mapillary deep link
  - `static/style.css` — header/map/sidebar/year-chips/toast
- `README.md` (setup/run/usage)
- `CLAUDE.md` (guidance for future sessions)
- `PROGRESS.md` (this file)

### Acceptance checklist (unverified — needs manual run against real Mapillary token)
- [ ] Draw polygon on map
- [ ] Roads render as light gray lines
- [ ] Captures appear as color-coded dots
- [ ] Year chip filter toggles dots
- [ ] Clicking a dot shows image + date + coords in sidebar
- [ ] No auth anywhere except the server-side Mapillary token

### Next (explicitly out of MVP scope, deferred)
- Claude/Opus integration for image analysis
- Persistence (accounts, saved polygons, export)
- Multiple simultaneous polygons
- Full mobile optimization

---

## 2026-04-24 — Safety-atlas pivot (10 hazard categories)

### Done
- `scripts/run_area_triage.py` — full pipeline polygon → spatial dedup → Haiku triage per image with 10-category confidence scores.
- `app/prompts/triage.py` — Haiku triage prompt (usable + category confidence JSON).
- `app/prompts/near_miss.py` — deep-analysis rubric for the near-miss category.
- `app/panorama.py` — equirect → rectilinear viewport extraction with per-category recipes (`py360convert.e2p`).
- `app/claude.py` — `analyze_hazards` (Opus, sync), `triage_image_async` (Haiku, async), pricing constants.
- `download_images.py` / `_run_download.py` — bulk full-res pano + flat download, organized under `downloads/<year>/<kind>/<image_id>.jpg`.
- First run against a Chestnut-corridor bbox in Ventura — 500-image sample triaged, per-category markdown reports generated.

### Findings
- The 10-category atlas produced thin, ambiguous signal on a real-world street. Many categories had very few ≥0.6 confidence hits; those that did tended to fire on the same handful of shots. Not enough differentiated output to build a compelling demo around.
- Pavement surface is where Opus reliably sees fine detail at Mapillary resolution. Pivoting the project accordingly.

---

## 2026-04-24 — Pavement pivot (final direction)

### Context
Shifted from the 10-category safety atlas back to **pavement condition triage**. Legacy 10-category files stay in the tree (`scripts/run_area_triage.py`, `app/prompts/triage.py`, `app/prompts/near_miss.py`), but new work targets pavement.

### New architecture (all shipped)

**Prompts**
- `app/prompts/pavement.py` — three prompts, all prompt-cache-ready:
  1. `VALIDITY_VISIBILITY_SYSTEM_PROMPT` — Haiku gatekeeper on thumb_1024. Returns `{valid, reason, vis_forward_m, vis_backward_m}`. Filters off-street frames (garage / parking lot / indoor / night). Explicit guard in the prompt that the 2:1 equirect projection is expected and is NOT a reason to reject.
  2. `ORIENTATION_SYSTEM_PROMPT` — Haiku pick-the-forward-tile on a 2×2 probe grid. Returns `{forward_yaw_deg, confidence, reason}`.
  3. `RATING_SYSTEM_PROMPT` — Opus 4.7 5-tier condition (Good / Satisfactory / Fair / Poor / Failed) + distress taxonomy (pothole, crack_{longitudinal,transverse,alligator,block}, patch_failure, raveling, edge_break, rutting, utility_cut) with pixel bboxes + per-distress `approx_distance_m` + `viewport` tag.

**Geometry / geoprojection**
- `app/geoproject.py` (new) — pixel → world back-projection under flat-ground assumption (`h=2.5 m`). Mirrors panorama strip geometry. Supports all 4 viewports. `location_confidence = 1 − d/30` clamped to [0.05, 1.0]. Haversine helpers. Road-centerline clamping via shapely `MultiLineString + nearest_points`.

**Panorama rendering**
- `app/panorama.py` extended with:
  - `extract_pavement_strip(equi, viewport, forward_yaw_offset, …)` — renders one rectilinear crop for any of {forward, backward, left, right}.
  - `extract_pavement_strips(pano, forward_yaw_offset, …)` — 4-way convenience wrapper.
  - `render_orientation_probe(pano, …)` — 2×2 grid of (yaw=0, 90, 180, 270) at `pitch=−25°, hfov=100°, 384×256` per tile for the orientation Haiku probe.
  - `draw_distress_overlay(strip, distresses, out)` — PIL overlay with severity-colored bboxes + labels.
  - Final strip geometry (after iteration): **pitch=−25°, hfov=100°, 768×512, 3:2 aspect**. Same intrinsics across all 4 viewports. Matches the probe exactly, so whatever a human sees in a probe tile is what Opus sees as the scaled-up strip.

**Claude client**
- `app/claude.py` extended with:
  - `classify_validity_visibility_async` — Haiku validity + visibility call, prompt-cached.
  - `detect_forward_yaw_async` — Haiku orientation probe, prompt-cached, returns {0,90,180,270}.
  - `rate_pavement_async` — Opus 4.7 rating on up to 4 strips (fixed order: forward → backward → left → right) with per-image text captions so bbox `viewport` values are unambiguous. Prompt-cached on the long rubric.
  - `_usage_to_tokens` / `estimate_cost_with_cache` — cache-aware pricing (cache read 0.10×, cache write 1.25× of base input price).

**OSM / geocoding**
- `app/osm.py` extended with:
  - `_street_variants` — combinatorial expansion of name abbreviations (`S Chestnut St` → {S, South} × {St, Street, St.} — 6 variants for this case; important because OSM often uses the fully-spelled form).
  - `fetch_street_by_name(name, city, postcode=None)` — Overpass query using an `area[name=city]` filter + case-insensitive name regex. Tries all 4 Overpass mirrors (prefers `overpass-api.de` first); keeps trying if a mirror returns zero features rather than taking the first non-error response.
  - `buffer_street_to_polygon(street_geojson, width_m=8)` — local equirect meter projection around the street centroid, buffers LineStrings by N m into one Polygon. Handles multi-way streets via convex-hull fallback.

**Pipeline**
- `scripts/run_pavement_assessment.py` (new) — end-to-end, stage-gated, idempotent. Stages: fetch → validate → orient → chain → strips → rate → temporal → project → overlays → geojson → summary. Each stage writes its output JSON/GeoJSON; re-running skips completed stages unless `--force`. Per-sequence orient cache in `sequence_orientations.json`. Visibility-chain greedy selector uses per-image `vis_forward_m + vis_backward_m` to place next-pano coverage adjacency (code path in `visibility_chain`).

### Run output layout
```
downloads/pavement/<slug>/
  config.json, street.geojson, polygon.geojson
  captures_all.json, primaries.json
  validity.json, sequence_orientations.json, chain.json, strip_paths.json
  strips/<id>_{f,b,l,r}.jpg
  probes/<id>_probe.jpg
  ratings.json, temporal.json
  overlays/<id>_{f,b,l,r}.png
  points.geojson, distresses.geojson, segments.geojson
  summary.md
```

### Iterations during build
1. **Haiku false-rejection on equirect distortion** — first validity pass killed 23 of 30 primaries for "360_pano_distortion". Prompt updated to tell Haiku the 2:1 projection is expected, not distortion. Valid count jumped 7 → 15.
2. **Per-pano yaw orientation bug** — `yaw=0` assumed to be travel direction is only correct for ~85% of Mapillary panos; some rigs (e.g. the 2025 red-car in this run) capture with the pano rotated 180°, producing "forward" strips full of vehicle hood. Added `render_orientation_probe` + `detect_forward_yaw_async` to classify one representative pano per Mapillary sequence and apply the correction to all panos in that sequence.
3. **Strip geometry mismatch with probe** — initial strips were pitch=−35°/−50°/−55°, 2:1 aspect. Probes (pitch=−25°, 3:2) looked great; strips showed mostly hood. Unified strip geometry to match probe exactly (pitch=−25°, hfov=100°, 768×512, same for all 4 viewports). Distress count doubled (13 → 26) and `unknown` tier went to zero.
4. **4-way strips + rating prompt** — Opus now gets forward + backward + left + right in one call with per-image text captions naming the viewport. Prompt tells Opus to tag every distress with `viewport ∈ {forward, backward, left, right}` and to return zero distresses for strips that show only vehicle body.

### Validation run on S Chestnut St, Ventura 93001

| Stage | In | Out | Cost |
|---|---:|---:|---:|
| Overpass geocode (`name=S Chestnut St`, area=Ventura) | — | 5 OSM ways | $0 |
| Mapillary captures (8 m buffered polygon) | — | 3,787 captures | $0 |
| Pano-only filter | 3,787 | 3,429 (91%) | $0 |
| 10 m spatio-temporal group | 3,429 | 30 primaries (25 w/ historic twin) | $0 |
| Haiku validity + visibility | 30 | 15 valid | **$0.06** |
| Haiku orient (per-sequence) | 13 sequences | 11× yaw=0, 2× yaw=180 | **$0.02** |
| Visibility-chain greedy | 15 | 14 | $0 |
| 4-way strip render | 28 panos | 112 strips | $0 |
| Opus 4.7 rating (prompt-cached, 4 strips/call) | 14 | 14 | **$0.98** |
| Opus 4.7 temporal twins (Fair+) | 3 | 3 | **$0.20** |
| **Total** | | | **$1.26** |

**Condition distribution**: 3 Good, 7 Satisfactory, 4 Fair, 0 Poor/Failed, 0 unknown.
**Distresses**: 26 total (12 crack_longitudinal, 7 patch_failure, 3 crack_transverse, 2 raveling, 2 utility_cut). 19 minor, 7 moderate.
**Viewport distribution**: forward 8, right 9, left 5, backward 4 — balanced, meaning the 4-way rendering pulled real signal from all sides (vs old 2-way which concentrated in forward).

### UI (shipped end of day)

**Routes added to `app/main.py`**:
- `GET /pavement` → loader page, picks the most recent run.
- `GET /pavement/{slug}` → same page, for a specific run.
- `GET /api/pavement/runs` → list of available runs.
- `GET /api/pavement/{slug}/bundle` → config + street + polygon + points + distresses + segments + temporal, one JSON.
- `GET /api/pavement/{slug}/file/{path}` → serves files from the run dir (strips, overlays). Path-traversal guarded via `Path.resolve()` + `relative_to(run_dir.resolve())`.

**New static files**:
- `static/pavement.html` — entry point with top bar (brand, street meta, condition summary chips, layer toggles, run picker), full-screen map, right-edge slide-in detail panel, legend, loader.
- `static/pavement.css` — Apple-flavor: one font (Inter), Apple condition palette (Good=`#34C759`, Satisfactory=`#0A84FF`, Fair=`#FFCC00`, Poor=`#FF9500`, Failed=`#FF3B30`), rounded pill buttons, subtle shadows, respects `prefers-color-scheme: dark` and `prefers-reduced-motion: reduce`, responsive to 720 px.
- `static/pavement.js` — Leaflet on CARTO Positron / Dark-Matter tiles:
  - Layer 1: faint gray street centerline.
  - Layer 2: OSM segments (LineStrings) colored by `condition_median`.
  - Layer 3: 14 camera-point circle markers colored by condition.
  - Layer 4: 26 diamond markers colored by distress severity.
  - Click segment → segment-summary panel. Click point → detail panel with 4 viewport thumbnails (overlays when distresses exist, plain strips with 60% opacity otherwise), distress list with distance badges, temporal twin block with critical/degrading/stable flag, Mapillary link. Click distress → open source-point panel and scroll the matching distress row into view (flash animation).
  - Full-screen lightbox on thumbnail click.
  - Escape closes panels + lightbox.
  - Run picker in top bar for multi-run navigation.

### Files by path
- New: `app/prompts/pavement.py`, `app/geoproject.py`, `scripts/run_pavement_assessment.py`, `static/pavement.html`, `static/pavement.css`, `static/pavement.js`
- Extended: `app/panorama.py`, `app/claude.py`, `app/osm.py`, `app/main.py`
- Updated: `CLAUDE.md` (full rewrite to reflect pavement direction)
- Dependencies added to environment (not requirements.txt yet): `shapely`, `mercantile`, `vt2geojson`, `py360convert`, `fastapi`, `uvicorn[standard]`

### Open follow-ups
- Run picker is populated but only one run exists. Nothing cross-run yet (e.g. city-wide summary).
- Historic twins show up in the primary's detail panel but aren't rendered as their own map entities — no side-by-side "then vs. now" yet.
- Orientation detection is single-probe per sequence. On ambiguous intersection panos (like `753395900511661`) Haiku's pick is non-deterministic between adjacent runs. Mitigation (not implemented): majority vote across 3 panos per sequence, or GPS-delta sanity check against `compass_angle`.
- No segment-level detail page / "top 5 worst stretches" view.
- Layer-toggle state isn't in the URL (refresh resets toggles).
- Backward strips on roof-mounted rigs still contain mostly the vehicle's own roof/trunk — physically unavoidable at pitch −25° for those camera configurations. Opus correctly returns few/no backward distresses for those; no action needed.

---

## 2026-04-24 (evening) — Agentic pivot: Opus as autonomous surveyor

### Decision
Abandon the batch pipeline as the hero demo. Volume-of-images is not a compelling story. **New hero:** Opus 4.7 drives the survey itself via a tool-use loop — decides where to look, which pano to fetch, which viewports to extract, and records pavement + hazard findings in one pass per image. This is the agentic-capability demo the hackathon is about.

### Demo target
- **One block:** Broadway between Canal St and Broome St, SoHo, Manhattan, NY 10013. Chosen for (a) top-tier Mapillary coverage density, (b) visible pavement wear from truck traffic + utility cuts + manhole settlement, (c) varied signage/scaffolding/hazard content, (d) iconic location for a demo video.
- **No live demo.** Run locally, log every agent turn to `agent_trace.jsonl`, UI replays as an animation. Recording only.
- **Budget:** hard cap $20 Opus + 80 turns.

### Plan (2-day build)

**Day 1**
1. Scaffold `app/agent/` — `tools.py` (JSONSchema + implementations), `loop.py` (Anthropic SDK tool-use loop with prompt caching, context pruning to last 2 image blocks, turn/cost caps), `trace.py` (JSONL writer), `prompts.py` (unified pavement + hazard rubric).
2. Implement the 7 tools as thin wrappers over existing `app/mapillary.py` / `app/panorama.py` / `app/osm.py`. Contract-stable names: `get_network_summary`, `list_captures_near`, `fetch_pano_thumbnail`, `extract_viewport`, `record_finding`, `mark_segment_covered`, `done`.
3. `scripts/run_agent_survey.py` — entrypoint. Seeds the network from Overpass (Broadway, Canal, Broome, and the two side-street stubs inside the block). Starts the loop.
4. First end-to-end dry run on the SoHo block. Expect iteration on the system prompt (too exploratory / too rigid / wrong viewport choice).

**Day 2**
5. Tune the agent — enough runs to get a trace that tells a coherent story in the video (~30–60 turns, roughly one pano per ~20 m, decisions visibly varied).
6. Build replay UI (`static/agent.html|css|js`): left-side live transcript, right-side map with moving agent marker and pinning findings, timeline scrubber, 1×/2×/8× playback. Inherits styling from `static/pavement.css`.
7. Record the demo video.

### Explicit non-goals for the agent run
- Temporal twins / historic comparison. Parked.
- Multi-block sweeps. One block only.
- Live streaming of the run. Replay only.
- Keeping the batch pipeline as a user-facing feature. It stays in the tree as a reference, not as the ship surface.

### Open questions (resolved during Day 1 build)
- **Image caching vs pruning** — resolved in favor of context pruning (`keep_last_n_images=2`). Image blocks in `tool_result` messages get rewritten to text placeholders after turn N+2. Prompt caching is applied ONLY to the text system message (7.9k chars). Net effect: per-turn input tokens stayed under ~15k even at turn 58.
- **Agent position** — resolved to the camera GPS of the last-fetched viewport/finding. Clear map animation; see `AgentState.note_visit`.
- **Coverage logic** — resolved to Opus-decides, with the heuristic "≥1 finding within 30 m of every 20 m of length" baked into the system prompt. In practice Opus marked 6/30 segments in the validation run — honest about coverage it couldn't verify.

### Day 1 ship (shipped 2026-04-24)

**Agent package** (`app/agent/`)
- `state.py` (~260 lines) — `AgentState`, `Segment`, `Primary`, `Finding`, GeoJSON dumpers, `haversine_m` helper.
- `trace.py` (~130 lines) — `TraceWriter` for JSONL, `prune_image_blocks` for bounded context.
- `tools.py` (~600 lines) — 7 tools with JSONSchema definitions where the `description` field carries decision heuristics (cached as part of the tool list). `TOOL_DISPATCH` maps to async implementations.
- `prompts.py` — 7.9k-char system prompt (rubric + hazard taxonomy + heuristics + narration directive + output discipline) + `build_seed_user_message` for the per-run mission.
- `loop.py` (~230 lines) — tool-use loop with prompt caching, context pruning per turn, budget + turn caps, graceful termination warnings at 85% budget and 3-turns-before-cap.

**Entrypoint**
- `scripts/run_agent_survey.py` — resolves block preset, fetches roads (Overpass), captures (Mapillary zoom-14 tiles), spatio-temporal dedup at 12 m, bulk Graph API for detail, builds state, runs loop. Presets include `soho_broadway` (Broadway/Canal→Broome, Manhattan, NY 10013).

**Backend routes added** (`app/main.py`)
- `GET /api/agent/runs` — list all runs with config + state.
- `GET /api/agent/{slug}/bundle` — config + polygon + network + primaries + findings + state in one JSON.
- `GET /api/agent/{slug}/trace` — JSONL trace (streamed).
- `GET /api/agent/{slug}/file/{path}` — path-traversal-guarded file server (viewports, panos, probes).
- `GET /agent`, `GET /agent/{slug}` — replay page.

**Replay UI**
- `static/agent.html` — topbar (brand + run meta + run picker + play/pause + 1×/2×/4×/8×), transcript column (440 px), map stage, HUD (turn/findings/segments/budget), legend, lightbox, bottom scrubber.
- `static/agent.css` (~500 lines) — imports pavement.css tokens, adds agent-specific styles, Apple-flavored (condition palette, dark-mode responsive, reduced-motion honored).
- `static/agent.js` (~480 lines) — replay engine: loads bundle + trace, builds turn events, applies state-delta per turn, renders transcript cards (reasoning, tool-call, finding, done), animates map (pulsing agent cursor, finding pins colored by condition, dashed agent path, segment recolor on cover). Scrubber reconstructs state deterministically turn-by-turn.

### Validation run (shipped)

Run: `soho_broadway_canal_broome` — Broadway between Canal St and Broome St, Manhattan, NY 10013.

| Stage | Count | Cost |
|---|---:|---:|
| Overpass road segments | 30 | $0 |
| Mapillary raw captures in polygon | 606 | $0 |
| Pano filter | 45 | $0 |
| Spatio-temporal dedup @12 m | 21 | $0 |
| Graph API bulk detail | 21 | $0 |
| Agent loop (Opus 4.7 + Haiku prescreen) | 58 turns | **$8.11** |
| **Total** | | **$8.11** / $15.00 cap |

**Agent behavior:** 13 haiku_prescreen calls → 16 extract_viewport calls → 9 record_finding calls → 6 mark_segment_covered → 1 done. Ratio ≈ 1.8 viewports per finding. Narration rate: 38/58 turns with text (65%) after the "narrate every turn" directive was added.

**Findings:** 9 total.
- 4 Fair, 5 Satisfactory, 0 Poor/Failed, 0 unknown.
- 19 pavement distresses (7 crack_longitudinal, 6 utility_cut, 3 patch_failure, 2 raveling, 1 edge_break). All with pixel bboxes.
- 13 hazards (6 construction_zone, 3 faded_markings, 2 sightline_obstruction, 1 sidewalk_damage, 1 drainage_issue). All with pixel bboxes (schema tightened after the dry run).

**Agent's own summary:** "Broadway shows manhole settlement and longitudinal cracking (esp. broadway_1 near Grand, F004), and Centre St has the worst condition with longitudinal cracks, raveling, and edge break (F006). Top inspector priorities: (1) Broadway @ Grand St — settled manhole and patched lane (F004); (2) Centre St near Howard — moderate raveling/edge break and catch-basin cracking (F006); (3) persistent scaffolding sheds along Lafayette/Centre creating ongoing pedestrian sightline and sidewalk shed hazards."

### Next steps (ordered by priority, before hackathon submission)

**Must-do before submission**

1. **Polish pass on the replay UI** (~30 min). Open [http://127.0.0.1:8000/agent/soho_broadway_canal_broome](http://127.0.0.1:8000/agent/soho_broadway_canal_broome), click through one full play, scrub around, open a few lightboxes, note any visual/interaction glitches. Fix anything that jumps out. Check dark-mode by toggling system appearance. Check reduced-motion.
2. **Record the demo video** (~1 hr). QuickTime screen record at 1440×900 (or whatever renders cleanest), 2× playback speed → full run replays in ~25 s. Two takes: one silent, one with voice-over / captions explaining what the agent is doing at key moments (first `haiku_prescreen`, first `extract_viewport`, first `record_finding`, `mark_segment_covered`, `done`). Crop / trim to 60–90 s for the submission.
3. **README update** (~15 min). One short section at the top pointing to the `/agent` demo — command to start uvicorn, URL, what the viewer is looking at. Anyone cloning the repo should reach the demo in <2 minutes.

**Nice-to-have if time permits**

4. **Second run on a rougher corridor** (~30 min + ~$10 budget). SoHo Broadway is in genuinely decent shape → 0 Poor / 0 Failed findings. A visibly-rougher location would let the video show the full 5-tier spectrum. Candidates: an industrial stretch of Queens/Bronx, a heavily-patched side street, or a rural corridor with chipseal wear. Just needs a new `BLOCKS` preset in [scripts/run_agent_survey.py](scripts/run_agent_survey.py) — copy the SoHo rectangle approach.
5. **Bbox overlays on viewport thumbnails** (~40 lines of JS). Data is already in `findings.geojson` (every distress + hazard has a 768×512 pixel bbox). Draw them as SVG rectangles over the thumbnail `<div>` in the transcript cards. Small but visually striking in the video.
6. **Hide `_dryrun` runs from the run picker** (~5 min). Filter by slug regex in [app/main.py](app/main.py) `list_agent_runs`.
7. **Drop image blocks on viewports older than N findings ago.** Current pruner keeps the last 2 image blocks across the full message history. For very long runs this could become a budget issue; not a concern at 58 turns but note it.

**Parked (post-hackathon)**

- Idempotent loop resume — reconstruct `messages` from `agent_trace.jsonl` on restart.
- Historic imagery / temporal twins — Mapillary sparsity makes this corridor-dependent.
- Multi-block sweeps, city-wide views.
- Live demo mode (streaming tool-use, not just replay).
- Bbox back-projection to world coordinates for a "distress density" heatmap (the batch pipeline's `app/geoproject.py` has this already).

### Known limitations to be honest about in the demo

- **Condition mix was safe.** 0 Poor, 0 Failed in the SoHo run — the real street is actually fine. Not a bug; feature 4 above fixes the demo optics.
- **Mapillary coverage is crowdsourced.** 7 of 30 segments had no usable imagery; the agent correctly acknowledged this in its summary.
- **Bbox coordinates are model-level approximate.** Good for human click-through, not surveyor-grade.
- **The agent is not an engineer.** Plain-language 5-tier output; no PCI/PASER/IRI.

---

## 2026-04-25 — Live transparency UI, fleet architecture, calibration crisis, LA pivot, and the street-walker direction

This is the biggest architectural arc of the project so far. Documented in
detail because the journey itself is part of the "Keep Thinking" submission
narrative.

### Phase 1 — `/live/{slug}` transparency feed

**Why:** The `/agent/{slug}` replay UI animated a finished trace, but mid-run
the user couldn't tell what the agent was doing or why. Wanted a real-time
"every move, every intermediate output, every image" view.

**Done:**
- `static/live.html` — single-file polling page. One card per trace event,
  color-coded by event type (assistant / prescreen / viewport / finding /
  done). Inline viewport JPEG embeds so the operator sees what the agent
  sees.
- `app/main.py` — added `/api/agent/{slug}/trace/tail?offset=N` (byte-offset
  tail of `agent_trace.jsonl`) + `/live` and `/live/{slug}` routes.
- `scripts/run_agent_survey.py` — added `--slug` override so live runs can
  use a separate run dir from the validated $15 SoHo run.

**Validated $2 SoHo run:** $1.85 / 20 turns / 2 findings / clean `done`.
About 6× cheaper per finding than the $15 run and the live page kept
streaming the entire time.

**Problems hit + fixes:**
- Windows `cp1252` stdout choked on the `→` in the orchestrator's progress
  prints. Fix: `PYTHONIOENCODING=utf-8` on every Bash invocation that runs
  Python scripts.
- `uvicorn --reload` was started without `--reload` initially, so route
  additions weren't picked up. Restarted with the flag; intermittently
  WatchFiles got stuck mid-reload — when that happens, kill via `TaskStop`
  and restart manually.

### Phase 2 — Fleet architecture (Pattern 3 from the strategy doc)

**Why:** The single-loop $15 SoHo run cost $0.90 per finding because every
turn re-reads the entire growing conversation history (≈70 % of every dollar
went to the model re-reading itself). That economic model can't reach
city-scale at any reasonable budget. Strategy doc Pattern 3 says
`Opus(narrow_subtask) × N` beats `Opus(everything)`.

**Reframing for the user:** "agentic capability" doesn't mean one giant
multi-turn agent — the winning pattern is a Coordinator + parallel narrow
sub-agents (CrossBeam) plus structured-output and deep-dive Inspector tiers.
This actually showcases Opus 4.7 *more* (three Opus instances at different
scopes) while being cheaper.

**Done:**
- `app/agent/worker.py` — narrow Worker loop. Tool surface trimmed to 4 tools
  (`haiku_prescreen`, `extract_viewport`, `record_finding`, `done`). System
  prompt shrunk from 7.9 KB → 2.3 KB. Cluster scope (3–5 panos) instead of
  city scope. Per-worker budget cap.
- `scripts/run_fleet_survey.py` — Python-orchestrated parallel dispatch.
  Clusters primaries by Mapillary `sequence_id`, fires N workers via
  `asyncio.gather` with a `Semaphore`. Shared `fleet_trace.jsonl` with
  `worker_id` on every event, lock-protected.
- `app/main.py` — `/api/fleet/{slug}/trace/tail` + `/fleet`/`/fleet/{slug}`
  routes mirroring the agent set.
- `static/fleet.html` — color-coded-by-worker live feed. Each event card has
  a worker chip + worker-color left border. Workers' events interleave in
  real time as the parallel story.

**Validated $2 SoHo Broadway fleet run:**
- $1.79 / $2.00 cap, 6/6 workers completed, 13 findings, 89 s wall clock.
- $0.14 per finding — **6.8× cheaper** than the single-loop architecture on
  the same block. Worker W02 found the right tempo (3 turns, $0.32, 4
  findings = $0.08 / finding).
- All tier conditions represented; W02 caught Fair-tier patches and faded
  markings on Centre St near Howard.

### Phase 3 — Bbox-overlay generation & quality crisis

**Why:** With 19 distresses + 13 hazards in the fleet run, we wanted to
visually inspect the bboxes Opus was drawing.

**Done:**
- `scripts/draw_bbox_overlays.py` — reads any agent or fleet trace, draws
  per-finding distress (red) + hazard (amber) rectangles on the
  corresponding viewport JPEG. 7 overlays generated for the SoHo fleet run.

**What we found (looking at the actual overlays):**
- **Bboxes are huge.** Distress rectangles cover 30–50 % of the image. Not
  surveyor-grade localization — region-of-interest at best.
- **Painted markings misclassified as patch failures + cracks.** A worn
  yellow chevron and bike-lane symbol got bracketed as `patch_failure` and
  `crack_longitudinal`.
- **Vehicle body contamination.** Bottom 25–30 % of viewports is the camera
  vehicle's own panel; bboxes extended into it as distresses.
- **Hallucination on occluded pavement.** When parked cars filled the lane,
  Opus still recorded `crack_longitudinal · minor` on the unviewable
  asphalt.

**Honest diagnosis (root causes, not surface fixes):**
1. **Resolution.** A 768×512 viewport covers ~40 m of road; a 2 mm hairline
   crack at 4 m is 0.1 px. Below the sampling rate. The model is hallucinating
   cracks because it cannot see them and the prompt asks for them.
2. **Viewport geometry.** ~20 % of pixels are pavement. The other 80 % is
   sky / sidewalks / parked cars / vehicle body. Wrong signal-to-noise.
3. **Vision LMs are not pixel-precise object detectors.** Opus 4.7, GPT-4V,
   Gemini — none compete with YOLO/DETR at tight bbox tasks. Loose region
   selection is what they're good at.
4. **No reference exemplars.** The prompt lists distress *names* and
   one-line definitions. Specialized visual concepts (alligator vs block
   vs utility cut) need few-shot exemplar images to calibrate, which we
   don't have.
5. **Prompt is permissive.** No penalty for false positives. Default
   to recall over precision.

**Solution direction:** stop claiming pixel-precise bbox localization. Pivot
the deliverable to per-location condition tier + plain-language rationale,
which is what Opus is genuinely strong at AND what public-works engineers
actually want for triage. Bboxes stay as advanced metadata, hidden by
default.

### Phase 4 — Strategy review + LA pivot

Reviewed the hackathon judging criteria (Impact / Demo quality / Opus 4.7 use
/ Depth). The strategy notes (synthesized from the organizer talks) emphasized:
- Domain expertise visible (P1)
- Skills-as-knowledge-graph (P2)
- Parallel narrow agents (P3)
- Async (P4)
- **Boots-on-the-ground validation (P5) — the biggest gap in our SoHo demo.**
- Demo discipline (P6)
- Keep-Thinking pivot signal (P7)

Cued the user to switch the validation target from NYC to LA — they have
domain depth there AND LA publishes one of the cleanest segment-level PCI
datasets in the country.

**Done:**
- `scripts/download_la_pci.py` — paginated download of the LA StreetsLA PCI
  feature service. Found the actual ArcGIS Online item by searching the AGOL
  REST endpoint (the `geohub.lacity.org` Hub UI is JS-rendered and 404'd in
  WebFetch). Item id: `8439cf3e810b46489956ad5450875e2a` →
  `https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/arcgis/rest/services/StreetsLAMap_20240812_LM/FeatureServer/18`.
  All 69,282 LA street segments with `PCI`, `STATUS`, `ST_NAME`, `SECT_ID`,
  LineString geometry. 40 MB GeoJSON in `data/la_pci/segments.geojson`.

**Tier mapping (industry-standard PCI thresholds):**
- Good ≥ 86 (25 % of segments)
- Satisfactory 71–85 (34 %)
- Fair 56–70 (19 %)
- Poor 41–55 (7 %)
- Failed < 41 (14 %)

### Phase 5 — Calibration set v1

**Why:** Without ground truth, every prompt change is faith. Built a
50-sample stratified test set in DTLA → Mid-Wilshire → Boyle Heights bbox.

**Done:**
- `scripts/build_calibration_set.py` — for each of 5 PCI tiers, randomly
  sample segments inside the study bbox, find the nearest Mapillary 360°
  pano within 20 m of segment midpoint, download the `thumb_1024_url`.
  Outputs: `manifest.json`, `segments.geojson`, `panos.geojson`, 50
  thumbnails.
- `app/main.py` — `/api/calibration/runs`, `/api/calibration/{slug}/bundle`,
  `/api/calibration/{slug}/thumb/{pano_id}`, `/calibration/{slug}` page
  routes.
- `static/calibration.html` — Leaflet map with segments + pano points
  colored by tier, dashed link lines visualizing the ≤20 m matching
  distance, sidebar of clickable sample cards, popup with thumbnail.

**Problem hit:** Initial bbox was so Mapillary-dense (cell-phone uploads
dominated) that the existing `fetch_captures` cap of 2 000 produced **only
4 panos** out of 2 000 captures. **Solution:** added `panos_only=True` flag
to `fetch_captures` that filters non-panos at *tile-parse time* before the
cap. With it on, the same bbox returned 10 000 panos.

**Result:** 50 samples / 10 per tier / all matched panos within 20 m.

### Phase 6 — Calibration runner v1 (single-call grader)

**Done:**
- `scripts/run_calibration.py` — for each sample, send the 1024×512 equirect
  thumbnail to a single Opus call with a focused tier-classification prompt.
  5×5 confusion matrix, per-tier P/R/F1, Fair-or-worse binary recall,
  off-by-one accuracy. Saves `predictions_<runid>_<model>.json` so prompt
  versions can A/B head-to-head.

**Result:** **14 % 5-way accuracy** on Opus 4.7. **20 % Fair-or-worse
recall.** Worse than random for 5-way (random = 20 %). Confusion matrix
revealed the model averaging everything to "Satisfactory" — 29 of 50
predictions were Sat regardless of expected tier. Zero Poor or Failed
predictions. Cost $1.45 / 50 ($0.029 per sample).

### Phase 7 — Agentic calibration runner (look + grade tools)

**Why:** User pushed back on single-call grading: *"can't we generate a tool
that pitches and yaws and do whatever to the pano and Opus will figure out
which location it needs? I want to utilize agentic capability."* Right call.
The 360° pano has multiple regions; the model should choose viewports
itself.

**Done:**
- `app/agent/pano_inspector.py` — narrow per-pano agentic loop with two
  tools:
  - `look(yaw_deg, pitch_deg, hfov_deg, purpose)` — render a rectilinear
    viewport at any angle. Mapillary forward-direction = yaw 0; pitch -30 is
    pavement-forward; hfov 40-50 is zoomed in.
  - `grade(tier, confidence, rationale)` — submit final + terminate.
  Tight system prompt: tier rubric, visual-confuser list, "no Sat hedging on
  bad pavement" rule, recommended scan plan.
- `scripts/run_calibration_agentic.py` — dispatches 50 inspectors in
  parallel via `asyncio.gather` + semaphore. Same metric machinery as v1.

**Smoke test (5 samples):** revealed inspectors weren't committing — 4/5
hit turn cap without grading. They kept exploring "to confirm." Fix: added
strict commitment language to the prompt + a forced "FINAL TURN: you must
call `grade` now" message injected at turn `cap-1`. After fix: 100 %
commitment, avg 3.6 turns.

**Agentic v1 result:** 20 % 5-way / 30 % Fair-or-worse recall / first-ever
correct Poor prediction. Better than baseline on every accuracy axis. Cost
$7.40 / 50 ($0.148 / sample, ~5× v1 baseline).

### Phase 8 — Full-res panos

**Why:** v1 used the 1024 thumbnail. Hypothesis: full-res panos (5760×2880)
let the inspector see actual defects.

**Done:**
- `scripts/download_calibration_fullres.py` — fetched `thumb_original_url`
  for all 50 samples (185 MB total).
- `app/agent/pano_inspector.py` updated to take an arbitrary equirect path.
- `scripts/run_calibration_agentic.py` updated to prefer `pano_path` over
  `thumb_path`.

**Result:** still **20 %**. Resolution wasn't the dominant ceiling. We did
get our first correct Poor prediction (sample 44) and avg viewports dropped
from 3.0 → 2.4 (commit faster on clean views), but the headline number
didn't move.

### Phase 9 — Self-inspection of pano anatomy (the user's "you idiot" moment)

The user pointed out that I'd been guessing at pano structure rather than
*looking at one*. Rendered standard viewports (forward, side, back, down)
from Good / Fair / Failed samples and inspected them via the Read tool.

**Critical discovery: panos in our calibration set come from a MIX of
capture rigs, not just car-mounted:**
- Sample 1 (Good, MERWIN ST) — pedestrian holding a 360 stick on a
  staircase. Bottom band shows the operator in a red shirt with their phone.
  No road in frame to grade.
- Sample 2 (Fair, ELMORAN ST) — pedestrian chest-mount on a hiking trail.
  Bottom shows the operator's lap and shoes on dirt. **No paved road exists
  at this location.**
- Sample 3 (Failed, WEST SILVER LAKE DR) — actually a tripod / pedestrian
  beside parked cars. The car-shaped object in the frame is a *different*
  parked car, not the camera carrier.

**Solution:** rewrote the inspector system prompt with a `# What a Mapillary
360° panorama actually looks like` section based on what we actually
observed:
- Car/SUV roof rig (dark glossy roof at bottom)
- Pedestrian handheld stick (operator's head + arms + phone at bottom)
- Pedestrian chest/helmet mount (torso + lap + shoes)
- Bicycle/scooter rig (handlebars + helmet)
- Tripod (legs + small ground patch)
Plus a "many panos do NOT show paved road at all" warning with a list of
non-road indicators (sidewalks, plazas, dirt, staircases, walls) and an
explicit "grade `unknown` rather than hedge to Sat when no road is visible"
rule.

**Agentic v2 result (full-res + new prompt):** still **20 %**. A few more
correct Poor predictions but Sat-bias remained.

### Phase 10 — Road-visibility filter on the calibration set

**Why:** if 5–10 of our 50 samples genuinely have no road in frame, we're
measuring against impossible ground truth on those. Filter them out and
re-measure cleanly.

**Done:**
- `scripts/filter_calibration_road_visible.py` — for each sample, render a
  2×2 grid of cardinal viewports (forward / right / back / left at
  pitch -30°, hfov 80°), send to Haiku with a "is paved vehicular road
  visible in any quadrant?" prompt. Outputs annotated `manifest.json` with
  `road_visible` per sample, plus `manifest_clean.json` containing only the
  road-visible subset.
- `scripts/run_calibration_agentic.py` — added `--manifest-name` flag so
  runs can target the cleaned subset.

**Filter cost:** $0.06 total (50 Haiku calls).
**Filter result:** 5/50 dropped. Specifically: ELMORAN ST trail (dirt only),
two pedestrian-staircase shots, an "all dirt + construction equipment"
shot, an "all body + tree + concrete wall" shot.

**Agentic v3 (clean) result:** still **20 % 5-way**. The 5 dropped panos
weren't the dominant reason for low accuracy.

### Phase 11 — The actual diagnosis (the perception-vs-PCI gap)

Pulled the 4 Failed → Good disagreements and looked at the model's
rationales:
- **NORMANDIE AV (PCI 2.28)** — model: "Concrete freeway, uniform surface,
  only construction joints visible." This *is* a Caltrans concrete
  freeway. PCI 2 likely reflects subsurface laser-measured rutting + IRI
  roughness scoring; the surface visually looks fine.
- **ROSEMONT AV (PCI 0)** — model: "highway pavement uniform, just lane
  markings and tire wear sheen." Another freeway/highway segment with
  bogus or laser-only PCI.
- **6TH ST (PCI 13, 17)** — model: "uniform asphalt, no visible
  cracking/raveling/patches, crisp double-yellow." 6th St is a real
  downtown LA street; LA's van rated it Failed using subsurface
  measurements that don't show in optical imagery.

**Key insight:** the model isn't wrong. **PCI scoring uses laser-measured
rutting + IRI that aren't visible at street-level photography distances.**
Many "Failed" segments in LA's dataset look visually intact. We're asking
optical imagery to predict something it cannot perceive.

**Coarser-bucket metrics on v3:**
- 3-way (OK / MID / BAD): 42.2 %
- 2-way (Triage / Fine): 48.9 %
- TRIAGE precision: **70 %** ← when model says "needs inspection," it's
  reliable
- TRIAGE recall: 29 % ← but it misses 70 % of bad pavement

The model has high-precision low-recall on the binary that matters for
triage. That's a usable signal but the recall ceiling is real and
measurable.

### Phase 12 — Architectural pivot: Street Walker

User reframed the project. **Stop pre-picking 50 panos against PCI scores.**
Mapillary is crowdsourced — most images are noise. The right use of Opus's
agentic capability is as the **brain that filters that noise**:

- Pick a target *street*, not random panos
- Walk it end-to-end at fixed waypoint spacing (variable, default 50 m)
- At each waypoint: query Mapillary candidates within ~25 m, peek a few of
  N (cheap Haiku quality probe), pick the best one (recent + correct
  rig + clear road), grade pavement, advance
- Spatial state in the agent's context — it knows where on the map it is,
  which segment it's on, what it's already seen
- Validate per-segment grades against LA PCI (still our ground truth, but
  applied per-segment rather than per-arbitrary-pano)

This explicitly demonstrates the agentic capability the strategy doc
rewards: spatial reasoning, image-quality judgment, sampling strategy,
narrative trace. And it sidesteps the calibration-set quality issue —
the agent picks its own evidence rather than being handed broken pairs.

**Plan saved to:** `STREET_WALKER_PLAN.md` (next document).

**Out of scope for this iteration:** parallel walker fleets, $500 city-scale
demo, adaptive waypoint spacing. Single 1-mile corridor first; scale
afterward.

---

## 2026-04-25 — Skills library: research-grounded engineering depth

This is the Pattern 2 / Mike Brown CrossBeam shape applied to pavtrace. The
walker's monolithic system prompt was extracted into 12 curated skill files
backed by authoritative open-source pavement-engineering references. The
skill is the product, not the code.

### What was built

`.claude/skills/pavement/` directory with 12 `.md` files:

**Tier 1 — core grading skills:**
- `tier_rubric.md` — 5-tier scale with per-tier ATTRIBUTE TABLES
  (surface uniformity, crack density, crack width thresholds in mm,
  patches, rutting, edge condition, markings, raveling). Tier→PCI band
  mapping per ASTM D6433-21 [2]. Disqualifiers per tier
  ("ANY visible crack > 2mm → not Good").
- `distress_taxonomy.md` — 10 distress types (longitudinal/transverse/
  alligator/block cracking, pothole, patch failure, raveling, edge break,
  rutting, utility cut). Each: definition + mechanism + visual cues +
  severity bands with mm thresholds + common confusers. Notes the
  ASTM-19 → our-10 collapse explicitly.
- `visual_confusers.md` — paint vs crack, manhole vs pothole, shadow vs
  distress, oil vs raveling, sealcoat sheen vs raveling, sand vs degraded
  surface, wet pavement vs depression, patch vs alligator. Each pairing
  has explicit "tells" and a default rule under uncertainty.
- `grade_discipline.md` — order of operations before grading,
  under-report rule (Good/Sat/Fair only — does NOT apply to Fair/Poor/
  Failed), confidence calibration (0.9-1.0 → unmistakable; 0.5-0.7 →
  borderline; < 0.3 → grade unknown), anti-pattern catalog from prior
  walker iterations.

**Tier 2 — operational discipline + geometry:**
- `pano_anatomy.md` — Mapillary equirect coordinate system; capture rigs
  (car/SUV/pedestrian-stick/pedestrian-chest/bicycle/tripod) and where
  the carrier sits per rig.
- `viewport_geometry.md` — yaw/pitch/hfov conventions; minimap
  interpretation (band crop +10° to -60°, rectangle position by pitch,
  rectangle size by hfov); zoom math (hfov=30 = 2.3× detail).
- `scan_plan.md` — recommended workflow per waypoint with branches
  for blocked-forward, no-candidates, all-views-carrier-dominated.
- `zoom_investigation.md` — when to zoom (any hint of distress at
  hfov ≥ 70), how to zoom (yaw same, pitch slightly steeper, hfov 30-45),
  common patterns (zoom on wheelpath, intersection, patch boundary,
  near lane line, on dark spot for pothole-vs-shadow), pixel arithmetic
  (5 mm crack at 8 m subtends 0.036°; visible at hfov ≤ 35).

**Tier 3 — engineering depth (advisory):**
- `deterioration_progression.md` — the asphalt S-curve with 3 phases.
  Crack progression sequence (hairline → block → wheelpath
  longitudinal → alligator → potholes). Crack-widening rates by
  climate. Service-life expectations per treatment type. Cost-deferral
  economics ("$1 now / $4-7 later" cited from FHWA [7]).
- `treatment_signatures.md` — 10 treatments (crack seal, fog seal,
  slurry, microsurfacing, chip seal, cape seal, thin overlay,
  mill-and-overlay, hot in-place recycling, full-depth reclamation /
  reconstruction). Each: visual signature when fresh, service life,
  trigger condition, grading implication ("a fog seal-treated pavement
  should grade Good or Sat — the surface was just preserved, don't
  penalize the underlying age").
- `climate_failure_modes.md` — freeze-thaw vs hot-arid vs coastal vs
  heavy-truck. LA-specific guidance: "block cracking + fading + light
  raveling without alligator is the typical Phase 2 signature for LA's
  hot-arid climate; don't over-call as Poor."
- `repair_priority_logic.md` — risk × consequence framework that public-
  works dispatchers actually use. Modifiers (pedestrian exposure, ADA
  compliance, drainage adjacency, 311 reports). What this means for
  the agent's rationales: "make rationales actionable" (location, type,
  severity, surrounding context).

### Skill loader

`app/agent/skill_loader.py` reads the 12 skill files (strips YAML
frontmatter, joins bodies with `----------` separators), assembles the
walker's system param into **2 `cache_control: ephemeral` blocks**:

  Block 1 (60 KB, "core engineering knowledge")  → ROLE preamble +
    tier_rubric + distress_taxonomy + visual_confusers +
    deterioration_progression + treatment_signatures +
    climate_failure_modes + repair_priority_logic
  Block 2 (30 KB, "operational discipline + geometry")  → pano_anatomy +
    viewport_geometry + scan_plan + zoom_investigation + grade_discipline

Total system prompt: ~90 KB. The walker's `WALKER_SYSTEM_PROMPT` constant
is now legacy (kept for reference, no longer used at runtime); the live
system param is `compose_walker_system()` from `skill_loader.py`.

### Cache-economics validation (v5 trace)

  Turn 1:  cache_write 38,392 tokens   ← system prompt cached
  Turn 2:  cache_read 26,262 tokens
  Turn 3+: cache_read 38,000-50,000 tokens per turn
           input (uncached growing context) 2,500-5,000 tokens per turn

Turn 1 cost: ~$0.42 (cache-write at 1.25× input price).
Turn 2+ cost: ~$0.07-0.12 per turn (cache-read at 0.10× + small uncached
  context + output). Skills overhead per-turn is essentially free.

Total v5 SPRING ST 4-waypoint run: **$5.14**, 30 turns, 17 looks (10
zoomed), 4 look_around, 4 graded. Cost is up vs v4 ($2.35) but the
agent's reasoning depth is markedly higher.

### Demonstrated agentic improvements

Direct quotes from v5 trace narration showing skill-vocabulary in use:

> "The pavement shows clear longitudinal cracks running along the
>  wheelpath ... cracks appear to be moderate-width" (distress_taxonomy
>  + severity bands)
> "I can see a manhole cover ahead (round, intact - normal)"
>  (visual_confusers default — manhole is intentional, not pothole)
> "Mottled with darker patch boundaries" (rubric Phase-2 vocabulary)
> "transverse and possibly longitudinal cracks" (correctly using
>  taxonomy names, not casual descriptors)
> "Light tonal mottling - typical aging" (deterioration_progression
>  Phase-1 calibration)

Compared to v4, the agent's grade rationales are now **specific** and
**rubric-grounded**, not vibe-based. The "looks weathered" anti-pattern
is gone.

### Sources cited (full bibliography in `research/source_index.md`)

[1] FHWA Distress Identification Manual for the LTPP (FHWA-HRT-13-092, 2014)
[2] ASTM D6433-21
[3] PASER Asphalt Manual (UW-Madison TIC)
[4] Pavement Interactive — distress reference desk
[5] Washington Asphalt Pavement Association
[6] FHWA Pavement Preservation Program
[7] FHWA — Pavement Preservation: Preserving Our Investment in Highways
    (Public Roads, 2000) — origin of the "$1 now / $4-7 later" claim
[8] FHWA — Preventive Maintenance Treatments Instructor's Guide
[9] Caltrans MTAG Volume I — Flexible Pavement Preservation
[10] Pavement Tech Inc — Asphalt Surface Treatment Methods
[11], [14] FHWA + VTTI deterioration-curve references
[15], [16], [17] Climate-failure-mode references
[18] City of LA StreetsLA PCI dataset (our local ground truth)

### What this lifts in the writeup

This refactor moves the project squarely onto strategy doc Pattern 2
("the skill is the product, not the code"). The `.claude/skills/`
directory is now substantively larger than the application code that
loads it — the same shape that defined Mike Brown's CrossBeam and
the other Opus 4.6 winners. Judges evaluating axis 3 (Opus 4.7 use)
and axis 4 (Depth) can read the skill files and see real engineering
substance. The walker's behavior demonstrably tracks the skills'
guidance.

### Out of scope for this iteration

- Per-skill A/B (which skill changed which grade). Defer.
- Reference imagery in skills (was planned; deferred — text + citations
  proved sufficient given the agent's existing visual-concept training).
- Hazard-side skills (sidewalk damage, signage, drainage). Walker doesn't
  grade hazards yet.

---

## 2026-04-25 — v6 zoom_into_region: agent-picked bbox zoom

### The problem v5 surfaced

User inspecting v5's zoom calls noticed a critical failure: when the agent
called `look(yaw=180, pitch=-25, hfov=40)` to zoom on a back view, the
PITCH actually went DOWN from the source view's `pitch=-20`. On a car-rig
pano, deeper pitch puts MORE car body in frame, not less. The "zoom" rendered
~50% car hood, defeating the investigation.

User's diagnosis:
> "There is so much of pavement area that it could zoom on but still the
>  bottom part of the zoom, the bottom border is like being constant. It's
>  not like moving beyond the car's body. So OPUS should powerfully decide
>  which pixel, which part of the image to zoom. We should not hard code
>  which part you need to zoom or like what's the minimap. I want it to
>  very accurately find what's the pavement area and it knows which height
>  level it needs to zoom to and then get the image."

The right fix isn't more prompt rules ("always pitch up when zooming") —
it's a tool that lets the agent point at pixels in the previous viewport
and the system computes the right (yaw, pitch, hfov) automatically.

### What was built

**`zoom_into_region(image_id, source_yaw, source_pitch, source_hfov,
x1, y1, x2, y2, purpose)`** — new tool in `app/agent/street_walker.py`.

Agent specifies:
- The previous viewport's params (copied from the look() result caption)
- A bbox in normalized coords (0-1) of the SOURCE viewport's pixel space

System computes:
- `new_yaw = src_yaw + (bbox_center_x - 0.5) × src_hfov`
- `new_pitch = src_pitch − (bbox_center_y − 0.5) × src_vfov`
  (note: minus, because y=0 is top of viewport = higher pitch)
- `new_hfov = src_hfov × bbox_width_pct`
  (bbox width determines magnification)
- Renders at full equirect resolution → no pixelation

The agent's mental model becomes: "I can see the road in the upper portion
of my last view; let me bbox the upper portion and zoom there."

### Why this matters geometrically

Rectilinear projection from an equirectangular pano: a viewport at
`(yaw_c, pitch_c)` with `hfov` and aspect 1.5:1 covers angle ranges
`[yaw_c ± hfov/2]` × `[pitch_c ± vfov/2]` where `vfov = hfov × 0.667`.

The agent didn't know how to translate "I want to see the upper third of my
view" into "(yaw, pitch, hfov) values." Now they don't have to. The system
linearizes the projection (small-angle approximation works well near the
center) and re-renders.

### Skill updates

**`zoom_investigation.md`** — restructured to put `zoom_into_region` as the
PREFERRED zoom tool. New section "How to zoom — TWO tools, choose the right
one" with:
- Tool A (preferred): `zoom_into_region` for drilling into a region of
  the previous view
- Tool B: `look(narrow hfov)` ONLY when changing direction
- Bbox-width-to-zoom-factor table
- Example bboxes for the carrier-dominated case

**`viewport_geometry.md`** — added `zoom_into_region` to the "picking the
right viewport" quick-reference table.

### v5 → v6 head-to-head on SPRING ST first 4 waypoints

| Metric | v5 (look-with-narrow-hfov) | v6 (zoom_into_region) |
|---|---|---|
| Total tool calls | 30 | 27 |
| Looks via `look()` | 17 | 5 |
| Looks via `look_around` | 4 | 4 |
| Zooms via `look(narrow hfov)` | 10 | 0 |
| Zooms via `zoom_into_region` | 0 | **9** |
| Avg rendered pitch on zooms | -25° to -45° (carrier band) | **-7° to -17° (horizon band)** |
| Carrier in zoom views | 30-50% of frame | **0-15% of frame** |
| Cost | $5.14 | $5.00 |
| Rationale specificity (WP1) | "mottled with longitudinal cracks" | "5+ irregular pothole-repair patches of varying ages, mottled/blotchy" |
| Tier accuracy vs LA PCI | 0/4 | 0/4 (perception ceiling unchanged) |

### What v6 demonstrates visually

Three images from the v6 trace tell the story:

1. **Source `look(yaw=0, pitch=-20, hfov=80)`** — wide forward view; bottom
   ~35% is the white car hood/roof.
2. **`zoom_into_region` with bbox `(0.25, 0.15) → (0.85, 0.55)`** — agent
   picked the upper-center of the source. System rendered at
   `yaw=-4°, pitch=-12°, hfov=48°` (a 1.7× zoom in the horizon band).
   **Car body is 0% of the frame.** Just road + parked cars + opposite
   buildings.
3. **Tighter `zoom_into_region` at 3×** — agent picked an even tighter
   region. System rendered at `yaw=-6°, pitch=-10°, hfov=24°`. Frame is
   pure asphalt + manhole + paint markings — exactly the close-up
   pavement view the agent needed for distress investigation.

The minimap rectangles in the zoom views are SMALLER (zoomed in) and
POSITIONED in the upper portion of the band (above carrier). Agent
visually confirms it's looking at the right region.

### What v6 didn't change

- Tier accuracy vs LA PCI ground truth on SPRING ST first 4 waypoints
  is still 0/4. Same diagnosis as v5: SPRING ST PCI 8-9 reflects
  laser-measured subsurface failure that doesn't show in optical
  imagery. The walker is now seeing the pavement well; the pavement
  just doesn't visually present as Failed.
- This is a **modality limit**, not an agentic-architecture limit.
  Closing it requires either a different ground truth (311 reports,
  visibly-bad corridors) or a different sensor modality (we don't
  have access to ground-laser data).

### Tool surface — current full list (9 tools)

```
get_position()                    — orient at current waypoint (FREE)
find_candidates(radius_m=30)      — list nearby panos (FREE)
peek_candidate(image_id)          — Haiku quality gate (~$0.001)
look_around(image_id, pitch, hfov) — 2×2 cardinal grid in one call
look(image_id, yaw, pitch, hfov)  — render arbitrary rectilinear viewport
zoom_into_region(image_id, src..., x1, y1, x2, y2)
                                  — drill into bbox of last look
grade(tier, confidence, ...)      — record finding, advance
skip_waypoint(reason)             — advance without grading
done(summary)                     — terminate
```

### UI — `static/walker.html` changes

Added a `zoom_into_region` event renderer that displays:
- The zoom factor (e.g., "1.7× zoom")
- The source bbox `(x1, y1) → (x2, y2)`
- A "src → rendered" line showing both the source params and the
  computed render params
- The actual zoomed viewport with its minimap

Open at `http://127.0.0.1:8000/walker/spring_v6_zoom`.

### What this validates about the agentic story

The user's framing: "we should not hard code which part you need to zoom...
I want it to like very accurately find what's the pavement area and it
knows which height level it needs to zoom to."

`zoom_into_region` delivers exactly this. The agent reads the source
viewport, picks a bbox where the road actually is, and the system
computes the correct projection. The "where to zoom" decision is now
fully agentic — visible in the bbox the agent emits — and the math is
mechanical.

This pattern (let the model point at structured outputs the system can
interpret) is the right way to extend agentic vision capabilities.

### Out of scope for this iteration

- Bbox-coords-to-(yaw,pitch) math at the EDGES of the source view
  (perspective distortion makes the linear mapping inaccurate; for now
  agent uses center-third bboxes which the linearization handles well).
- Auto-suggesting bbox regions (e.g., haiku-pre-screening for "where's
  the road" in a viewport). Not needed — the model handles this itself.
- Stacked zooms (zoom on a zoom). Possible mathematically; agent hasn't
  needed it yet. The `source_*` args support arbitrary nesting if
  required.


---

## 2026-04-26 — v2 Evolution: Temporal walker, UI polish, real-data integrity

This is the day-of-submission session. Eleven phases (A–K), in chronological order: agent agency restoration, temporal multi-image investigation, temporal walker MVP UI, deterministic discipline gate, data-integrity hardening, LA corridor probe, prefetch optimisation, tier-rubric simplification, evidence extraction, minimap relocation, and v2 UI redesign.

### Phase A — Agency restoration (multi-image per waypoint)

**Problem (from observation of `spring_v6_zoom`):** the agent picked one image per waypoint and never tried alternatives. Across 4 waypoints with 4–10 candidates each, only 1 distinct `image_id` was ever look()-ed at.

**Root causes (audited):**

1. `find_candidates` sorted by recency-DESC and took top-10. In dense corridors that filled all slots with the most-recent year, hiding multi-year coverage entirely.
2. `look()` tool description said "RECOMMENDED PATTERN per waypoint: 1-2 looks max." Active anti-pattern in cached tool schema.
3. `peek_candidate` was framed as "Optional but recommended". Empirical: 0 peeks across the entire v6 run.
4. `keep_last_n_images=2` killed the model's visual memory across cross-temporal comparisons.
5. `per_waypoint_turn_cap=14` capped investigations before cross-witness was possible.

**Fixes shipped:**

- `_find_candidates_impl` rewritten to **stratify by year** — `min_per_year=6`, `limit=30`, sorted year-DESC + distance-ASC. Returns ALL years' top-N closest, not just the latest.
- Added `year_filter` parameter so the agent can explicitly fetch more candidates from a specific older year.
- Removed the "1-2 looks max" phrasing from `look()` description; replaced with "YAW MATCHING is non-negotiable when ≥2 years exist" + "10-20 look/zoom calls per waypoint is normal when temporal evidence is rich."
- Tightened `peek_candidate` description to "REQUIRED before look() on any older-year candidate (2018 and earlier)."
- Bumped defaults: `keep_last_n_images=4→8`, `per_waypoint_turn_cap=12→40`, `max_total_turns=200→800`.

**Validation:** chestnut_v3_disciplined run vs v2 baseline:

| Metric | v2 (lazy) | v3 (disciplined) |
|---|---|---|
| WP0 turns | 6 | 12 |
| WP0 panos visited per year | 1 in 2025, 1 in 2016 | 3 in 2025, 1 in 2016 |
| Yaw match across years | partial | identical [0,180] both |
| Peeks | 0 | 1 |
| Cost | $1.49 | $2.41 |

### Phase B — Temporal multi-image storyteller

**Idea:** Send N epoch images for the same waypoint in **one Opus turn**, asking for a temporal report.

**Built `scripts/run_temporal_demo.py`:** for each waypoint, cluster candidates into ≤4 epochs (year buckets), Haiku-peek each, render compass-aligned forward views, send to Opus in one user message with structured-JSON output schema (per-epoch tier + treatments_inferred_between_epochs + deterioration_velocity + stale_pci_flag + dot_actionable_observation).

**Validation (Sunset Strip 2 anchors, $0.30 total):** anchor A detected thin overlay between 2016→2025 + recommended crack-seal at year 5-7 post-overlay; anchor B detected stable 9-year arc with no treatment + recommended preventive crack-seal in 2-3 yr.

**User pushback:** the single-turn shape removed all the agentic depth (zoom, look_around, multi-pano cross-witness). Wanted that capability BACK but applied across years. The temporal demo became a useful one-shot reporting tool, but not the hero.

### Phase C — Temporal walker MVP UI

**Goal:** polygon-draw entry point → matched-streets picker → live walker run with multi-year discipline.

**Backend additions to [`app/main.py`](app/main.py):**

- `POST /api/temporal/streets-in-polygon` — wraps Overpass `fetch_roads`, groups features by `name`, returns one entry per distinct named street with combined LineString segments + total length. Sorted by length DESC.
- `POST /api/temporal/start` — chains chosen segments into a polyline, builds waypoints, **runs prefetch + walker as a background asyncio task**, returns slug immediately. Writes minimal artifacts (config.json + street.geojson + waypoints.geojson) before background work so the UI can render the map straight away.
- `GET /api/temporal/runs/{slug}/status` — phase tracker reading `_temporal_status.json` (prefetching → running → completed → errored).
- `POST /api/temporal/runs/{slug}/stop` — writes `_stop_requested.flag` in run_dir; walker checks this at the top of each turn-loop iteration, exits cleanly with `stop_reason="user_stopped"`.
- `GET /temporal` (and `/temporal/{slug}`) → serves [`static/temporal_v2.html`](static/temporal_v2.html). v1 still available at `/temporal_v1`.

**Frontend ([`static/temporal_v2.html`](static/temporal_v2.html)) — three phases:**

- **DRAW**: empty map + polygon-draw control. After polygon → `enterPickPhase`.
- **PICK**: streets render as **clickable blue polylines** on the map; sidebar lists them sorted by length. Click any line OR list item to select. "Select all" / "Clear" / "Launch survey" controls bottom-left of map.
- **LIVE**: chat-style transcript polls `/api/walker/{slug}/trace/tail`, agent cursor pulses + auto-flies to each survey point, status banner reports prefetch progress. Stop button visible. After completion the cursor freezes into a static green dot.

**Per-epoch cross-witness discipline:** Updated [`scan_plan.md`](.claude/skills/pavement/scan_plan.md) — same investigation depth NON-NEGOTIABLE in every epoch, yaw-matching across years required, ≥2 candidates per year before declaring it unusable.

### Phase D — Pre-grade discipline gate (deterministic enforcement)

**Problem:** prompt-only rules in scan_plan worked partially. Opus self-disciplined at the start of a run but drifted (10/6/5 turns per waypoint pattern). Wanted deterministic enforcement.

**Built `_check_temporal_discipline` ([`app/agent/street_walker.py`](app/agent/street_walker.py)):**

Three rules checked when `grade()` is called (skipped for `tier=unknown`):

- **Rule A** — multi-year coverage available but only one year investigated → REFUSED with a step-by-step recovery plan and the visit log surfaced back to the agent.
- **Rule B** — temporal-claim language detected in rationale but yaws don't overlap across years (binned to 30°) → REFUSED with the specific yaws used per year.
- **Rule C** — declared a year unusable but only tried ≤1 candidate → REFUSED, instructed to peek ≥2 candidates first.

**2-strike escape hatch** — after 2 rejections at the same waypoint the gate lets the grade through.

**State tracking:** added `WalkerState.visit_log` (per-waypoint per-image dict) populated by `_record_and_pack_look/zoom/look_around` helpers in each tool dispatcher.

**Validation (chestnut_v4_gated):** gate fired once at WP1, agent recovered with a proper 2016 investigation. WP0=17 turns, WP1=14, WP2=9 — peek used at every waypoint, yaw matching now consistent.

### Phase E — Data-integrity discovery (SHA collision detector)

**Discovery:** the chestnut_v5 run produced an honest "older epoch unusable" rationale at multiple waypoints. User asked me to verify the agent's claim that 2016 and 2025 panos at WP2 were "visually identical." Direct probe of Mapillary Graph API revealed:

- `766435462471334` (date 2025-07-10) and `605264895954940` (date 2016-07-10) returned **SHA-256 identical thumbnails** (sha=`05b04480…`)
- Identical to 11 decimal places: coordinates, compass_angle, altitude
- Same creator, same camera (GoPro Max — released Oct 2019, so a 2016 capture from this model is impossible)
- Pattern repeated at WP1 (sha=`570bfc88…`, 274,633 bytes both times)

**Implication:** the corridor's apparent multi-year coverage was a duplicate-listing artifact, not real historical imagery. The agent's `inconsistencies` field caught this without being prompted to look for it.

**Fix shipped:**

- `_compute_thumb_shas` (later folded into `_enrich_candidates_and_shas`) — async parallel SHA-256 fetcher, caches per `image_id`.
- `_dedup_by_sha` — groups candidates by SHA, keeps one per group (newest year, then closest), returns `(kept, suppressed)` with structured rejection records.
- `_find_candidates_impl` made async, runs SHA dedup whenever multi-year coverage exists. Header includes a "Data inconsistency" note (neutral language — no accusations) listing each suppressed image_id and what it duplicated. Special after-dedup warning when "multi-year" collapses to single-year (the chestnut case).

### Phase F — LA corridor probe

User: "find me the perfect spot where Mapillary has high coverage and doesn't have a lot of traffic."

Probed **47 LA-area anchors**. Findings:

| Location | Coverage | Multi-year? | SHA-distinct? |
|---|---|---|---|
| **Sunset Strip west** (-118.3550, 34.0962) | 1,163 panos | ✅ 2015×50 + 2016×420 + 2025×693 | ✅ 12/12 unique |
| **Sunset Strip mid** (-118.3480, 34.0970) | 1,481 | ✅ 2015×51 + 2016×314 + 2025×1116 | ✅ 12/12 unique |
| **Sunset Strip east** (-118.3460, 34.0975) | 2,127 | ✅ 2015×33 + 2016×482 + 2025×1612 | ✅ 12/12 unique |
| USC Hoover area | 200+ | ❌ 2020 only | ✅ |
| Larchmont, Pico/Robertson | 130+ | ❌ single-year | ✅ |
| Hollywood Blvd / DTLA / Wilshire / Vermont / La Brea | 0–13 | ❌ all empty or single | n/a |

**Conclusion: Sunset Strip is the only clean multi-year corridor in the LA area** (West Hollywood — outside LA PCI, but the SHA detector now catches duplicate-listing cases anyway, so ground truth is less load-bearing).

### Phase G — Performance: prefetch optimisation (49s → 6s)

User: "why does fetching mapillary candidates take so long?"

Profiled — `prefetch_corridor_candidates` was hitting Mapillary Graph API for ALL 5,000 corridor panos (concurrency 12) just to enrich metadata. Timing: ~49 seconds on Chestnut.

**Fix:** moved Graph API enrichment out of prefetch. Prefetch now uses tile-level metadata only (image_id, lat, lon, captured_at, year, is_pano). Inside `_find_candidates_impl`, after stratification + dedup, the new `_enrich_candidates_and_shas` helper enriches **just the ~30 returned candidates** (concurrency 32) — fetching `thumb_1024_url`, compass_angle, make/model/camera_type, and SHA-hashing the thumb in one Graph API call per image.

**Result:** 49s → **6s** prefetch on the same Chestnut corridor (8× faster). Per-waypoint enrichment is ~1s amortised.

### Phase H — Tier rubric simplification (5 tiers → 3 tiers)

User: "most of the Fair grading needed to be Poor. Let's just keep Good Fair and Poor."

**Rewrote `tier_rubric.md`** to define exactly 3 tiers (+ unknown):

- **Good** (PCI ≈ 70-100) — essentially intact, includes both pristine and aged-but-stable
- **Fair** (PCI ≈ 40-69) — visible engineering distress, 1-3 yr action horizon
- **Poor** (PCI ≈ 0-39) — widespread structural failure, ≤12 month action

Added a **non-negotiable "no Fair-hedge" rule** with explicit disqualifiers that auto-trigger Poor: any pothole, alligator pattern, exposed base, deep rutting, spalled wide cracks, multi-generation patches with widespread failure.

**Backwards-compat** in the grade dispatcher: `Satisfactory→Good`, `Failed→Poor` mapping for skill-cache stragglers from the previous 5-tier era.

### Phase I — Evidence extraction skill

Goal: every grade should ship with structured evidence so a downstream re-analysis stage can re-grade or aggregate cheaply (text-only, no imagery).

**New skill [`evidence_extraction.md`](.claude/skills/pavement/evidence_extraction.md)** + new fields on the `grade()` tool schema:

- `distresses_observed` — per-year distress catalog with location-in-frame
- `treatments_observed` — visible treatments per epoch
- `safety_flags` — dispatcher-actionable safety items
- `surroundings_notes` — context (school zone, bus stop, etc.)
- `inconsistencies` — visible-evidence vs metadata mismatches (with explicit guidance to use neutral language only — no "fraud" / "falsified" / blaming language)
- `evidence_image_ids` — list of image_ids used as basis for the grade

**Persisted** at `evidence/wp00N.json` per finding (alongside findings.geojson) so the demo / future re-analysis can pull the rich extras out.

### Phase J — Minimap relocation (out of viewport)

**Problem:** the minimap inset on every `look()` was bottom-right of the viewport, sometimes blocking the very distress the model was trying to investigate.

**Fix:** `_composite_with_minimap` rewritten. Minimap is now a **strip ABOVE the viewport** at full panorama width (96 px tall). The viewport image below is pristine — no inset overlap, full road surface visible.

### Phase K — v2 UI redesign (Apple-flavour light)

User: "I want to record the demo. Think yourself as an Apple engineer. Sidebar on the right, light mode, white map, looks attractive."

**Built [`static/temporal_v2.html`](static/temporal_v2.html)** as a complete reskin:

**Layout:** top bar (60 px) with brand-dot gradient logo, status pill, point counter, Stop button (during live runs), New-survey button, dev-mode toggle. Map fills left of viewport (~70%); sidebar on **right** (480 px). Sidebar has white header, white filter bar, tinted gray-white feed area, **white chat-style cards floating with shadows**.

**Visual hierarchy** (after iteration): page chrome `#e6e8ec` (cool gray); top bar solid white with `0 4px 12px` shadow underneath; map area pure white background; sidebar tinted `#f1f3f7` with left shadow (panel depth); sidebar sub-sections (head, filter) solid white; cards inside feed white with shadow.

**Tile layer**: CARTO Voyager (colourful, full labels) — initial view downtown Ventura at zoom 16.

**Chat cards:** 32 px **avatar gutter** on the left of every card with class-specific glyphs:
- Assistant (Opus reasoning): 🤖 on blue gradient
- Tool (find / look / look_around): eye glyph 👁
- Peek (Haiku quality probe): ✦ amber
- Zoom: ⌖ pink
- Grade: ✓ accent-blue
- Skip: ↷
- System: •

Names are weight-600 12px (chat-sender feel). Step counter (`step N`) only renders when dev mode is ON. Auto-scroll anchored to bottom; if user scrolls up, "↓ new updates" pill appears.

**Map:** streets returned by `streets-in-polygon` render as clickable blue polylines (hover thickens, click selects). Selected streets darken. Per-waypoint markers small (radius 7), dark border, white fill, numbered tooltip on hover ONLY. Agent cursor pulses with two staggered rings; map smoothly `flyTo` each waypoint. After `walker_run_complete`, pulse rings removed, cursor freezes as a static green dot.

**Multi-line pills (post-iteration):** initial pills were `border-radius: 999px` — looked stretched when wrapping. Switched to `border-radius: 8px` rounded rectangles with 5px×10px padding.

**Markdown rendering (post-iteration):** agent narration contained `**Poor**` markdown that was rendering as literal text. Added a small XSS-safe markdown renderer (`mdToHtml` + `elMd`) supporting bold, italic, inline code, paragraph breaks. Applied to assistant body text and grade rationale.

**Vocabulary fixes (post-iteration):**
- `waypoint` → **`survey point`** in all user-facing strings (data keys `wp_idx` etc. unchanged for backend compat)
- `turn` → **`step`** (and dev-only)
- "metadata fraud" → **"data inconsistency / same image under different dates"** — across `find_candidates` header, the find_candidates UI card, AND `evidence_extraction.md` skill

**Recoverable error states:**
- Launch failure (e.g., "no Mapillary panos in corridor") no longer strands the user. Error shows in map banner; sidebar restores street picker; user can pick another street with one click.
- Stop button writes `_stop_requested.flag`; walker exits cleanly; UI shows a "Survey stopped" CTA with "↻ Start a new survey" primary button.
- "↻ New survey" button always visible during pick + live phases (top bar) — confirms before abandoning a live run.

### What's running at end of session

- `uvicorn` on port 8000, serving `temporal_v2.html` at `/temporal`
- Skills compose to ~109 KB across 2 cache_control blocks (added `evidence_extraction.md` and substantially expanded `scan_plan.md` and `grade_discipline.md`)
- Verified runs on disk: `chestnut_temporal_v2/v3_disciplined/v4_gated/v5_3tier_evidence`, `prefetch_speed_test`
- Recommended demo polygon: Sunset Strip western slot, NW (-118.3565, 34.0975) → SE (-118.3525, 34.0958)

### Lessons learned

1. **Cached tool descriptions whisper across every turn.** The "1-2 looks max" phrase in `look()` was overriding skill-level rules because tool schemas are part of the cache_control block and read on EVERY turn. Hunt these aggressively.
2. **Don't trust API metadata as ground truth for temporal claims.** SHA-256 of the thumbnail is the only reliable cross-year distinctness check.
3. **Lazy-fetch large metadata sets.** Push enrichment to the consumer, not the producer.
4. **Deterministic gates beat aspirational prompts.** Three rounds of skill-prompt tightening produced partial discipline; one round of `_check_temporal_discipline` produced reliable behaviour.
5. **Visual hierarchy via tinted surfaces.** Apple-style whitespace alone reads as "blank and confusing." A 3-tier surface system (chrome / panel / card) gives navigation cues without adding noise.
6. **Markdown in chat output is non-negotiable.** The model uses `**bold**` regardless of prompt. Render it.

---

## 2026-04-26 (late session) — 3-TIER HIERARCHICAL MULTI-AGENT (the new hero)

The single-loop walker won engineering-depth points (discipline gate, SHA detector, 13-skill cache stack, structured evidence) but didn't showcase Opus 4.7's agentic ceiling. To win the hackathon, the hero artifact pivoted from **one Opus loop walking a street sequentially** to a **3-tier parallel hierarchy** that mirrors how a real DOT actually triages a corridor: district engineer → corridor inspector → field crew. Multiple subagents run concurrently, communicate through structured blackboards, cross-witness each other's claims, and visibly accelerate the work — turning a ~5 min sequential walk into a ~60–90 s parallel investigation.

### Architecture as shipped

```
                          STREET CAPTAIN  (1×, Opus 4.7, 60-turn cap)
                          tools: read_street_blackboard, read_point_blackboard,
                                 plan_dispatch_batches, dispatch_surveyors,
                                 request_redo, cross_check_claim,
                                 finalize_street, done
                                          │
                            asyncio.Semaphore(3)
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                             ▼
      POINT SURVEYOR (Opus 4.7, 12-turn cap, one per survey point)
      tools: get_point_brief, enumerate_candidates_by_year,
             dispatch_year_investigators, read_point_blackboard,
             request_more_evidence, cross_witness_check,
             grade (gated by _check_temporal_discipline), report_to_captain
                                          │
                            asyncio.Semaphore(2)  (per-surveyor)
            ┌──────────────────┬──────────┼──────────┬──────────────────┐
            ▼                  ▼          ▼          ▼                  ▼
      YEAR INVESTIGATOR  (Opus 4.7, 8-turn cap, one per year per point)
      tools: peek_candidate, look_around, look, zoom_into_region,
             read_sibling_claims, post_claim, report_year_findings
```

Peak concurrency: 3 surveyors × 2 investigators = up to 6 simultaneous Opus calls, plus the captain. A global `asyncio.Semaphore(6)` wraps every `messages.create` as a rate-limit backstop with exponential backoff (4s → 8s → 16s) on `RateLimitError`.

### Inter-agent communication — two blackboards

**Per-point** (`evidence/wp{idx:03d}_blackboard.json`):
- `claims_by_year` — investigators write structured posts (`distress`, `treatment`, `hazard`, `unusable_evidence`, `temporal_anchor`, `note`) with `yaw_deg` + `confidence` + `image_ids`.
- `cross_witness_yaws` — running list of yaws each year has investigated, drives the surveyor's cross-witness check.
- `status_by_year` — per-investigator state (running / completed / failed) + summary + yaws_covered + distresses + treatments.
- `final_grade` — surveyor writes here.

**Per-street** (`street_blackboard.json`):
- `point_summaries` — surveyors append on completion.
- `dispatch_log` — captain's wave history.
- `captain_redo_log` — every redo with reason + focus_year.
- `captain_narrative_draft` — final corridor synthesis (also written to `summary.md`).
- `flagged_inconsistencies` — captain-level cross-point findings.

Both blackboards are protected by per-instance `asyncio.Lock` covering both in-memory mutation AND the JSON file rewrite. Every mutation also emits a `blackboard_post` trace event so the UI replay is lossless.

**Key communication patterns enabled:**

- **Cross-year (within point):** Y-2025 investigator posts `{category:"temporal_anchor", yaw:180, content:"yaw 180 has visible 2024 mill seam; check this yaw in sibling years"}`. Y-2016 investigator's next turn calls `read_sibling_claims` and gets that anchor — it now investigates yaw 180 first instead of picking a yaw blindly.
- **Cross-point (within street):** captain reads `point_summaries`, sees WP3 reports "2024 mill+overlay" but WP4 reports "aged surface, no recent treatment." It calls `cross_check_claim(claim, point_a=3, point_b=4)` to inspect both blackboards side-by-side, then issues `request_redo(point_idx=4, reason=..., focus_year=2024)` if the contradiction warrants it.

### Reusable primitives — zero rewrites

The street-walker's tool implementations (`_find_candidates_impl`, `_peek_candidate_impl`, render impls for `look`/`zoom_into_region`/`look_around`, `_check_temporal_discipline`, `_compute_thumb_shas`, `_dedup_by_sha`) are **re-exported** from `app.agent.street_walker` via [`app/agent/hierarchy/primitives.py`](app/agent/hierarchy/primitives.py). A small `_StateAdapter` quacks like `WalkerState` for the duration of a tool call, backed by `RunState` (shared caches, atomic budget) and `AgentScratch` (per-agent visit log). Each tool call is wrapped in an `async with adapter_for(rs, scratch, point_idx, lat, lon)` context manager that auto-flushes accumulated cost back to `RunState` via `await scratch.add_cost(...)` on exit.

This means the SHA-collision detector, year-stratified candidate selection, lazy enrichment, render+minimap pipeline, and the discipline gate are all reused **identically** at the new tier — the hierarchy is a coordination layer, not a re-implementation.

### Skill composition — 3 role-specific 2-block prompts

Each role gets its own subset of the 16 skill files (13 existing + 3 new), arranged into 2 `cache_control: ephemeral` blocks. Per-agent role preamble (`"you are S1-WP3, your point is X"`) goes in a 3rd, non-cached text block at the front so it doesn't bust cache.

| Role | Block 1 (engineering) | Block 2 (operational) | New skill |
|---|---|---|---|
| Captain | tier_rubric, deterioration_progression, repair_priority_logic | — | [`cross_point_synthesis.md`](.claude/skills/pavement/cross_point_synthesis.md) |
| Surveyor | tier_rubric, grade_discipline, deterioration_progression | scan_plan, evidence_extraction | [`temporal_reconciliation.md`](.claude/skills/pavement/temporal_reconciliation.md) |
| Investigator | distress_taxonomy, visual_confusers, treatment_signatures | pano_anatomy, viewport_geometry, scan_plan, zoom_investigation | [`year_investigator_brief.md`](.claude/skills/pavement/year_investigator_brief.md) |

Final prompt sizes — Captain 24.7 KB, Surveyor 45.7 KB, Investigator 67.2 KB. Cache amortization with 3 surveyors + ~10 investigators per run: 13 cache hits vs 1 write per role.

### Trace schema — extended, backwards-compatible

Every event now carries `agent_id`, `agent_role` (`captain`/`surveyor`/`investigator`), `parent_agent_id`, `point_idx`, `year`. `worker_id` is mirrored to `agent_id` for fleet-UI back-compat. The captain still emits the legacy `walker_run_header` and `walker_run_complete` records so the v2 UI's existing handlers degrade gracefully.

New `record_type` values: `agent_spawned`, `agent_completed`, `dispatch_order`, `blackboard_post`, `cross_witness_handoff`, `redo_order_issued`, `report_up`, `hierarchy_run_header`.

### Backend — mode switch + 4 new endpoints

- `POST /api/temporal/start` accepts `mode: "hierarchy" | "single_walker"` (default `single_walker` for compat). When `hierarchy`, the `_runner()` builds a `RunState` and calls `run_captain(...)` instead of `run_street_walker(...)`. Also accepts `n_surveyor_slots` (default 3) and `n_investigator_slots` (default 2).
- `GET /api/temporal/runs/{slug}/trace/tail` — alias of `/api/walker/{slug}/trace/tail` (same JSONL file).
- `GET /api/temporal/runs/{slug}/file/{filepath}` — alias of the walker file route.
- `GET /api/temporal/runs/{slug}/hierarchy` — live tree built from the trace tail. Returns `{captain, surveyors:[{...investigators:[...]}], n_total_agents}`.
- `GET /api/temporal/runs/{slug}/blackboard/street` — returns `street_blackboard.json` raw.
- `GET /api/temporal/runs/{slug}/blackboard/wp/{point_idx}` — returns the point blackboard raw.

### v2 UI — multi-cursor, tree panel, 6 new card types

- **Launch screen** gained a **Mode** dropdown (Hierarchical default / Sequential fallback) plus default budget 12 USD for hierarchy runs.
- **Hierarchy tree panel** above the chat (max-height 160 px, collapsible) polls `/api/temporal/runs/{slug}/hierarchy` every 1.5 s. Indented rows: Captain → Surveyors → Investigators with state pills (`spawned`/`running`/`completed`/`failed`) + per-agent step count + cost. Each agent gets a colored dot from `workerColor(agent_id)` (HSL hash, ported from `fleet.html`).
- **Multi-cursor map**. Replaced single `agentCursor` with `cursorsByAgent = new Map()`. Surveyors get colored circle-markers at their assigned points; the primary blue pulse still tracks last activity. On `agent_completed` the surveyor cursor freezes (`fillOpacity 0.55`) tinted by final tier.
- **Avatar gutter** extended with role glyphs: ⌂ captain, ◉ surveyor, ◎ investigator. Plus event-type glyphs: ✶ spawn, ⇉ dispatch, ✎ claim, ⇄ handoff, ⟲ redo, ⤴ report.
- **6 new card types** in `renderEvent` switch:
  - `agent_spawned` → small card with agent_id + colored dot + spawn reason
  - `agent_completed` → completion summary with cost + steps + stop reason
  - `dispatch_order` → yellow left border, lists target agent_ids/years as pills + directive in italic
  - `blackboard_post` → cyan left border, category chip + yaw chip + content + confidence
  - `report_up` → green left border, from→to + tier chip + narrative
  - `redo_order_issued` → red left border, point_idx + reason + focus_year
- Role-tinted name colors: captain `#5856d6`, surveyor `#007aff`, investigator `#ac8e68`.

### Validation runs

**1-pt SPRING ST smoke** (`scripts/run_hierarchy_walker.py --street-name "SPRING ST" --limit-waypoints 1 --budget 4 --slug hierarchy_smoke_v1`):
- $2.68 of $4 budget, clean `agent_done`.
- 1 captain + 1 surveyor + 1 investigator, 52 trace events.
- Investigator covered yaws [0, 90, 180, 270] (full cardinal scan).
- Surveyor graded **Good** (confidence 0.78). Hairline longitudinal at right wheelpath identified as construction joint, not fatigue.
- Captain wrote a 3-paragraph corridor synthesis citing aged HMA + no fatigue + no recent treatment + lane joint correctly identified.

**3-pt SPRING ST demo** (`--limit-waypoints 3 --budget 12 --slug hierarchy_spring_v1`):
- $4.78 of $12 budget, clean `agent_done`.
- 3 distinct tiers across the corridor: WP0 **Good** (N end, oxidized but intact), WP1 **Poor** (mid-corridor, medium-severity alligator confirmed across 2 panos at yaw 180), WP2 **Fair** (S end near STOP intersection).
- WP1 blackboard ([`evidence/wp001_blackboard.json`](downloads/walker/hierarchy_spring_v1/evidence/wp001_blackboard.json)) shows the investigator posted 3 claims: longitudinal at yaw 180 (conf 0.65), `temporal_anchor` at yaw 180 for sibling years (conf 0.85), and confirmed alligator at yaw 180 cross-witnessed across 2 panos (conf 0.90). The surveyor's `final_grade` rationale references the cross-witness explicitly.
- Captain narrative: *"Aged HMA throughout, no recent corridor-wide preservation visible. Condition is non-uniform: Good at the north end, structural failure at mid-corridor, Fair at the south near a STOP intersection. All evidence is single-epoch (2025 imagery only)."*

**3-pt SUNSET BL "no imagery" honest-failure run** (`--street-name "SUNSET BL" --limit-waypoints 3 --slug hierarchy_sunset_v1`):
- $1.04 of $12 budget. All 3 points returned `unknown` because the western SUNSET BL centerline starts where Mapillary coverage is sparse — zero panos within 60 m radius at any year for any waypoint.
- Captain wrote an honest gap-narrative: *"This 3-point sample on Sunset Bl returned zero Mapillary panoramas within the 60 m search radius at any year for all three waypoints. No pavement assessment was possible from street-level imagery."* No hallucinations, no Fair-hedge — the discipline holds even when the input is genuinely empty.

### Files added / modified

**New (under `app/agent/hierarchy/`):**

- [`__init__.py`](app/agent/hierarchy/__init__.py) — re-exports `RunState`, `AgentScratch`, `PointBlackboard`, `StreetBlackboard`, `run_captain`, `run_point_surveyor`, `run_year_investigator`, `PointReport`.
- [`run_state.py`](app/agent/hierarchy/run_state.py) — `RunState` + `AgentRegistration`. Shared atomic state: budget lock, surveyor/investigator/api semaphores, peek/equirect/SHA caches lifted from `WalkerState`, TraceWriter + lock, agent registry, stop signal (file flag + asyncio.Event), agent-id minting helpers.
- [`agent_scratch.py`](app/agent/hierarchy/agent_scratch.py) — `AgentScratch`. Per-agent: messages history, turns_used, cost_usd_local, visit_log + discipline_gate_strikes, agent_id/role/parent. Mirrors `WalkerState`'s `record_peek/look/zoom`/`visits_summary` helpers per-point.
- [`blackboard.py`](app/agent/hierarchy/blackboard.py) — `PointBlackboard` + `StreetBlackboard`. Lock-protected JSON-snapshot persistence. `post_claim`, `set_year_status`, `set_final_grade`, `get_sibling_claims(asking_year)`, `append_point_summary`, `append_dispatch`, `append_redo`, `set_narrative`.
- [`primitives.py`](app/agent/hierarchy/primitives.py) — `_StateAdapter` + `adapter_for` context manager. Re-exports walker primitives unchanged.
- [`skills.py`](app/agent/hierarchy/skills.py) — `compose_captain_system`, `compose_surveyor_system`, `compose_investigator_system` + `assert_all_role_skills_present`.
- [`runner.py`](app/agent/hierarchy/runner.py) — shared `run_agent_loop` (turn driver, image-block pruning, budget warnings at 85%/95%, terminal side-effects, retry-with-backoff `call_messages_with_retry`).
- [`year_investigator.py`](app/agent/hierarchy/year_investigator.py) — `INVESTIGATOR_TOOL_SCHEMAS` (7 tools) + `_make_dispatch` + `run_year_investigator`. Merges per-investigator visit_log into parent surveyor's after each child completes.
- [`point_surveyor.py`](app/agent/hierarchy/point_surveyor.py) — `SURVEYOR_TOOL_SCHEMAS` (8 tools) + `_make_dispatch` + `run_point_surveyor` + `PointReport`. Owns the per-surveyor `investigator_sem`, hosts the discipline gate inside `grade()`.
- [`captain.py`](app/agent/hierarchy/captain.py) — `CAPTAIN_TOOL_SCHEMAS` (8 tools) + `_make_dispatch` + `run_captain`. Lazy point-blackboard creation on first dispatch, asyncio.gather-based wave dispatch with per-tier budget gates.

**New skills:** [`cross_point_synthesis.md`](.claude/skills/pavement/cross_point_synthesis.md), [`temporal_reconciliation.md`](.claude/skills/pavement/temporal_reconciliation.md), [`year_investigator_brief.md`](.claude/skills/pavement/year_investigator_brief.md).

**New scripts:** [`scripts/run_hierarchy_walker.py`](scripts/run_hierarchy_walker.py) — CLI runner mirroring `run_street_walker.py` shape.

**Modified:**

- [`app/main.py`](app/main.py) — `mode` switch on `POST /api/temporal/start`, 4 new endpoints (`/hierarchy`, `/blackboard/street`, `/blackboard/wp/{idx}`, `/trace/tail` alias, `/file/{path}` alias).
- [`static/temporal_v2.html`](static/temporal_v2.html) — multi-cursor map, hierarchy tree panel, 6 new card types in `renderEvent` switch, role glyphs in avatar gutter, role-tinted name colors, Mode dropdown on launch screen, `pollHierarchy()` alongside `pollTrace()`.
- [`CLAUDE.md`](CLAUDE.md) — direction block added.
- [`HANDOFF.md`](HANDOFF.md) — direction-update banner at top.

### Risks accepted (deliberate scope cuts)

- **No prompt-cache pre-warming.** Captain's first turn pays the cache write while surveyors wait on the api_sem; in practice this adds <2 s per run because the captain turn arrives first.
- **Tree panel polls at 1.5 s instead of streaming.** Acceptable — the `pollHierarchy` is read-only and rebuilt from scratch each call (~100 ms server-side for typical traces).
- **No filter-chat-by-agent click-handler on tree nodes yet.** The tree is informational only; clicking a row doesn't filter the chat. Out of scope for v1; existing `wp-filter` dropdown still works for survey-point filtering.
- **No cross-witness handoff arrow card.** When Y-2016 reads a Y-2025 anchor, no special card type fires — just a `blackboard_post` event surfaces in the chat. Out of scope for v1.

### Lessons learned

1. **The fleet-pattern trace schema's `worker_id` field was a gift that paid forward.** Existing fleet UI plumbing (`workerColor` HSL hash, `asyncio.Lock`-protected shared trace) ported to the hierarchy with zero churn. Schemas designed for "any actor" beat schemas designed for "one walker" every time.
2. **Adapter pattern beats refactor.** A 100-line `_StateAdapter` let us reuse `_find_candidates_impl` (which has 200+ lines of stratification + dedup + lazy enrichment) without touching it. The adapter took 30 minutes; rewriting the candidate logic would have taken 4 hours and risked regressions in the SHA-collision detector that took half a day to get right.
3. **Per-agent semaphores beat fleet-wide.** A fleet-wide investigator semaphore would stall the fastest surveyor waiting for slots held by a slow sibling, undermining the "independent point" model. Per-agent caps (`run_state.investigator_sem_factory()` per surveyor) keep parallelism truly independent.
4. **Honest-failure cases validate the discipline gate.** The SUNSET BL run with zero Mapillary coverage produced 3 honest `unknown` grades + a captain narrative explicitly naming the gap. No fabricated condition. The discipline that was painful to build for the single-loop walker pays off here too.
5. **Sibling-claims pattern is cheap and powerful.** `read_sibling_claims` is a free tool call; the per-point blackboard fits in <50 KB. Y-2016 investigators that anchor on Y-2025's `temporal_anchor` claim produce yaw-aligned evidence the surveyor's discipline gate accepts on the first call. That's exactly the cross-witness behavior we tried to enforce via prompts in v2 and only partially achieved.
6. **The captain's "active dispatcher" role earned its keep.** In the SPRING ST 3-pt run, the captain didn't fire a redo (every surveyor produced clean evidence on first pass), but the captain DID make scope decisions: it dispatched all 3 points in a single wave (geographic order, max parallelism), then synthesized. A "synthesizer-only" captain would have produced the same narrative but missed the visible parallel-dispatch story that's central to the demo.

### What's next (post-implementation, pre-submission)

- **Demo video recording** — focus on the parallel-dispatch moment: 3 cursors lighting up simultaneously on different survey points, then 6 cursors as their investigators spawn beneath. The tree panel filling in left-to-right as agents complete.
- **README polish** — pointer to `/temporal` with Mode set to Hierarchical as the demo entry.
- **Pre-flight** the demo polygon — confirm ≥2 multi-year points exist before locking the recording so a `temporal_anchor` claim actually fires across years.
- **Out of scope for Sunday:** click-to-filter chat by tree node; cross-witness handoff arrow card; per-point blackboard drawer on map-click. All listed as deferred in the plan file at `~/.claude/plans/so-now-i-want-shiny-spindle.md`.
