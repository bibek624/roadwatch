# PavTrace — Context Handoff (2026-04-26, end of v3 session — HIERARCHY)

> **Update (2026-04-26, late session — 3-TIER HIERARCHY shipped):** The single-loop walker described below still works (now `mode="single_walker"`, fallback at `/temporal_v1`) but the **hackathon hero is now a 3-tier hierarchical multi-agent ecosystem**. Captain (1×) → up to 3 parallel Surveyors → up to 2 parallel Year Investigators per Surveyor. Inter-agent communication via per-point + per-street JSON blackboards (`asyncio.Lock`-protected). New code lives under [`app/agent/hierarchy/`](app/agent/hierarchy/). UI gained a launch-screen Mode dropdown (Hierarchical default), a live tree-view above the chat, multi-cursor map, and 6 new event card types (`agent_spawned`, `agent_completed`, `dispatch_order`, `blackboard_post`, `report_up`, `redo_order_issued`). CLI: `scripts/run_hierarchy_walker.py`. Validated on SPRING ST 3-pt run: $4.78 / 12, 3 tiers (Good/Poor/Fair), clean `agent_done`. See the **3-TIER HIERARCHY** Direction block at the bottom of [`CLAUDE.md`](CLAUDE.md) for the full architecture; the plan file is at `~/.claude/plans/so-now-i-want-shiny-spindle.md`.



This file is the **starting point for a fresh Claude Code session**. Read this first; it points at everything else.

Author: **Bibek Acharya** — pavement engineer + remote-sensing background. Built for the **Opus 4.7 hackathon** (submission Sun Apr 26 2026, 8:00 PM EST).

---

## TL;DR — what the app is, where we are

**PavTrace** is an agentic pavement-condition-triage system. The user draws a polygon on a map, picks a street from auto-matched results, and an Opus 4.7 agent walks the street's centerline. At each **survey point** (every 50 m by default) it:

1. Pulls Mapillary 360° pano candidates within 30 m, **stratified by year** so multi-year coverage surfaces
2. Detects + suppresses **byte-identical duplicates** (SHA-256 collision check) so duplicate-listing artifacts don't fake a temporal arc
3. Investigates each year's panos: peek (Haiku) → look_around → look → zoom_into_region, with **mandatory cross-witness** (≥2 panos per year, same yaw across years)
4. A deterministic **pre-grade discipline gate** refuses sloppy grades and instructs the agent to fix specific gaps
5. Grades on a **3-tier scale (Good / Fair / Poor / unknown)** + emits structured evidence (distresses, treatments, safety_flags, surroundings, inconsistencies, evidence_image_ids)

The hero artifact is the **live UI at `/temporal`** showing the agent reasoning + investigating in real time, plus a structured per-point evidence catalog at `evidence/wp00N.json`.

---

## Read these in order to bootstrap

1. **`README.md`** — project overview + how to run
2. **`CLAUDE.md`** — repo instructions + Direction blocks at top (auto-loaded)
3. **`PROGRESS.md`** — full ship log; the **2026-04-26 section at the bottom** is most relevant for v2
4. **`DECISIONS.md`** — key architectural decisions with rationale
5. **`.claude/skills/pavement/README.md`** — index of the 13 engineering-knowledge skill files

---

## Architecture as shipped (end of session)

```
USER draws polygon → /temporal page
                          │
                          ▼
        POST /api/temporal/streets-in-polygon
        → Overpass: named streets in the polygon
                          │
                          ▼
        User picks one (or selects all)
                          │
                          ▼
        POST /api/temporal/start  ── returns slug IMMEDIATELY
        (background task: prefetch panos via tiles → run_street_walker)
                          │
                          ▼
        Walker loop, per survey point:
          • find_candidates(stratified by year, ≥6/year, +SHA dedup)
          • peek_candidate (Haiku, ~$0.001) on each older-year cand.
          • look_around → look → zoom_into_region
          • Cross-witness: ≥2 panos/year at MATCHING yaw
          • grade()  ← passes through pre-grade discipline gate
                       Three rules; 2-strike escape; refusals
                       come back to the agent as tool errors with
                       a recovery plan + visit log.
                          │
                          ▼
        Live UI at /temporal/{slug} polls trace + status.
        Stop button writes _stop_requested.flag → walker exits cleanly.
```

### The 9 walker tools

