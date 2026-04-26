# PavTrace — Architectural Decisions Log

This file captures the *why* behind each load-bearing architecture choice. Read this when:
- you're considering reverting one of these decisions
- you're touching a system that touches one
- you want to understand why something is the way it is before changing it

Format: each decision has **Context**, **Decision**, **Consequences**, **Status**, and (optionally) **Alternatives considered**.

---

## D-001 — Skills as the engineering brain (Pattern 2 from strategy doc)

**Context.** Pavement engineering knowledge is deep, structured, and stable. Putting it in code (Python heuristics) makes it brittle and hard to iterate on. Putting it in a single-file system prompt makes it monolithic and hard to maintain.

**Decision.** Engineering knowledge lives in 13 curated `.md` skill files at `.claude/skills/pavement/`. They are composed at runtime by `app/agent/skill_loader.py` into 2 `cache_control: ephemeral` blocks (~109 KB total). Code is the tool host; skills are the brain.

**Consequences.**
- Cache write costs ~$0.42 once per session; reads are ~$0.005/turn. Negligible per-turn cost after first cache write.
- Adding a skill is a markdown edit, not a code change. Zero retraining.
- Tier rubric, treatment signatures, deterioration progression, etc. each live in their own file with citations.
- The skill is the product. If we'd have built this in 2024, this would have been a 5,000-line system prompt; instead it's modular.

**Status.** Load-bearing. Don't roll back.

---

## D-002 — Tier rubric is 3 tiers, not 5

**Context.** Initial design used 5 tiers (Good / Satisfactory / Fair / Poor / Failed) calibrated to ASTM D6433 PCI bands. In practice, the model collapsed to "Fair" for most marginal cases, including pavement that should have been Poor — a calibrated false-negative.

**Decision (2026-04-26).** Collapse to **Good / Fair / Poor / unknown**. Add a non-negotiable "no Fair-hedge" rule with explicit auto-Poor disqualifiers (any pothole, alligator pattern, exposed base, deep rutting, spalled wide cracks, multi-generation patches with widespread failure).

**Consequences.**
- Fewer borderline calls. Each tier is broader and the agent commits more confidently.
- Backwards-compat shim in the grade dispatcher: `Satisfactory→Good`, `Failed→Poor` for any skill-cache stragglers.
- Loses fine-grained "Excellent vs Good" distinction — but at street-level Mapillary resolution that distinction was already unreliable.

**Alternatives considered.** Keep 5 tiers + add stricter under-call rules. Rejected — the agent's Sat-bias was structural, not a prompt gap.

**Status.** Load-bearing. Don't re-expand.

---

## D-003 — Deterministic pre-grade discipline gate (not just prompt rules)

**Context.** Skill-prompt rules ("non-negotiable", "MUST", etc.) produced *partial* discipline — agent self-disciplined at WP0, drifted by WP4 (10/6/5 turn pattern). Three rounds of prompt tightening only moved the needle slightly.

**Decision (2026-04-26).** When `grade()` is called, run `_check_temporal_discipline` against `WalkerState.visit_log`. Three rules, all REJECT the grade with a structured recovery plan if violated:

- **Rule A** — multi-year coverage available but only one year visited
- **Rule B** — temporal claim made but yaws don't overlap across years (binned to 30°)
- **Rule C** — declared a year unusable but only tried ≤1 candidate

2-strike escape per waypoint to prevent infinite loops on genuinely impossible cases.

**Consequences.**
- Reliable per-epoch cross-witness. Agent visits ≥2 panos/year, peeks first on older years, matches yaws across epochs.
- `WalkerState` carries `visit_log`, `discipline_gate_strikes`, `thumb_sha_cache` — slightly more state to maintain.
- The agent occasionally loops through 2 strikes before giving up. That's working as designed.
- If we ever add new tools that affect the visit_log, we need to record them in the dispatcher (see `_record_and_pack_*` helpers).

**Alternatives considered.** Stricter prompt language only. Rejected — three rounds proved insufficient.

**Status.** Load-bearing. Don't disable. If you need to relax it, ship a flag.

---

## D-004 — find_candidates is year-stratified, not recency-sorted

**Context.** Original `_find_candidates_impl` sorted candidates by `captured_at` DESC and took top-10. In dense corridors with thousands of recent panos, this filled all 10 slots with the most-recent year — hiding multi-year coverage entirely. The agent thought there was no temporal arc when there was.

**Decision (2026-04-26).** Stratify by year. Default `min_per_year=6, limit=30`. Take up to N closest from each distinct year, then fill remaining slots by recency. Sort the final list by year-DESC then distance-ASC. Surface the **total-available-per-year** in the response so the agent knows it can ask for more via `year_filter=N`.

**Consequences.**
- Multi-year coverage now always reaches the agent.
- `year_filter=2016` lets the agent fetch the next 30 closest 2016 panos when the first batch was bad — solves the "give up after 2-3 panos" failure mode.
- Slightly more complex code path; well-tested across chestnut + sunset runs.

