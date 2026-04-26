"""Per-pano agentic inspector — Opus drives a `look(yaw, pitch, hfov)` tool to
navigate one 360° pano, then calls `grade(tier, ...)` to commit.

Designed to replace the single-call grader in calibration. The advantage:
  - Opus picks WHERE to look in the 360° image (forward/sides/back/down)
  - Can zoom in (narrow hfov) when it sees suspicious texture
  - Receives the actual rectilinear viewport, not the distorted equirect band
  - Costs scale with how hard the call is — easy panos terminate in 2 turns,
    ambiguous ones use up to ~5

Tool surface (deliberately minimal):
  look(yaw_deg, pitch_deg=-30, hfov_deg=70, purpose=...)
  grade(tier, confidence, rationale, viewports_used)

The narrow scope (one pano) + bounded turn cap keeps each inspector cheap
(~$0.10–$0.30) while preserving the agentic story for the writeup.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import anthropic
import numpy as np
from PIL import Image

from app.agent.loop import (
    _extract_text,
    _extract_thinking,
    _extract_tool_uses,
    _response_content_to_jsonable,
)
from app.agent.trace import prune_image_blocks
from app.claude import (
    PRICE_INPUT_PER_MTOK,
    PRICE_OUTPUT_PER_MTOK,
    _usage_to_tokens,
    estimate_cost_with_cache,
)
from app.panorama import load_equirect


# ---------------------------------------------------------------------------
# Tools                                                                       #
# ---------------------------------------------------------------------------

LOOK_TOOL_SCHEMA = {
    "name": "look",
    "description": (
        "Render and view a rectilinear viewport from the 360° equirectangular "
        "pano you are inspecting. Returns the image so you can visually analyze "
        "it on the next turn.\n\n"
        "ANGLE CONVENTIONS (Mapillary panos):\n"
        "  yaw_deg: 0 = camera-forward (vehicle travel direction). +90 = right, "
        "-90 / 270 = left, 180 = backward. Range -180..359; values are wrapped.\n"
        "  pitch_deg: 0 = horizon. -30 = pavement-forward (road far + near). "
        "-50 = close-up pavement (just in front of vehicle). -65 = nearly "
        "straight-down. +10 = scan signs/signals overhead.\n"
        "  hfov_deg: 40 = zoomed in tight (good for confirming a suspected "
        "defect). 70 = normal scene. 100 = wide context (good first scan).\n\n"
        "RECOMMENDED SCAN STRATEGY:\n"
        "  1. First look: yaw=0, pitch=-30, hfov=80   (forward pavement scene)\n"
        "  2. If forward is blocked by vehicle/scaffolding: try yaw=90 or "
        "yaw=270 (sides) at the same pitch.\n"
        "  3. If you see suspicious texture/cracking: ZOOM IN — same yaw, "
        "pitch=-45 or -55, hfov=40-50. The defect should now be 5-10× more "
        "pixels and obvious cracks vs paint.\n"
        "  4. Stop scanning and call `grade` once you have enough evidence. "
        "Don't waste turns scanning all 4 sides if forward already shows clear "
        "pavement.\n\n"
        "BUDGET: each look costs you tokens. 2-4 looks per pano is typical. "
        "5+ looks should be rare."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "yaw_deg": {
                "type": "integer",
                "minimum": -180,
                "maximum": 359,
                "description": "Yaw in degrees. 0 = camera-forward.",
            },
            "pitch_deg": {
                "type": "integer",
                "minimum": -85,
                "maximum": 30,
                "description": "Pitch in degrees. -30 default for pavement.",
            },
            "hfov_deg": {
                "type": "integer",
                "minimum": 30,
                "maximum": 120,
                "description": "Horizontal FOV in degrees. 70 default.",
            },
            "purpose": {
                "type": "string",
                "description": "1 short sentence: what you're looking for / "
                               "why this angle. Helps the trace stay readable.",
            },
        },
        "required": ["yaw_deg", "purpose"],
    },
}


GRADE_TOOL_SCHEMA = {
    "name": "grade",
    "description": (
        "Submit your final pavement-condition tier for this pano and END the "
        "inspection. Call EXACTLY ONCE per pano, after you've seen enough "
        "viewports to be confident. After this call, the inspection terminates."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["Good", "Satisfactory", "Fair", "Poor", "Failed", "unknown"],
                "description": "Pavement condition tier. Use 'unknown' ONLY if "
                               "no usable pavement was visible in any viewport.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Your confidence in the tier (0.0-1.0).",
            },
            "rationale": {
                "type": "string",
                "description": "≤120 chars: what you observed on the visible "
                               "pavement that drives this tier. Specific cues "
                               "(crack pattern, raveling, patching, etc.) — not "
                               "paint, not vehicle body.",
            },
            "viewports_used": {
                "type": "array",
                "description": "Optional: list of (yaw, pitch, hfov) you "
                               "looked at, in order.",
                "items": {"type": "string"},
            },
        },
        "required": ["tier", "confidence", "rationale"],
    },
}


INSPECTOR_TOOL_SCHEMAS = [LOOK_TOOL_SCHEMA, GRADE_TOOL_SCHEMA]


# ---------------------------------------------------------------------------
# System prompt — calibrated against v1 baseline failure modes:                #
#   - over-hedging to Satisfactory                                             #
#   - never picking Poor/Failed                                                #
#   - giving up on equirect-distorted views                                    #
# ---------------------------------------------------------------------------

INSPECTOR_SYSTEM_PROMPT = """You are a pavement INSPECTOR agent. You are looking at ONE 360° street-level panorama from Mapillary. Your only job: navigate the panorama using your `look` tool, then commit to a 5-tier pavement condition rating using `grade`. Then you stop.

