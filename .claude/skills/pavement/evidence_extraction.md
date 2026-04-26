---
name: evidence_extraction
scope: per-waypoint information extraction discipline — flag everything observable so a downstream re-analysis can re-grade cheaply
---

# Evidence extraction — extract everything, flag everything

The grade itself is the headline output. But the REAL value of running an Opus 4.7 walker over a corridor is the rich evidence catalog you can re-analyze later at lower cost. **Every grade should ship with structured fields that capture all observable signals so a downstream re-analysis stage can use them.**

The `grade()` tool accepts these structured fields:

```
distresses_observed   — ALL visible distresses, per epoch
treatments_observed   — ALL treatments visible, per epoch
safety_flags          — anything safety-relevant a dispatcher should see
surroundings_notes    — context (school zone, bus stop, construction, etc.)
inconsistencies       — visible-evidence vs metadata mismatches
evidence_image_ids    — image_ids of all panos used as evidence
```

**Fill them all out.** Empty fields are NOT acceptable except where the field genuinely doesn't apply (e.g., no inconsistencies → empty list).

## What goes in `distresses_observed`

Every observable distress, with the year and rough location within the frame. Format: `"<distress_type> <year> <location_in_frame>"`.

Distress types (use these exact strings — supports re-analysis aggregation):
- `longitudinal_crack`
- `transverse_crack`
- `block_cracking`
- `alligator`
- `pothole`
- `patch_failure`
- `patch_intact`
- `raveling`
- `edge_break`
- `rutting`
- `utility_cut`
- `crack_seal_visible` (intact past intervention)
- `oxidation` (greying surface)
- `spalling` (chunks missing along crack lines)

Examples:
```
"alligator 2025 right wheelpath"
"longitudinal_crack 2016 lane center"
"patch_failure 2025 near manhole — sinking edge"
"oxidation 2016 entire lane"
"crack_seal_visible 2016 transverse cracks — intact"
```

## What goes in `treatments_observed`

Treatments visible IN the imagery (current) or inferred between epochs. Format: `"<treatment> <year_or_range> <evidence>"`.

Treatment types:
- `fresh_overlay`
- `mill_overlay`
- `thin_overlay`
- `slurry_seal`
- `microsurface`
- `chip_seal`
- `cape_seal`
- `crack_seal`
- `fog_seal`
- `patch`
- `reconstruction`
- `none_visible`

Examples:
```
"fresh_overlay 2025 — uniform jet-black with crisp curb edge"
"crack_seal 2016 — visible black bead lines on transverse cracks"
"mill_overlay 2018-2025 inferred — tonal reset from 2016 oxidized to 2025 dark, no chip texture"
"none_visible 2016 — natural aging only"
```

## What goes in `safety_flags`

Anything a public-works dispatcher should know about beyond the tier itself. **This is dispatcher-actionable information** — the agent's job is to surface it, not solve it.

Examples:
```
"pothole at crosswalk approach — pedestrian risk"
"spalled patch in bike lane — bike risk"
"faded stop bar at high-traffic intersection"
"damaged/loose manhole cover"
"sidewalk crack at curb ramp — ADA concern"
"missing or knocked-over street sign visible at corner"
"drainage grate clogged with debris"
"wheelpath rutting > 15 mm — hydroplaning risk in rain"
"exposed rebar / deep pothole"
```

If nothing safety-relevant: `[]` (empty list — explicit, not omitted).

## What goes in `surroundings_notes`

Context that shapes which intervention is right OR informs prioritization — observable in the pano frame.

Examples:
```
"school zone — yellow school sign visible"
"bus stop on east side — high pedestrian traffic"
"active construction zone — barriers and cones visible"
"truck route based on lane width"
"signalized intersection — protected left-turn lane visible"
"residential block — driveways every ~10 m"
"commercial frontage — parallel parking on both sides"
"hilly grade — visible incline"
```

## What goes in `inconsistencies`

Observable mismatches between the imagery and the listed metadata. Useful for downstream filtering of bad ground truth + flagging stale or duplicate data.

**Use neutral, descriptive language. Do NOT speculate about intent.**
- ✅ "same image appears under two different captured_at dates"
- ✅ "GPS appears to be at intersection but pano shows mid-block"
- ✅ "compass_angle=0 but forward view shows perpendicular street"
- ❌ "metadata fraud", "falsified data", "bad-faith upload" — never use this language. Describe what you see, not what motivated it.
- ❌ Naming or blaming organizations / contributors / platforms.

Examples:
```
"same image bytes appear under both 2016 and 2025 dates — only one capture is real"
"GPS appears to be at intersection but pano shows mid-block"
"vehicle and lane patterns suggest the listed date may not match the actual capture year"
"rig metadata said car but bottom band shows bicycle handlebars"
"compass_angle=0 but forward view shows perpendicular street"
"two 2025 panos at this waypoint show different surface conditions — possible re-paving in interim"
```

If no inconsistencies: `[]`.

## What goes in `evidence_image_ids`

The image_ids of every pano you used as a basis for the grade — primary investigation, cross-witness, older-epoch comparison. The UI persists the rendered viewports for these IDs so a demo can show them.

Format: list of strings:
```
["1234567890123456", "987654321098765", "555444333222111"]
```

This is what the demo will pull up to show the dispatcher "here's the evidence the agent based the grade on."

## Why all of this matters

A `grade(tier='Poor', confidence=0.85, rationale='alligator at WP3 right wheelpath')` is fine. A `grade(...)` that ALSO includes 12 specific distress observations across 2 epochs, 2 treatments inferred, 3 safety flags, surroundings context, and 0 inconsistencies — THAT is dispatcher-grade output. The same pavement, same agent, same cost. The difference is investigative discipline.

The downstream re-analysis stage will take ALL waypoints' rich extras and produce a higher-level corridor narrative for ~10× cheaper because it's pure text-in / text-out (no imagery). So the value compounds: extract everything once, re-analyze cheaply.

**The non-negotiable rule:** every grade must include non-trivial entries in `distresses_observed`, `treatments_observed`, and `evidence_image_ids`. The other lists may be empty if not applicable.

## Sources

Empirical — derived from observed under-extraction patterns in walker runs v3-v4 where rationales were structured but distinct distress / safety / context signals went into free-text only and were lost to downstream re-analysis.

Full bibliography: [`../../../research/source_index.md`](../../../research/source_index.md).