| Tool | Cost | Returns |
|---|---|---|
| `get_position()` | free | current point + total + budget |
| `find_candidates(radius_m=30, max_age_years=12, limit=30, min_per_year=6, year_filter=N?)` | ~$0 | candidates stratified by year + total-per-year breakdown + suppressed-duplicate notes |
| `peek_candidate(image_id)` | ~$0.001 (Haiku) | usable / rig / time_of_day / summary |
| `look_around(image_id, pitch, hfov, purpose)` | render only | 2×2 cardinal grid |
| `look(image_id, yaw, pitch, hfov, purpose)` | render only | rectilinear view + minimap strip ABOVE |
| `zoom_into_region(image_id, src_*, x1,y1,x2,y2, purpose)` | render only | bbox-zoomed view at full equirect res |
| `grade(tier, confidence, rationale, chosen_image_id, evidence_image_ids?, distresses_observed?, treatments_observed?, safety_flags?, surroundings_notes?, inconsistencies?)` | free | records + advances; **passes through `_check_temporal_discipline`** |
| `skip_waypoint(reason)` | free | advances without grade |
| `done(summary)` | free | terminate |

### The 13 skill files at `.claude/skills/pavement/`

Cache block 1 (~70 KB):
- `tier_rubric.md` — **3 tiers** (Good / Fair / Poor / unknown) — recently rewritten
- `distress_taxonomy.md` — 10 distress types with mm thresholds
- `visual_confusers.md` — paint vs crack, manhole vs pothole, etc.
- `deterioration_progression.md` — S-curve, $1-now-$4-later economics
- `treatment_signatures.md` — 10 treatments visually identified
- `climate_failure_modes.md` — LA hot-arid signatures
- `repair_priority_logic.md` — risk × consequence

Cache block 2 (~39 KB):
- `pano_anatomy.md`
- `viewport_geometry.md`
- `scan_plan.md` — **rewritten for per-epoch cross-witness + N-candidate fallback**
- `zoom_investigation.md`
- `grade_discipline.md` — **rewritten for 3-tier + temporal pre-grade gate**
- `evidence_extraction.md` — **NEW** — structured per-grade evidence fields with neutral-language rules

Total system prompt: **~109 KB** (cached, $0.005/turn after first cache write).

---

## What's running right now

- `uvicorn app.main:app` on port **8000**
- v2 UI at **http://127.0.0.1:8000/temporal**
- v1 fallback at `/temporal_v1`
- Auto-refresh on file edit is OFF (started without `--reload` for stability)

---

## Quick-start commands

```bash
# Start uvicorn
PYTHONIOENCODING=utf-8 python -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --log-level warning > /tmp/uvicorn.log 2>&1 &

# Open the UI
http://127.0.0.1:8000/temporal

# Run walker from CLI (still works)
PYTHONIOENCODING=utf-8 python scripts/run_street_walker.py \
    --street-name "SPRING ST" --waypoint-spacing-m 50 \
    --limit-waypoints 4 --budget 14 --per-waypoint-turn-cap 24 \
    --slug spring_v8_test --max-age-years 12

# Verify skills compose
PYTHONIOENCODING=utf-8 python -c \
  "from app.agent.skill_loader import compose_walker_system, assert_all_skills_present; \
   assert_all_skills_present(); \
   p = compose_walker_system(); \
   print(f'OK: {len(p)} blocks, {sum(len(b[\"text\"]) for b in p):,} chars')"
# Expected: 2 blocks, ~109k chars
```

---

## Recommended demo

**Polygon over Sunset Strip western slot:** NW (-118.3565, 34.0975) → SE (-118.3525, 34.0958). This is the only verified clean multi-year corridor in the LA area — 1,163 panos across 2015 + 2016 + 2025, all SHA-distinct (verified by direct Mapillary Graph API probe).

For LA-PCI-grounded testing without temporal: **USC Hoover area** (-118.286, 34.022) has 200+ panos, all 2020, low pedestrian traffic outside school hours.

**Demo script (3 min):**

1. Open http://127.0.0.1:8000/temporal — landing on Ventura
2. Pan/zoom to Sunset Strip; draw polygon over the western slot
3. Pick "Sunset Boulevard" from the matched list (or click the blue line)
4. Ensure **Dev mode is OFF** (top-right toggle) — cost and step pills disappear, clean demo view
5. Launch survey → "Prefetching candidates" banner clears in ~6s
6. Watch the agent: chat-style cards stream in (🤖 assistant reasoning, 👁 tool calls, ✦ peeks, ⌖ zooms, ✓ grades). Map auto-flies to each survey point, blue pulse shows where the agent is, points recolor green/orange/red after grading.
7. Click into a verdict card to see the evidence pills (distresses, treatments, safety flags, surroundings, inconsistencies)
8. After completion, "↻ Start a new survey" CTA in sidebar; pulse stops, point shown as static green dot

---

## Files map (where to look for what)