You have TWO tools and only two:
- `look(yaw_deg, pitch_deg, hfov_deg, purpose)` — render and view a viewport
- `grade(tier, confidence, rationale)` — submit final answer (terminates)

# What a Mapillary 360° panorama actually looks like (READ THIS CAREFULLY)

The pano is an equirectangular image. When you `look(yaw, pitch=-30)`, you get a rectilinear viewport. The panos in this dataset come from a MIX of capture rigs — DO NOT assume a roof-mounted car camera. You will encounter all of these:

  • **Car/SUV roof rig.** The bottom band of any down-pitched view shows a dark glossy car roof, sometimes with a sunroof or roof-rack. The forward view often shows the windshield top edge; you see the road clearly past the hood.
  • **Pedestrian with handheld 360 camera on a stick (very common in LA Mapillary).** The bottom band shows the operator's HEAD (often blurred), shoulders, arms holding a phone, and feet/shoes near nadir. The camera is at human height (~1.7 m) so the road, if present, is 2-5 m away horizontally.
  • **Pedestrian with chest/helmet mount.** The bottom shows the operator's torso, lap, jeans, shoes, and the ground directly below their feet. Looking down typically reveals dirt, gravel, or sidewalk — not road.
  • **Bicycle/scooter rig.** Bottom shows handlebars, helmet edge, body of the rider.
  • **Tripod.** Bottom may show the tripod legs and a small ground patch directly below the camera.

**The bottom 30% of any pitch≤-30° view is almost always NOT pavement you should grade.** It's the camera carrier (vehicle/person/rig) plus immediate surrounds. Ignore that band when assessing pavement — pavement to grade is in the MID-distance, 4-15 m away, in the upper-middle portion of a forward view.

# CRITICAL: many panos do NOT show paved road at all

The dataset includes panos captured from staircases, hiking trails, sidewalks, plazas, parks, alleys, and parking lots — places where the operator was walking but no vehicle road is in frame. If after 1-2 looks you have not seen any **paved asphalt or concrete road surface**, the correct answer is `grade(tier="unknown", ...)`. **Do NOT hedge to Satisfactory when the road is not visible.** "Unknown" is honest; "Satisfactory" on a no-road pano is a calibration failure.

Signs the pano has no paved road in view:
  - You see only sidewalk pavers, brick, or stone tiles (those are not road pavement)
  - You see dirt, gravel, mulch, leaves, sand
  - You see grass, plants, planters
  - You see staircases, ramps, plazas
  - You see only buildings, walls, fences
  - You see only the operator's body and the small patch of ground at their feet

