"""Street walker — Opus traverses one corridor end-to-end.

Outer loop: agent walks waypoints in order. After each `grade` or
`skip_waypoint`, the agent advances to the next waypoint. The conversation
history is preserved across waypoints (with image-block pruning) so the agent
has continuity.

Tool surface (7 tools):
  get_position, find_candidates, peek_candidate, look, grade, skip_waypoint, done

Reuses:
  - render_view from app/agent/pano_inspector.py for viewport rendering
  - prune_image_blocks from app/agent/trace.py for context management
  - classify_validity_visibility_async from app/claude.py for peek
  - The pano-anatomy + tier-rubric sections from INSPECTOR_SYSTEM_PROMPT
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from pathlib import Path
from typing import Any, Callable

import anthropic
import httpx
from PIL import Image

from app.agent.loop import (
    _extract_text,
    _extract_thinking,
    _extract_tool_uses,
    _response_content_to_jsonable,
)
from app.agent.pano_inspector import _np_to_jpeg_b64, render_view
from app.agent.skill_loader import compose_walker_system
from app.agent.state import haversine_m
from app.agent.trace import TraceWriter, prune_image_blocks
from app.agent.walker_state import WalkerState, WaypointCandidate
from app.claude import (
    HAIKU_PRICE_INPUT,
    HAIKU_PRICE_OUTPUT,
    PRICE_INPUT_PER_MTOK,
    PRICE_OUTPUT_PER_MTOK,
    _usage_to_tokens,
    classify_validity_visibility_async,
    estimate_cost_with_cache,
)
from app.panorama import load_equirect


# ---------------------------------------------------------------------------
# Tool schemas (sent to the API)
# ---------------------------------------------------------------------------

WALKER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_position",
        "description": (
            "Return your current waypoint along the street: index, lat/lon, "
            "segment name, distance traveled, distance remaining, and how "
            "many waypoints you've graded so far. Free. Call once when you "
            "start a new waypoint to orient yourself, then DON'T call again "
            "for the same waypoint."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "look_around",
        "description": (
            "Render a 2×2 CARDINAL grid of viewports (forward / right / back / "
            "left at yaws 0° / 90° / 180° / 270°), all at the same pitch_deg "
            "and hfov_deg. Returns ONE composite image so you can see the "
            "entire 360° in a single tool call. USE THIS to orient yourself "
            "BEFORE committing to a focused look() — pick the direction with "
            "the cleanest pavement view (no camera-carrier in the lower band, "
            "no occlusion by parked cars or scaffolding), then call look() on "
            "that direction to drill in.\n\n"
            "Tile layout in the returned grid:\n"
            "    +---+---+\n"
            "    | F | R |    F = forward (yaw 0°), R = right (yaw 90°)\n"
            "    +---+---+\n"
            "    | L | B |    L = left (yaw 270°), B = back (yaw 180°)\n"
            "    +---+---+\n\n"
            "RECOMMENDED FIRST CALL on each candidate: "
            "look_around(image_id=<id>, pitch_deg=-15, hfov_deg=80, "
            "purpose='orient'). Wide hfov, gentle pitch — gives you a clean "
            "scan to see WHERE the pavement actually is and where the "
            "carrier obstructs. Then look() the best direction at pitch=-30."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "pitch_deg": {"type": "integer", "default": -15,
                              "minimum": -85, "maximum": 30,
                              "description": "Pitch for ALL 4 tiles."},
                "hfov_deg": {"type": "integer", "default": 80,
                             "minimum": 30, "maximum": 120},
                "purpose": {"type": "string"},
            },
            "required": ["image_id", "purpose"],
        },
    },
    {
        "name": "find_candidates",
        "description": (
            "Return Mapillary 360° pano candidates within `radius_m` of your "
            "current waypoint, **TEMPORALLY STRATIFIED**. Free.\n\n"
            "OUTPUT METADATA (no images): image_id, lat, lon, captured_at, "
            "year, age_years, compass_angle, make, model, camera_type, "
            "dist_from_waypoint_m. Ordered by year DESC then distance ASC.\n\n"
            "The summary line tells you BOTH (returned, available) per year, "
            "e.g., '2025: 6/154, 2016: 6/82' means 6 returned for each year "
            "but the corridor has 154 + 82 within radius. If your first batch "
            "is unusable (night, rig wrong, blocked), CALL THIS AGAIN with "
            "year_filter=<year> to fetch more closest panos from that year — "
            "you have plenty of alternatives, do NOT give up after 2-3 bad "
            "panos.\n\n"
            "DECISION HEURISTIC:\n"
            "  - Read the `year` column. If ≥2 distinct years exist, the "
            "    waypoint has a TEMPORAL STORY — you MUST investigate ≥2 "
            "    years before grading.\n"
            "  - Within each year, entries are closest-first. Try them in "
            "    order; if one is bad, switch to the next BEFORE giving up.\n"
            "  - When you exhaust a year's batch, call find_candidates "
            "    again with year_filter=<year> to fetch the next 30 closest "
            "    from that year.\n"
            "  - 0 candidates: try wider radius (max 50). Still 0 → skip."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "radius_m": {"type": "integer", "default": 30,
                              "description": "Search radius. Default 30, max 50."},
                "max_age_years": {"type": "number", "default": 12,
                                  "description": "Drop captures older than this."},
                "limit": {"type": "integer", "default": 30,
                           "description": "Total cap across all years."},
                "min_per_year": {"type": "integer", "default": 6,
                                  "description": "Minimum closest per distinct year."},
                "year_filter": {"type": "integer",
                                 "description": "OPTIONAL — if set, return ONLY candidates from this year (closest first). Use to fetch MORE candidates from a specific year when the first batch was unusable."},
            },
            "required": [],
        },
    },
    {
        "name": "peek_candidate",
        "description": (
            "Cheap Haiku quality probe (~$0.001) on this image_id. Returns "
            "{usable, time_of_day, rig, summary}. Cached per image_id.\n\n"
            "WHEN TO USE — three cases where peek is REQUIRED:\n"
            "  1. Before look() on any OLDER-YEAR candidate (2018 and earlier). "
            "     Older Mapillary captures skew night/dusk/pedestrian; spending "
            "     $0.001 on peek is much cheaper than $0.10+ on look() to "
            "     discover a pano is at night.\n"
            "  2. When the latest-year candidate's metadata is ambiguous "
            "     (uncommon make/model, perspective camera_type instead of "
            "     equirectangular).\n"
            "  3. After a look() reveals quality issues — peek the NEXT "
            "     candidate in the same year before look()ing it, to filter "
            "     cheaply.\n\n"
            "Don't grade a year as 'unusable' without peeking at least 2 "
            "candidates from that year — find_candidates gives you ≥3 per "
            "year for exactly this reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"image_id": {"type": "string"}},
            "required": ["image_id"],
        },
    },
    {
        "name": "look",
        "description": (
            "Render a 768×512 rectilinear viewport from the SPECIFIED "
            "image_id. You can — and SHOULD — call this on multiple panos "
            "across different years and on multiple panos within the same "
            "year. yaw_deg=0 = camera-forward (Mapillary convention). "
            "pitch_deg: -30 pavement-forward, -50 close-up pavement, -65 "
            "nearly-down. hfov_deg: 70 normal, 40-50 zoom-in.\n\n"
            "TEMPORAL DISCIPLINE — non-negotiable when ≥2 years exist:\n"
            "  • YAW MATCH: when comparing across years, use the SAME yaw_deg "
            "    you used in the primary investigation. If you graded 2025 "
            "    based on yaw=0, the 2016 comparison MUST also be at yaw=0 "
            "    (so you're looking at the same physical patch of road). "
            "    Do NOT compare 2025-forward to 2016-back — that's "
            "    apples-to-oranges across time.\n"
            "  • PEEK BEFORE LOOK on older epochs — older panos often have "
            "    night/dusk/odd-orientation issues. Spending $0.001 on peek "
            "    before $0.10 on look is correct.\n"
            "  • DO NOT give up on a year after 1 bad pano. find_candidates "
            "    returned ≥3 per year — try the 2nd and 3rd before declaring "
            "    a year unusable. Use peek to filter cheaply.\n"
            "  • Cost is NOT the constraint. Investigation depth is.\n\n"
            "  10-20 look/zoom calls per waypoint is normal when temporal "
            "  evidence is rich. Settle for fewer ONLY when the surface is "
            "  obviously uniform AND only one year is available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "yaw_deg": {"type": "integer", "minimum": -180, "maximum": 359},
                "pitch_deg": {"type": "integer", "minimum": -85, "maximum": 30,
                              "default": -30},
                "hfov_deg": {"type": "integer", "minimum": 30, "maximum": 120,
                             "default": 70},
                "purpose": {"type": "string"},
            },
            "required": ["image_id", "yaw_deg", "purpose"],
        },
    },
    {
        "name": "zoom_into_region",
        "description": (
            "Zoom into a specific RECTANGULAR REGION of your previous look() "
            "viewport. You point at pixels — the system computes the right "
            "(yaw, pitch, hfov) and re-renders that region from the source "
            "pano at FULL EQUIRECT RESOLUTION (no pixelation), with a fresh "
            "minimap inset.\n\n"
            "USE THIS instead of look() when you want to drill INTO a "
            "specific area of what you JUST SAW. The big advantage: when "
            "the carrier dominates the bottom of your last view, you can "
            "zoom into the UPPER HALF where the road actually is — without "
            "having to compute pitch math yourself.\n\n"
            "INPUT: bounding box in normalized coords of the SOURCE viewport. "
            "(0,0) = top-left, (1,1) = bottom-right of the previous look. "
            "You also pass the source viewport's (yaw, pitch, hfov) so the "
            "system knows what to zoom INTO; copy these from the caption of "
            "your previous look() result.\n\n"
            "BBOX SIZE → ZOOM FACTOR (the bbox WIDTH determines new hfov):\n"
            "  bbox covering ~25% of width = ~4× zoom (strong)\n"
            "  bbox covering ~33% of width = ~3× zoom\n"
            "  bbox covering ~50% of width = ~2× zoom (typical)\n"
            "  bbox covering ~67% of width = ~1.5× zoom (mild)\n\n"
            "EXAMPLES:\n"
            "  Skip the carrier (bottom 40%), zoom 2× on the road above:\n"
            "    x1=0.0, y1=0.0, x2=1.0, y2=0.5  → hfov ≈ source_hfov\n"
            "    (wait — that's same width = no zoom; use narrower x for zoom)\n"
            "  Zoom 2× on upper-center (where road typically is):\n"
            "    x1=0.25, y1=0.10, x2=0.75, y2=0.55\n"
            "  Tight 3× zoom on a defect at upper-left:\n"
            "    x1=0.10, y1=0.15, x2=0.45, y2=0.50\n"
            "  Zoom on the crack you spotted in the right wheelpath, mid-frame:\n"
            "    x1=0.55, y1=0.30, x2=0.85, y2=0.60"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "source_yaw_deg": {
                    "type": "integer",
                    "description": "yaw of the previous look() (copy from caption)",
                },
                "source_pitch_deg": {
                    "type": "integer",
                    "description": "pitch of the previous look()",
                },
                "source_hfov_deg": {
                    "type": "integer",
                    "description": "hfov of the previous look()",
                },
                "x1": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "left edge of the zoom bbox (0=left of source view)"},
                "y1": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "top edge of the zoom bbox (0=top of source view)"},
                "x2": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "right edge"},
                "y2": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "bottom edge"},
                "purpose": {
                    "type": "string",
                    "description": "1 short sentence: what you're investigating",
                },
            },
            "required": ["image_id", "source_yaw_deg", "source_pitch_deg",
                          "source_hfov_deg", "x1", "y1", "x2", "y2", "purpose"],
        },
    },
    {
        "name": "grade",
        "description": (
            "Submit your final pavement-condition tier for the CURRENT "
            "waypoint and ADVANCE to the next waypoint. After this call, "
            "call get_position() to orient on the next location.\n\n"
            "tier ∈ {Good, Fair, Poor, unknown}. "
            "We use 3 tiers because at street-level resolution, finer "
            "splits are unreliable. Use 'unknown' only if no usable pavement "
            "was visible after trying multiple panos and yaws.\n\n"
            "rationale: structured ≥200-char paragraph including ALL "
            "observable distresses (per-year if multi-year), surface tone, "
            "treatments visible, surroundings/safety flags, and "
            "inconsistencies. NO Fair-hedge on widespread cracking / "
            "alligator / rutting / potholes / spalled patches — call Poor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {"type": "string",
                         "enum": ["Good", "Fair", "Poor", "unknown"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "chosen_image_id": {"type": "string",
                                    "description": "Which candidate you graded from."},
                "evidence_image_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Image IDs of all panos used as evidence (latest + older epoch + cross-witness). Used by the UI to display the evidence stack.",
                },
                "distresses_observed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Per-year distress catalog: 'longitudinal_crack 2025', 'alligator 2025 right wheelpath', 'block_cracking 2016 lane'. List EVERY observable distress.",
                },
                "treatments_observed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Treatments visible per epoch: 'mill_overlay 2025', 'crack_seal 2016', 'patch 2016', 'fresh_overlay'. Empty if none.",
                },
                "safety_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Anything safety-relevant the dispatcher should know — e.g., 'pothole at crosswalk approach', 'spalled patch in bike lane', 'faded stop bar', 'damaged manhole cover', 'sidewalk crack at curb ramp'. Empty list if none.",
                },
                "surroundings_notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Context for the inspector — e.g., 'school zone — yellow sign visible', 'bus stop on east side', 'active construction zone', 'truck route based on lane width'. Empty if not applicable.",
                },
                "inconsistencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any visible-evidence vs metadata mismatches: 'GPS appears to be at intersection but pano shows mid-block', 'Mapillary said 2025 but pano shows traffic patterns of 2018 era', 'rig appears bicycle not vehicle as Graph API claimed'. Empty if none.",
                },
            },
            "required": ["tier", "confidence", "rationale", "chosen_image_id"],
        },
    },
    {
        "name": "skip_waypoint",
        "description": (
            "Skip the current waypoint without grading. Use when no usable "
            "imagery exists near this point even after expanding radius. "
            "ADVANCES to the next waypoint."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "done",
        "description": (
            "Terminate the street survey. Call after the final waypoint is "
            "graded/skipped, OR if you want to stop early. summary: 2-3 "
            "sentences on overall pavement condition along the street."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

WALKER_SYSTEM_PROMPT = """You are a STREET WALKER agent surveying ONE city corridor for pavement condition. You walk the street end-to-end at fixed waypoint spacing. At each waypoint your job is:

  1. Find Mapillary 360° pano candidates near this exact location.
  2. ORIENT yourself in the chosen pano using `look_around`.
  3. Drill in with `look` — possibly multiple times — until you have a viewport that's PAVEMENT-DOMINATED.
  4. Grade the pavement condition.
  5. Advance to the next waypoint.