**Alternatives considered.** Just bump `limit` to 100. Rejected — agent would still be biased toward recent years if they dominate by count.

**Status.** Load-bearing.

---

## D-005 — SHA-256 collision detector for cross-year duplicates

**Context.** Discovered while verifying agent's "older epoch unusable" claim. Mapillary contributors can upload byte-identical panos under multiple `image_id`s with different `captured_at` timestamps, falsely creating a "multi-year coverage" appearance. Verified by direct API probe: chestnut "2016" and "2025" panos returned SHA-256 identical thumbnails, identical compass/coords/altitude to 11 decimals, same camera (GoPro Max — released 2019, so a 2016 capture is impossible).

**Decision (2026-04-26).** When `find_candidates` returns multi-year panos, fetch `thumb_1024` for each (concurrency 32), SHA-256 hash, group by hash. Keep the newest per group; suppress the rest. Surface a "Data inconsistency" note to the agent listing each suppressed `image_id` and what it duplicated.

**Consequences.**
- The agent gets honest temporal coverage. When a corridor's "multi-year" data is fake, the dedup collapses it back to single-year and warns the agent explicitly.
- Per-find_candidates call: ~30 thumb downloads. Cached per `image_id`, so re-calls are free.
- LANGUAGE: strictly neutral. No accusatory phrasing anywhere ("fraud", "falsified", vendor blame). Just "same image listed under different dates."

**Alternatives considered.** Metadata-only fingerprint (compass + coords). Rejected — false positives possible if a contributor walks the same path twice. SHA-256 of the actual image bytes is definitive.

**Status.** Load-bearing.

---

## D-006 — Lazy enrichment (skip bulk Graph API in prefetch)

**Context.** Original `prefetch_corridor_candidates` hit Mapillary Graph API for ALL 5,000 corridor panos at concurrency 12, just to enrich metadata (compass_angle, make, model, camera_type, sequence). Total: ~49 seconds on Chestnut. The walker only ever consumed ~30 candidates, so 99% of that work was wasted.

**Decision (2026-04-26).** Prefetch uses tile-level metadata only (image_id, lat, lon, captured_at, year, is_pano). Inside `_find_candidates_impl`, after stratification + dedup, lazy-enrich only the ~30 returned candidates via `_enrich_candidates_and_shas` at concurrency 32 — combining metadata fetch + thumb SHA in one Graph API call per image. Cached per `image_id`.

**Consequences.**
- Prefetch time: 49s → **6s** on Chestnut (8× faster).
- Per-waypoint enrichment: ~1s amortised across the run.
- The agent sees the same metadata it always saw — just on the candidates it actually considers.
- Tradeoff: if the agent calls find_candidates many times with no overlap, enrichment cost adds up. In practice, panos overlap heavily across waypoints in a corridor, so the cache is hot.

**Status.** Load-bearing.

---

## D-007 — `look()` tool descriptions are system instructions

**Context.** A latent "RECOMMENDED PATTERN per waypoint: 1-2 looks max" string in the `look()` tool description was overriding all skill-level rules about deep investigation. Reason: tool schemas are part of the cache_control block, so the description is read on EVERY turn — effectively a system instruction.

**Decision (2026-04-26).** Treat tool descriptions as system instructions. Audit them when behavior drifts. Removed the "1-2 looks max" phrase; replaced with explicit "10-20 look/zoom calls per waypoint is normal" + "YAW MATCHING is non-negotiable when ≥2 years exist."

**Consequences.**
- When the agent shows lazy patterns, FIRST audit tool descriptions, THEN the skills.
- Tool description text contributes to the cache write cost — keep them tight but don't be afraid to use them for behavior shaping.

**Status.** Operational principle. Re-audit any time agent behavior drifts.

---

## D-008 — Vocabulary normalisation (waypoint→survey point, turn→step)

**Context.** Domain-specific UX feedback: "waypoint" felt like jargon. "Turn" was confusing for civil engineers (means "turn a corner" in their context, not "tool-use turn" in LLM context).

**Decision (2026-04-26).** All user-facing strings use `survey point` instead of `waypoint`, and `step` instead of `turn`. Internal data keys (`wp_idx`, `waypoint_idx`, etc.) untouched for backend compatibility. Step counter is dev-only — hidden from the demo UI.

**Consequences.**
- Demo audience (DOT engineers, judges) never sees agent-loop jargon.
- Code stays compatible — searching for `wp_idx` still works.
- New strings should follow the same pattern: descriptive English in UI, technical keys in data.

**Status.** Operational principle.

---

## D-009 — Neutral data-inconsistency language

**Context.** Initial SHA-collision warning used phrases like "Mapillary metadata fraud" and "duplicate-upload artifact." User flagged: "don't trash any other companies. Just say inconsistency."