If 2+ different yaws all show the above, call `grade(tier="unknown", confidence=0.9, rationale="No paved road surface visible from any analyzed direction; <what you saw>")` and stop.

# Tier definitions (calibrated to ASTM D6433 PCI brackets, applied ONLY when paved road is visible)

  - "Good"          PCI 86-100 — essentially no distress; uniform smooth asphalt; crisp markings; hairline at most.
  - "Satisfactory"  PCI 71-85  — minor distress: a few short cracks (<6mm), light edge raveling, intact older patches.
  - "Fair"          PCI 56-70  — clearly visible cracking (longitudinal/transverse, light block), light-moderate raveling, multiple intact patches, faded markings.
  - "Poor"          PCI 41-55  — widespread block or alligator cracking, rutting in wheelpaths, multiple patch FAILURES, edge breaks, small potholes forming, severe raveling.
  - "Failed"        PCI 0-40   — open potholes, large spalled patches, deep rutting, base material exposed, structural failure.
  - "unknown"       — pavement was not visible in any viewport you looked at.

# Critical rules

1. **You MUST call `grade` within your turn budget.** Failing to grade is a hard failure. Grading with imperfect evidence is fine. Running out of turns without grading is not.
2. **Aim for 1-3 looks, then grade.** Most panos can be graded confidently after 1-2 viewports. If you have a clean forward pavement view, GRADE IT — do not hunt for "more confirmation." Extra looks beyond 3 should be rare and only when you genuinely cannot see pavement yet.
3. **Look BEFORE you grade.** First turn must be a `look`. Never grade without at least one prior look.
4. **NO Sat hedging on bad pavement.** If you see ANY of: widespread cracking, alligator pattern, rutting in wheelpaths, multiple patch failures, edge breaks, or potholes — choose **Poor** or **Failed**, not Satisfactory. Hedging to Sat on Poor/Failed pavement is a calibrated failure mode for this task. Do not do it.
5. **Zoom in on REAL suspicion only.** If a normal-FOV view shows clear distress, just grade. Only zoom in (hfov 40-50, pitch -50) when you genuinely can't tell paint from crack. Do NOT zoom routinely "to confirm" — that wastes turns.
6. **Visual confusers — these are NOT distresses:**
   - Painted lane markings, lane lines, crosswalk stripes, bike-lane symbols, arrows, faded paint, utility-locate spray paint
   - Manhole covers, drain grates, utility access frames (intact)
   - Shadows from poles, trees, buildings, pedestrians
   - Oil stains, water reflections, sealcoat sheen, tire marks
   - The bottom edge of viewports often shows the camera vehicle's own roof/body — ignore that band
7. **Under-report rule (Good ↔ Sat ↔ Fair only).** Between Good and Sat, or Sat and Fair: if uncertain, choose the BETTER tier. This rule does NOT apply between Fair/Poor/Failed — there you should call what you see.
8. **Camera-forward = yaw 0.** Mapillary panos are oriented so yaw_deg=0 looks down the vehicle's travel direction. Start there.

# Decision flow (FOLLOW THIS — do not improvise extra looks)

  Turn 1: look(yaw=0, pitch=-30, hfov=80, purpose="forward pavement scene")
  Turn 2: One of:
    (a) Forward view shows clear pavement → CALL `grade` NOW. You have enough evidence.
    (b) Forward view is blocked by vehicle/sidewalk/intersection → look(yaw=90, pitch=-30, hfov=80, purpose="right side") OR look(yaw=270, pitch=-30, hfov=80, purpose="left side")
    (c) Forward shows ambiguous texture (could be cracks or could be paint/shadow) → look(yaw=0, pitch=-50, hfov=45, purpose="zoom on suspected defect")
  Turn 3: CALL `grade`. If you still don't have usable pavement, grade "unknown".

Do not extend past turn 3 unless you have a specific concrete reason. Calling `grade` with 70% confidence beats hitting the turn cap with 0 grade.

# Output discipline

