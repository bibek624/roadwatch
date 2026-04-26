"""Point Surveyor — middle of the 3-tier hierarchy.

One surveyor per survey point. Owns the discipline gate. Spawns Year
Investigators in parallel (max 2 concurrent), reads their reports from the
point blackboard, reconciles across years, grades.

Tool surface (8):
  get_point_brief, enumerate_candidates_by_year, dispatch_year_investigators,
  read_point_blackboard, request_more_evidence, cross_witness_check,
  grade, report_to_captain
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.blackboard import PointBlackboard, StreetBlackboard
from app.agent.hierarchy.primitives import (
    _check_temporal_discipline,
    _find_candidates_impl,
    adapter_for,
)
from app.agent.hierarchy.run_state import RunState
from app.agent.hierarchy.runner import run_agent_loop
from app.agent.hierarchy.skills import compose_surveyor_system
from app.agent.hierarchy.year_investigator import run_year_investigator
from app.agent.walker_state import Waypoint, WaypointCandidate


SURVEYOR_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_point_brief",
        "description": (
            "Return your assigned survey point: idx, lat/lon, captain's "
            "directive (if any), prior redo reason (if any). Free. Call "
            "ONCE at start to orient."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "enumerate_candidates_by_year",
        "description": (
            "Return Mapillary 360-pano candidates within radius_m, stratified "
            "by year. Free. Tells you which years have how many candidates so "
            "you can decide which to dispatch year-investigators for. Default "
            "radius 30m. Multi-year SHA-collision dedup is automatic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "radius_m": {"type": "number", "default": 50,
                              "minimum": 10, "maximum": 80},
            },
            "required": [],
        },
    },
    {
        "name": "dispatch_year_investigators",
        "description": (
            "Spawn 1-3 Year Investigators in PARALLEL — one per year. Each "
            "gets a slice of candidates filtered to its year. The call BLOCKS "
            "until all return. Their reports go into your point blackboard "
            "AND into structured YearReport objects in the tool result. Use "
            "focus_yaw to anchor cross-year reconciliation when you already "
            "know which yaw matters (e.g. from a sibling claim)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "years": {"type": "array", "items": {"type": "integer"},
                           "minItems": 1, "maxItems": 3},
                "focus_yaw": {"type": "integer", "minimum": -180, "maximum": 180,
                                "description": "optional anchor yaw for "
                                                "cross-year alignment"},
                "purpose": {"type": "string"},
            },
            "required": ["years", "purpose"],
        },
    },
    {
        "name": "read_point_blackboard",
        "description": (
            "Read the live point blackboard — your investigators' claims AND "
            "their per-year status. Free. Call between dispatch waves to see "
            "what they're finding."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "request_more_evidence",
        "description": (
            "Re-spawn ONE year-investigator for a year that already returned, "
            "with a tighter focus. Use when the first pass left a gap (e.g. "
            "need yaw=270 in 2016 to match yaw=270 in 2025 for a temporal "
            "comparison). Costs ~$1; budget-check first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "focus": {"type": "string"},
                "anchor_yaw": {"type": "integer", "minimum": -180, "maximum": 180},
            },
            "required": ["year", "focus"],
        },
    },
    {
        "name": "cross_witness_check",
        "description": (
            "Compare yaws covered between two completed year-investigators. "
            "Returns the yaw-overlap matrix and a verdict on whether a "
            "temporal comparison is well-grounded. Free."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year_a": {"type": "integer"},
                "year_b": {"type": "integer"},
            },
            "required": ["year_a", "year_b"],
        },
    },
    {
        "name": "grade",
        "description": (
            "Final tier grade for this survey point. Goes through the "
            "deterministic temporal-discipline gate (3 rules — see "
            "temporal_reconciliation skill). The gate inspects your "
            "investigators' merged visit_log; if rules fail, it REFUSES with "
            "a structured fix-the-gap message and you MUST address it before "
            "calling grade again. 2 strikes per point."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {"type": "string",
                          "enum": ["Good", "Fair", "Poor", "unknown"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "chosen_image_id": {"type": "string"},
                "evidence_image_ids": {"type": "array",
                                         "items": {"type": "string"}},
                "distresses_observed": {"type": "array",
                                         "items": {"type": "object"}},
                "treatments_observed": {"type": "array",
                                         "items": {"type": "object"}},
                "safety_flags": {"type": "array",
                                  "items": {"type": "string"}},
                "surroundings_notes": {"type": "array",
                                        "items": {"type": "string"}},
                "inconsistencies": {"type": "array",
                                     "items": {"type": "string"}},
            },
            "required": ["tier", "rationale"],
        },
    },
    {
        "name": "report_to_captain",
        "description": (
            "Final report to the captain. Implies done. narrative_for_street "
            "is <=300 chars and feeds the captain's corridor narrative — "
            "write it tight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {"type": "string",
                          "enum": ["Good", "Fair", "Poor", "unknown"]},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
                "evidence_image_ids": {"type": "array",
                                         "items": {"type": "string"}},
                "narrative_for_street": {"type": "string"},
            },
            "required": ["tier", "rationale", "narrative_for_street"],
        },
    },
]


@dataclass
class PointReport:
    point_idx: int
    surveyor_id: str
    tier: str
    confidence: float | None
    rationale: str
    evidence_image_ids: list[str]
    narrative_for_street: str
    distresses_observed: list[Any]
    treatments_observed: list[Any]
    safety_flags: list[str]
    surroundings_notes: list[str]
    inconsistencies: list[str]
    chosen_image_id: str
    cost_usd: float
    turns_used: int
    stop_reason: str
    completed_ms: int

    def to_summary(self) -> dict[str, Any]:
        return {
            "point_idx": self.point_idx,
            "surveyor_id": self.surveyor_id,
            "tier": self.tier,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "evidence_image_ids": self.evidence_image_ids,
            "narrative_for_street": self.narrative_for_street,
            "distresses_observed": self.distresses_observed,
            "treatments_observed": self.treatments_observed,
            "safety_flags": self.safety_flags,
            "surroundings_notes": self.surroundings_notes,
            "inconsistencies": self.inconsistencies,
            "chosen_image_id": self.chosen_image_id,
            "cost_usd": round(self.cost_usd, 5),
            "turns_used": self.turns_used,
            "stop_reason": self.stop_reason,
            "completed_ms": self.completed_ms,
        }


def _build_seed(
    point_idx: int, lat: float, lon: float,
    captain_directive: str | None,
    redo_reason: str | None,
    n_points_total: int,
) -> str:
    parts = [
        f"You are Point Surveyor for survey point {point_idx} of "
        f"{n_points_total} on this street (lat={lat:.6f}, lon={lon:.6f}).",
        "",
    ]
    if captain_directive:
        parts.append(f"**Captain directive:** {captain_directive}")
        parts.append("")
    if redo_reason:
        parts.append(f"**REDO ORDERED.** Reason: {redo_reason}")
        parts.append("Address this specifically before grading.")
        parts.append("")
    parts.extend([
        "Workflow:",
        "  1. get_point_brief (orient)",
        "  2. enumerate_candidates_by_year",
        "  3. dispatch_year_investigators on the latest year + 1-2 older years (PARALLEL)",
        "  4. read_point_blackboard to see what they posted",
        "  5. cross_witness_check between years to validate temporal claims",
        "  6. (optional) request_more_evidence for a year that left a gap",
        "  7. grade — discipline gate WILL refuse sloppy grades",
        "  8. report_to_captain",
        "",
        "You have 12 turns and a per-point budget of ~$2.50. Keep "
        "investigator dispatches tight: 2 years parallel is plenty for most "
        "points. Don't dispatch 3 years unless multi-year SHA-distinct "
        "imagery genuinely exists.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _make_dispatch(
    rs: RunState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    point_idx: int,
    lat: float,
    lon: float,
    captain_directive: str | None,
    redo_reason: str | None,
    pbb: PointBlackboard,
    surveyor_id: str,
    final_report_holder: dict[str, Any],
):
    # Per-surveyor cache: candidates by year (populated by enumerate_candidates_by_year)
    candidates_state: dict[str, Any] = {
        "all": [],            # list[WaypointCandidate]
        "by_year": {},        # {year: list[WaypointCandidate]}
        "total_per_year": {},
        "suppressed": [],
    }
    # Per-surveyor per-year investigator semaphore
    investigator_sem = rs.investigator_sem_factory()

    async def dispatch(tname: str, targs: dict[str, Any], scratch: AgentScratch
                       ) -> dict[str, Any]:
        if tname == "get_point_brief":
            brief = {
                "point_idx": point_idx, "lat": lat, "lon": lon,
                "street_name": rs.street.name,
                "n_points_total": len(rs.street.waypoints),
                "captain_directive": captain_directive,
                "redo_reason": redo_reason,
                "fleet_budget_used_usd": round(rs.budget_used_usd, 4),
                "fleet_budget_cap_usd": rs.budget_cap_usd,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(brief, indent=2)}],
                "summary": f"point {point_idx} brief",
                "is_error": False,
                "state_delta": {"point_idx": point_idx, "lat": lat, "lon": lon},
                "side_effect": None,
            }

        if tname == "enumerate_candidates_by_year":
            radius = float(targs.get("radius_m", 50) or 50)
            async with adapter_for(rs, scratch, point_idx=point_idx,
                                    lat=lat, lon=lon) as ad:
                cands, total_per_year, suppressed = await _find_candidates_impl(
                    ad, mapillary_token=mapillary_token,
                    radius_m=radius, max_age_years=12,
                    limit=30, min_per_year=6, year_filter=None,
                )
            candidates_state["all"] = list(cands)
            by_year: dict[int, list[WaypointCandidate]] = {}
            for c in cands:
                by_year.setdefault(c.year or 0, []).append(c)
            candidates_state["by_year"] = by_year
            candidates_state["total_per_year"] = dict(total_per_year)
            candidates_state["suppressed"] = list(suppressed)
            public = {
                "radius_m": radius,
                "n_total": len(cands),
                "by_year": {
                    str(y): [c.to_public() for c in by_year[y]]
                    for y in sorted(by_year, reverse=True)
                },
                "total_per_year_in_radius": {
                    str(y): n for y, n in total_per_year.items()
                },
                "suppressed_duplicates": suppressed,
            }
            year_summary = ", ".join(
                f"{y}:{len(by_year.get(y, []))}/{total_per_year.get(y, 0)}"
                for y in sorted(by_year, reverse=True)
            )
            return {
                "content": [{"type": "text",
                              "text": json.dumps(public, indent=2)}],
                "summary": (f"{len(cands)} cands in {radius:.0f}m: "
                            f"{year_summary}" +
                            (f" ({len(suppressed)} SHA-dupes suppressed)"
                             if suppressed else "")),
                "is_error": False,
                "state_delta": {
                    "n_total": len(cands),
                    "year_breakdown": {y: len(by_year.get(y, []))
                                         for y in by_year},
                    "total_per_year": dict(total_per_year),
                    "suppressed_duplicates": suppressed,
                },
                "side_effect": None,
            }

        if tname == "dispatch_year_investigators":
            years = list(targs.get("years") or [])
            try:
                years = [int(y) for y in years]
            except (TypeError, ValueError):
                return _err("invalid years")
            if not years:
                return _err("missing years")
            if len(years) > 3:
                years = years[:3]
            focus_yaw = targs.get("focus_yaw")
            try:
                focus_yaw = int(focus_yaw) if focus_yaw is not None else None
            except (TypeError, ValueError):
                focus_yaw = None
            purpose = str(targs.get("purpose", ""))[:300]

            if not candidates_state["all"]:
                return _err(
                    "no candidates enumerated yet — call "
                    "enumerate_candidates_by_year first."
                )

            # Prepare per-year candidate slices
            slices: dict[int, list[WaypointCandidate]] = {}
            for y in years:
                slices[y] = list(candidates_state["by_year"].get(y, []))
            # Skip dispatch for empty slices but still report them
            empty_years = [y for y, lst in slices.items() if not lst]
            dispatch_years = [y for y, lst in slices.items() if lst]

            await scratch.trace({
                "record_type": "dispatch_order",
                "from_agent": surveyor_id,
                "dispatch_kind": "year_investigators",
                "years": dispatch_years,
                "focus_yaw": focus_yaw,
                "purpose": purpose,
                "empty_years": empty_years,
            })

            # Run them in parallel under the surveyor's investigator semaphore
            async def run_one(y: int) -> dict[str, Any]:
                async with investigator_sem:
                    return await run_year_investigator(
                        rs=rs,
                        parent_scratch=scratch,
                        point_idx=point_idx, lat=lat, lon=lon,
                        year=y, candidates=slices[y],
                        pbb=pbb, aclient=aclient,
                        mapillary_token=mapillary_token,
                        captain_directive=captain_directive,
                        surveyor_directive=purpose or None,
                        focus_yaw=focus_yaw,
                    )

            reports: list[dict[str, Any]] = []
            if dispatch_years:
                results = await asyncio.gather(
                    *[run_one(y) for y in dispatch_years],
                    return_exceptions=True,
                )
                for y, r in zip(dispatch_years, results):
                    if isinstance(r, Exception):
                        await scratch.trace({
                            "record_type": "system_note",
                            "note": f"investigator y{y} crashed: {r}",
                        })
                        reports.append({
                            "year": y, "usable": False,
                            "summary": f"investigator crashed: {r}",
                            "distresses": [], "treatments": [],
                            "yaws_covered": [], "best_image_id": "",
                            "stop_reason": "exception",
                        })
                    else:
                        reports.append(r)

            for y in empty_years:
                reports.append({
                    "year": y, "usable": False,
                    "summary": "no candidates in this year",
                    "distresses": [], "treatments": [],
                    "yaws_covered": [], "best_image_id": "",
                })

            # After investigators complete, the merged visit_log is already
            # in scratch.visit_log (run_year_investigator merged it).

            text = "Year investigators reported:\n\n" + json.dumps({
                "reports": reports,
            }, indent=2, default=str)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": (f"dispatched {len(dispatch_years)} investigator(s) "
                            f"for years {dispatch_years}; "
                            f"{sum(1 for r in reports if r.get('usable'))} "
                            f"reported usable"),
                "is_error": False,
                "state_delta": {
                    "dispatched_years": dispatch_years,
                    "n_reports": len(reports),
                    "n_usable": sum(1 for r in reports if r.get("usable")),
                },
                "side_effect": None,
            }

        if tname == "read_point_blackboard":
            snap = await pbb.snapshot()
            return {
                "content": [{"type": "text",
                              "text": json.dumps(snap, indent=2, default=str)}],
                "summary": (f"blackboard: "
                            f"{sum(len(v) for v in snap.get('claims_by_year', {}).values())} "
                            f"claims across "
                            f"{len(snap.get('claims_by_year', {}))} year(s)"),
                "is_error": False,
                "state_delta": {
                    "n_claims": sum(len(v) for v in snap.get(
                        "claims_by_year", {}).values()),
                    "n_years_with_status": len(snap.get("status_by_year", {})),
                },
                "side_effect": None,
            }

        if tname == "request_more_evidence":
            try:
                year = int(targs.get("year"))
            except (TypeError, ValueError):
                return _err("invalid year")
            focus = str(targs.get("focus") or "")
            anchor_yaw = targs.get("anchor_yaw")
            try:
                anchor_yaw = int(anchor_yaw) if anchor_yaw is not None else None
            except (TypeError, ValueError):
                anchor_yaw = None
            slc = candidates_state["by_year"].get(year, [])
            if not slc:
                return _err(f"no candidates available for year {year}")
            # Budget gate: refuse if budget >85% used
            if rs.budget_pct_used >= 0.85:
                return _err(
                    f"budget at {rs.budget_pct_used*100:.0f}%; cannot dispatch "
                    f"more investigators. Grade with what you have."
                )
            await pbb.append_request({
                "from": surveyor_id, "year": year, "focus": focus,
                "anchor_yaw": anchor_yaw, "ts_ms": int(time.time() * 1000),
            })
            await scratch.trace({
                "record_type": "dispatch_order",
                "from_agent": surveyor_id,
                "dispatch_kind": "year_investigator_redo",
                "years": [year],
                "focus_yaw": anchor_yaw,
                "purpose": focus,
            })
            async with investigator_sem:
                report = await run_year_investigator(
                    rs=rs, parent_scratch=scratch,
                    point_idx=point_idx, lat=lat, lon=lon,
                    year=year, candidates=slc,
                    pbb=pbb, aclient=aclient,
                    mapillary_token=mapillary_token,
                    captain_directive=captain_directive,
                    surveyor_directive=focus,
                    focus_yaw=anchor_yaw,
                )
            return {
                "content": [{"type": "text",
                              "text": json.dumps(report, indent=2, default=str)}],
                "summary": (f"re-dispatched y{year}; "
                            f"usable={report.get('usable')}"),
                "is_error": False,
                "state_delta": {
                    "year": year,
                    "usable": report.get("usable"),
                    "yaws_covered": report.get("yaws_covered"),
                },
                "side_effect": None,
            }

        if tname == "cross_witness_check":
            try:
                ya = int(targs.get("year_a"))
                yb = int(targs.get("year_b"))
            except (TypeError, ValueError):
                return _err("invalid year_a/year_b")
            visits = scratch.visits_summary()
            yaws_a = set(visits.get("yaws_per_year", {}).get(ya, []))
            yaws_b = set(visits.get("yaws_per_year", {}).get(yb, []))
            # Bin to nearest 30° (same as discipline gate Rule B)
            bins_a = {y // 30 for y in yaws_a}
            bins_b = {y // 30 for y in yaws_b}
            overlap = sorted(bins_a & bins_b)
            verdict = (
                "OK — yaws overlap, temporal comparison is grounded."
                if overlap else
                "MISMATCH — no overlapping yaws. Call request_more_evidence "
                f"on year={yb} with anchor_yaw matching one of "
                f"{sorted(yaws_a)} from year {ya}."
            )
            text = json.dumps({
                "year_a": ya, "year_b": yb,
                "yaws_a": sorted(yaws_a), "yaws_b": sorted(yaws_b),
                "yaw_bins_a_30deg": sorted(bins_a),
                "yaw_bins_b_30deg": sorted(bins_b),
                "overlap_bins": overlap,
                "verdict": verdict,
            }, indent=2)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": (f"cross-witness {ya} vs {yb}: "
                            f"{'OK' if overlap else 'MISMATCH'}"),
                "is_error": False,
                "state_delta": {
                    "year_a": ya, "year_b": yb,
                    "overlap_bins": overlap,
                    "ok": bool(overlap),
                },
                "side_effect": None,
            }

        if tname == "grade":
            tier = str(targs.get("tier", "")).strip()
            if tier in {"Satisfactory"}:
                tier = "Good"
            if tier in {"Failed"}:
                tier = "Poor"
            valid = {"Good", "Fair", "Poor", "unknown"}
            if tier not in valid:
                return _err(f"invalid tier {tier!r}; valid: {sorted(valid)}")
            try:
                confidence = float(targs.get("confidence")) \
                    if targs.get("confidence") is not None else None
            except (TypeError, ValueError):
                confidence = None
            rationale = str(targs.get("rationale") or "").strip()
            chosen = str(targs.get("chosen_image_id") or "").strip()
            evidence_image_ids = list(targs.get("evidence_image_ids") or [])
            distresses = list(targs.get("distresses_observed") or [])
            treatments = list(targs.get("treatments_observed") or [])
            safety_flags = list(targs.get("safety_flags") or [])
            surroundings = list(targs.get("surroundings_notes") or [])
            inconsistencies = list(targs.get("inconsistencies") or [])

            # Discipline gate (skipped for unknown — honest no-evidence call)
            if tier != "unknown":
                async with adapter_for(rs, scratch, point_idx=point_idx,
                                        lat=lat, lon=lon) as ad:
                    # Adapter exposes candidates_by_idx[point_idx] — we need to
                    # populate it from candidates_state["all"] so the gate can
                    # see the available years.
                    ad.cache_candidates(point_idx, candidates_state["all"])
                    ok, reason = _check_temporal_discipline(
                        ad, point_idx, rationale,
                    )
                if not ok:
                    scratch.discipline_gate_strikes += 1
                    vsum = scratch.visits_summary()
                    return {
                        "content": [{"type": "text", "text": (
                            reason + "\n\nVisit log so far:\n" +
                            json.dumps({
                                "by_year": {str(y): vs for y, vs in
                                             vsum["by_year"].items()},
                                "yaws_per_year": {str(y): yws for y, yws in
                                                   vsum["yaws_per_year"].items()},
                            }, indent=2, default=str)
                        )}],
                        "summary": (f"grade refused — discipline gate "
                                    f"(strike {scratch.discipline_gate_strikes}/2)"),
                        "is_error": True,
                        "state_delta": {
                            "discipline_gate_rejection": True,
                            "strike": scratch.discipline_gate_strikes,
                            "reason": reason[:300],
                        },
                        "side_effect": None,
                    }

            # Pass: stash for use by report_to_captain
            final_report_holder.update({
                "tier": tier, "confidence": confidence,
                "rationale": rationale, "chosen_image_id": chosen,
                "evidence_image_ids": evidence_image_ids,
                "distresses_observed": distresses,
                "treatments_observed": treatments,
                "safety_flags": safety_flags,
                "surroundings_notes": surroundings,
                "inconsistencies": inconsistencies,
            })
            await pbb.set_final_grade({
                "tier": tier, "confidence": confidence,
                "rationale": rationale, "chosen_image_id": chosen,
                "evidence_image_ids": evidence_image_ids,
                "ts_ms": int(time.time() * 1000),
            })
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "graded": tier,
                    "next": "call report_to_captain to finalize",
                }, indent=2)}],
                "summary": f"graded {tier}",
                "is_error": False,
                "state_delta": {
                    "waypoint_idx": point_idx,
                    "predicted_tier": tier,
                    "tier": tier,
                    "confidence": confidence,
                    "rationale": rationale,
                    "chosen_image_id": chosen,
                    "evidence_image_ids": evidence_image_ids,
                    "distresses_observed": distresses,
                    "treatments_observed": treatments,
                    "safety_flags": safety_flags,
                    "surroundings_notes": surroundings,
                    "inconsistencies": inconsistencies,
                    "n_evidence": len(evidence_image_ids),
                    "n_distresses": len(distresses),
                    "n_treatments": len(treatments),
                    "lat": lat, "lon": lon,
                },
                "side_effect": None,
            }

        if tname == "report_to_captain":
            tier = str(targs.get("tier", "")).strip()
            if tier in {"Satisfactory"}:
                tier = "Good"
            if tier in {"Failed"}:
                tier = "Poor"
            try:
                confidence = float(targs.get("confidence")) \
                    if targs.get("confidence") is not None else None
            except (TypeError, ValueError):
                confidence = None
            rationale = str(targs.get("rationale") or "").strip()
            evidence_image_ids = list(targs.get("evidence_image_ids") or [])
            narrative = str(targs.get("narrative_for_street") or "").strip()[:500]

            # Use cached grade if available, else fall back to the final-report args
            stash = dict(final_report_holder)
            stash.update({
                "tier": tier or stash.get("tier", "unknown"),
                "confidence": confidence if confidence is not None
                              else stash.get("confidence"),
                "rationale": rationale or stash.get("rationale", ""),
                "evidence_image_ids": (evidence_image_ids
                                        or stash.get("evidence_image_ids", [])),
                "narrative_for_street": narrative,
            })
            stash.setdefault("chosen_image_id",
                              stash.get("chosen_image_id", ""))
            stash.setdefault("distresses_observed", [])
            stash.setdefault("treatments_observed", [])
            stash.setdefault("safety_flags", [])
            stash.setdefault("surroundings_notes", [])
            stash.setdefault("inconsistencies", [])
            final_report_holder.update(stash)

            await scratch.trace({
                "record_type": "report_up",
                "from_agent": surveyor_id,
                "to_agent": "captain",
                "report": {
                    "point_idx": point_idx,
                    "tier": stash["tier"],
                    "confidence": stash["confidence"],
                    "narrative_for_street": narrative,
                },
            })
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "reported_tier": stash["tier"],
                    "to": "captain",
                }, indent=2)}],
                "summary": f"reported to captain: {stash['tier']}",
                "is_error": False,
                "state_delta": {
                    "tier": stash["tier"],
                    "confidence": stash["confidence"],
                    "narrative_for_street": narrative,
                },
                "side_effect": "report_to_captain",
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

async def run_point_surveyor(
    *,
    rs: RunState,
    parent_scratch: AgentScratch,
    point_idx: int,
    lat: float,
    lon: float,
    captain_directive: str | None,
    redo_reason: str | None,
    pbb: PointBlackboard,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    model: str = "claude-opus-4-7",
    max_turns: int = 12,
    max_tokens_per_turn: int = 1500,
) -> PointReport:

    surveyor_id = rs.mint_surveyor_id(point_idx)
    await rs.register_agent(
        agent_id=surveyor_id,
        agent_role="surveyor",
        parent_agent_id=parent_scratch.agent_id,
        point_idx=point_idx,
    )
    await pbb.set_surveyor(surveyor_id, captain_directive)
    if redo_reason:
        await pbb.add_redo(by="captain", reason=redo_reason, focus_year=None)

    scratch = AgentScratch(
        agent_id=surveyor_id,
        agent_role="surveyor",
        parent_agent_id=parent_scratch.agent_id,
        point_idx=point_idx,
        year=None,
        run_state=rs,
    )
    seed = _build_seed(
        point_idx, lat, lon, captain_directive, redo_reason,
        len(rs.street.waypoints),
    )

    final_report_holder: dict[str, Any] = {}
    dispatch = _make_dispatch(
        rs, aclient, mapillary_token,
        point_idx, lat, lon,
        captain_directive, redo_reason,
        pbb, surveyor_id, final_report_holder,
    )

    system = compose_surveyor_system()
    result = await run_agent_loop(
        rs=rs,
        scratch=scratch,
        aclient=aclient,
        model=model,
        system_prompt=system,
        tool_schemas=SURVEYOR_TOOL_SCHEMAS,
        seed_user_text=seed,
        dispatch_tool=dispatch,
        max_turns=max_turns,
        max_tokens_per_turn=max_tokens_per_turn,
        keep_last_n_images=4,
        terminal_side_effects=("report_to_captain",),
    )

    # Build PointReport from final_report_holder; fill defaults for the
    # graceful-stop case (no report).
    report = PointReport(
        point_idx=point_idx,
        surveyor_id=surveyor_id,
        tier=final_report_holder.get("tier", "unknown"),
        confidence=final_report_holder.get("confidence"),
        rationale=final_report_holder.get(
            "rationale",
            (f"surveyor stopped without grading "
              f"(stop_reason={result['stop_reason']})"),
        ),
        evidence_image_ids=list(final_report_holder.get("evidence_image_ids", [])),
        narrative_for_street=final_report_holder.get(
            "narrative_for_street", ""
        )[:500],
        distresses_observed=list(final_report_holder.get("distresses_observed", [])),
        treatments_observed=list(final_report_holder.get("treatments_observed", [])),
        safety_flags=list(final_report_holder.get("safety_flags", [])),
        surroundings_notes=list(final_report_holder.get("surroundings_notes", [])),
        inconsistencies=list(final_report_holder.get("inconsistencies", [])),
        chosen_image_id=final_report_holder.get("chosen_image_id", ""),
        cost_usd=scratch.cost_usd_local,
        turns_used=result["turns_used"],
        stop_reason=result["stop_reason"],
        completed_ms=int(time.time() * 1000),
    )
    return report