### Code
```
app/agent/
  walker_state.py       — Waypoint, Street, WaypointCandidate, WalkerState
                          (visit_log, thumb_sha_cache, discipline_gate_strikes)
  street_walker.py      — Walker driver loop + 9 tool schemas + tool impls
                          + _find_candidates_impl (stratified, async, lazy enrich)
                          + _enrich_candidates_and_shas (parallel Graph API + SHA)
                          + _dedup_by_sha (collision detector)
                          + _check_temporal_discipline (the pre-grade gate)
                          + _composite_with_minimap (now strip-above-viewport)
                          + run_street_walker (stop-flag check, raised caps)
  skill_loader.py       — composes 13 skills into 2 cache_control blocks
  pano_inspector.py     — render_view primitive
  loop.py / state.py / trace.py / tools.py / worker.py — older paths

app/
  main.py               — FastAPI: temporal routes, walker routes, stop endpoint
  mapillary.py          — fetch_captures + fetch_image_detail (no longer bulk-called by prefetch)
  panorama.py           — equirect → rectilinear projection
  osm.py / claude.py    — Overpass + Anthropic helpers

scripts/
  run_street_walker.py  — CLI entry: build_street + prefetch + walker
                          (prefetch now skips bulk metadata by default)
  run_temporal_demo.py  — Phase B's single-turn multi-image storyteller
  ... older calibration / fleet scripts unchanged

static/
  temporal_v2.html      — THE PRIMARY DEMO UI (light mode, Apple-style,
                          chat sidebar on right, polygon + street picker)
  temporal.html         — v1 (kept as fallback at /temporal_v1)
  walker.html           — older walker UI (kept; CLI runs serve here)
  ... fleet.html, agent.html, live.html, calibration.html, pavement.html — legacy

.claude/skills/pavement/
  13 skill .md files (see "skill files" section above)
```

### Data
```
data/la_pci/
  segments.geojson       — 69k LA street segments with PCI 0-100 (40 MB)

downloads/walker/
  <slug>/
    config.json polygon.geojson roads.geojson
    captures_all.json primaries.json
    walker_trace.jsonl     ← THE HERO ARTIFACT
    findings.geojson waypoints.geojson street.geojson state.json summary.md
    evidence/wp00N.json    ← per-grade structured evidence (NEW in v2)
    viewports/<id>_y±NNN_p±NN_hNNN.jpg  (cached renders)
    panos/<id>.jpg         (full-res equirects, lazy)
    peek_grids/, thumbnails/, probes/
    _temporal_status.json  ← phase tracker for /api/temporal/runs/.../status
    _stop_requested.flag   ← present iff user clicked Stop
```

---

## Decisions to remember (don't relitigate) — see DECISIONS.md for rationale

1. **Tier rubric is 3 tiers**, not 5. Good / Fair / Poor / unknown. Sat→Good and Failed→Poor mappings exist as backwards-compat shims.
2. **Pre-grade gate is mandatory.** Three rules, 2-strike escape. Don't disable.
3. **Multi-year find_candidates is stratified, not recency-sorted.** `min_per_year=6` default.
4. **SHA-collision dedup** runs on multi-year requests. Trust the SHA, not the metadata `captured_at`.
5. **Bulk Graph API metadata is NOT fetched at prefetch.** Lazy-enrich the ~30 returned candidates inside `_find_candidates_impl`.
6. **Don't re-add "1-2 looks max" or any cost-saving phrase to tool descriptions.** Cached tool schema = system instruction on every turn.
7. **Vocabulary discipline:** `survey point` (not waypoint) in user-facing strings; `step` (not turn). Internal data keys unchanged.
8. **Neutral data-inconsistency language only.** No "fraud", "falsified", or vendor blame anywhere.
9. **Cost is dev-only.** Hidden by default in the demo UI.
10. **Minimap goes ABOVE the viewport, not as an inset.** Pavement image stays pristine.
11. **`PYTHONIOENCODING=utf-8`** on every Python invocation in this repo.
12. **Don't `cd` in Bash commands** — working dir is already correct.

---

## What's still on the critical path before submission

- **Demo video recording** (3 min, three acts × three beats)
- **Submission writeup** — lead with skills inventory + agentic capability + SHA-detector story
- **README polish** — pointer to `/temporal` as demo entry

## Open follow-ups (post-submission)

- Multi-street campaigns (sequential queue) — UI selects all, runs them one at a time
- Stale-PCI auto-flag with year math (Mapillary date - PCI survey date > N years)
- Distress-density heatmap (back-project bboxes to world coords)
- Idempotent restart of a stopped run

---

*End of handoff. Server's at http://127.0.0.1:8000/temporal. Sunset Strip western slot is the demo target. The architecture is solid — focus on the video.*