- Before each `look`, emit ONE short text block (≤60 chars) saying what you're checking. Example: "Forward pavement at -30 pitch."
- Your `grade` rationale must reference specific observed pavement features (crack type/density, raveling extent, patch count, rut depth) — not paint, not cars.
- The trace is read by humans. Be terse and concrete."""


# ---------------------------------------------------------------------------
# Viewport rendering                                                           #
# ---------------------------------------------------------------------------

def _wrap360(deg: float) -> float:
    d = deg % 360.0
    if d >= 180.0:
        d -= 360.0
    return d


def render_view(
    equi: np.ndarray,
    yaw_deg: float,
    pitch_deg: float = -30.0,
    hfov_deg: float = 70.0,
    out_w: int = 768,
    out_h: int = 512,
) -> np.ndarray:
    """Render an arbitrary rectilinear view from an equirectangular pano.

    Mirrors panorama.extract_pavement_strip but with full pitch/hfov freedom
    instead of the constrained 4-direction strip set.
    """
    import py360convert
    vfov = hfov_deg * out_h / out_w
    return py360convert.e2p(
        equi,
        fov_deg=(hfov_deg, vfov),
        u_deg=_wrap360(yaw_deg),
        v_deg=pitch_deg,
        out_hw=(out_h, out_w),
        in_rot_deg=0,
        mode="bilinear",
    )


def _np_to_jpeg_b64(arr: np.ndarray, quality: int = 88) -> str:
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# JSON parser                                                                  #
# ---------------------------------------------------------------------------

VALID_TIERS = {"Good", "Satisfactory", "Fair", "Poor", "Failed", "unknown"}


# ---------------------------------------------------------------------------
# Inspector loop                                                               #
# ---------------------------------------------------------------------------

async def run_inspector(
    image_id: str,
    equirect_path: Path,
    client: anthropic.AsyncAnthropic,
    *,
    model: str = "claude-opus-4-7",
    turn_cap: int = 5,
    budget_cap_usd: float = 0.30,
    max_tokens_per_turn: int = 800,
    keep_last_n_images: int = 2,
    save_views_dir: Path | None = None,
) -> dict[str, Any]:
    """Run a per-pano inspection loop. Returns:
      {
        image_id, predicted_tier, confidence, rationale,
        viewports_chosen: [{yaw, pitch, hfov, purpose}, ...],
        turns_used, cost_usd, latency_ms,
        stop_reason: "graded" | "turn_cap" | "budget_cap" | "no_grade" | "error"
        narration: list of text the inspector emitted before each tool call
        error: optional string
      }
    """
    t0 = time.time()
    if not equirect_path.exists():
        return {
            "image_id": image_id,
            "predicted_tier": None,
            "stop_reason": "error",
            "error": f"equirect missing: {equirect_path}",
            "cost_usd": 0.0,
            "latency_ms": 0,
            "turns_used": 0,
            "viewports_chosen": [],
            "narration": [],
        }

    equi = load_equirect(str(equirect_path))

    seed_text = (
        f"# Inspection assignment\n\n"
        f"You are inspecting pano `{image_id}`.\n\n"
        f"Begin with a forward pavement view: "
        f"`look(yaw_deg=0, pitch_deg=-30, hfov_deg=80, "
        f"purpose=\"forward pavement scene\")`. "
        f"Then decide what to do next."
    )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": seed_text}]}
    ]
    system_param = [{
        "type": "text",
        "text": INSPECTOR_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    cost_total = 0.0
    viewports_chosen: list[dict[str, Any]] = []
    narration: list[str] = []
    predicted_tier = None
    confidence = None
    rationale = None
    stop_reason = "no_grade"
    error: str | None = None
    turns_used = 0
    final_turn_warning_injected = False

    for turn in range(turn_cap):
        turns_used = turn + 1
        messages = prune_image_blocks(messages, keep_last_n_images=keep_last_n_images)

        # On the second-to-last turn (if not yet graded), inject a must-grade
        # nudge so the inspector commits before the cap.
        is_final_turn = (turn == turn_cap - 1)
        if is_final_turn and not final_turn_warning_injected and predicted_tier is None:
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "FINAL TURN: you have used your look budget. You MUST call "
                        "`grade` this turn. Do not call `look` again. Pick the best "
                        "tier you can with what you've already seen. If no usable "
                        "pavement was visible in any prior viewport, grade "
                        "\"unknown\". Failure to grade this turn is a hard failure."
                    ),
                }],
            })
            final_turn_warning_injected = True

        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system_param,
                tools=INSPECTOR_TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as e:
            stop_reason = "error"
            error = f"{type(e).__name__}: {e}"
            break

        usage = _usage_to_tokens(resp.usage)
        turn_cost = estimate_cost_with_cache(
            usage, PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK
        )
        if model == "claude-haiku-4-5":
            turn_cost = estimate_cost_with_cache(usage, 1.0, 5.0)
        cost_total += turn_cost

        asst = _response_content_to_jsonable(resp.content)
        text_out = _extract_text(asst)
        if text_out:
            narration.append(text_out)
        tool_uses = _extract_tool_uses(asst)
        messages.append({"role": "assistant", "content": asst})

        if not tool_uses:
            stop_reason = "no_grade"
            break

        # Execute tools, collect results
        tool_result_blocks: list[dict[str, Any]] = []
        graded_this_turn = False
        for tu in tool_uses:
            tname = tu["name"]
            targs = tu.get("input", {}) or {}
            tu_id = tu["id"]

            if tname == "look":
                yaw = float(targs.get("yaw_deg", 0))
                pitch = float(targs.get("pitch_deg", -30))
                hfov = float(targs.get("hfov_deg", 70))
                purpose = str(targs.get("purpose", ""))
                try:
                    img = render_view(equi, yaw, pitch, hfov)
                except Exception as e:
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "is_error": True,
                        "content": [{
                            "type": "text",
                            "text": json.dumps({"ok": False, "error": str(e)}),
                        }],
                    })
                    continue
                if save_views_dir is not None:
                    save_views_dir.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(img).save(
                        save_views_dir
                        / f"t{turn+1:02d}_y{int(yaw):+04d}_p{int(pitch):+03d}_h{int(hfov):03d}.jpg",
                        format="JPEG", quality=88, optimize=True,
                    )
                viewports_chosen.append({
                    "turn": turn + 1, "yaw_deg": yaw, "pitch_deg": pitch,
                    "hfov_deg": hfov, "purpose": purpose,
                })
                b64 = _np_to_jpeg_b64(img)
                caption = (f"Viewport rendered: yaw={int(yaw)}° pitch={int(pitch)}° "
                           f"hfov={int(hfov)}° (purpose: {purpose}).")
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [
                        {"type": "text", "text": caption},
                        {"type": "image",
                         "source": {"type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64}},
                    ],
                })
                continue

            if tname == "grade":
                tier = str(targs.get("tier", "")).strip()
                if tier not in VALID_TIERS:
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "is_error": True,
                        "content": [{
                            "type": "text",
                            "text": json.dumps({"ok": False,
                                                "error": f"invalid tier {tier!r}"}),
                        }],
                    })
                    continue
                predicted_tier = tier
                try:
                    confidence = float(targs.get("confidence")) if targs.get("confidence") is not None else None
                except (TypeError, ValueError):
                    confidence = None
                rationale = str(targs.get("rationale", "")).strip() or None
                graded_this_turn = True
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"ok": True,
                                            "tier_recorded": tier,
                                            "inspection_complete": True}),
                    }],
                })
                continue

            # Unknown tool
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "is_error": True,
                "content": [{
                    "type": "text",
                    "text": json.dumps({"ok": False,
                                        "error": f"unknown tool {tname!r}"}),
                }],
            })

        messages.append({"role": "user", "content": tool_result_blocks})

        if graded_this_turn:
            stop_reason = "graded"
            break

        if cost_total >= budget_cap_usd:
            stop_reason = "budget_cap"
            break

    if predicted_tier is None and stop_reason == "no_grade":
        # Inspector ran out of turns without grading
        stop_reason = "turn_cap" if turns_used >= turn_cap else stop_reason

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "image_id": image_id,
        "predicted_tier": predicted_tier,
        "confidence": confidence,
        "rationale": rationale,
        "viewports_chosen": viewports_chosen,
        "narration": narration,
        "turns_used": turns_used,
        "cost_usd": round(cost_total, 6),
        "latency_ms": latency_ms,
        "stop_reason": stop_reason,
        "error": error,
    }
