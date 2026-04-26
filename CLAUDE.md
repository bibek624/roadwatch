# PavTrace — Street-Level Pavement Condition Triage

Guidance for future Claude Code sessions working in this repo.

## Companion docs (read these in this order on a fresh session)

1. **[PROGRESS.md](PROGRESS.md)** — chronological ship log. The 2026-04-26 section at the bottom covers the v2 evolution (temporal walker, discipline gate, SHA detector, UI redesign).
2. **[DECISIONS.md](DECISIONS.md)** — architectural decisions log. Read this BEFORE reverting any of the 14 load-bearing decisions (D-001 through D-014). Format: Context / Decision / Consequences / Alternatives considered / Status.
3. **[HANDOFF.md](HANDOFF.md)** — fresh-session bootstrap. Architecture diagram, tool surface, demo runbook, "what's running right now" pointers.

## Hackathon context

This was built as an entry for the **Opus 4.7 hackathon** (submission Sun Apr 26 2026, 8 PM EST). The four judging axes were Impact, Demo quality, Opus 4.7 use, and Depth/execution. The Direction blocks below capture the actual pivot story; PROGRESS.md is the chronological ship log.

> **Direction (2026-04-24):** Pivoted back to pavement condition assessment. The broader 10-category safety atlas produced thin signal on the first Chestnut run, so we're refocusing on the thing Opus 4.7 *can* reliably see from Mapillary imagery: surface distress on the road itself. The 10-category prompts and `run_area_triage.py` stay in the tree for now, but new work targets pavement.
>
> **Direction (2026-04-24, shipped — AGENTIC PAVEMENT SURVEYOR is the hero):** The hackathon entry is an autonomous agent, not a batch pipeline. Opus 4.7 drives a tool-use loop: given a street network, it decides where to look, which 360° pano to fetch, which viewports to extract, and records pavement condition + hazards in one pass per image. **Validated end-to-end on Broadway/SoHo: $8.11 / 58 turns / 9 findings (19 distresses + 13 hazards, all with pixel bboxes) / clean `done` exit — see [PROGRESS.md](PROGRESS.md) for the full ship log.** The replay UI at `/agent/{slug}` animates the trace (agent cursor walks the block, decisions stream into a transcript panel, findings pin onto the map, segments recolor on cover). The existing batch pipeline (`scripts/run_pavement_assessment.py` + `static/pavement.*`) stays in the tree as a reference implementation but is NOT what we demo. See the **Agentic architecture** section at the bottom for the as-shipped design.
>
> **Direction (2026-04-25, shipped — FLEET architecture cuts $/finding by ~7×):** Replaced the single 58-turn loop with a Coordinator-Workers pattern (Pattern 3 from the strategy doc). Python orchestrator clusters Mapillary primaries by `sequence_id`, dispatches N narrow Worker agents in parallel via `asyncio.gather`+`Semaphore`. Each Worker has a 4-tool surface (haiku_prescreen, extract_viewport, record_finding, done), short scope (3–5 panos), turn cap 4–6, ~$0.30 budget. Shared `fleet_trace.jsonl` with `worker_id` per event. Validated $2 SoHo Broadway run: $1.79 / 6 workers / 13 findings / 89s wall clock = **$0.14/finding (vs $0.90 in single-loop)**. Live UI at `/fleet/{slug}` color-codes events by worker so the parallel story is visible. Live transparency feed also at `/live/{slug}`.
>
> **Direction (2026-04-25, in-progress — calibration-and-LA pivot):** Pivoted validation target from NYC to LA because the user has domain depth there AND LA publishes one of the cleanest segment-level PCI datasets in the country. Built `data/la_pci/segments.geojson` (69,282 LA street segments with PCI 0-100, ASTM D6433-mapped to our 5 tiers — see [scripts/download_la_pci.py](scripts/download_la_pci.py)). Built a 50-sample stratified calibration set + Haiku-screened road-visibility filter + per-pano agentic inspector (`look(yaw,pitch,hfov)` + `grade(tier,...)`) + calibration runner with 5×5 confusion matrix. **Result on Opus 4.7: 14% (single-call) → 20% (agentic, full-res, road-filtered) 5-way accuracy. Triage precision 70%, recall 29%.** Diagnosis: the model isn't wrong — LA's PCI scores include subsurface laser-measured rutting + IRI roughness that **aren't visible in optical imagery** (e.g., 6TH ST PCI 13 looks visually intact; NORMANDIE AV PCI 2 is a Caltrans concrete freeway). See [PROGRESS.md](PROGRESS.md) Phase 11 for the full diagnosis.
>
> **Direction (2026-04-25, planned — STREET WALKER):** Pivoted from pre-picked-pano calibration to **agentic Mapillary-noise filtering on real streets**. The agent walks a target corridor end-to-end at fixed waypoint spacing (default 50m, variable). At each waypoint it queries Mapillary candidates within ~25m, peeks a few of N (cheap Haiku quality probe), picks the best one (recent + correct rig + clear road), grades pavement, advances. Spatial state lives in agent context. Validates per-segment vs LA PCI. **This explicitly demonstrates the agentic capability the strategy doc rewards: spatial reasoning, image-quality judgment, sampling strategy, narrative trace.** Full plan: [STREET_WALKER_PLAN.md](STREET_WALKER_PLAN.md).
>
> **Direction (2026-04-25, shipped — SKILLS LIBRARY + agentic refinement):** Built [.claude/skills/pavement/](.claude/skills/pavement/) — 12 research-grounded engineering-knowledge files cited from FHWA Distress Identification Manual (FHWA-HRT-13-092), ASTM D6433-21, PASER, FHWA preservation guidance, and other open-source authorities. The walker's system prompt is now composed at runtime via [app/agent/skill_loader.py](app/agent/skill_loader.py) into 2 cache_control blocks (~90 KB total: core engineering knowledge + operational discipline + geometry). Agent now references rubric vocabulary explicitly in its narration ("alligator", "longitudinal in wheelpath", "transverse with spalling", "manhole — round + flush", "mottled with patch boundaries"). Skills also added: `look_around` tool (4-up cardinal grid), minimap inset on every `look` (cropped to pavement-relevant pitch band +10° to -60°, with red rectangle showing sampling location), zoom-investigation rule (must zoom on suspected distress before grading). Validated v5 SPRING ST run: $5.14 / 4 waypoints / 17 looks (10 zoomed) / 4 look_around / 4 grades / clean done.
>
> **Cache-economics insight:** the 90 KB skills system prompt costs $0.42 to write to cache once, then reads at 0.10× input price (~$0.005/turn). Over a 30-turn run that's ~$0.13 cached vs ~$22.50 uncached for the system context. Skills layer is essentially free per-turn after the first cache-write.
>
> **Direction (2026-04-25, shipped — `zoom_into_region` tool: agent picks pixels, system computes geometry):** Discovered v5 zooms were rendering at deeper pitch than the source view (because the agent followed the `look(narrow_hfov)` zoom convention but kept yaw/pitch the same), which on car-rig panos puts MORE carrier in the zoomed frame, not less. Added a 9th tool: `zoom_into_region(image_id, source_yaw, source_pitch, source_hfov, x1, y1, x2, y2, purpose)`. The agent specifies a bbox in normalized pixel coords of its previous viewport; the system computes new (yaw, pitch, hfov) and re-renders at full equirect resolution. v6 SPRING ST run: 9/9 zooms rendered at pitch -7° to -17° (horizon band, above carrier) vs v5's -25° to -45° (carrier band). Cost stayed the same ($5.00 vs $5.14). Agent picks bboxes with `y1 ≥ 0.15` instinctively, skipping the carrier band visible at the bottom of source views. Updated [`zoom_investigation.md`](.claude/skills/pavement/zoom_investigation.md) skill to make `zoom_into_region` the preferred zoom tool over narrow-hfov `look()`. Tool count is now **9** (added on top of get_position, look_around, find_candidates, peek_candidate, look, grade, skip_waypoint, done).
>
> **Direction (2026-04-26, shipped — TEMPORAL WALKER + DETERMINISTIC DISCIPLINE GATE):** The walker now investigates every survey point across ALL available years, not just the latest. `find_candidates` was rewritten to **stratify by year** (`min_per_year=6`, `limit=30`, `year_filter` param to fetch more from one specific year) so multi-year coverage is never hidden behind a recency-DESC sort. Per-epoch cross-witness (≥2 panos, same yaw across years) is now NON-NEGOTIABLE — enforced by `_check_temporal_discipline` in [`app/agent/street_walker.py`](app/agent/street_walker.py), which inspects `WalkerState.visit_log` and **refuses `grade()` calls** that violate any of three rules (multi-year coverage available but only one year visited; temporal claim made but yaws don't overlap; year declared unusable but ≤1 candidate tried). 2-strike escape hatch prevents infinite loops. Verified on chestnut_v4_gated: gate fired once at WP1, agent recovered with proper 2016 investigation. Skill files [`scan_plan.md`](.claude/skills/pavement/scan_plan.md), [`grade_discipline.md`](.claude/skills/pavement/grade_discipline.md) updated with the new discipline; [`evidence_extraction.md`](.claude/skills/pavement/evidence_extraction.md) added as a 13th skill — every grade now ships with `distresses_observed`, `treatments_observed`, `safety_flags`, `surroundings_notes`, `inconsistencies`, `evidence_image_ids` persisted to `evidence/wp00N.json`. Tier rubric collapsed from 5 to **3 tiers (Good / Fair / Poor / unknown)** with a non-negotiable "no Fair-hedge" rule for visible structural failure.
>
> **Direction (2026-04-26, shipped — SHA COLLISION DETECTOR):** Discovered while verifying agent's "older epoch unusable" claim on Chestnut Ventura: `766435462471334` (date 2025) and `605264895954940` (date 2016) had byte-identical thumbnails (SHA-256 = `05b04480…`), identical coordinates to 11 decimal places, and were captured on a GoPro Max (model released October 2019, so a 2016 capture from it is impossible). The "multi-year coverage" was duplicate listings of 2025 panos with backdated metadata, not real history. Added `_compute_thumb_shas` + `_dedup_by_sha` in [`app/agent/street_walker.py`](app/agent/street_walker.py): when `find_candidates` returns multi-year panos, the system fetches `thumb_1024` for each and groups by SHA-256, suppressing duplicates and surfacing a "Data inconsistency" note to the agent. Language is **strictly neutral** across the codebase — no "fraud" or "falsified" — just descriptive ("same image listed under different dates"). All accusatory phrasing was scrubbed from the find_candidates header, the UI card, and the evidence_extraction skill examples.
>
> **Direction (2026-04-26, shipped — PERFORMANCE: lazy enrichment, 49s → 6s prefetch):** Profiled prefetch and found `fetch_image_detail_bulk` was hitting Mapillary Graph API for ALL 5,000 corridor panos at concurrency 12 — ~49s on Chestnut. The agent only ever consumed ~30 candidates per waypoint, so 99% of that work was wasted. Solution: skip bulk metadata in [`prefetch_corridor_candidates`](scripts/run_street_walker.py) (`skip_bulk_metadata=True` default), use tile-level fields only (image_id, lat, lon, captured_at, year, is_pano). Inside `_find_candidates_impl`, after stratification + dedup, the new `_enrich_candidates_and_shas` helper enriches **just the ~30 returned candidates** at concurrency 32 — fetching `thumb_1024_url`, compass_angle, make/model/camera_type, AND SHA-hashing the thumb in one Graph API call per image. Result: 49s → **6s** prefetch (8× faster). Per-waypoint enrichment is ~1s amortised.
>
> **Direction (2026-04-26, shipped — POLYGON-DRIVEN UI + STOP BUTTON):** Built `static/temporal_v2.html` as the new entry point at `/temporal`. Three phases: **DRAW** (polygon-draw on map) → **PICK** (matched streets render as clickable blue polylines + sidebar list, with "Select all" / "Clear" / "Launch" controls) → **LIVE** (chat-style transcript, agent cursor pulses + auto-flies to each survey point, status banner reports prefetch progress). New backend routes in [`app/main.py`](app/main.py): `POST /api/temporal/streets-in-polygon`, `POST /api/temporal/start` (returns slug immediately, runs prefetch+walker as a background asyncio task), `GET /api/temporal/runs/{slug}/status` (reads `_temporal_status.json`), and `POST /api/temporal/runs/{slug}/stop` (writes `_stop_requested.flag`; walker checks at top of each turn-loop iteration and exits cleanly with `stop_reason="user_stopped"`). Recoverable error states throughout — launch failure restores the picker, stop preserves graded points, "↻ New survey" button always one click away. v1 still served at `/temporal_v1` for fallback.
>
> **Direction (2026-04-26, shipped — APPLE-STYLE LIGHT UI):** Complete reskin: light mode only, **sidebar on the right** (was left), white CARTO-Voyager tile layer (initial view downtown Ventura at zoom 16), **layered surface hierarchy** (cool gray body `#e6e8ec` → solid white top bar with shadow → tinted gray-white sidebar `#f1f3f7` → white cards floating with shadows). Top bar has dev-mode toggle (ON by default) — when OFF, all cost/turn pills are hidden for the demo. Chat cards use a 32 px **avatar gutter** with class-specific glyphs: 🤖 (assistant), 👁 (tool/look), ✦ (peek), ⌖ (zoom), ✓ (grade), ↷ (skip), • (system). Per-waypoint markers on the map are small (radius 7) with numbered tooltips ON HOVER ONLY — no big labels covering the map. Agent cursor pulses with two staggered rings during the run; on `walker_run_complete`, pulse rings are removed and the cursor freezes into a static green dot. Sidebar feed has auto-scroll that pauses when the user scrolls up (with a "↓ new updates" pill to re-anchor). Vocabulary normalised throughout: `waypoint` → **`survey point`** in user-facing strings (data keys unchanged); `turn` → **`step`** (and dev-only); accusatory data-integrity language scrubbed. Multi-line evidence pills are now **rounded rectangles** (8px radius) with proper padding so they wrap cleanly. Agent narration markdown (`**bold**`, `*italic*`, `` `code` ``, paragraph breaks) is rendered correctly via an XSS-safe markdown helper; previously `**Poor**` showed as literal text. The minimap was also relocated — formerly bottom-right inset that occasionally blocked distresses, now a full-width strip ABOVE the viewport (96 px tall) so the pavement image below is pristine.
>
> **Direction (2026-04-26, shipped — 3-TIER HIERARCHICAL MULTI-AGENT — the new hero):** The hackathon hero is now a 3-tier parallel hierarchy. **Street Captain** (1×, Opus 4.7, active dispatcher) plans batches and dispatches up to 3 **Point Surveyors** in parallel; each Surveyor in turn spawns up to 2 **Year Investigators** in parallel — peak ~6 concurrent Opus calls. Inter-agent communication via two blackboards: per-point `evidence/wp{idx:03d}_blackboard.json` (year investigators write `claims_by_year` + `temporal_anchor` claims for siblings; surveyor writes `final_grade`) and per-run `street_blackboard.json` (surveyors append `point_summaries`; captain writes `dispatch_log` + `captain_redo_log` + `captain_narrative_draft`). Investigators call `read_sibling_claims` on turn 1 to anchor their yaws to siblings' findings; captain calls `request_redo(point_idx)` to re-dispatch a surveyor with a focus directive. Discipline gate moved to the surveyor (it sees all year evidence at once). Validated on SPRING ST 3-pt run: $4.78 of $12 budget, 3 distinct tiers (Good/Poor/Fair), 3 surveyors + 3 investigators completed cleanly, captain wrote a coherent corridor narrative referencing temporal arc + treatment history. New code in [`app/agent/hierarchy/`](app/agent/hierarchy/) (9 files: run_state, agent_scratch, blackboard, primitives [re-exports — zero rewrites], skills, runner, captain, point_surveyor, year_investigator). 3 new role-specific skills at [`.claude/skills/pavement/cross_point_synthesis.md`](.claude/skills/pavement/cross_point_synthesis.md), [`temporal_reconciliation.md`](.claude/skills/pavement/temporal_reconciliation.md), [`year_investigator_brief.md`](.claude/skills/pavement/year_investigator_brief.md). UI: launch screen has a **Mode** dropdown (Hierarchical default / Sequential fallback), live tree-view above the chat (Captain → Surveyors → Investigators with state pills + per-agent costs), per-agent multi-cursors on the map (workerColor HSL hash), 6 new card types (`agent_spawned`, `agent_completed`, `dispatch_order`, `blackboard_post`, `report_up`, `redo_order_issued`). New endpoints: `GET /api/temporal/runs/{slug}/hierarchy` (live tree from trace tail), `GET /blackboard/street`, `GET /blackboard/wp/{idx}`, alias `/api/temporal/runs/{slug}/trace/tail`. CLI runner: [`scripts/run_hierarchy_walker.py`](scripts/run_hierarchy_walker.py). Legacy `run_street_walker` stays as `mode="single_walker"` fallback at `/temporal_v1`. Full plan at `~/.claude/plans/so-now-i-want-shiny-spindle.md`.

## What this project is

A pipeline that assesses **visible pavement condition** across a street or area from Mapillary street-level imagery. Input: a street name (or polygon). Output: a map-overlayable GeoJSON bundle with per-location condition grades, individual distress points (potholes, cracks, patch failures), and overlay images with the distresses highlighted.

The core claim: Claude Opus 4.7 can grade pavement condition on every publicly imaged street and pinpoint where the problems are, without training a custom CV model — at a cost that fits a municipal triage budget, not an engineering-firm budget.

**Built for the Opus 4.7 Hackathon. Goal: win.**

## UI/UX bar

The interface must be the cleanest and smoothest in the world — design as if an Apple designer is building it. Every interaction: deliberate, quiet, fast. Whitespace, restraint, typography, precision. No busy layouts, no gratuitous animation, no noise. If a UI decision feels "fine" it isn't good enough. Friction is the enemy; polish is the product. This applies to every screen, every transition, every micro-interaction — there are no throwaway surfaces.

## The target user

Public-works departments, city DOTs, pavement-management contractors, asset-management consultants, and citizen advocates who want to answer **"which streets need attention first, and what's wrong with them?"** today — without waiting for the next $250k windshield survey.

Explicit positioning: this is **triage**, not a PCI/PASER survey. Claude is not a pavement engineer. We produce a plain-language 5-tier condition grade and a map of visible distresses — intended as an input to an engineer's prioritization, not a replacement for one.

## What we produce per run

One run takes a **street name (or polygon)** and yields:

1. **Per-image condition grade** on a 5-tier Claude-triage scale (labels loosely familiar to pavement engineers, but *not* claiming PCI equivalence):
   - **Good** — no visible distress; smooth surface.
   - **Satisfactory** — hairline cracks, minor wear; no intervention needed.
   - **Fair** — visible longitudinal/transverse cracks, light raveling, minor patches; watch-list.
   - **Poor** — widespread cracking (block/alligator), rutting, multiple patch failures, edge breaks.
   - **Failed** — potholes, structural failure, impassable-grade damage.
2. **Distress inventory** per image — a list of individual distresses with type, severity, and a bounding box in image pixels. Types: `pothole`, `crack_longitudinal`, `crack_transverse`, `crack_alligator`, `patch_failure`, `raveling`, `edge_break`, `rutting`, `utility_cut`.
3. **Approximate distress geolocation** — each bbox back-projected to lat/lon using the camera's GPS + compass + a flat-ground assumption, then clamped to the nearest road centerline. Every distress carries a `location_confidence` because this degrades with distance from the camera.
4. **Overlay images** — the pavement-strip crop with bboxes drawn, for click-through from the map.
5. **Temporal comparison** (where both epochs exist) — same location, earliest-available vs. latest; surfaces a `critical_flag` for locations that have been Poor/Failed for multiple years.
6. **Map-ready GeoJSON bundle** — `points.geojson` (one Point per camera with condition), `distresses.geojson` (one Point per distress), `segments.geojson` (OSM-way aggregation).

## Image-selection algorithm (the cost-control core)

Naive sampling of Mapillary over a city burns >$100 on Opus. We use a visibility-chain strategy to minimize redundant calls:

1. **Pano-only filter.** Only 360° panoramas — they give us front + back ground views from one capture.
2. **Haiku validity + visibility pass.** One cheap Haiku call per candidate image predicts (a) whether this is actually a drivable street (not a garage, park, driveway, or indoor shot) and (b) `vis_forward_m` / `vis_backward_m` — how many meters of pavement the down-pitched front and back viewports can *reliably assess* for a ~2 cm crack or pothole.
3. **Greedy visibility chain.** Along each street direction, sort candidates by position. Keep image A, then pick the farthest next image B such that `dist(A, B) ≤ vis_forward(A) + vis_backward(B)`. B's backward coverage and A's forward coverage tile the road with minimal overlap. Result: ~1 Opus call per 40–60 m (vs 1 per 10 m naive) — roughly **4–6× fewer deep analyses**.
4. **Temporal twin.** For each chained location, also retain the earliest-year pano within ~15 m. Historic twin is only Opus-analyzed where its latest counterpart scored Fair or worse — keeps temporal cost proportional to finding severity.

## Distress geolocation (back-projection)

For each bbox from Opus on a pavement strip rendered at known (yaw, pitch, hfov):
- Compute pixel center → angle offsets → under a flat-ground assumption with camera height ≈ 2.5 m, convert to ground distance + bearing offset from the camera's compass heading.
- Offset the camera's lat/lon by that bearing × distance to get a world point.
- Snap to the nearest road centerline (shapely `nearest_points`) to absorb small angular errors.
- Tag each distress with `location_confidence = 1 − min(dist_m / 30, 1)` — honest about degradation past 30 m.

This is *approximate* (±1–2 m near-field, ±5–10 m far-field). It's good enough for a map click-through, not good enough for GIS asset records. Documented as such in output.

## Architecture (decided)

### Stack
- **Backend:** FastAPI (Python 3.11+), async httpx, SQLite for caching
- **Frontend:** vanilla JS + Leaflet + Leaflet.draw, no framework
- **Data:** OpenStreetMap via Overpass (road geometry, schools, transit stops, hydrants, etc.), Mapillary API v4 (imagery)
- **AI:** Claude Haiku 4.5 for triage, Claude Opus 4.7 for deep analysis
- **Env:** python-dotenv with .env holding `MAPILLARY_TOKEN` and `ANTHROPIC_API_KEY`

### Data flow (pavement assessment)
1. User provides a **street name + city/zip** (or a polygon). Geocode via Overpass `name=` filter → get the OSM LineString(s) → buffer ~8 m → polygon.
2. Fetch all Mapillary captures inside polygon; **filter to panos only** (`is_pano=True`).
3. Spatio-temporal grouping: cluster captures by ~10 m, keep (a) the **latest** capture per cluster as the primary and (b) the **earliest** capture within ±15 m as the temporal twin.
4. **Haiku validity + visibility pass** (one call per primary image): returns `{valid, reason, vis_forward_m, vis_backward_m}`. Rejects garage/park/indoor/blurry shots; predicts how far forward and backward the pavement can be reliably assessed.
5. **Visibility-chain greedy selection**: sort primaries along each street sequence; pick A, then the farthest B such that `dist(A,B) ≤ vis_f(A) + vis_b(B)`; repeat. Produces a minimal set where adjacent images cover each other's blind spots.
6. **Opus 4.7 pavement rating** (one call per chained image, prompt cached on rubric): extract forward + backward pavement strips (yaw=0/180, pitch=-35°, 768×384), return 5-tier condition + distress list with pixel bboxes + per-distress approx ground distance.
7. **Distress geolocation**: back-project each bbox using camera intrinsics + compass + flat-ground (h≈2.5 m); offset the camera's GPS by bearing × distance; snap to nearest road centerline; attach `location_confidence` that decays past 30 m.
8. **Temporal pass** (only where primary ≥ Fair and a historic twin exists): Opus-rate the historic image → flag `critical_flag=true` if both are Poor/Failed, or `trending=degrading` if the jump is ≥ 2 tiers.
9. Render distress overlays (strip JPEG with bboxes drawn, severity-colored).
10. Write **map-ready GeoJSON bundle** + `summary.md` + `demo.html`.

### Cost management
- **Pano filter first** — panos give front + back from one capture, halving the image count for the same coverage.
- **Haiku-gate every Opus call** — garbage frames rejected for ~$0.002 instead of ~$0.08.
- **Visibility-chain spacing** cuts Opus calls by ~4–6× vs naive 10 m spacing.
- **Prompt caching** on the Opus rubric (long system prompt, same every call) → ~10× cheaper on cached input tokens.
- **Idempotent caching**: results keyed by `image_id + prompt_version`; re-running is free.
- Target for a single street (~500 m): **≤ $5 all-in**, with temporal comparison enabled.

### Honest limitations (document in README)
- Mapillary coverage is crowdsourced and uneven. Dense in major cities, sparse in rural areas.
- Claude is not a pavement engineer. Our 5-tier grade is plain-language triage, explicitly not PCI/PASER/IRI.
- Bbox coordinates are approximate (model-level); overlays are illustrative, not surveyor-grade.
- Distress geolocation degrades with distance — near-field ±1–2 m, far-field (>20 m) ±5–10 m.
- Pavement surface is sensitive to shadows, wetness, and glare. Prompt guards against inferring cracks from shadows/oil/paint lines, but failures still happen. Use multi-image agreement when possible.
- Historic imagery is sparse (Mapillary is user-contributed); temporal comparison only works on corridors with repeat coverage.

## Prompt engineering principles

The pavement prompt has:
- **Role framing:** "You are a pavement-triage spotter for a public-works department. You are NOT an engineer. Your job is to flag which sections a human inspector should visit first."
- **Urgency ladder (5 tiers)** with concrete visual cues per tier — no PCI numbers.
- **Distress taxonomy** with one-line definitions + how-to-spot cues.
- **Hard guardrails:** "If you cannot see pavement, return condition=unknown and distresses=[]. Do not infer cracks from shadows, oil stains, or paint lines. Prefer under-reporting to speculation."
- **Ultra-terse JSON output schema** — every extra field is money.
- **`cache_control: ephemeral`** on the system prompt so the long rubric is cached across calls within a 5-minute window.

## Target file structure (post-pivot)

```
pavtrace/
  app/
    main.py                  # FastAPI entry
    mapillary.py             # Mapillary API client
    osm.py                   # Overpass queries (+ fetch_street_by_name, buffer_to_polygon)
    panorama.py              # equirect -> viewport + pavement-strip extraction + overlay drawing
    geoproject.py            # bbox -> world lat/lon back-projection + road clamping
    claude.py                # Haiku validity+visibility + Opus pavement rating (prompt cached)
    prompts/
      triage.py              # LEGACY: 10-category Haiku triage (kept for run_area_triage.py)
      near_miss.py           # LEGACY: one of 10 safety categories
      pavement.py            # NEW: validity+visibility Haiku prompt + Opus 5-tier rubric
  scripts/
    run_area_triage.py       # LEGACY: 10-category safety-atlas pipeline
    run_pavement_assessment.py  # NEW: street-name -> condition GeoJSON bundle
  static/
    index.html               # map UI (polygon draw kept; condition overlay layer next)
    app.js
    style.css
  downloads/
    thumbs_1024/             # shared 1024-max-side thumbnail cache
    <year>/pano/<image_id>.jpg  # full-res equirectangular panos (bulk download)
    metadata_enriched.json   # compass_angle + sequence + date per image_id
    pavement/<run_slug>/     # one directory per pavement run (see below)
    area_triage/<run_slug>/  # legacy safety-atlas runs
  .env (MAPILLARY_TOKEN, ANTHROPIC_API_KEY)
  requirements.txt
  CLAUDE.md                  # this file
```

Per-run output layout (pavement):
```
downloads/pavement/<run_slug>/
  config.json                # street name, polygon, versions, seed, budget
  street.geojson             # geocoded OSM ways for the street
  polygon.geojson            # buffered capture polygon
  captures_all.json          # raw Mapillary result
  primaries.json             # spatio-temporally grouped {primary_id, twin_id, lat, lon, year}
  validity.json              # Haiku pass: {image_id: {valid, vis_f, vis_b, reason, cost}}
  chain.json                 # visibility-chain selected image_ids (per sequence)
  strips/<image_id>_f.jpg    # 768x384 forward pavement strip
  strips/<image_id>_b.jpg    # 768x384 backward pavement strip
  ratings.json               # Opus pass: {image_id: {condition, distresses[], cost, usage}}
  temporal.json              # {primary_id: {historic_condition, critical_flag, trending}}
  overlays/<image_id>_f.png  # strip with distress bboxes drawn
  overlays/<image_id>_b.png
  points.geojson             # Point per primary with condition + distress_count
  distresses.geojson         # Point per detected distress with type/severity/confidence
  segments.geojson           # OSM-way aggregation
  summary.md                 # run report
  demo.html                  # static Leaflet viewer
```

## Existing endpoints (MVP prototype, still in place)

- `POST /api/analyze` — body `{polygon}` → `{roads (FeatureCollection), road_count}`. Overpass filter: `motorway|trunk|primary|secondary|tertiary|unclassified|residential|service`.
- `POST /api/images` — body `{polygon}` → `{captures[], years_available[], truncated}`. Captures capped at 2000 (`MAX_CAPTURES` in `app/mapillary.py`).
- `GET /api/image/{image_id}` → `{url, captured_at, lat, lon, is_pano}`. Fetches `thumb_1024_url` from Graph API.

## Implementation notes (carry-overs from MVP)

1. Overpass query uses `(poly:"lat lon lat lon ...")` filter (note: lat lon order, not lon lat).
2. Mapillary vector tiles fetched at zoom 14 using `mercantile.tiles(bbox, 14)`. Each tile decoded via `vt2geojson` (layer `image`). Points then filtered with `shapely` against the actual polygon (not just bbox).
3. `captured_at` from Mapillary comes as ms-since-epoch — `mapillary._parse_captured_at` handles both that and ISO strings.
4. For full-res image downloads, use Graph API field `thumb_original_url` (full 360 equirectangular for panos) with `thumb_2048_url` as fallback. See `download_images.py`.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # add MAPILLARY_TOKEN (and later ANTHROPIC_API_KEY)
uvicorn app.main:app --reload
```

## Current state

- MVP prototype working: polygon draw, Overpass road geometry, Mapillary image fetch, year filter, click-to-view in sidebar
- Bulk image download utility working (`download_images.py`, `_run_download.py`) — pulls full-res Mapillary originals including 360 panos, organized by `year/pano|flat/<image_id>.jpg`
- 869 panos already downloaded for the Chestnut corridor (2016, 2018, 2019, 2025, 2026); `downloads/metadata_enriched.json` caches compass_angle + sequence + date
- Legacy safety-atlas pipeline (`scripts/run_area_triage.py`) complete but produced thin signal — kept, not extended
- **Pavement pipeline in progress** (2026-04-24): `app/prompts/pavement.py`, `app/geoproject.py`, extensions to `panorama.py`/`claude.py`/`osm.py`, and `scripts/run_pavement_assessment.py`

## What's explicitly out of scope

- User accounts, saved polygons, multi-tenant features (post-hackathon)
- Live streaming of new imagery as it arrives (post-hackathon)
- Mobile app (post-hackathon)
- Commercial deployment (post-hackathon)
- **PCI / PASER / IRI-scaled outputs** — we produce plain-language triage tiers only. No index numbers that imply engineered accuracy.
- Training custom CV models — Opus 4.7 with good prompts is the whole intelligence layer
- Any analysis that requires imagery we don't have (aerial views, nighttime imagery, LiDAR)
- Broader 10-category hazard atlas — parked; pavement is the focus.

---

## Agentic architecture (hackathon hero — SHIPPED 2026-04-24)

The batch pipeline above still exists and is correct, but the hackathon entry is a separate agent loop. Opus 4.7 drives; the code is a tool host + replay UI. **Validated end-to-end on Broadway/SoHo: $8.11 / 58 turns / 9 findings (19 distresses + 13 hazards, all with pixel bboxes) / clean `done` exit.**

### Demo target
- **Area:** Broadway between Canal St and Broome St, SoHo, Manhattan, NY 10013 — preset key `soho_broadway` in [scripts/run_agent_survey.py](scripts/run_agent_survey.py). Bounding rectangle ~260 m × 250 m around Broadway between the two cross-streets, including a half-block buffer east/west to pick up Crosby and Mercer captures.
- **Deliverable:** recorded video. No live demo. Agent runs locally; every turn is logged to `agent_trace.jsonl`; the UI replays the trace as animation.
- **Budget caps:** soft-tunable via CLI. Validation run used `--budget 15 --turns 60` and self-terminated at 54% of budget.

### Tool surface exposed to Opus

Seven tools, all in [app/agent/tools.py](app/agent/tools.py). The JSONSchema `description` fields carry decision heuristics so they're cached as part of the tool list and don't have to be re-explained in the system prompt every turn.

1. **`get_network_state()`** — returns segment coverage, uncovered ids, current position, turn/budget counters, last 5 findings. Free.
2. **`list_captures_near(lat, lon, radius_m=25, limit=8)`** — metadata-only list of pre-filtered pano candidates within radius. Reads from `primaries.json` in memory. Free.
3. **`haiku_prescreen(image_id)`** — one Haiku 4.5 call (~$0.001) wrapping `classify_validity_visibility_async` + `detect_forward_yaw_async`. Returns `{valid, reason, vis_forward_m, vis_backward_m, forward_yaw_deg, recommended_viewports}`. Cached per image_id.
4. **`extract_viewport(image_id, direction)`** — `direction ∈ {forward, backward, left, right, pavement_down}`. Renders a 768×512 strip via `extract_pavement_strip` with the prescreen's `forward_yaw_deg` correction applied. Returns image block + text caption. Cached on disk per `(image_id, direction)`.
5. **`record_finding(image_id, pavement_condition, distresses, hazards, notes)`** — single per-location consolidator. Both `distresses` and `hazards` require pixel bboxes [x1,y1,x2,y2] in the 768×512 strip. Hazard taxonomy: faded_markings, missing_sign, damaged_sign, drainage_issue, debris, sightline_obstruction, sidewalk_damage, curb_damage, vegetation_overgrowth, construction_zone.
6. **`mark_segment_covered(segment_id, reason)`** — Opus declares completion to avoid revisits.
7. **`done(summary)`** — terminates the loop; the summary lands in `summary.md`.

Trace format (one JSONL line per record): `record_type ∈ {run_header, turn_assistant, tool_result, system_note, run_complete}`, plus `t_ms`, `turn`, and type-specific fields. See [app/agent/trace.py](app/agent/trace.py).

### Agent loop ([app/agent/loop.py](app/agent/loop.py))
- Async Anthropic SDK tool-use loop. Default model `claude-opus-4-7` (NOT the [1m] variant — context pruning keeps us under 20k input tokens/turn).
- System prompt (~7.9k chars, [app/agent/prompts.py](app/agent/prompts.py)) is single-block + `cache_control: ephemeral` → first turn writes cache, subsequent turns read at 0.10× input price.
- Context pruning ([app/agent/trace.py](app/agent/trace.py) `prune_image_blocks`) keeps the last 2 image blocks verbatim and replaces older image blocks (including those inside `tool_result` content arrays) with `{"type":"text","text":"[image pruned …]"}` placeholders.
- Hard caps: `--turns N` and `--budget USD`. Soft warnings inject a user message at 85% budget and turn_cap-3 telling Opus to wrap up cleanly.
- Idempotent restart not implemented in v1; trace is append-only but a fresh run starts from turn 1.

### Pre-filtering before the loop ([scripts/run_agent_survey.py](scripts/run_agent_survey.py))
The agent never sees raw Mapillary output. Before turn 1 the script:
1. Overpass → road segments inside polygon → `roads.geojson` + `Segment[]`.
2. `fetch_captures` → all captures in polygon → `captures_all.json`.
3. Pano filter → drop ~10%.
4. Spatio-temporal dedup at 12 m → keep latest pano per cluster → ~20–40 primary candidates.
5. `fetch_image_detail_bulk` → thumb_1024_url + compass_angle + sequence per primary → `primaries.json`.
6. Build `AgentState`, invoke `run_agent`.

This cuts the agent's first message size 10× and eliminates the "agent wastes turns filtering" failure mode.

### Budget optimization (the architectural commitments that make this fit in budget)
1. **Prompt caching** on the system message (10× savings on rubric across turns).
2. **Haiku prescreen tool** keeps Opus off garbage frames (~$0.001 vs ~$0.08 for the same decision).
3. **Context pruning** — last 2 image blocks only.
4. **Pre-filtered candidate list** so Opus sees ~30 image_ids, not 3000.
5. **Per-disk viewport cache** so re-extraction is free.
6. **Resolution discipline** — never send full-res panos to the model; only 768×512 strips.
7. **Hard caps with graceful termination** — system message at 85% budget tells Opus to call `done` cleanly.
8. **One-pass richness** — same image, same tokens, both pavement + hazards extracted per finding.

Validation run came in at $8.11 of $15.00 cap (54%) — comfortable headroom.

### Replay UI ([static/agent.{html,css,js}](static/agent.html))
- Routes (added to [app/main.py](app/main.py)): `/agent`, `/agent/{slug}`, `/api/agent/runs`, `/api/agent/{slug}/bundle`, `/api/agent/{slug}/trace`, `/api/agent/{slug}/file/{path}` (path-traversal-guarded mirror of the pavement file route).
- Layout: 64-px topbar (brand, run picker, play/pause, 1×/2×/4×/8× speed) · 440-px transcript column · map stage with HUD overlay · 58-px scrubber.
- Transcript: live-scrolling cards, one per assistant turn + one per tool_result. Reasoning cards show `text` (and `thinking` if present, dimmed). Tool cards have a color-coded badge (blue=prescreen, green=viewport, orange=finding, red=done). Finding cards include condition chip, distress + hazard lists with severity colors, and 4 viewport thumbnails (click → lightbox).
- Map: CARTO Positron tiles (Dark-Matter under `prefers-color-scheme: dark`). Polygon outlined; segments start gray and recolor by median nearby finding when `mark_segment_covered` fires; finding pins drop in as `record_finding` fires; pulsing-blue agent cursor moves to camera GPS of last viewport/finding; dashed agent path traces movement.
- Scrubber rebuilds state deterministically turn-by-turn (cheap — ≤80 turns).
- Apple-grade styling: imports CSS variables from [static/pavement.css](static/pavement.css), respects `prefers-color-scheme: dark` and `prefers-reduced-motion: reduce`, responsive collapse to vertical stack at < 880 px.

### File layout (as shipped)
```
app/agent/
  __init__.py
  state.py        # AgentState, Segment, Primary, Finding, GeoJSON dumpers, haversine
  trace.py        # TraceWriter (JSONL), prune_image_blocks
  tools.py        # 7 tool schemas + impls + ToolServices + execute_tool dispatcher
  prompts.py      # AGENT_SYSTEM_PROMPT, build_seed_user_message
  loop.py         # run_agent: tool-use loop, pruning, budget caps
scripts/
  run_agent_survey.py   # block presets, pre-filter pipeline, invokes run_agent
static/
  agent.html      # topbar, transcript, map, HUD, legend, scrubber, lightbox
  agent.css       # ~500 lines, imports pavement.css tokens
  agent.js        # replay engine: trace loader, transcript renderer, map animator
downloads/agent/<run_slug>/
  config.json, polygon.geojson, roads.geojson
  captures_all.json, primaries.json
  agent_trace.jsonl              ← hero artifact for replay
  findings.geojson, network.geojson
  state.json                     ← persisted after every record_finding
  viewports/<image_id>_<dir>.jpg  ← cached renders, served to UI
  panos/<image_id>.jpg            ← on-demand full-res download from Mapillary
  thumbnails/<image_id>.jpg
  probes/<image_id>_probe.jpg
  summary.md                      ← agent's own wrap-up + condition/distress tables
```

### Block presets ([scripts/run_agent_survey.py](scripts/run_agent_survey.py) `BLOCKS` dict)
- `soho_broadway` — Broadway · Canal → Broome, Manhattan NY 10013. Validated.
- `chestnut_ventura` — legacy corridor, kept as fallback.

To add a new preset: append to `BLOCKS` with a polygon (use `_rect_polygon(west, south, east, north)`), a name, a description. Then `python scripts/run_agent_survey.py --block <key>`.

### How to run

```bash
# Cheap dry run to validate plumbing
python scripts/run_agent_survey.py --block soho_broadway --budget 2 --turns 15

# Full demo run
python scripts/run_agent_survey.py --block soho_broadway --budget 15 --turns 60

# Resume — re-use cached captures/primaries from a prior run dir
python scripts/run_agent_survey.py --block soho_broadway --resume
```

After a run: `uvicorn app.main:app --reload` then open [http://127.0.0.1:8000/agent/soho_broadway_canal_broome](http://127.0.0.1:8000/agent/soho_broadway_canal_broome). Auto-plays at 2×.

### Validation snapshot (`soho_broadway_canal_broome`, $15/60-turn run)
- 30 OSM segments · 606 Mapillary captures · 45 panos · 21 primaries after dedup
- 58 turns / $8.11 / 9 findings / 6 segments covered / clean `done` exit
- Tool ratio: 13 prescreens → 16 viewport extracts → 9 findings (~1.8 viewports/finding)
- Narration: 38/58 turns with text (65%) — driven by the "narrate every turn" directive in the system prompt
- Conditions: 4 Fair · 5 Satisfactory · 0 Poor/Failed (SoHo Broadway is in decent shape)
- Distresses: 7 crack_longitudinal, 6 utility_cut, 3 patch_failure, 2 raveling, 1 edge_break — all with pixel bboxes
- Hazards: 6 construction_zone, 3 faded_markings, 2 sightline_obstruction, 1 sidewalk_damage, 1 drainage_issue — all with pixel bboxes

### Out of scope for v1
- Temporal twins / historic comparison.
- Multi-block sweeps, city-wide views.
- Live demo / streaming. Recording only.
- Idempotent loop resume (trace is append-only but reruns start from turn 1).
- Bbox overlays drawn on viewport thumbnails in the UI (data is there, just not rendered yet).

### Next steps (see [PROGRESS.md](PROGRESS.md) § "Next steps" for the full ordered list)

Must-do before hackathon submission:
1. Polish pass on the replay UI (~30 min).
2. Record the demo video (~1 hr) — QuickTime at 2× playback, 60–90 s final cut.
3. README update (~15 min) — one section pointing to the `/agent` demo.

Nice-to-have if time permits:
4. Second run on a rougher corridor to show Poor/Failed tiers in the video (~30 min + ~$10).
5. Bbox overlays on viewport thumbnails (~40 lines JS).
6. Hide `_dryrun` slugs from the run picker (~5 min).

---

## Operational gotchas + key references (for future Claude sessions)

### Mapillary v4 API quirks discovered the hard way

- **`fetch_captures` cap of 2000 features starves panos in dense LA bboxes.**
  Cell-phone uploads dominate the first 2000 in DTLA-area tiles, leaving 0-4 panos.
  **Fix**: pass `panos_only=True` to filter at tile-parse time *before* the cap
  ([app/mapillary.py:65](app/mapillary.py#L65)). With it on the same bbox returns 10,000+ panos.
- **Graph API `/images?lat=Y&lng=X&radius=R` caps at 25 m** (default 10 m). For
  tighter waypoint queries this is great; for wider radii fall back to tile-fetch +
  haversine filter (the existing `fetch_captures` pattern).
- **No `quality_score` field.** Image-quality screening must be done by Haiku probe
  on a downloaded thumbnail.
- **Available metadata fields (no JPEG download)**: `captured_at`, `compass_angle`,
  `creator`, `sequence`, `is_pano`, `mesh`, `altitude`, `geometry`, `width`,
  `height`, `make`, `model`, `camera_type` (perspective/fisheye/equirectangular).
- **No time-of-day field.** Derive from `captured_at` ISO 8601 timestamp client-side.
- **Vehicle vs pedestrian rig**: not directly reported. Infer from
  `make`/`model`/`camera_type` + visual peek (this is exactly what Opus is for).

### Mapillary 360° pano anatomy (verified by direct inspection — see [app/agent/pano_inspector.py](app/agent/pano_inspector.py) `INSPECTOR_SYSTEM_PROMPT`)

LA Mapillary panos are a **mix of capture rigs**, not just car-roof:
- **Car/SUV roof rig** — bottom band shows dark glossy roof, sometimes sunroof.
- **Pedestrian handheld stick (very common in LA)** — bottom shows operator's
  head + arms + phone + feet. Camera at human height; road 2-5 m away.
- **Pedestrian chest/helmet mount** — bottom shows torso + lap + shoes; looking
  down reveals dirt/gravel/sidewalk, not road.
- **Bicycle/scooter** — handlebars + helmet at bottom.
- **Tripod** — tripod legs + small ground patch at nadir.

**Bottom 30% of any pitch≤-30° view is rarely the pavement to grade.** Some panos
have NO paved road in frame at all (staircases, hiking trails, plazas). In that
case grading "unknown" is correct; hedging to Sat is a calibration failure.

### LA PCI ground truth ([data/la_pci/segments.geojson](data/la_pci/segments.geojson))

- 69,282 LA street segments, downloaded from
  `https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/arcgis/rest/services/StreetsLAMap_20240812_LM/FeatureServer/18`.
  Item id `8439cf3e810b46489956ad5450875e2a` (search AGOL REST, the public
  geohub.lacity.org dataset URL is JS-rendered and 404s in WebFetch).
- Schema: `SECT_ID`, `ST_NAME`, `ST_TYPE`, `ST_SURFACE`, `PCI` (double 0-100),
  `STATUS` (Good/Fair/Poor LA's coarse 3-tier), `CD` (council district),
  LineString geometry. Spatial reference WKID 103007 — request `outSR=4326`
  for WGS84 on download.
- **Tier mapping (industry-standard PCI thresholds, in [scripts/build_calibration_set.py](scripts/build_calibration_set.py))**:
  - Good ≥ 86 (25%) · Sat 71-85 (34%) · Fair 56-70 (19%) · Poor 41-55 (7%) · Failed <41 (14%)
- **Caveats discovered in calibration**: PCI scoring uses laser-measured rutting +
  IRI roughness. Many "Failed" segments look visually intact at street-level
  imagery resolution. NORMANDIE AV (PCI 2.28) is a Caltrans concrete freeway —
  may not be BSS-jurisdiction-correct. Filter `ST_SURFACE` if doing strict
  asphalt-only validation.

### Calibration toolkit (built 2026-04-25)

- [scripts/build_calibration_set.py](scripts/build_calibration_set.py) — stratified-sample N segments per tier inside a study bbox, match each to a Mapillary 360° pano within ≤20 m of segment midpoint. Writes `manifest.json` + thumbnails.
- [scripts/download_calibration_fullres.py](scripts/download_calibration_fullres.py) — fetch `thumb_original_url` (5760×2880 equirect) for every sample. Adds `pano_path` to manifest.
- [scripts/filter_calibration_road_visible.py](scripts/filter_calibration_road_visible.py) — for each sample, render a 2×2 grid of cardinal viewports, run a Haiku "is paved vehicular road visible?" check. Writes `manifest_clean.json` with only road-visible samples.
- [scripts/run_calibration.py](scripts/run_calibration.py) — single-call grader (one Opus call per sample, equirect input). Confusion matrix + per-tier metrics.
- [scripts/run_calibration_agentic.py](scripts/run_calibration_agentic.py) — per-pano agentic inspector ([app/agent/pano_inspector.py](app/agent/pano_inspector.py)). Opus has `look(yaw,pitch,hfov)` + `grade(tier,...)` tools. Forces commitment via "FINAL TURN" injection.
- [static/calibration.html](static/calibration.html) — Leaflet map of the calibration set with tier-colored segments + pano markers + clickable thumbnails. Routes in [app/main.py](app/main.py).

### Per-pano inspector loop ([app/agent/pano_inspector.py](app/agent/pano_inspector.py))

- Two-tool surface: `look(yaw_deg, pitch_deg, hfov_deg, purpose)` and `grade(tier, confidence, rationale)`.
- System prompt has explicit Mapillary-anatomy section + tier rubric tied to ASTM PCI brackets + visual-confuser list + anti-Sat-hedge rule + recommended scan plan.
- Turn cap 4–6, budget cap ~$0.30/pano. Forced final-turn grade injection prevents no-grade timeouts.
- `render_view(equi, yaw_deg, pitch_deg, hfov_deg, out_w=768, out_h=512)` is the underlying viewport primitive. Reuse it; don't re-implement.

### Fleet pattern ([app/agent/worker.py](app/agent/worker.py) + [scripts/run_fleet_survey.py](scripts/run_fleet_survey.py))

- One narrow Worker per primary-cluster (3-5 panos), parallel via `asyncio.gather` + `Semaphore`.
- Tool surface trimmed: `haiku_prescreen`, `extract_viewport`, `record_finding`, `done`.
- Shared `fleet_trace.jsonl` with `worker_id` per event, `asyncio.Lock` protecting writes.
- UI at `/fleet/{slug}` color-codes events by worker.

### Live transparency feed ([static/live.html](static/live.html))

- Polls `/api/agent/{slug}/trace/tail?offset=N` every 800 ms, parses new JSONL lines, renders cards as they arrive. Inline viewport JPEGs.
- Same pattern at `/fleet/{slug}` for the fleet trace.

### Bbox overlay generator ([scripts/draw_bbox_overlays.py](scripts/draw_bbox_overlays.py))

Reads any agent or fleet trace, draws per-finding distress (red) + hazard (amber) rectangles on the corresponding viewport JPEG. Note: bboxes are loose (region-of-interest, not surveyor-grade); the deliverable has been pivoted away from claiming pixel precision.

### Windows + uvicorn gotchas

- **`PYTHONIOENCODING=utf-8`** on every Bash command that runs a Python script in this repo. The default `cp1252` Windows stdout chokes on `→`, `…`, etc., used in our progress prints.
- **`uvicorn --reload` on Windows** sometimes stalls mid-reload (WatchFiles event-loop issue). When it happens: stop the background task, restart with `--reload` again. Always wait for an HTTP-200 probe on a known route before assuming reload took.
- **Don't prefix Bash commands with `cd`** when the working dir is already correct (causes permission prompts on git).