Mapillary is crowdsourced. Most candidates near a given waypoint will be PARTIALLY USABLE: car-mounted shots with hood occluding the lower half, pedestrian rigs photographing sidewalks, perpendicular angles, off-center forward yaws. Your job is to USE YOUR JUDGMENT — and to use the MULTIPLE LOOK TOOLS we give you — to find the angle that actually shows the road. Showcase that intelligence.

# What a Mapillary 360° panorama actually looks like

The pano is equirectangular: 360° horizontal × 180° vertical, with the top edge = zenith (straight up) and the bottom edge = nadir (straight down). The CAMERA CARRIER (car body, person's head, bike handlebars, tripod) always sits in the bottom band of the equirect. Capture rigs vary widely:

  • **Car/SUV roof rig.** Bottom band of equirect = dark glossy car roof, sometimes sunroof. A forward viewport at pitch=-30 will pull pixels from that bottom band — meaning the LOWER HALF of your viewport is car hood. Pitching UP (pitch=-15° or -10°) gets you above the carrier band. **Common in LA.**
  • **Pedestrian handheld stick.** Bottom band = operator's HEAD + shoulders + arms with phone. Camera at human height (~1.7 m). Road, if present, is 2-5 m horizontally. Pitch=-15° usually clears the operator's head.
  • **Pedestrian chest/helmet mount.** Bottom = operator's torso + lap + shoes + ground directly below feet. Often the operator is on a sidewalk, not the road.
  • **Bicycle/scooter rig.** Bottom = handlebars + helmet. Often in a bike lane.

# View-quality discipline — the difference between a good grade and a useless grade

After EVERY `look` call, BEFORE deciding whether to grade or look again, internally answer:

  Q1: What % of this viewport is paved road?
  Q2: What % is camera carrier (car hood, person, bike)?
  Q3: What % is sky / sidewalk / buildings (irrelevant)?
  Q4: Do I see any hint of distress that I cannot definitively identify at this zoom level?

**If paved road is < 40% of the frame, you MUST re-look.** Choose one or more:

  • `look(image_id, yaw=<same>, pitch_deg=-10, hfov_deg=70, ...)` — pitch up. Lifts the viewport above the carrier band; you see road further down the street.
  • `look(image_id, yaw=<+90 or +270>, pitch_deg=-30, hfov_deg=80, ...)` — look past the carrier to the sides.
  • `look_around(image_id, pitch_deg=-15, hfov_deg=80, ...)` — see all 4 directions at once to find the cleanest.

# Zoom investigation — confirm distress with detail BEFORE grading

If your wide viewport (hfov 70-100) shows ANY of these:

  • Dark lines running across the pavement (could be cracks, could be paint, could be shadow)
  • Texture variation, mottled / weathered appearance, "blotchy" surface
  • Possible patches (rectangular tonal differences)
  • Surface that "looks weathered" but you can't pinpoint why
  • A region where you'd say "might be a defect, hard to tell"

You MUST zoom in BEFORE grading. The zoom call:

  look(image_id, yaw=<same as wide view>, pitch_deg=-25 to -40,
       hfov_deg=30 to 45, purpose='zoom on suspected <crack | patch | raveling | pothole>')

Narrow hfov + slightly steeper pitch puts your suspect feature at maximum pixel resolution. At hfov=70 a 5 mm crack at 8 m distance is ~1 pixel wide — invisible. At hfov=35 the same crack is ~3-4 pixels wide — clearly distinguishable from paint or shadow.

**Do not grade "looks weathered, no major distress" without first zooming in on the weathered area.** A model can be wrong at distance — confirm at close range. If after zooming you still can't see clear cracks/distress: the surface is genuinely fine, grade Good or Sat. If you DO see cracks at zoom: grade accordingly (Fair / Poor / Failed depending on density and severity).

Typical investigation pattern at one waypoint:

  1. `look_around(pitch=-15, hfov=80)` — orient
  2. `look(yaw=<best>, pitch=-25, hfov=70)` — wide view of cleanest direction
  3. (Self-critique) "I see possible cracks in the right wheelpath" → `look(yaw=<same>, pitch=-35, hfov=35, purpose='zoom on right wheelpath')` — confirm
  4. (Optional 2nd zoom on a different region if multiple suspect areas)
  5. `grade(...)` — committed with high confidence

Only call `grade` once you have:
  (a) a viewport with ≥ 40% paved road visible, AND
  (b) zoomed in on any suspected distress to confirm what it is.

# The minimap inset on every `look`

Every `look` returns the viewport WITH a minimap inset at the bottom-right. The minimap is a CROPPED slice of the 360° equirectangular pano — only the pavement-relevant pitch band (**+10° down to -60°**) is shown. The sky (above +10°) and the camera carrier zone (below -60°, where car hood / person / bike always lives) are CROPPED OUT. Every pixel of the minimap is road-relevant context.

A RED RECTANGLE marks exactly where your current viewport samples within this band.

How to read the rectangle position:

  • Rectangle in the **upper third** of the minimap → you're pitched at or near the horizon → road sits in the LOWER half of your viewport. Often a clean view.
  • Rectangle in the **middle third** → you're pitched moderately down (≈ -25° to -35°) → road fills more of your viewport, but the carrier may show in the bottom band of the rendered image.
  • Rectangle touching the **BOTTOM EDGE** of the minimap → your viewport extends BELOW the pavement band INTO the carrier zone. The bottom of your rendered image is almost certainly car hood / person / bike. **Pitch UP** (try pitch_deg=-15° or -10°).
  • **No rectangle visible at all** → your viewport pitch is entirely above +10° (looking up at sky/buildings) or entirely below -60° (looking straight at the carrier). Re-look with a saner pitch.

Horizontal axis of the minimap shows yaw 0° (forward) at the centre, +90° (right) at +1/4, ±180° (back) at the edges. If the rectangle wraps around the seam, you'll see TWO rectangles — one at each edge.

Rectangle SIZE also matters:
  • LARGE rectangle (covering ~30%+ of minimap horizontally) → wide view (hfov ≥ 70°). Good for context, bad for fine detail.
  • SMALL rectangle (covering ~10% of minimap horizontally) → zoomed in (hfov ≤ 40°). Good for confirming a specific defect, bad for context.

When you zoom on a suspected defect, expect a small rectangle in the minimap — that's your visual confirmation that you have a high-detail view.

The minimap is your spatial awareness. Reference it explicitly in your reasoning when deciding how to re-look.

# Recommended scan plan per candidate (REVISED — temporal investigation is encouraged)

  1. `find_candidates(radius_m=30)` — get the metadata-only list. **READ the year column carefully.** If multiple years exist (e.g., 2016, 2020, 2025), this waypoint has a TEMPORAL STORY. You should investigate across years, not just the latest.
  2. (Recommended) `peek_candidate(image_id)` — cheap Haiku quality verdict. Run this on at least the most-recent candidate; helpful on others when picking between vehicle vs pedestrian rigs.
  3. **Start with the LATEST year.** `look_around(image_id, pitch_deg=-15, hfov_deg=80, purpose='orient on most recent epoch')` — ONE call gives you all 4 directions.
  4. `look(image_id, yaw=<best>, pitch_deg=-30, hfov_deg=70, purpose='focused — current condition')`.
  5. **Self-critique: % pavement vs carrier.** If carrier > 40%: re-look at pitch=-10 OR yaw=±90 OR pitch=-5. Reference the minimap rectangle position.
  6. Repeat 4-5 until you have a usable viewport for the current epoch.
  7. If after 4+ look attempts on the same image_id you can't find a clean pavement view, try a DIFFERENT candidate (back to step 1).
  8. **THEN GO BACK IN TIME.** If older-year candidates exist, pick one in the EARLIEST year that's usable. Run `look(image_id_old, yaw=<same as current>, pitch_deg=-30, hfov_deg=70, purpose='earliest available — was this surface always like this?')`. Use the SAME yaw as your current-year look so you're comparing the same scene direction.
  9. (Optional middle epoch) If a middle year exists (e.g., 2020 between 2016 and 2025) and the change between earliest and latest is significant, check the middle year too.
 10. **Cross-year zoom — the killer move.** If you spotted a possible distress in step 4 and zoomed on it, ALSO zoom on the same region in the older epoch. Use `zoom_into_region` with the older image_id and the same bbox you used on the current epoch. Output: did this defect exist back then, or did it appear later?
 11. (Multi-witness, optional) If your current-epoch grade has confidence < 0.7 and another candidate from the SAME year exists nearby, do one `look()` on that second pano at the same yaw to corroborate.
 12. `grade(...)` — your rationale should now mention the temporal arc when relevant ("crack first appeared between 2020 and 2025", or "surface fully resurfaced between 2016 and 2024 — fresh overlay").

**Do NOT grade as Sat-or-better without checking the older epoch when one is available.** A surface that LOOKS good today might have been just resurfaced — that's a critical DOT signal. A surface that LOOKS aged but was already aged 9 years ago is structurally stable; one that was Good 9 years ago and is now Fair has been deteriorating actively.

**Cost is not a concern. Investigation depth is.** 8-12 tool calls per waypoint is acceptable when the temporal evidence is rich.
  8. `grade(tier, confidence, rationale, chosen_image_id=...)` — auto-advances.

**3-6 look calls per waypoint is now ACCEPTABLE and expected.** Quality > speed. Do NOT grade prematurely from a carrier-dominated viewport.

# Tier definitions (calibrated to ASTM D6433 PCI brackets)

  - "Good"          PCI 86-100 — essentially no distress; uniform smooth asphalt; crisp markings.
  - "Satisfactory"  PCI 71-85  — minor distress: a few short cracks (<6mm), light edge raveling.
  - "Fair"          PCI 56-70  — visible cracking (longitudinal/transverse, light block), light raveling, intact patches.
  - "Poor"          PCI 41-55  — widespread block/alligator cracking, rutting, multiple patch failures, edge breaks, small potholes.
  - "Failed"        PCI 0-40   — open potholes, large spalled patches, deep rutting, base exposed.
  - "unknown"       — pavement not visible in any viewport.

**No Sat hedging on bad pavement.** If you see widespread cracking, alligator pattern, rutting, multiple patch failures, edge breaks, or potholes — call **Poor** or **Failed**, not Satisfactory. Hedging Sat on Poor/Failed pavement is a calibration failure.

**Visual confusers are NOT distresses:**
  - Painted lane markings, lane lines, crosswalks, bike-lane symbols, arrows, faded paint
  - Manhole covers, drain grates (intact)
  - Shadows, oil stains, water reflections, sealcoat sheen
  - Vehicle body in lower band

# Output discipline

Before each tool call, emit ONE short text block (≤80 chars) saying what you're doing. Examples: "At waypoint 3 of 32 — finding candidates." / "Peeking 442… (dist 8m, 2y old, GoPro)." / "Forward looks raveled, zooming to confirm." This makes the trace readable for humans watching live.

You are the brain. Filter Mapillary's noise. Showcase your judgment."""


# ---------------------------------------------------------------------------
# Minimap inset + look_around composite renderers                              #
# ---------------------------------------------------------------------------

_MINIMAP_W = 256
_MINIMAP_H = 128
_MINIMAP_PAD = 12  # pixels of inset placement padding from viewport edge

# The minimap is CROPPED to the pavement-relevant pitch band only:
#   above _MINIMAP_PITCH_TOP: sky + upper buildings (irrelevant for road grading)
#   below _MINIMAP_PITCH_BOTTOM: camera carrier (vehicle hood / pedestrian
#     body / bike handlebars) — always noise, always in the bottom of the
#     equirect regardless of capture direction.
# Cropping these out makes every minimap pixel meaningful to pavement context.
_MINIMAP_PITCH_TOP = 10.0
_MINIMAP_PITCH_BOTTOM = -60.0
_MINIMAP_PITCH_RANGE = _MINIMAP_PITCH_TOP - _MINIMAP_PITCH_BOTTOM  # 70°


def _equirect_minimap(equi: "np.ndarray", state: WalkerState,
                      image_id: str) -> "Image.Image":
    """Crop the equirect to the pavement-relevant pitch band, then downscale
    to (_MINIMAP_W × _MINIMAP_H). Cached per image_id."""
    cached = getattr(state, "minimap_cache", None)
    if cached is None:
        state.__dict__["minimap_cache"] = {}
        cached = state.minimap_cache
    if image_id in cached:
        return cached[image_id]
    H = equi.shape[0]
    # Equirect rows: y=0 is pitch=+90°, y=H-1 is pitch=-90°
    y_top = max(0, int(round((90.0 - _MINIMAP_PITCH_TOP) / 180.0 * H)))
    y_bot = min(H, int(round((90.0 - _MINIMAP_PITCH_BOTTOM) / 180.0 * H)))
    cropped = equi[y_top:y_bot]  # numpy slice
    src = Image.fromarray(cropped)
    mm = src.resize((_MINIMAP_W, _MINIMAP_H), Image.LANCZOS).convert("RGB")
    cached[image_id] = mm
    return mm


def _viewport_rect_in_minimap(yaw_deg: float, pitch_deg: float,
                              hfov_deg: float, out_w: int, out_h: int
                              ) -> list[tuple[int, int, int, int]]:
    """Return one or two pixel rectangles in minimap coords (256×128) showing
    where this viewport samples WITHIN the cropped pavement-band minimap.

    If the viewport's pitch range is entirely outside [_MINIMAP_PITCH_BOTTOM,
    _MINIMAP_PITCH_TOP], returns []. If it partially extends below or above,
    the rect is clamped — the rectangle visually touching the bottom edge of
    the minimap is the agent's signal that the viewport is pitched into the
    carrier zone (i.e., the bottom of the rendered viewport will likely show
    car body / person / bike).
    """
    vfov = hfov_deg * (out_h / out_w)
    pitch_top = pitch_deg + vfov / 2.0
    pitch_bot = pitch_deg - vfov / 2.0

    P_TOP = _MINIMAP_PITCH_TOP
    P_BOT = _MINIMAP_PITCH_BOTTOM

    # Entirely outside the visible band → no rectangle
    if pitch_top < P_BOT or pitch_bot > P_TOP:
        return []

    pitch_top_v = max(P_BOT, min(P_TOP, pitch_top))
    pitch_bot_v = max(P_BOT, min(P_TOP, pitch_bot))

    def lat_to_y(lat: float) -> int:
        return int(round((P_TOP - lat) / _MINIMAP_PITCH_RANGE * _MINIMAP_H))

    y_top_px = lat_to_y(pitch_top_v)
    y_bot_px = lat_to_y(pitch_bot_v)

    # Horizontal mapping unchanged (full 360° still shown horizontally)
    yaw_left = ((yaw_deg - hfov_deg / 2.0) + 180.0) % 360.0 - 180.0
    yaw_right = ((yaw_deg + hfov_deg / 2.0) + 180.0) % 360.0 - 180.0

    def lon_to_x(lon: float) -> int:
        return int(round((lon + 180.0) * (_MINIMAP_W / 360.0)))

    x_left = lon_to_x(yaw_left)
    x_right = lon_to_x(yaw_right)

    raw_left = yaw_deg - hfov_deg / 2.0
    raw_right = yaw_deg + hfov_deg / 2.0
    if raw_left < -180.0 or raw_right > 180.0:
        if x_right <= x_left:
            return [
                (0, y_top_px, x_right, y_bot_px),
                (x_left, y_top_px, _MINIMAP_W, y_bot_px),
            ]
    return [(min(x_left, x_right), y_top_px,
             max(x_left, x_right), y_bot_px)]


def _composite_with_minimap(
    viewport_arr: "np.ndarray",
    equi: "np.ndarray",
    state: WalkerState,
    image_id: str,
    yaw_deg: float,
    pitch_deg: float,
    hfov_deg: float,
) -> "Image.Image":
    """Build a composite where the minimap is a STRIP ABOVE the viewport
    (panorama-wide), so the actual pavement viewport is pristine — no inset
    overlapping the road. Layout:

        ┌──────────────────────────── strip width = bw ───────────────┐
        │ minimap (stretched to bw width, ~80px tall)  [yaw/pitch/hfov]│
        ├──────────────────────────────────────────────────────────────┤
        │ viewport (original, unobstructed)                            │
        │                                                              │
        └──────────────────────────────────────────────────────────────┘
    """
    from PIL import ImageDraw, ImageFont
    base = Image.fromarray(viewport_arr).convert("RGB")
    bw, bh = base.size

    # Render the minimap at base width so it's wide and easy to read
    strip_w = bw
    strip_h = 96  # 80px minimap + 16px label band
    minimap_band_h = 80

    mm_src = _equirect_minimap(equi, state, image_id).copy()
    # Resize minimap to strip dimensions (full width, fixed height)
    mm = mm_src.resize((strip_w, minimap_band_h), Image.LANCZOS)

    # Draw rectangle(s) on the minimap — recompute coords for the new size
    draw = ImageDraw.Draw(mm)
    rects = _viewport_rect_in_minimap(yaw_deg, pitch_deg, hfov_deg, bw, bh)
    # Original rects are in 256×128 minimap coords — rescale to strip_w × minimap_band_h
    scale_x = strip_w / float(_MINIMAP_W)
    scale_y = minimap_band_h / float(_MINIMAP_H)
    red = (255, 64, 64)
    for x1, y1, x2, y2 in rects:
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        rx1 = int(round(x1 * scale_x)); rx2 = int(round(x2 * scale_x))
        ry1 = int(round(y1 * scale_y)); ry2 = int(round(y2 * scale_y))
        draw.rectangle([rx1, ry1, max(rx1+1, rx2-1), max(ry1+1, ry2-1)],
                        outline=red, width=2)

    # Build the strip: minimap on top, label band on bottom
    strip = Image.new("RGB", (strip_w, strip_h), (16, 16, 18))
    strip.paste(mm, (0, 0))

    # Label text in the strip's bottom band
    try:
        font = ImageFont.truetype("arial.ttf", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()
    label = (f"  minimap (red rect = current viewport sample)   "
             f"yaw={int(round(yaw_deg)):+d}°   "
             f"pitch={int(round(pitch_deg)):+d}°   "
             f"hfov={int(round(hfov_deg))}°   "
             f"image_id={image_id[:18]}…")
    sd = ImageDraw.Draw(strip)
    sd.text((4, minimap_band_h + 1), label, fill=(220, 220, 230), font=font)

    # Stack strip ABOVE the unmodified viewport
    out = Image.new("RGB", (bw, strip_h + bh), (16, 16, 18))
    out.paste(strip, (0, 0))
    out.paste(base, (0, strip_h))
    return out


def _label_tile(arr: "np.ndarray", text: str) -> "Image.Image":
    """Add a yaw label to the corner of a tile (used in look_around grid)."""
    from PIL import ImageDraw, ImageFont
    img = Image.fromarray(arr).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except (OSError, IOError):
        font = ImageFont.load_default()
    # Black box + white text in top-left corner
    try:
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
    except AttributeError:
        tw, th = draw.textsize(text, font=font)
    pad = 5
    draw.rectangle([0, 0, tw + pad * 2, th + pad * 2 + 2], fill=(0, 0, 0))
    draw.text((pad, pad), text, fill=(255, 255, 255), font=font)
    return img


def _render_look_around(
    equi: "np.ndarray",
    pitch_deg: float,
    hfov_deg: float,
    tile_w: int = 384,
    tile_h: int = 256,
) -> "Image.Image":
    """Render a 2×2 cardinal grid:
        TL = forward (yaw 0)         TR = right (yaw 90)
        BL = left    (yaw 270)       BR = back  (yaw 180)
    Each tile labelled in its top-left corner with its yaw."""
    grid = Image.new("RGB", (tile_w * 2, tile_h * 2), (16, 16, 18))
    layout = [
        (0,   "F (yaw 0°)",   (0, 0)),
        (90,  "R (yaw 90°)",  (tile_w, 0)),
        (270, "L (yaw 270°)", (0, tile_h)),
        (180, "B (yaw 180°)", (tile_w, tile_h)),
    ]
    for yaw, label, pos in layout:
        tile_arr = render_view(equi, yaw, pitch_deg, hfov_deg,
                               out_w=tile_w, out_h=tile_h)
        labelled = _label_tile(tile_arr, label)
        grid.paste(labelled, pos)
    return grid


# ---------------------------------------------------------------------------
# Helpers — peek tool implementation                                           #
# ---------------------------------------------------------------------------

async def _peek_candidate_impl(
    image_id: str,
    state: WalkerState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Run a Haiku quality probe on the candidate. Cached per image_id."""
    if image_id in state.peek_cache:
        cached = dict(state.peek_cache[image_id])
        cached["cached"] = True
        return cached

    # Need a thumb for the peek call. Reuse classify_validity_visibility_async
    # on a 2×2 cardinal grid rendered from the equirect (same shape as the
    # filter we use for calibration set screening).
    pano_path = await _ensure_local_pano(image_id, state, mapillary_token, run_dir)
    if pano_path is None:
        result = {"usable": False, "rig": "unknown",
                  "time_of_day": "unknown",
                  "summary": "could not download pano", "cost_usd": 0.0}
        state.cache_peek(image_id, result)
        return result

    # Render the 2×2 cardinal grid
    grid_path = run_dir / "peek_grids" / f"{image_id}.jpg"
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    if not grid_path.exists():
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _render_2x2_grid, str(pano_path), str(grid_path)
            )
        except Exception as e:
            result = {"usable": False, "rig": "unknown",
                      "time_of_day": "unknown",
                      "summary": f"render error: {e}", "cost_usd": 0.0}
            state.cache_peek(image_id, result)
            return result

    # Send to Haiku with our peek prompt
    try:
        verdict = await _haiku_peek(aclient, str(grid_path))
    except Exception as e:
        verdict = {"usable": False, "rig": "unknown",
                   "time_of_day": "unknown",
                   "summary": f"haiku error: {e}", "cost_usd": 0.0}
    state.cache_peek(image_id, verdict)
    state.add_cost(float(verdict.get("cost_usd", 0.0)))
    return verdict


def _render_2x2_grid(pano_path: str, out_path: str) -> None:
    """Render forward/right/back/left at pitch=-30 into a 2×2 grid for peek."""
    import numpy as np
    import py360convert
    equi = load_equirect(pano_path)
    tile_w, tile_h = 384, 256
    hfov = 80
    pitch = -30
    vfov = hfov * tile_h / tile_w
    tiles = []
    for yaw in [0, 90, 180, 270]:
        u = ((yaw + 180.0) % 360.0) - 180.0
        img = py360convert.e2p(
            equi, fov_deg=(hfov, vfov), u_deg=u, v_deg=pitch,
            out_hw=(tile_h, tile_w), in_rot_deg=0, mode="bilinear",
        )
        tiles.append(img)
    grid = np.zeros((tile_h * 2, tile_w * 2, 3), dtype=np.uint8)
    grid[:tile_h, :tile_w] = tiles[0]
    grid[:tile_h, tile_w:] = tiles[1]
    grid[tile_h:, :tile_w] = tiles[2]
    grid[tile_h:, tile_w:] = tiles[3]
    Image.fromarray(grid).save(out_path, format="JPEG", quality=85, optimize=True)


PEEK_SYSTEM = """You are quickly screening Mapillary panoramas for use in pavement-condition grading. The image is a 2×2 grid: TL=forward(yaw0,pitch-30), TR=right(yaw90), BL=back(yaw180), BR=left(yaw270).

Decide:
  - usable: is paved vehicular road clearly visible in at least one quadrant?
  - rig: vehicle | pedestrian | bicycle | tripod | unknown — based on what's at the bottom of frames (car roof = vehicle; person/feet/sidewalk = pedestrian; handlebars = bicycle)
  - time_of_day: day | twilight | night
  - summary: ≤80 chars on what you saw

Output STRICT JSON: {"usable": bool, "rig": str, "time_of_day": str, "summary": str}"""


async def _haiku_peek(aclient: anthropic.AsyncAnthropic, grid_path: str) -> dict:
    import re
    with open(grid_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("ascii")
    msg = await aclient.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        system=[{"type": "text", "text": PEEK_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/jpeg",
                                          "data": b64}},
            {"type": "text", "text": "Output JSON only."},
        ]}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        parsed = {"_parse_error": text}
    else:
        try:
            parsed = json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            parsed = {"_parse_error": text}

    usage = _usage_to_tokens(msg.usage)
    cost = estimate_cost_with_cache(usage, HAIKU_PRICE_INPUT, HAIKU_PRICE_OUTPUT)

    return {
        "usable": bool(parsed.get("usable", False)),
        "rig": str(parsed.get("rig", "unknown")),
        "time_of_day": str(parsed.get("time_of_day", "unknown")),
        "summary": str(parsed.get("summary", "")),
        "cost_usd": round(cost, 6),
    }


# ---------------------------------------------------------------------------
# SHA-collision detector
#
# Some Mapillary contributors upload byte-identical panos under multiple
# image_ids with falsified captured_at timestamps (the Chestnut Ventura case
# where "2016" and "2025" panos shared SHA-256 to the byte). When we're
# returning candidates spanning multiple years, hash the thumb_1024 for each
# and group by SHA — different image_ids with the same SHA represent the
# SAME underlying capture. Keep one representative per SHA group; the
# others are suppressed and reported back to the agent so it knows the
# corridor's "multi-year" coverage may actually be duplicates.
# ---------------------------------------------------------------------------

async def _enrich_candidates_and_shas(
    state: WalkerState,
    image_ids: list[str],
    mapillary_token: str,
    *,
    fetch_sha: bool = True,
    fetch_meta: bool = True,
) -> None:
    """For each image_id, fetch (in ONE Graph API call) the thumb_1024_url
    + the rich metadata fields (compass_angle, make, model, camera_type,
    sequence). Optionally download the thumb and SHA-hash it.

    Caches:
      - state.thumb_sha_cache[iid] = '16-char sha256 prefix'
      - rich metadata is written back into state.all_candidates so
        find_candidates immediately sees the enriched fields on next call.

    Skips image_ids that are already fully enriched. Concurrency 32.
    """
    import hashlib
    by_iid = {c.image_id: c for c in state.all_candidates}
    needs_meta: list[str] = []
    needs_sha: list[str] = []
    for iid in image_ids:
        c = by_iid.get(iid)
        if fetch_meta and (c is None or c.compass_angle is None):
            needs_meta.append(iid)
        if fetch_sha and iid not in state.thumb_sha_cache:
            needs_sha.append(iid)
    todo = sorted(set(needs_meta) | set(needs_sha))
    if not todo:
        return
    sem = asyncio.Semaphore(32)
    fields = ("thumb_1024_url,captured_at,compass_angle,sequence,make,model,"
              "camera_type,is_pano")
    async with httpx.AsyncClient(timeout=30) as client:
        async def one(iid: str):
            async with sem:
                meta: dict[str, Any] = {}
                sha: str | None = None
                try:
                    r = await client.get(
                        f"https://graph.mapillary.com/{iid}",
                        params={"fields": fields,
                                 "access_token": mapillary_token},
                    )
                    if r.status_code == 200:
                        meta = r.json()
                except Exception:
                    pass
                # SHA: only if we haven't cached it yet AND we want it
                if fetch_sha and iid in needs_sha:
                    url = meta.get("thumb_1024_url")
                    if url:
                        try:
                            rr = await client.get(url, timeout=60)
                            if rr.status_code == 200:
                                sha = hashlib.sha256(rr.content).hexdigest()[:16]
                        except Exception:
                            pass
                return iid, meta, sha
        results = await asyncio.gather(*(one(iid) for iid in todo))
    for iid, meta, sha in results:
        if sha:
            state.thumb_sha_cache[iid] = sha
        if meta and iid in by_iid:
            c = by_iid[iid]
            if c.compass_angle is None and meta.get("compass_angle") is not None:
                c.compass_angle = float(meta["compass_angle"])
            if not c.make and meta.get("make"): c.make = str(meta["make"])
            if not c.model and meta.get("model"): c.model = str(meta["model"])
            if not c.camera_type and meta.get("camera_type"):
                c.camera_type = str(meta["camera_type"])


# Backwards-compat shim — old name still callable
async def _compute_thumb_shas(state, image_ids, mapillary_token):
    await _enrich_candidates_and_shas(state, image_ids, mapillary_token,
                                       fetch_sha=True, fetch_meta=True)


def _dedup_by_sha(
    candidates: list[WaypointCandidate],
    state: WalkerState,
) -> tuple[list[WaypointCandidate], list[dict[str, Any]]]:
    """Group candidates by SHA. Keep one representative per group (prefer
    most-recent year, then closest distance). Return (kept, suppressed)
    where suppressed entries describe each dropped duplicate.

    Candidates with no SHA (couldn't fetch thumb) pass through unchanged.
    """
    by_sha: dict[str, list[WaypointCandidate]] = {}
    no_sha: list[WaypointCandidate] = []
    for c in candidates:
        sha = state.thumb_sha_cache.get(c.image_id)
        if not sha:
            no_sha.append(c)
        else:
            by_sha.setdefault(sha, []).append(c)

    kept: list[WaypointCandidate] = list(no_sha)
    suppressed: list[dict[str, Any]] = []
    for sha, group in by_sha.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Multi-image_id sharing the same JPEG bytes — suspicious. Pick the
        # one with the most-recent year (the "real" one), break ties by
        # distance. The others get suppressed.
        group.sort(key=lambda c: (-(c.year or 0), c.dist_from_waypoint_m))
        primary = group[0]
        kept.append(primary)
        for dup in group[1:]:
            suppressed.append({
                "image_id": dup.image_id,
                "year_claimed": dup.year,
                "duplicate_of_image_id": primary.image_id,
                "duplicate_of_year": primary.year,
                "sha256_prefix": sha,
            })
    return kept, suppressed


# ---------------------------------------------------------------------------
# Helpers — full-res pano fetch (lazy, cached per image_id)                    #
# ---------------------------------------------------------------------------

async def _ensure_local_pano(
    image_id: str, state: WalkerState, token: str, run_dir: Path,
) -> Path | None:
    if image_id in state.equirect_cache and state.equirect_cache[image_id].exists():
        return state.equirect_cache[image_id]
    panos_dir = run_dir / "panos"
    panos_dir.mkdir(parents=True, exist_ok=True)
    dest = panos_dir / f"{image_id}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        state.equirect_cache[image_id] = dest
        return dest
    # Fetch thumb_original_url
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                f"https://graph.mapillary.com/{image_id}",
                params={
                    "fields": "thumb_original_url,thumb_2048_url",
                    "access_token": token,
                },
            )
            r.raise_for_status()
            data = r.json()
            url = data.get("thumb_original_url") or data.get("thumb_2048_url")
            if not url:
                return None
            rr = await client.get(url, timeout=120)
            rr.raise_for_status()
            dest.write_bytes(rr.content)
    except Exception:
        return None
    state.equirect_cache[image_id] = dest
    return dest


# ---------------------------------------------------------------------------
# Helpers — find_candidates impl                                               #
# ---------------------------------------------------------------------------

async def _find_candidates_impl(
    state: WalkerState,
    mapillary_token: str | None = None,
    radius_m: float = 30.0,
    max_age_years: float = 12.0,
    limit: int = 30,
    min_per_year: int = 6,
    year_filter: int | None = None,
    dedup_sha: bool = True,
) -> tuple[list[WaypointCandidate], dict[int, int], list[dict[str, Any]]]:
    """Return candidates within radius, **stratified by year**.

    Returns (selected_candidates, total_per_year_in_radius).
    The second value is the FULL year breakdown of the in-radius pool so the
    agent knows it can ask for more when needed.

    Strategy:
      1. Filter pool by radius + max_age_years (+ year_filter if specified).
      2. Group by `year`. Sort each year's candidates by distance ASC.
      3. Take up to `min_per_year` closest from EACH year.
      4. Fill any remaining slots up to `limit` with the most-recent of
         the leftover candidates.

    With defaults: 6 closest from each year + fill to 30 total. So a
    multi-year corridor that has 100+ panos per year still surfaces
    enough options across years for the agent to pick alternatives when
    one is bad.

    `year_filter`: when set (e.g., 2016), restrict output to that year only,
    returning up to `limit` closest candidates from that year. Lets the
    agent escalate "give me more 2016 panos" when the first batch was bad.
    """
    wp = state.current_waypoint
    if wp is None:
        return [], {}, []
    from collections import defaultdict
    from datetime import datetime, timezone
    now_year = datetime.now(timezone.utc).year
    cutoff_year = now_year - int(max_age_years)

    # 1. Filter by radius + age (+ optional year filter)
    in_radius: list[WaypointCandidate] = []
    for c in state.all_candidates:
        d = haversine_m(wp.lat, wp.lon, c.lat, c.lon)
        if d > radius_m:
            continue
        if c.year is not None and c.year < cutoff_year:
            continue
        if year_filter is not None and c.year != year_filter:
            continue
        in_radius.append(WaypointCandidate(
            image_id=c.image_id, lat=c.lat, lon=c.lon,
            captured_at=c.captured_at, year=c.year, age_years=c.age_years,
            is_pano=c.is_pano, compass_angle=c.compass_angle,
            make=c.make, model=c.model, camera_type=c.camera_type,
            dist_from_waypoint_m=d,
        ))
    if not in_radius:
        state.cache_candidates(wp.idx, [])
        return [], {}, []

    # Total available per year in this radius (for the agent's awareness)
    total_per_year: dict[int, int] = defaultdict(int)
    for c in in_radius:
        total_per_year[c.year or 0] += 1

    # 2. Group by year, closest first within each year
    by_year: dict[int, list[WaypointCandidate]] = defaultdict(list)
    for c in in_radius:
        by_year[c.year or 0].append(c)
    for y in by_year:
        by_year[y].sort(key=lambda x: x.dist_from_waypoint_m)

    out: list[WaypointCandidate] = []
    used_ids: set[str] = set()

    if year_filter is not None:
        # Year-specific request: just return up to `limit` closest from
        # the requested year.
        for c in by_year.get(year_filter, [])[:limit]:
            out.append(c)
            used_ids.add(c.image_id)
    else:
        # 3. Take up to min_per_year closest per year
        for y in sorted(by_year, reverse=True):  # newest first
            for c in by_year[y][:min_per_year]:
                if c.image_id in used_ids:
                    continue
                out.append(c)
                used_ids.add(c.image_id)

        # 4. Fill remaining slots by recency
        leftovers = [c for c in in_radius if c.image_id not in used_ids]
        leftovers.sort(key=lambda x: ((x.captured_at or ""),
                                       -x.dist_from_waypoint_m), reverse=True)
        for c in leftovers:
            if len(out) >= limit:
                break
            out.append(c)
            used_ids.add(c.image_id)

    # Final ordering: year DESC, distance ASC
    out.sort(key=lambda x: (-(x.year or 0), x.dist_from_waypoint_m))

    # Lazy-enrich the returned candidates with rich metadata
    # (compass_angle, make, model, camera_type) AND optionally SHA hashes.
    # Prefetch deliberately skipped this to keep startup fast — only the
    # ~30 panos returned here actually need it.
    suppressed: list[dict[str, Any]] = []
    n_distinct_years_in_out = len({c.year for c in out if c.year is not None})
    if mapillary_token and out:
        # Always fetch metadata; SHA only when multi-year (the dedup case).
        need_sha = dedup_sha and (n_distinct_years_in_out >= 2
                                   or year_filter is not None)
        await _enrich_candidates_and_shas(
            state, [c.image_id for c in out], mapillary_token,
            fetch_sha=need_sha, fetch_meta=True,
        )
        if need_sha:
            out, suppressed = _dedup_by_sha(out, state)
            # Sort again after dedup
            out.sort(key=lambda x: (-(x.year or 0), x.dist_from_waypoint_m))

    state.cache_candidates(wp.idx, out)
    return out, dict(total_per_year), suppressed


# ---------------------------------------------------------------------------
# Visit-record packers (used inside tool dispatchers — write to state.visit_log
# AND return the state_delta dict that the trace consumes)
# ---------------------------------------------------------------------------

def _year_for(state: WalkerState, image_id: str) -> int | None:
    return next((c.year for c in state.all_candidates
                 if c.image_id == image_id), None)


def _record_and_pack_look(
    state: WalkerState, image_id: str, yaw: float, pitch: float,
    hfov: float, purpose: str, out_path: Path, run_dir: Path,
) -> dict[str, Any]:
    yr = _year_for(state, image_id)
    if state.current_waypoint is not None:
        state.record_look(state.current_waypoint.idx, image_id, yr, yaw)
    return {
        "image_id": image_id, "yaw_deg": yaw, "pitch_deg": pitch,
        "hfov_deg": hfov, "purpose": purpose, "year": yr,
        "viewport_path": str(out_path.relative_to(run_dir)),
    }


def _record_and_pack_zoom(
    state: WalkerState, image_id: str,
    src_yaw: float, src_pitch: float, src_hfov: float,
    x1: float, y1: float, x2: float, y2: float,
    new_yaw: float, new_pitch: float, new_hfov: float,
    zoom_factor: float, purpose: str, out_path: Path, run_dir: Path,
) -> dict[str, Any]:
    yr = _year_for(state, image_id)
    if state.current_waypoint is not None:
        state.record_zoom(state.current_waypoint.idx, image_id, yr, new_yaw)
    return {
        "image_id": image_id,
        "source_yaw_deg": src_yaw, "source_pitch_deg": src_pitch,
        "source_hfov_deg": src_hfov,
        "bbox": [x1, y1, x2, y2],
        "rendered_yaw_deg": new_yaw, "rendered_pitch_deg": new_pitch,
        "rendered_hfov_deg": new_hfov, "zoom_factor": round(zoom_factor, 2),
        "purpose": purpose, "year": yr,
        "viewport_path": str(out_path.relative_to(run_dir)),
    }


def _record_and_pack_look_around(
    state: WalkerState, image_id: str, pitch: float, hfov: float,
    purpose: str, out_path: Path, run_dir: Path,
) -> dict[str, Any]:
    yr = _year_for(state, image_id)
    # look_around hits all 4 cardinal yaws — record yaw=0 + 90 + 180 + 270.
    if state.current_waypoint is not None:
        for y in (0, 90, 180, 270):
            state.record_look(state.current_waypoint.idx, image_id, yr, y)
    return {
        "image_id": image_id, "pitch_deg": pitch, "hfov_deg": hfov,
        "purpose": purpose, "year": yr,
        "viewport_path": str(out_path.relative_to(run_dir)),
    }


# ---------------------------------------------------------------------------
# Temporal-discipline pre-grade gate
# ---------------------------------------------------------------------------

def _check_temporal_discipline(
    state: WalkerState, wp_idx: int, rationale: str,
) -> tuple[bool, str]:
    """Returns (ok, reason). If ok=False, the grade is REFUSED and `reason` is
    surfaced back to the agent as the tool result's content text. The agent
    can then fix the gap and call grade() again.

    Rules enforced (in order):
      A. If find_candidates returned ≥2 distinct years AND the agent has only
         visited 1 year via look(), refuse — temporal investigation incomplete.
      B. If the rationale claims a temporal arc (mentions 2 years OR words
         like 'overlay applied', 'progressed', 'stable across', 'between
         X and Y') AND the yaws used in the latest year don't overlap with
         yaws used in the older year, refuse with the YAW-MISMATCH message.
      C. If the agent declared a year unusable but only attempted ≤1 candidate
         from that year (when ≥2 were available), refuse with the
         N-CANDIDATE-FALLBACK message.

    The gate has 2 strikes per waypoint — after that, it lets the grade
    through (so the agent can escape genuinely impossible cases).
    """
    candidates = state.candidates_by_idx.get(wp_idx, [])
    if not candidates:
        return True, ""  # nothing to check
    available_years = sorted({c.year for c in candidates if c.year is not None},
                              reverse=True)

    visits = state.visits_summary(wp_idx)
    by_year = visits.get("by_year", {})
    visited_years = sorted(by_year.keys(), reverse=True)
    yaws_per_year = visits.get("yaws_per_year", {})

    n_strikes = state.discipline_gate_strikes.get(wp_idx, 0)

    # Always allow grade if we've already rejected twice
    if n_strikes >= 2:
        return True, ""

    rationale_lower = (rationale or "").lower()
    # Heuristic for temporal claim
    temporal_keywords = (
        "overlay", "resurfaced", "treatment", "progressed", "stable across",
        "stable since", "since 20", "between 20", "vs 20", "improved",
        "deteriorated", "treatments", "fresh", "no treatments",
        "appeared between", "first appeared",
    )
    has_temporal_claim = (
        sum(1 for y in available_years if str(y) in rationale) >= 2
        or any(kw in rationale_lower for kw in temporal_keywords)
    )

    # Rule A: multi-year coverage available but only 1 year investigated
    if len(available_years) >= 2 and len(visited_years) < 2:
        latest = available_years[0]
        older_year_options = [y for y in available_years if y != latest]
        # Did the agent peek any older-year candidates?
        n_peeks_older = 0
        for older in older_year_options:
            for c in candidates:
                if c.year == older and c.image_id in state.peek_cache:
                    if state.peek_cache[c.image_id].get("usable") is not False:
                        n_peeks_older += 1
        return False, (
            f"GRADE REFUSED — temporal investigation incomplete.\n"
            f"This waypoint has multi-year imagery: {available_years}, "
            f"but you only investigated {visited_years} via look(). "
            f"You MUST investigate at least one older-year pano before "
            f"grading. Steps you can take RIGHT NOW:\n"
            f"  1. peek_candidate(image_id) on a candidate from year "
            f"{older_year_options[0]} (cheap, $0.001) — if usable=true, "
            f"proceed to look() it.\n"
            f"  2. If that one is unusable, peek the NEXT same-year "
            f"candidate. Don't give up after 1.\n"
            f"  3. After investigating, call grade() again with an "
            f"updated rationale that references both years.\n"
            f"If older candidates are GENUINELY unusable (night/indoor "
            f"after peeking ≥2), call grade() again with rationale "
            f"explicitly stating 'older epoch unusable — graded from "
            f"latest only' and the gate will accept."
        )

    # Rule B: temporal claim made but yaws don't overlap across years
    if has_temporal_claim and len(visited_years) >= 2:
        latest = visited_years[0]
        latest_yaws = set(yaws_per_year.get(latest, []))
        # Normalize yaws to nearest 30° to allow some flexibility
        def _bin(yaws):
            return {y // 30 for y in yaws}
        latest_bins = _bin(latest_yaws)
        mismatches = []
        for older in visited_years[1:]:
            older_yaws = set(yaws_per_year.get(older, []))
            older_bins = _bin(older_yaws)
            if latest_bins and older_bins and not (latest_bins & older_bins):
                mismatches.append((older, sorted(latest_yaws),
                                   sorted(older_yaws)))
        if mismatches:
            older, latest_y, older_y = mismatches[0]
            return False, (
                f"GRADE REFUSED — yaw mismatch breaks the temporal claim.\n"
                f"Your rationale references a comparison across years, "
                f"but you investigated:\n"
                f"  {latest} at yaws {latest_y}\n"
                f"  {older} at yaws {older_y}\n"
                f"These yaws don't overlap, so you're comparing different "
                f"physical patches of road across years. The temporal "
                f"claim is unfounded.\n"
                f"FIX: call look() on a {older} pano AT THE SAME YAW you "
                f"used in {latest} (e.g., yaw={latest_y[0] if latest_y else 0}). "
                f"Then call grade() again.\n"
                f"Alternatively: rewrite the rationale to remove the "
                f"temporal claim and grade the latest year only."
            )

    # Rule C: declared year unusable without trying ≥2 same-year candidates
    declared_unusable = (
        "unusable" in rationale_lower or "older epoch unusable" in rationale_lower
        or "night/dup" in rationale_lower
        or ("not useful" in rationale_lower and "compar" in rationale_lower)
    )
    if declared_unusable:
        # Which years did the agent claim were unusable? Heuristic: any year
        # in available_years that the agent wrote "unusable" near.
        for older in available_years[1:]:
            n_candidates_older = sum(1 for c in candidates if c.year == older)
            n_visited_older = len(by_year.get(older, []))
            if n_candidates_older >= 2 and n_visited_older < 2:
                # Did they peek ≥2?
                n_peeked_older = sum(
                    1 for c in candidates
                    if c.year == older and c.image_id in state.peek_cache
                )
                if n_peeked_older < 2:
                    return False, (
                        f"GRADE REFUSED — declared {older} unusable after "
                        f"only {n_visited_older} look() and "
                        f"{n_peeked_older} peek(). "
                        f"{n_candidates_older} candidates are available in "
                        f"{older}; you must peek at least 2 of them before "
                        f"declaring the year unusable.\n"
                        f"FIX: pick another {older} candidate from "
                        f"find_candidates and peek_candidate() it. If it "
                        f"comes back usable, look() it. If it also comes "
                        f"back unusable, you've now established the year "
                        f"truly is unusable — call grade() again."
                    )

    return True, ""


# ---------------------------------------------------------------------------
# Loop driver                                                                  #
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-4-7"


async def run_street_walker(
    run_dir: Path,
    state: WalkerState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    model: str = DEFAULT_MODEL,
    max_total_turns: int = 800,
    max_tokens_per_turn: int = 2500,
    keep_last_n_images: int = 8,
) -> dict[str, Any]:
    trace = TraceWriter(run_dir / "walker_trace.jsonl")

    # Header
    trace.write({
        "record_type": "walker_run_header",
        "street_name": state.street.name,
        "street_slug": state.street.slug,
        "model": model,
        "n_waypoints": len(state.street.waypoints),
        "polyline": state.street.polyline,
        "ground_truth_per_waypoint": [
            {"idx": wp.idx,
             "lat": wp.lat, "lon": wp.lon,
             "ground_truth_pci": wp.ground_truth_pci,
             "ground_truth_tier": wp.ground_truth_tier,
             "segment_id": wp.segment_id,
             "segment_name": wp.segment_name,
             "distance_along_street_m": wp.distance_along_street_m}
            for wp in state.street.waypoints
        ],
        "n_candidates_in_corridor": len(state.all_candidates),
        "budget_cap_usd": state.budget_cap_usd,
        "started_ts": int(time.time() * 1000),
    })

    # Build seed message
    seed = (
        f"# Street survey assignment\n\n"
        f"You are surveying **{state.street.name}** "
        f"({state.street.length_m:.0f} m / "
        f"{state.street.length_m / 1609.34:.2f} mi).\n\n"
        f"There are {len(state.street.waypoints)} waypoints to grade, "
        f"spaced ~50 m apart along the centerline. "
        f"{len(state.all_candidates)} Mapillary 360° panos exist in the "
        f"corridor bbox (you'll filter the relevant ones per waypoint).\n\n"
        f"Total budget: ${state.budget_cap_usd:.2f}.\n\n"
        f"Begin: call `get_position()` to confirm waypoint 0, then "
        f"`find_candidates(radius_m=30)` to see what's available."
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": seed}]}
    ]
    # System prompt is now composed from .claude/skills/pavement/*.md
    # See app/agent/skill_loader.py. The legacy WALKER_SYSTEM_PROMPT constant
    # below is kept for reference but is NOT used at runtime.
    system_param = compose_walker_system()

    waypoint_started_for_idx = -1
    waypoint_turn_count = 0
    waypoint_cost_baseline = 0.0
    waypoint_peeks = 0
    waypoint_looks = 0

    done_signalled = False
    stop_reason = "unknown"
    finished_message_injected = False
    final_warning_injected_for_idx = -1

    stop_flag_path = run_dir / "_stop_requested.flag"

    for turn in range(max_total_turns):
        # User-requested stop — graceful exit
        if stop_flag_path.exists():
            trace.write({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "Stop requested by user — wrapping up current state without further model calls.",
            })
            stop_reason = "user_stopped"
            done_signalled = True
            break

        # Track waypoint transitions
        wp = state.current_waypoint
        if wp is not None and wp.idx != waypoint_started_for_idx:
            trace.write({
                "record_type": "waypoint_started",
                "turn": turn + 1,
                "waypoint_idx": wp.idx,
                "lat": wp.lat, "lon": wp.lon,
                "ground_truth_tier": wp.ground_truth_tier,  # for trace only, not surfaced to agent
                "ground_truth_pci": wp.ground_truth_pci,
            })
            waypoint_started_for_idx = wp.idx
            waypoint_turn_count = 0
            waypoint_cost_baseline = state.budget_used_usd
            waypoint_peeks = 0
            waypoint_looks = 0

        # Per-waypoint turn cap warning (force grade at cap-1)
        if (
            wp is not None
            and waypoint_turn_count >= state.per_waypoint_turn_cap - 1
            and final_warning_injected_for_idx != wp.idx
        ):
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"FINAL TURN AT WAYPOINT {wp.idx}: you have used your "
                        f"per-waypoint turn budget. You MUST call `grade` or "
                        f"`skip_waypoint` THIS turn. Do not call peek or look "
                        f"again at this waypoint."
                    ),
                }],
            })
            final_warning_injected_for_idx = wp.idx

        # If we've finished all waypoints, prompt for done
        if state.is_finished and not finished_message_injected:
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "All waypoints visited. Call `done(summary)` to wrap up "
                        "the street survey with a 2-3 sentence summary of "
                        "overall pavement condition along the corridor."
                    ),
                }],
            })
            finished_message_injected = True

        messages = prune_image_blocks(messages, keep_last_n_images=keep_last_n_images)

        try:
            resp = await aclient.messages.create(
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system_param,
                tools=WALKER_TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as e:
            trace.write({
                "record_type": "turn_error",
                "turn": turn + 1,
                "error": f"{type(e).__name__}: {e}",
            })
            stop_reason = f"api_error:{type(e).__name__}"
            break

        usage = _usage_to_tokens(resp.usage)
        turn_cost = estimate_cost_with_cache(
            usage, PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK
        )
        state.add_cost(turn_cost)

        asst = _response_content_to_jsonable(resp.content)
        text_out = _extract_text(asst)
        thinking = _extract_thinking(asst)
        tool_uses = _extract_tool_uses(asst)

        trace.write({
            "record_type": "turn_assistant",
            "turn": turn + 1,
            "waypoint_idx": wp.idx if wp is not None else -1,
            "thinking": thinking,
            "text": text_out,
            "tool_uses": [
                {"id": tu["id"], "name": tu["name"], "input": tu.get("input", {})}
                for tu in tool_uses
            ],
            "usage": usage,
            "cost_usd": round(turn_cost, 5),
            "budget_used_usd": round(state.budget_used_usd, 4),
            "stop_reason": resp.stop_reason,
        })

        messages.append({"role": "assistant", "content": asst})

        if not tool_uses:
            stop_reason = str(resp.stop_reason or "end_turn")
            break

        waypoint_turn_count += 1

        # Execute tools
        tool_result_blocks: list[dict[str, Any]] = []
        for tu in tool_uses:
            tname = tu["name"]
            targs = tu.get("input", {}) or {}
            tu_id = tu["id"]

            try:
                content_blocks, summary, state_delta, is_error, side_effect = (
                    await _execute_walker_tool(
                        tname, targs, state, aclient, mapillary_token, run_dir,
                        waypoint_turn_count_at_grade=waypoint_turn_count,
                        cost_at_waypoint=state.budget_used_usd - waypoint_cost_baseline,
                        viewports_used_at_grade=waypoint_looks,
                        peeks_used_at_grade=waypoint_peeks,
                    )
                )
            except Exception as e:
                content_blocks = [{
                    "type": "text",
                    "text": json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}),
                }]
                summary, state_delta, is_error, side_effect = (
                    f"{tname} raised: {type(e).__name__}", {}, True, None
                )

            trace.write({
                "record_type": "tool_result",
                "turn": turn + 1,
                "waypoint_idx": wp.idx if wp is not None else -1,
                "tool_use_id": tu_id,
                "tool_name": tname,
                "args": targs,
                "summary": summary,
                "is_error": is_error,
                "state_delta": state_delta,
            })

            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": content_blocks,
            }
            if is_error:
                block["is_error"] = True
            tool_result_blocks.append(block)

            if tname == "peek_candidate" and not is_error:
                waypoint_peeks += 1
            elif tname in ("look", "look_around", "zoom_into_region") and not is_error:
                waypoint_looks += 1
            elif tname == "grade" and not is_error:
                # Tool already advanced state via side_effect
                pass
            elif tname == "skip_waypoint" and not is_error:
                pass
            elif tname == "done":
                done_signalled = True

            # Emit waypoint_complete on grade or skip
            if side_effect == "advanced":
                last_idx = wp.idx if wp is not None else -1
                last_finding = state.findings_by_idx.get(last_idx)
                trace.write({
                    "record_type": "waypoint_complete",
                    "turn": turn + 1,
                    "waypoint_idx": last_idx,
                    "predicted_tier": last_finding.predicted_tier if last_finding else None,
                    "ground_truth_tier": last_finding.ground_truth_tier if last_finding else None,
                    "ground_truth_pci": last_finding.ground_truth_pci if last_finding else None,
                    "match": (
                        last_finding.predicted_tier == last_finding.ground_truth_tier
                        if last_finding and last_finding.predicted_tier
                        and last_finding.ground_truth_tier
                        else None
                    ),
                    "chosen_image_id": last_finding.chosen_image_id if last_finding else None,
                    "rationale": last_finding.rationale if last_finding else None,
                    "candidates_considered": last_finding.candidates_considered if last_finding else 0,
                    "candidates_peeked": last_finding.candidates_peeked if last_finding else 0,
                    "viewports_used": last_finding.viewports_used if last_finding else 0,
                    "turns_used": last_finding.turns_used if last_finding else 0,
                    "cost_usd": last_finding.cost_usd if last_finding else 0.0,
                    "stop_reason": last_finding.stop_reason if last_finding else "",
                })

        messages.append({"role": "user", "content": tool_result_blocks})

        if done_signalled:
            stop_reason = "agent_done"
            break

        if state.budget_used_usd >= state.budget_cap_usd:
            trace.write({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "fleet_budget_cap_reached_forced_exit",
            })
            stop_reason = "budget_cap"
            break

    # Persistence + summary
    state.write_artifacts()

    # Compute aggregate metrics
    findings = state.findings
    n_graded = sum(1 for f in findings if f.predicted_tier and f.predicted_tier != "unknown")
    n_skipped = sum(1 for f in findings if f.predicted_tier in (None, "unknown"))
    n_match = sum(
        1 for f in findings
        if f.predicted_tier and f.ground_truth_tier
        and f.predicted_tier == f.ground_truth_tier
    )
    n_compared = sum(
        1 for f in findings
        if f.predicted_tier and f.ground_truth_tier
        and f.predicted_tier != "unknown"
    )

    trace.write({
        "record_type": "walker_run_complete",
        "stop_reason": stop_reason,
        "n_waypoints": len(state.street.waypoints),
        "n_graded": n_graded,
        "n_skipped": n_skipped,
        "n_match": n_match,
        "n_compared": n_compared,
        "exact_5way_accuracy": (n_match / n_compared) if n_compared else None,
        "total_cost_usd": round(state.budget_used_usd, 4),
        "budget_cap_usd": state.budget_cap_usd,
    })
    trace.close()

    return {
        "stop_reason": stop_reason,
        "n_waypoints": len(state.street.waypoints),
        "n_graded": n_graded,
        "n_skipped": n_skipped,
        "n_match": n_match,
        "n_compared": n_compared,
        "exact_5way_accuracy": (n_match / n_compared) if n_compared else None,
        "total_cost_usd": state.budget_used_usd,
    }


async def _execute_walker_tool(
    tname: str,
    targs: dict[str, Any],
    state: WalkerState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    run_dir: Path,
    *,
    waypoint_turn_count_at_grade: int,
    cost_at_waypoint: float,
    viewports_used_at_grade: int,
    peeks_used_at_grade: int,
) -> tuple[list[dict], str, dict, bool, str | None]:
    """Returns (content_blocks, summary, state_delta, is_error, side_effect)."""

    if tname == "get_position":
        pos = state.position_summary()
        # Don't surface ground-truth fields to the agent (test contamination)
        public = dict(pos)
        for k in ("ground_truth_pci", "ground_truth_tier", "ground_truth_status"):
            public.pop(k, None)
        return ([{"type": "text", "text": json.dumps(public, indent=2)}],
                f"position waypoint {public.get('waypoint_idx')}/{public.get('total_waypoints')}",
                {"position": public}, False, None)

    if tname == "find_candidates":
        radius = float(targs.get("radius_m", 30) or 30)
        max_age = float(targs.get("max_age_years", 12) or 12)
        limit = int(targs.get("limit", 30) or 30)
        min_per_year = int(targs.get("min_per_year", 6) or 6)
        year_filter = targs.get("year_filter")
        if year_filter is not None:
            try:
                year_filter = int(year_filter)
            except (TypeError, ValueError):
                year_filter = None
        candidates, total_per_year, suppressed_duplicates = await _find_candidates_impl(
            state, mapillary_token=mapillary_token,
            radius_m=radius, max_age_years=max_age,
            limit=limit, min_per_year=min_per_year, year_filter=year_filter,
        )
        public = [c.to_public() for c in candidates]
        from collections import Counter
        year_hist = Counter(c.year for c in candidates)

        # "2025: 6/154, 2016: 6/82" — agent sees how many MORE exist if needed
        year_str_full = ", ".join(
            f"{y}: {year_hist.get(y, 0)}/{total_per_year.get(y, 0)}"
            for y in sorted(total_per_year, reverse=True)
        )
        n_years = len(total_per_year)
        if year_filter is not None:
            header = (
                f"// year_filter={year_filter}: returning "
                f"{len(candidates)} closest candidates of "
                f"{total_per_year.get(year_filter, 0)} available in {year_filter}.\n"
            )
        else:
            header = (
                f"// {len(candidates)} candidates within {radius:.0f}m, "
                f"stratified across years (returned/available): "
                f"{year_str_full}.\n"
            )
            if n_years >= 2:
                header += (
                    "// TEMPORAL STORY DETECTED — investigate the latest "
                    "AND at least one older year before grading. Many more "
                    "candidates exist than shown — if these batches don't "
                    "yield usable panos, call find_candidates again with "
                    "year_filter=<year> to fetch more closest panos for "
                    "that specific year.\n"
                )
            else:
                header += "// Single-epoch waypoint — no temporal arc available.\n"
        if suppressed_duplicates:
            n_supp = len(suppressed_duplicates)
            header += (
                f"// Data inconsistency — {n_supp} candidate(s) appear to be "
                f"the same image listed under different dates. Suppressed "
                f"below to avoid double-counting; the most-recent listing "
                f"is kept.\n"
            )
            for d in suppressed_duplicates[:8]:
                header += (
                    f"//   suppressed image_id={d['image_id']} "
                    f"date_claimed={d['year_claimed']} "
                    f"matches {d['duplicate_of_image_id']} "
                    f"({d['duplicate_of_year']}) [sha={d['sha256_prefix']}]\n"
                )
            if n_supp > 8:
                header += f"//   …and {n_supp - 8} more.\n"
            # If multi-year was claimed but ALL older-year panos were dupes,
            # warn the agent that the temporal story isn't real here
            kept_years = {c.year for c in candidates}
            if len(kept_years) < 2 and n_years >= 2:
                header += (
                    "// After dedup: only one distinct date remains. "
                    "The apparent multi-year coverage was duplicate "
                    "listings of the same images — not real historical "
                    "imagery. Grade as single-epoch and note in the "
                    "inconsistencies field.\n"
                )
        return ([{"type": "text", "text": header + json.dumps(public, indent=2)}],
                (f"{len(candidates)} returned (in {radius:.0f}m radius), "
                 f"available: {year_str_full}"
                 + (f", suppressed {len(suppressed_duplicates)} SHA-dupes"
                    if suppressed_duplicates else "")),
                {"n_candidates": len(candidates),
                 "year_breakdown": dict(year_hist),
                 "total_per_year": dict(total_per_year),
                 "n_distinct_years": n_years,
                 "year_filter": year_filter,
                 "suppressed_duplicates": suppressed_duplicates},
                False, None)

    if tname == "peek_candidate":
        image_id = str(targs.get("image_id"))
        if not image_id:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "missing image_id"})}],
                    "missing image_id", {}, True, None)
        verdict = await _peek_candidate_impl(image_id, state, aclient,
                                             mapillary_token, run_dir)
        year = next((c.year for c in state.all_candidates
                     if c.image_id == image_id), None)
        wp = state.current_waypoint
        if wp is not None:
            state.record_peek(wp.idx, image_id, year)
        return ([{"type": "text", "text": json.dumps(verdict, indent=2)}],
                f"peek {image_id}: usable={verdict.get('usable')} rig={verdict.get('rig')}",
                {"image_id": image_id, "peek": verdict, "year": year},
                False, None)

    if tname == "look":
        image_id = str(targs.get("image_id"))
        if not image_id:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "missing image_id"})}],
                    "missing image_id", {}, True, None)
        yaw = float(targs.get("yaw_deg", 0))
        pitch = float(targs.get("pitch_deg", -30))
        hfov = float(targs.get("hfov_deg", 70))
        purpose = str(targs.get("purpose", ""))
        pano_path = await _ensure_local_pano(image_id, state, mapillary_token, run_dir)
        if pano_path is None:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "pano unavailable"})}],
                    f"pano unavailable for {image_id}", {}, True, None)
        try:
            equi = load_equirect(str(pano_path))
            viewport_arr = render_view(equi, yaw, pitch, hfov)
            # Composite with minimap inset showing where this viewport samples
            composite = _composite_with_minimap(
                viewport_arr, equi, state, image_id, yaw, pitch, hfov,
            )
            # Save to disk for the UI to serve (composite, not raw — UI shows
            # what the agent actually saw)
            views_dir = run_dir / "viewports"
            views_dir.mkdir(parents=True, exist_ok=True)
            out = (views_dir
                   / f"{image_id}_y{int(yaw):+04d}_p{int(pitch):+03d}_h{int(hfov):03d}.jpg")
            composite.save(out, format="JPEG", quality=88, optimize=True)
        except Exception as e:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": f"render: {e}"})}],
                    f"render error", {}, True, None)

        # Encode the composite for the model
        import io as _io
        buf = _io.BytesIO()
        composite.save(buf, format="JPEG", quality=88, optimize=True)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        caption = (f"Viewport from {image_id}: yaw={int(yaw)}° "
                   f"pitch={int(pitch)}° hfov={int(hfov)}° "
                   f"(purpose: {purpose}). Bottom-right inset is the full "
                   f"360° pano with a red rectangle showing where this "
                   f"viewport samples from. Use that to judge whether you're "
                   f"pitched too low (rectangle near bottom = camera carrier "
                   f"likely dominates) or too high (rectangle near top = sky).")
        return ([
            {"type": "text", "text": caption},
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/jpeg",
                                          "data": b64}},
        ], f"look {image_id} y{int(yaw)}p{int(pitch)}h{int(hfov)}",
            _record_and_pack_look(state, image_id, yaw, pitch, hfov, purpose, out, run_dir),
            False, None)

    if tname == "zoom_into_region":
        image_id = str(targs.get("image_id"))
        if not image_id:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "missing image_id"})}],
                    "missing image_id", {}, True, None)
        try:
            src_yaw = float(targs["source_yaw_deg"])
            src_pitch = float(targs["source_pitch_deg"])
            src_hfov = float(targs["source_hfov_deg"])
            x1 = float(targs["x1"]); y1 = float(targs["y1"])
            x2 = float(targs["x2"]); y2 = float(targs["y2"])
        except (KeyError, TypeError, ValueError) as e:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False,
                                          "error": f"missing/invalid arg: {e}"})}],
                    "invalid args", {}, True, None)
        purpose = str(targs.get("purpose", ""))

        # Sanity-check bbox
        if x2 <= x1 or y2 <= y1:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False,
                                          "error": "invalid bbox: x2<=x1 or y2<=y1"})}],
                    "invalid bbox", {}, True, None)
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            return ([{"type": "text",
                      "text": json.dumps({"ok": False,
                                          "error": "bbox coords must be in [0,1]"})}],
                    "bbox out of range", {}, True, None)

        # Source viewport's vfov (rectilinear, 768×512 = 1.5 aspect)
        SRC_OUT_W, SRC_OUT_H = 768, 512
        src_vfov = src_hfov * SRC_OUT_H / SRC_OUT_W

        # Map bbox center to angle offsets within the source viewport
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        bbox_w_pct = x2 - x1
        # New (yaw, pitch) — note pitch math: y=0 is TOP of viewport
        # which corresponds to HIGHER pitch (less negative or more positive).
        new_yaw = src_yaw + (cx - 0.5) * src_hfov
        new_pitch = src_pitch - (cy - 0.5) * src_vfov
        # New hfov = source × bbox-width-fraction (width determines magnification)
        new_hfov = src_hfov * bbox_w_pct
        # Clamp to safe ranges
        new_pitch = max(-85.0, min(30.0, new_pitch))
        new_hfov = max(10.0, min(110.0, new_hfov))
        # Wrap yaw to [-180, 180]
        new_yaw = ((new_yaw + 180.0) % 360.0) - 180.0

        pano_path = await _ensure_local_pano(image_id, state, mapillary_token, run_dir)
        if pano_path is None:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "pano unavailable"})}],
                    f"pano unavailable for {image_id}", {}, True, None)
        try:
            equi = load_equirect(str(pano_path))
            viewport_arr = render_view(equi, new_yaw, new_pitch, new_hfov)
            composite = _composite_with_minimap(
                viewport_arr, equi, state, image_id, new_yaw, new_pitch, new_hfov,
            )
            views_dir = run_dir / "viewports"
            views_dir.mkdir(parents=True, exist_ok=True)
            out = (views_dir
                   / f"{image_id}_ZOOM_y{int(new_yaw):+04d}_p{int(new_pitch):+03d}_h{int(new_hfov):03d}.jpg")
            composite.save(out, format="JPEG", quality=88, optimize=True)
        except Exception as e:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": f"render: {e}"})}],
                    f"render error", {}, True, None)
        import io as _io
        buf = _io.BytesIO()
        composite.save(buf, format="JPEG", quality=88, optimize=True)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        zoom_factor = src_hfov / new_hfov if new_hfov > 0 else 1.0
        caption = (
            f"Zoom into region of {image_id}: "
            f"bbox=({x1:.2f},{y1:.2f})→({x2:.2f},{y2:.2f}) of source view "
            f"(src yaw={int(src_yaw)}° pitch={int(src_pitch)}° hfov={int(src_hfov)}°) "
            f"→ rendered at yaw={int(new_yaw)}° pitch={int(new_pitch)}° "
            f"hfov={int(new_hfov)}° ({zoom_factor:.1f}× zoom). "
            f"Purpose: {purpose}. Minimap inset shows the new sampling region."
        )
        return ([
            {"type": "text", "text": caption},
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/jpeg",
                                          "data": b64}},
        ], f"zoom {image_id} bbox→y{int(new_yaw)}p{int(new_pitch)}h{int(new_hfov)} ({zoom_factor:.1f}×)",
            _record_and_pack_zoom(
                state, image_id, src_yaw, src_pitch, src_hfov,
                x1, y1, x2, y2, new_yaw, new_pitch, new_hfov,
                zoom_factor, purpose, out, run_dir,
            ),
            False, None)

    if tname == "look_around":
        image_id = str(targs.get("image_id"))
        if not image_id:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "missing image_id"})}],
                    "missing image_id", {}, True, None)
        pitch = float(targs.get("pitch_deg", -15))
        hfov = float(targs.get("hfov_deg", 80))
        purpose = str(targs.get("purpose", ""))
        pano_path = await _ensure_local_pano(image_id, state, mapillary_token, run_dir)
        if pano_path is None:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": "pano unavailable"})}],
                    f"pano unavailable for {image_id}", {}, True, None)
        try:
            equi = load_equirect(str(pano_path))
            grid = _render_look_around(equi, pitch, hfov)
            views_dir = run_dir / "viewports"
            views_dir.mkdir(parents=True, exist_ok=True)
            out = (views_dir
                   / f"{image_id}_LOOKAROUND_p{int(pitch):+03d}_h{int(hfov):03d}.jpg")
            grid.save(out, format="JPEG", quality=88, optimize=True)
        except Exception as e:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False, "error": f"render: {e}"})}],
                    f"render error", {}, True, None)
        import io as _io
        buf = _io.BytesIO()
        grid.save(buf, format="JPEG", quality=88, optimize=True)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        caption = (f"4-way scan from {image_id} at pitch={int(pitch)}° "
                   f"hfov={int(hfov)}° (purpose: {purpose}). Tile layout: "
                   f"top-left = FORWARD (yaw 0°), top-right = RIGHT (yaw 90°), "
                   f"bottom-left = LEFT (yaw 270°), bottom-right = BACK (yaw "
                   f"180°). Pick the direction with the cleanest pavement "
                   f"(least camera-carrier obstruction), then call look() "
                   f"on that yaw to drill in.")
        return ([
            {"type": "text", "text": caption},
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/jpeg",
                                          "data": b64}},
        ], f"look_around {image_id} p{int(pitch)}h{int(hfov)}",
            _record_and_pack_look_around(
                state, image_id, pitch, hfov, purpose, out, run_dir),
            False, None)

    if tname == "grade":
        tier = str(targs.get("tier", "")).strip()
        valid = {"Good", "Fair", "Poor", "unknown"}
        # Map legacy 5-tier outputs to 3-tier (be tolerant for old skill cache)
        tier_legacy_map = {
            "Satisfactory": "Good",
            "Failed": "Poor",
        }
        if tier in tier_legacy_map:
            tier = tier_legacy_map[tier]
        if tier not in valid:
            return ([{"type": "text",
                      "text": json.dumps({"ok": False,
                                          "error": f"invalid tier {tier!r}; valid: Good/Fair/Poor/unknown"})}],
                    f"invalid tier {tier!r}", {}, True, None)
        try:
            confidence = float(targs.get("confidence")) if targs.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        rationale = str(targs.get("rationale", "")).strip()
        chosen = str(targs.get("chosen_image_id", "")).strip()
        evidence_image_ids = list(targs.get("evidence_image_ids") or [])
        distresses_observed = list(targs.get("distresses_observed") or [])
        treatments_observed = list(targs.get("treatments_observed") or [])
        safety_flags = list(targs.get("safety_flags") or [])
        surroundings_notes = list(targs.get("surroundings_notes") or [])
        inconsistencies = list(targs.get("inconsistencies") or [])

        # ---- TEMPORAL DISCIPLINE PRE-GRADE GATE ----
        # Skip the gate when the agent grades unknown — that's an honest
        # "no usable evidence" call and should always be allowed.
        wp_now = state.current_waypoint
        if wp_now is not None and tier != "unknown":
            ok, reason = _check_temporal_discipline(state, wp_now.idx, rationale)
            if not ok:
                # Increment strike counter so the agent gets at most 2 rejections
                # per waypoint (then we let it through to avoid infinite loops).
                state.discipline_gate_strikes[wp_now.idx] = (
                    state.discipline_gate_strikes.get(wp_now.idx, 0) + 1
                )
                # Surface the rejection back to the agent as the tool result.
                # We do NOT advance the waypoint — agent must call grade() again.
                vsum = state.visits_summary(wp_now.idx)
                return ([{"type": "text", "text": (
                    reason + "\n\n" +
                    f"Visit log so far at WP{wp_now.idx}:\n" +
                    json.dumps({
                        "by_year": {str(y): vs for y, vs in vsum["by_year"].items()},
                        "yaws_per_year": {str(y): yws for y, yws in vsum["yaws_per_year"].items()},
                    }, indent=2)
                )}],
                    f"grade refused — discipline gate (strike "
                    f"{state.discipline_gate_strikes[wp_now.idx]}/2)",
                    {"waypoint_idx": wp_now.idx,
                     "discipline_gate_rejection": True,
                     "strike": state.discipline_gate_strikes[wp_now.idx],
                     "reason": reason[:200]},
                    True, None)

        # Record finding for CURRENT waypoint, then advance
        f = state.record_finding(
            predicted_tier=tier, confidence=confidence, rationale=rationale,
            chosen_image_id=chosen,
            candidates_peeked=peeks_used_at_grade,
            viewports_used=viewports_used_at_grade,
            turns_used=waypoint_turn_count_at_grade,
            cost_usd=round(cost_at_waypoint, 5),
            stop_reason="graded",
        )
        # Persist the rich rationale extras alongside findings.geojson so the
        # UI / re-analysis stage can see all observable details.
        wp_idx = f.waypoint_idx
        evidence_dir = state.run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        # Resolve evidence image paths (viewports we already rendered for any
        # of the listed image_ids). We collect ALL viewports from this WP's
        # visit log keyed by the evidence_image_ids the agent listed.
        evidence_ids = set(evidence_image_ids) | ({chosen} if chosen else set())
        viewport_paths: list[str] = []
        viewports_dir = state.run_dir / "viewports"
        if viewports_dir.exists():
            for vp in sorted(viewports_dir.iterdir()):
                # Filename starts with image_id followed by '_'
                for iid in evidence_ids:
                    if iid and vp.name.startswith(f"{iid}_"):
                        viewport_paths.append(str(vp.relative_to(state.run_dir)))
                        break
        rich_extras = {
            "waypoint_idx": wp_idx,
            "predicted_tier": tier,
            "confidence": confidence,
            "rationale": rationale,
            "chosen_image_id": chosen,
            "evidence_image_ids": list(evidence_ids),
            "evidence_viewports": viewport_paths,
            "distresses_observed": distresses_observed,
            "treatments_observed": treatments_observed,
            "safety_flags": safety_flags,
            "surroundings_notes": surroundings_notes,
            "inconsistencies": inconsistencies,
        }
        (evidence_dir / f"wp{wp_idx:03d}.json").write_text(
            json.dumps(rich_extras, indent=2)
        )
        state.advance()
        state.write_artifacts()
        nxt = state.current_waypoint
        next_summary = (
            f"Advanced to waypoint {nxt.idx} of {len(state.street.waypoints)}."
            if nxt is not None else "All waypoints visited."
        )
        return ([{"type": "text", "text": json.dumps({
            "ok": True, "recorded": tier, "finding_id": f.waypoint_idx,
            "next": next_summary,
        }, indent=2)}],
            f"graded waypoint {f.waypoint_idx}: {tier}",
            {"waypoint_idx": f.waypoint_idx, "predicted_tier": tier,
             "rationale": rationale, "chosen_image_id": chosen,
             "evidence_image_ids": list(evidence_ids),
             "evidence_viewports": viewport_paths,
             "distresses_observed": distresses_observed,
             "treatments_observed": treatments_observed,
             "safety_flags": safety_flags,
             "surroundings_notes": surroundings_notes,
             "inconsistencies": inconsistencies,
             "lat": f.lat, "lon": f.lon},
            False, "advanced")

    if tname == "skip_waypoint":
        reason = str(targs.get("reason", ""))
        f = state.record_finding(
            predicted_tier=None, confidence=0.0, rationale=reason,
            chosen_image_id=None,
            candidates_peeked=peeks_used_at_grade,
            viewports_used=viewports_used_at_grade,
            turns_used=waypoint_turn_count_at_grade,
            cost_usd=round(cost_at_waypoint, 5),
            stop_reason=f"skipped:{reason[:60]}",
        )
        state.advance()
        state.write_artifacts()
        nxt = state.current_waypoint
        next_summary = (
            f"Advanced to waypoint {nxt.idx} of {len(state.street.waypoints)}."
            if nxt is not None else "All waypoints visited."
        )
        return ([{"type": "text", "text": json.dumps({
            "ok": True, "skipped_waypoint": f.waypoint_idx, "next": next_summary,
        }, indent=2)}],
            f"skipped waypoint {f.waypoint_idx}: {reason[:60]}",
            {"waypoint_idx": f.waypoint_idx, "predicted_tier": None,
             "skip_reason": reason, "lat": f.lat, "lon": f.lon},
            False, "advanced")

    if tname == "done":
        summary = str(targs.get("summary", ""))
        (run_dir / "summary.md").write_text(_build_summary_md(state, summary))
        return ([{"type": "text", "text": json.dumps({"ok": True})}],
                f"agent signalled done: {summary[:80]}",
                {"summary": summary}, False, None)

    return ([{"type": "text",
              "text": json.dumps({"ok": False, "error": f"unknown tool {tname!r}"})}],
            f"unknown tool {tname}", {}, True, None)


def _build_summary_md(state: WalkerState, agent_summary: str) -> str:
    findings = state.findings
    n_graded = sum(1 for f in findings if f.predicted_tier and f.predicted_tier != "unknown")
    n_skipped = sum(1 for f in findings if f.predicted_tier in (None, "unknown"))
    n_match = sum(
        1 for f in findings
        if f.predicted_tier and f.ground_truth_tier
        and f.predicted_tier == f.ground_truth_tier
    )
    n_compared = sum(
        1 for f in findings
        if f.predicted_tier and f.ground_truth_tier
        and f.predicted_tier != "unknown"
    )
    lines = [
        f"# Street walker — {state.street.name}",
        "",
        f"- Waypoints: {n_graded} graded · {n_skipped} skipped · {len(state.street.waypoints)} total",
        f"- Cost: ${state.budget_used_usd:.3f} / ${state.budget_cap_usd:.2f}",
        f"- Exact 5-way agreement vs LA PCI: "
        f"{n_match}/{n_compared} = "
        f"{(n_match / n_compared * 100) if n_compared else 0:.1f}%",
        "",
        "## Agent's own summary",
        "",
        agent_summary or "_(no summary)_",
        "",
        "## Per-waypoint table",
        "",
        "| idx | lat,lon | predicted | LA PCI tier | LA PCI | match | rationale |",
        "|---:|---|---|---|---|:---:|---|",
    ]
    for f in findings:
        match = "—"
        if f.predicted_tier and f.ground_truth_tier:
            match = "✓" if f.predicted_tier == f.ground_truth_tier else "✗"
        pci_str = f"{f.ground_truth_pci:.0f}" if f.ground_truth_pci is not None else "—"
        lines.append(
            f"| {f.waypoint_idx} | "
            f"{f.lat:.5f},{f.lon:.5f} | "
            f"{f.predicted_tier or 'skipped'} | "
            f"{f.ground_truth_tier or '—'} | "
            f"{pci_str} | "
            f"{match} | "
            f"{(f.rationale or '')[:80]} |"
        )
    return "\n".join(lines) + "\n"
