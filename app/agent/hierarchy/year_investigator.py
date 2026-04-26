"""Year Investigator — bottom of the 3-tier hierarchy.

One investigator per (point, year). Investigates panos for a single year,
posts structured claims to the per-point blackboard, reads sibling claims
to coordinate yaws across years, and ends by reporting year findings.

Tool surface (7):
  peek_candidate, look_around, look, zoom_into_region,
  read_sibling_claims, post_claim, report_year_findings
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import Any

import anthropic
from PIL import Image

from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.blackboard import PointBlackboard
from app.agent.hierarchy.primitives import (
    _ensure_local_pano,
    _peek_candidate_impl,
    _record_and_pack_look,
    _record_and_pack_look_around,
    _record_and_pack_zoom,
    _render_look_around,
    _composite_with_minimap,
    adapter_for,
)
from app.agent.hierarchy.run_state import RunState
from app.agent.hierarchy.runner import run_agent_loop
from app.agent.hierarchy.skills import compose_investigator_system
from app.agent.pano_inspector import render_view
from app.agent.walker_state import WaypointCandidate
from app.panorama import load_equirect


INVESTIGATOR_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "peek_candidate",
        "description": (
            "Quick Haiku quality probe (~$0.001) on ONE candidate from your "
            "year's pre-filtered list. Returns usable / rig / time_of_day / "
            "summary. Cached. Call this BEFORE any look()/zoom() to avoid "
            "wasting an Opus turn on a bad pano (night, indoor, garage)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"image_id": {"type": "string"}},
            "required": ["image_id"],
        },
    },
    {
        "name": "look_around",
        "description": (
            "Render a 2x2 cardinal grid (forward/right/back/left at yaws "
            "0/90/180/270) for ONE pano. Use to orient yourself on a usable "
            "pano. Pick the cleanest pavement direction, then call look()."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "pitch_deg": {"type": "integer", "default": -15,
                              "minimum": -85, "maximum": 30},
                "hfov_deg": {"type": "integer", "default": 80,
                             "minimum": 30, "maximum": 120},
                "purpose": {"type": "string"},
            },
            "required": ["image_id", "purpose"],
        },
    },
    {
        "name": "look",
        "description": (
            "Render a focused viewport at (yaw,pitch,hfov). Returns image + "
            "minimap-strip-above-viewport. Default pitch -30 for pavement. "
            "When a sibling investigator has anchored a yaw via "
            "post_claim(category='temporal_anchor', yaw=N), USE THE SAME YAW "
            "so cross-year reconciliation can compare matching road patches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "yaw_deg": {"type": "integer", "minimum": -180, "maximum": 180},
                "pitch_deg": {"type": "integer", "default": -30,
                              "minimum": -85, "maximum": 30},
                "hfov_deg": {"type": "integer", "default": 70,
                             "minimum": 20, "maximum": 120},
                "purpose": {"type": "string"},
            },
            "required": ["image_id", "yaw_deg", "purpose"],
        },
    },
    {
        "name": "zoom_into_region",
        "description": (
            "Re-render a tighter view INTO a bbox of a previous look(). Bbox "
            "coords (x1,y1,x2,y2) are normalized [0..1] of the source view. "
            "Use to confirm a suspected distress at full equirect resolution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string"},
                "source_yaw_deg": {"type": "number"},
                "source_pitch_deg": {"type": "number"},
                "source_hfov_deg": {"type": "number"},
                "x1": {"type": "number", "minimum": 0, "maximum": 1},
                "y1": {"type": "number", "minimum": 0, "maximum": 1},
                "x2": {"type": "number", "minimum": 0, "maximum": 1},
                "y2": {"type": "number", "minimum": 0, "maximum": 1},
                "purpose": {"type": "string"},
            },
            "required": ["image_id", "source_yaw_deg", "source_pitch_deg",
                          "source_hfov_deg", "x1", "y1", "x2", "y2", "purpose"],
        },
    },
    {
        "name": "read_sibling_claims",
        "description": (
            "Read claims posted to the point blackboard by SIBLING year "
            "investigators (other years at this same point). Free. CALL ON "
            "TURN 1 — sibling claims tell you which yaw to anchor at, what "
            "features to verify across years."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "post_claim",
        "description": (
            "Write a structured observation to the point blackboard. Sibling "
            "investigators (and your parent surveyor) can read it. Categories: "
            "'distress' (crack/pothole/etc.), 'treatment' (mill+overlay/etc.), "
            "'hazard', 'unusable_evidence', 'temporal_anchor' (THE most "
            "valuable — pin a yaw siblings should also investigate), 'note'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["distress", "treatment", "hazard",
                              "unusable_evidence", "temporal_anchor", "note"],
                },
                "content": {"type": "string", "description": "<=200 chars."},
                "image_ids": {"type": "array", "items": {"type": "string"}},
                "yaw_deg": {"type": "integer", "minimum": -180, "maximum": 180},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["category", "content"],
        },
    },
    {
        "name": "report_year_findings",
        "description": (
            "Final structured report for your year. Implies done — no more "
            "turns. The surveyor reads `summary` verbatim; `yaws_covered` "
            "drives the cross-witness check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "usable": {"type": "boolean"},
                "summary": {"type": "string"},
                "distresses": {"type": "array",
                                "items": {"type": "object"}},
                "treatments": {"type": "array",
                                "items": {"type": "object"}},
                "yaws_covered": {"type": "array",
                                  "items": {"type": "integer"}},
                "best_image_id": {"type": "string"},
            },
            "required": ["year", "usable", "summary"],
        },
    },
]


def _build_seed(
    point_idx: int,
    lat: float,
    lon: float,
    year: int,
    candidates: list[WaypointCandidate],
    captain_directive: str | None,
    surveyor_directive: str | None,
    focus_yaw: int | None,
) -> str:
    cand_lines = "\n".join(
        f"  - {c.image_id} | "
        f"dist={c.dist_from_waypoint_m:.1f}m | "
        f"compass={c.compass_angle if c.compass_angle is not None else '?'}° | "
        f"rig={c.make or '?'} {c.model or ''} ({c.camera_type or '?'}) | "
        f"captured={c.captured_at}"
        for c in candidates[:8]
    )
    parts = [
        f"You are Year Investigator for **{year}** at survey point {point_idx} "
        f"(lat={lat:.6f}, lon={lon:.6f}).",
        "",
        f"You have {len(candidates)} pano candidate(s) from year {year} "
        f"within 30m. Top {min(8, len(candidates))} by distance:",
        cand_lines if cand_lines else "  (none usable in this year)",
        "",
    ]
    if focus_yaw is not None:
        parts.append(
            f"**FOCUS YAW = {focus_yaw}°.** Your parent surveyor wants you "
            f"to anchor at this yaw so cross-year reconciliation works. "
            f"Investigate this yaw FIRST."
        )
        parts.append("")
    if captain_directive:
        parts.append(f"Captain directive: {captain_directive}")
        parts.append("")
    if surveyor_directive:
        parts.append(f"Surveyor directive: {surveyor_directive}")
        parts.append("")
    parts.extend([
        "Workflow:",
        "  1. read_sibling_claims (FREE — see what other years at this point have already pinned)",
        "  2. peek_candidate on the closest pano (skip if peek says unusable, peek the next)",
        "  3. look_around at pitch -15 to orient",
        "  4. look at the cleanest yaw (or focus_yaw) at pitch -30",
        "  5. zoom_into_region if a feature needs confirmation",
        "  6. post_claim for each distress/treatment/anchor you find",
        "  7. report_year_findings",
        "",
        "You have 8 turns. Stay focused. Don't grade — that's the surveyor's job.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool dispatcher (closure over scratch + per-investigator state)
# ---------------------------------------------------------------------------

def _make_dispatch(
    rs: RunState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    point_idx: int,
    lat: float,
    lon: float,
    year: int,
    candidates: list[WaypointCandidate],
    pbb: PointBlackboard,
    investigator_id: str,
    final_report_holder: dict[str, Any],
):
    candidate_pool = candidates  # restrict adapter's all_candidates to this year

    async def dispatch(tname: str, targs: dict[str, Any], scratch: AgentScratch
                       ) -> dict[str, Any]:
        if tname == "peek_candidate":
            image_id = str(targs.get("image_id") or "").strip()
            if not image_id:
                return _err("missing image_id")
            async with adapter_for(rs, scratch, point_idx=point_idx,
                                    lat=lat, lon=lon,
                                    candidate_pool=candidate_pool) as ad:
                verdict = await _peek_candidate_impl(
                    image_id, ad, aclient, mapillary_token, rs.run_dir,
                )
                yr = next((c.year for c in candidate_pool
                           if c.image_id == image_id), None)
                scratch.record_peek(image_id, yr)
            return {
                "content": [{"type": "text", "text": json.dumps(verdict, indent=2)}],
                "summary": (f"peek {image_id}: usable={verdict.get('usable')} "
                            f"rig={verdict.get('rig')}"),
                "is_error": False,
                "state_delta": {"image_id": image_id, "peek": verdict, "year": yr},
                "side_effect": None,
            }

        if tname == "look_around":
            image_id = str(targs.get("image_id") or "").strip()
            if not image_id:
                return _err("missing image_id")
            pitch = float(targs.get("pitch_deg", -15))
            hfov = float(targs.get("hfov_deg", 80))
            purpose = str(targs.get("purpose", ""))
            async with adapter_for(rs, scratch, point_idx=point_idx,
                                    lat=lat, lon=lon,
                                    candidate_pool=candidate_pool) as ad:
                pano_path = await _ensure_local_pano(
                    image_id, ad, mapillary_token, rs.run_dir
                )
                if pano_path is None:
                    return _err(f"pano unavailable for {image_id}")
                try:
                    equi = load_equirect(str(pano_path))
                    grid = _render_look_around(equi, pitch, hfov)
                    out = (rs.run_dir / "viewports"
                            / f"{image_id}_LOOKAROUND_p{int(pitch):+03d}_h{int(hfov):03d}.jpg")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    grid.save(out, format="JPEG", quality=88, optimize=True)
                except Exception as e:
                    return _err(f"render: {e}")
                buf = io.BytesIO()
                grid.save(buf, format="JPEG", quality=88, optimize=True)
                b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
                state_delta = _record_and_pack_look_around(
                    ad, image_id, pitch, hfov, purpose, out, rs.run_dir,
                )
            caption = (f"4-way scan from {image_id} at pitch={int(pitch)}° "
                       f"hfov={int(hfov)}° (purpose: {purpose}). TL=forward, "
                       f"TR=right, BL=left, BR=back.")
            return {
                "content": [
                    {"type": "text", "text": caption},
                    {"type": "image", "source": {"type": "base64",
                                                   "media_type": "image/jpeg",
                                                   "data": b64}},
                ],
                "summary": f"look_around {image_id} p{int(pitch)}h{int(hfov)}",
                "is_error": False,
                "state_delta": state_delta,
                "side_effect": None,
            }

        if tname == "look":
            image_id = str(targs.get("image_id") or "").strip()
            if not image_id:
                return _err("missing image_id")
            try:
                yaw = float(targs.get("yaw_deg"))
            except (TypeError, ValueError):
                return _err("missing/invalid yaw_deg")
            pitch = float(targs.get("pitch_deg", -30))
            hfov = float(targs.get("hfov_deg", 70))
            purpose = str(targs.get("purpose", ""))
            async with adapter_for(rs, scratch, point_idx=point_idx,
                                    lat=lat, lon=lon,
                                    candidate_pool=candidate_pool) as ad:
                pano_path = await _ensure_local_pano(
                    image_id, ad, mapillary_token, rs.run_dir
                )
                if pano_path is None:
                    return _err(f"pano unavailable for {image_id}")
                try:
                    equi = load_equirect(str(pano_path))
                    viewport_arr = render_view(equi, yaw, pitch, hfov)
                    composite = _composite_with_minimap(
                        viewport_arr, equi, ad, image_id, yaw, pitch, hfov,
                    )
                    out = (rs.run_dir / "viewports"
                            / f"{image_id}_y{int(yaw):+04d}_p{int(pitch):+03d}_h{int(hfov):03d}.jpg")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    composite.save(out, format="JPEG", quality=88, optimize=True)
                except Exception as e:
                    return _err(f"render: {e}")
                buf = io.BytesIO()
                composite.save(buf, format="JPEG", quality=88, optimize=True)
                b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
                state_delta = _record_and_pack_look(
                    ad, image_id, yaw, pitch, hfov, purpose, out, rs.run_dir,
                )
            caption = (f"Viewport from {image_id}: yaw={int(yaw)}° "
                       f"pitch={int(pitch)}° hfov={int(hfov)}° "
                       f"(purpose: {purpose}). Strip above viewport is the "
                       f"minimap with red rectangle showing your sample region.")
            return {
                "content": [
                    {"type": "text", "text": caption},
                    {"type": "image", "source": {"type": "base64",
                                                   "media_type": "image/jpeg",
                                                   "data": b64}},
                ],
                "summary": f"look {image_id} y{int(yaw)}p{int(pitch)}h{int(hfov)}",
                "is_error": False,
                "state_delta": state_delta,
                "side_effect": None,
            }

        if tname == "zoom_into_region":
            image_id = str(targs.get("image_id") or "").strip()
            if not image_id:
                return _err("missing image_id")
            try:
                src_yaw = float(targs["source_yaw_deg"])
                src_pitch = float(targs["source_pitch_deg"])
                src_hfov = float(targs["source_hfov_deg"])
                x1 = float(targs["x1"]); y1 = float(targs["y1"])
                x2 = float(targs["x2"]); y2 = float(targs["y2"])
            except (KeyError, TypeError, ValueError) as e:
                return _err(f"missing/invalid arg: {e}")
            purpose = str(targs.get("purpose", ""))
            if x2 <= x1 or y2 <= y1:
                return _err("invalid bbox: x2<=x1 or y2<=y1")
            if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
                return _err("bbox coords must be in [0,1]")

            SRC_OUT_W, SRC_OUT_H = 768, 512
            src_vfov = src_hfov * SRC_OUT_H / SRC_OUT_W
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            bbox_w_pct = x2 - x1
            new_yaw = src_yaw + (cx - 0.5) * src_hfov
            new_pitch = src_pitch - (cy - 0.5) * src_vfov
            new_hfov = src_hfov * bbox_w_pct
            new_pitch = max(-85.0, min(30.0, new_pitch))
            new_hfov = max(10.0, min(110.0, new_hfov))
            new_yaw = ((new_yaw + 180.0) % 360.0) - 180.0
            zoom_factor = src_hfov / new_hfov if new_hfov > 0 else 1.0

            async with adapter_for(rs, scratch, point_idx=point_idx,
                                    lat=lat, lon=lon,
                                    candidate_pool=candidate_pool) as ad:
                pano_path = await _ensure_local_pano(
                    image_id, ad, mapillary_token, rs.run_dir
                )
                if pano_path is None:
                    return _err(f"pano unavailable for {image_id}")
                try:
                    equi = load_equirect(str(pano_path))
                    viewport_arr = render_view(equi, new_yaw, new_pitch, new_hfov)
                    composite = _composite_with_minimap(
                        viewport_arr, equi, ad, image_id,
                        new_yaw, new_pitch, new_hfov,
                    )
                    out = (rs.run_dir / "viewports"
                            / f"{image_id}_ZOOM_y{int(new_yaw):+04d}_p{int(new_pitch):+03d}_h{int(new_hfov):03d}.jpg")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    composite.save(out, format="JPEG", quality=88, optimize=True)
                except Exception as e:
                    return _err(f"render: {e}")
                buf = io.BytesIO()
                composite.save(buf, format="JPEG", quality=88, optimize=True)
                b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
                state_delta = _record_and_pack_zoom(
                    ad, image_id, src_yaw, src_pitch, src_hfov,
                    x1, y1, x2, y2, new_yaw, new_pitch, new_hfov,
                    zoom_factor, purpose, out, rs.run_dir,
                )
            caption = (
                f"Zoom of {image_id}: bbox=({x1:.2f},{y1:.2f}->{x2:.2f},{y2:.2f}) "
                f"src yaw={int(src_yaw)}° pitch={int(src_pitch)}° hfov={int(src_hfov)}° "
                f"-> rendered yaw={int(new_yaw)}° pitch={int(new_pitch)}° "
                f"hfov={int(new_hfov)}° ({zoom_factor:.1f}x). Purpose: {purpose}."
            )
            return {
                "content": [
                    {"type": "text", "text": caption},
                    {"type": "image", "source": {"type": "base64",
                                                   "media_type": "image/jpeg",
                                                   "data": b64}},
                ],
                "summary": (f"zoom {image_id} y{int(new_yaw)}p{int(new_pitch)}"
                            f"h{int(new_hfov)} ({zoom_factor:.1f}x)"),
                "is_error": False,
                "state_delta": state_delta,
                "side_effect": None,
            }

        if tname == "read_sibling_claims":
            sibling = await pbb.get_sibling_claims(asking_year=year)
            text = json.dumps(sibling, indent=2, default=str)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": (f"sibling claims: "
                            f"{sibling.get('n_sibling_claims', 0)} from "
                            f"{len(sibling.get('sibling_claims_by_year', {}))} year(s)"),
                "is_error": False,
                "state_delta": {
                    "n_sibling_claims": sibling.get("n_sibling_claims", 0),
                    "sibling_yaws_per_year": sibling.get("sibling_yaws_per_year", {}),
                },
                "side_effect": None,
            }

        if tname == "post_claim":
            category = str(targs.get("category") or "note")
            content = str(targs.get("content") or "")[:400]
            image_ids = list(targs.get("image_ids") or [])
            yaw = targs.get("yaw_deg")
            try:
                yaw = int(yaw) if yaw is not None else None
            except (TypeError, ValueError):
                yaw = None
            confidence = targs.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence = None
            claim = await pbb.post_claim(
                investigator_id=investigator_id,
                year=year,
                category=category,
                content=content,
                image_ids=image_ids,
                yaw_deg=yaw,
                confidence=confidence,
            )
            await scratch.trace({
                "record_type": "blackboard_post",
                "blackboard": "point",
                "claim_id": claim["claim_id"],
                "category": category,
                "content": content,
                "yaw_deg": yaw,
                "confidence": confidence,
                "image_ids": image_ids,
            })
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "claim_id": claim["claim_id"],
                    "category": category,
                }, indent=2)}],
                "summary": (f"posted {category} claim "
                            f"yaw={yaw if yaw is not None else '-'}"),
                "is_error": False,
                "state_delta": {
                    "claim_id": claim["claim_id"],
                    "category": category,
                    "yaw_deg": yaw,
                },
                "side_effect": None,
            }

        if tname == "report_year_findings":
            try:
                rep_year = int(targs.get("year"))
            except (TypeError, ValueError):
                return _err("missing/invalid year")
            usable = bool(targs.get("usable"))
            summary = str(targs.get("summary") or "")
            distresses = list(targs.get("distresses") or [])
            treatments = list(targs.get("treatments") or [])
            yaws_covered = [int(y) for y in (targs.get("yaws_covered") or [])
                             if isinstance(y, (int, float))]
            best = str(targs.get("best_image_id") or "")
            await pbb.set_year_status(
                year=rep_year,
                investigator_id=investigator_id,
                state="completed",
                usable=usable,
                best_image_id=best or None,
                summary=summary,
                yaws_covered=yaws_covered,
                distresses=distresses,
                treatments=treatments,
            )
            report = {
                "year": rep_year, "usable": usable, "summary": summary,
                "distresses": distresses, "treatments": treatments,
                "yaws_covered": yaws_covered, "best_image_id": best,
            }
            final_report_holder.update(report)
            await scratch.trace({
                "record_type": "report_up",
                "from_agent": investigator_id,
                "to_agent": scratch.parent_agent_id or "",
                "report": report,
            })
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "reported": rep_year,
                    "yaws_covered": yaws_covered,
                }, indent=2)}],
                "summary": (f"reported year {rep_year} usable={usable} "
                            f"yaws={yaws_covered}"),
                "is_error": False,
                "state_delta": {
                    "report": report,
                },
                "side_effect": "report_year_findings",
            }

        return _err(f"unknown tool: {tname}")

    return dispatch


def _err(msg: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text",
                      "text": json.dumps({"ok": False, "error": msg})}],
        "summary": msg,
        "is_error": True,
        "state_delta": {"error": msg},
        "side_effect": None,
    }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

async def run_year_investigator(
    *,
    rs: RunState,
    parent_scratch: AgentScratch,
    point_idx: int,
    lat: float,
    lon: float,
    year: int,
    candidates: list[WaypointCandidate],
    pbb: PointBlackboard,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    captain_directive: str | None = None,
    surveyor_directive: str | None = None,
    focus_yaw: int | None = None,
    model: str = "claude-opus-4-7",
    max_turns: int = 8,
    max_tokens_per_turn: int = 2000,
) -> dict[str, Any]:
    """Run one year-investigator agent. Returns the final YearReport dict
    (year, usable, summary, distresses, treatments, yaws_covered, best_image_id)
    PLUS run-meta (stop_reason, turns_used, cost_usd, agent_id)."""

    investigator_id = rs.mint_investigator_id(point_idx, year)
    await rs.register_agent(
        agent_id=investigator_id,
        agent_role="investigator",
        parent_agent_id=parent_scratch.agent_id,
        point_idx=point_idx,
        year=year,
    )
    scratch = AgentScratch(
        agent_id=investigator_id,
        agent_role="investigator",
        parent_agent_id=parent_scratch.agent_id,
        point_idx=point_idx,
        year=year,
        run_state=rs,
    )

    # Mark the year status as "running" upfront
    await pbb.set_year_status(
        year=year, investigator_id=investigator_id, state="running",
    )

    seed = _build_seed(
        point_idx, lat, lon, year, candidates,
        captain_directive, surveyor_directive, focus_yaw,
    )

    final_report_holder: dict[str, Any] = {}
    dispatch = _make_dispatch(
        rs, aclient, mapillary_token,
        point_idx, lat, lon, year, candidates, pbb,
        investigator_id, final_report_holder,
    )

    system = compose_investigator_system()
    result = await run_agent_loop(
        rs=rs,
        scratch=scratch,
        aclient=aclient,
        model=model,
        system_prompt=system,
        tool_schemas=INVESTIGATOR_TOOL_SCHEMAS,
        seed_user_text=seed,
        dispatch_tool=dispatch,
        max_turns=max_turns,
        max_tokens_per_turn=max_tokens_per_turn,
        keep_last_n_images=4,
        terminal_side_effects=("report_year_findings",),
    )

    # If the investigator exited without reporting (turn cap, error), fill in
    # a "usable=false" placeholder so the surveyor doesn't deadlock.
    if not final_report_holder:
        final_report_holder.update({
            "year": year, "usable": False,
            "summary": (f"investigator stopped without reporting "
                         f"(stop_reason={result['stop_reason']})"),
            "distresses": [], "treatments": [], "yaws_covered": [],
            "best_image_id": "",
        })
        await pbb.set_year_status(
            year=year, investigator_id=investigator_id,
            state="failed", usable=False,
            summary=final_report_holder["summary"],
        )

    # Propagate sibling investigators' yaw findings into the parent's visit_log.
    # The surveyor needs ALL its children's yaws merged so the discipline gate
    # sees the complete picture.
    for image_id, entry in scratch.visit_log.items():
        parent_entry = parent_scratch.visit_log.setdefault(image_id, {
            "year": entry["year"], "peeked": False,
            "yaws_looked": set(), "yaws_zoomed": set(),
        })
        if parent_entry.get("year") is None:
            parent_entry["year"] = entry["year"]
        if entry.get("peeked"):
            parent_entry["peeked"] = True
        parent_entry["yaws_looked"] |= set(entry["yaws_looked"])
        parent_entry["yaws_zoomed"] |= set(entry["yaws_zoomed"])

    return {
        **final_report_holder,
        "investigator_id": investigator_id,
        "stop_reason": result["stop_reason"],
        "turns_used": result["turns_used"],
        "cost_usd": result["cost_usd"],
    }