**Decision (2026-04-26).** All data-inconsistency language is **strictly descriptive, never accusatory**. Scrubbed across:
- `find_candidates` header text Opus sees
- `find_candidates` UI card in `temporal_v2.html`
- `evidence_extraction.md` skill examples (with explicit DO/DON'T rules)

Pattern: "same image appears under different dates" — describe what's observed, not motivation behind it.

**Consequences.**
- The agent learns the language from the cached skills + tool descriptions and propagates it into its `inconsistencies` field automatically.
- Demo never has the optics of "this app accuses Mapillary of fraud."

**Status.** Operational principle.

---

## D-010 — Apple-style layered surface hierarchy in v2 UI

**Context.** v1 UI was monotone white. User: "It's all so bright and not very intuitive."

**Decision (2026-04-26).** Three-tier surface system:

1. **Page chrome** (`#e6e8ec`, cool gray) — body bg
2. **Top bar** (solid white + `0 4px 12px` shadow) — chrome separation
3. **Content panels** — map (white) + sidebar (tinted `#f1f3f7` with left shadow for depth)

Inside the sidebar: white sub-sections (header, filter) for visual section pulse; tinted feed bg with white cards floating on top.

Result: clear navigation cues without noise. The user instinctively knows where chrome ends and content begins.

**Consequences.**
- Light mode only. No dark-mode media queries.
- Cards have stronger shadows so they "lift" off the tinted feed bg.
- Top bar uses solid white (not translucent blur) for stronger chrome separation.

**Status.** Live.

---

## D-011 — Stop button is a file flag, not in-memory state

**Context.** Need a way to halt a running walker without killing uvicorn. Walker is a background asyncio task; could store a "stop" boolean in the task registry, but that doesn't survive uvicorn restarts.

**Decision (2026-04-26).** `POST /api/temporal/runs/{slug}/stop` writes `_stop_requested.flag` in the run dir. Walker's main loop checks for this file at the top of each turn-loop iteration. On detect: writes a `system_note` event, sets `stop_reason="user_stopped"`, breaks out of the loop. Findings already recorded are preserved.

**Consequences.**
- Survives uvicorn restarts (the flag persists on disk).
- Idempotent — clicking Stop multiple times is safe.
- Clean exit — partial state is preserved, not lost.

**Alternatives considered.** In-memory `Task.cancel()`. Rejected — abrupt cancellation would lose mid-flight tool results and leave inconsistent state.

**Status.** Load-bearing.

---

## D-012 — Markdown rendering in chat output (XSS-safe)

**Context.** Agent narration uses `**bold**` markdown for emphasis (e.g., "clear **Poor** tier"). Without rendering, this showed as literal asterisks in the UI — distracting.

**Decision (2026-04-26).** Small XSS-safe markdown helper (`mdToHtml` in `temporal_v2.html`): escape HTML first, THEN re-introduce only safe inline HTML for `**bold**`, `*italic*`, `` `code` ``, paragraph breaks (`\n\n`), and line breaks (`\n`).

**Consequences.**
- Bold/italic/code render correctly. Paragraph spacing is preserved.
- Safe — no raw HTML ever reaches the DOM, only the specific tokens we add.
- Applied to: agent assistant body text, grade rationale.

**Status.** Live. If we add more chat-style fields, render them through `elMd` not `el(..., {text: ...})`.

---

## D-013 — Background task with status-file polling for prefetch

**Context.** UI was stranded for 30-90 seconds during corridor prefetch. User couldn't tell if it was hung. Also: holding a synchronous HTTP request open for that long is fragile.

**Decision (2026-04-26).** `POST /api/temporal/start` writes minimal artifacts (config.json, street.geojson, waypoints.geojson, status file) IMMEDIATELY, spawns the prefetch+walker as a background asyncio task, and returns the slug. UI redirects to `/temporal/{slug}` and polls `/api/temporal/runs/{slug}/status` (which reads `_temporal_status.json`) until phase moves from `prefetching` → `running` → `completed`.

**Consequences.**
- UI shows immediate feedback.
- Status file is the source of truth for progress; survives uvicorn restarts.
- The background task references `WalkerState` and other in-memory objects — those don't survive uvicorn restart, so a mid-prefetch crash means the run is dead. That's acceptable for MVP.

**Status.** Load-bearing.

---

## D-014 — Minimap goes ABOVE the viewport, not as inset

**Context.** Minimap inset (bottom-right of viewport) sometimes covered the very distress the model was trying to investigate.

**Decision (2026-04-26).** `_composite_with_minimap` rewritten. Minimap is now a 96 px tall strip ABOVE the viewport, at full panorama width. Viewport image below is pristine.

**Consequences.**
- Pavement images are unobstructed.
- Total composite height is `viewport_height + 96 px` (was `viewport_height` with overlay). Acceptable for chat-card display.
- Minimap rectangle drawing logic re-scaled to the new strip dimensions.

**Status.** Live.

---

## How to add to this log

When you make a load-bearing decision:

1. Pick the next D-NNN number
2. Write a section using the same template (Context / Decision / Consequences / Status / Alternatives considered)
3. Reference it from CLAUDE.md or the relevant skill file when the decision affects future code reviews

Don't bury decisions in commit messages or PROGRESS.md alone — those are chronological. This file is *intent-organised*, which is what future-you needs when you're touching the code two months later.
