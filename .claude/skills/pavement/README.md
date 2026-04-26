# Pavement Skills Library

This directory holds the engineering knowledge that the [street-walker agent](../../../app/agent/street_walker.py) reasons over to grade pavement condition. Each `.md` file is a self-contained skill the agent loads as part of its system context.

The skill set is built from authoritative pavement-engineering references (FHWA Distress Identification Manual, ASTM D6433, PASER, Pavement Interactive, FHWA preservation guidance, Caltrans MTAG). Citations live in [`research/source_index.md`](../../../research/source_index.md).

## Loaded by `app/agent/skill_loader.py`

The loader composes skills into the agent's system message, grouping them into two `cache_control` blocks for prompt-cache hygiene:

**Block 1 — core engineering knowledge** (cached, rarely changes):
- `tier_rubric.md` — the 5-tier scale + per-tier attribute matrix
- `distress_taxonomy.md` — 10 distress types with FHWA-cited definitions
- `visual_confusers.md` — false-positive traps (paint vs cracks, etc.)
- `deterioration_progression.md` — how distresses evolve over time
- `treatment_signatures.md` — what recent treatments look like
- `climate_failure_modes.md` — what's plausible at a given location
- `repair_priority_logic.md` — risk × consequence framework

**Block 2 — operational discipline + geometry** (cached, stable across runs):
- `pano_anatomy.md` — Mapillary 360° structure
- `viewport_geometry.md` — yaw/pitch/hfov + minimap interpretation
- `scan_plan.md` — per-waypoint workflow
- `zoom_investigation.md` — when and how to zoom
- `grade_discipline.md` — under-report rule + confidence calibration

## Authoring conventions

Every skill file:
1. Starts with YAML frontmatter (`name`, `scope`, `references`).
2. **Leads with the practical decision rule the agent should APPLY.**
3. Then provides the engineering depth (mechanism, thresholds, examples).
4. Closes with a `## Sources` section citing references by number from `research/source_index.md`.

When iterating on a skill, prefer **adding measurable thresholds and decision rules** over adding prose. The agent uses these files to make calls, not to learn pavement engineering as a textbook.

## Out of scope

- Per-skill A/B testing — defer until we have a calibration loop that can attribute predictions to skill content.
- Hazard skills (sidewalk damage, signage, drainage) — the walker doesn't grade hazards yet.
- Multi-language versions — English only.
