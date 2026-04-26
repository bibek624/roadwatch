"""Street Captain — top of the 3-tier hierarchy.

One captain per run. Plans batches, dispatches Point Surveyors in parallel
(max 3 concurrent), reads the street blackboard, can issue redo orders,
writes the corridor narrative.

Tool surface (8):
  read_street_blackboard, read_point_blackboard, plan_dispatch_batches,
  dispatch_surveyors, request_redo, cross_check_claim, finalize_street, done
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import anthropic

from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.blackboard import PointBlackboard, StreetBlackboard
from app.agent.hierarchy.point_surveyor import (
    PointReport,
    run_point_surveyor,
)
from app.agent.hierarchy.run_state import RunState
from app.agent.hierarchy.runner import run_agent_loop
from app.agent.hierarchy.skills import compose_captain_system


CAPTAIN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_street_blackboard",
        "description": (
            "Return the current street blackboard: point_summaries (one per "
            "completed surveyor), flagged_inconsistencies, dispatch_log, "
            "fleet_budget. Free. Use to plan the next batch."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_point_blackboard",
        "description": (
            "Drill into ONE point's blackboard for its raw evidence "
            "(claims_by_year, status_by_year, redo_history). Use when a "
            "surveyor's report seems suspicious and you want raw evidence "
            "before issuing a redo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"point_idx": {"type": "integer"}},
            "required": ["point_idx"],
        },
    },
    {
        "name": "plan_dispatch_batches",
        "description": (
            "Dry-run: returns a proposed batching of REMAINING points into "
            "waves of up to 3 concurrent surveyors. Free. Helps you reason "
            "about ordering before committing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_size": {"type": "integer", "default": 3,
                                "minimum": 1, "maximum": 3},
            },
            "required": [],
        },
    },
    {
        "name": "dispatch_surveyors",
        "description": (
            "Spawn Point Surveyors in PARALLEL for the listed survey-point "
            "indices (max 3). Blocks until ALL spawned surveyors finish. "
            "Each surveyor receives your `directive` text in its seed — use "
            "it to pass cross-point context (e.g. 'point 2 graded Fair due "
            "to alligator at yaw 180; check continuity')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "point_ids": {"type": "array", "items": {"type": "integer"},
                                "minItems": 1, "maxItems": 3},
                "directive": {"type": "string",
                                "description": "<=300 chars context for each "
                                                "surveyor"},
            },
            "required": ["point_ids", "directive"],
        },
    },
    {
        "name": "request_redo",
        "description": (
            "Mark a completed point for REDO with a specific reason. The "
            "point's surveyor will be respawned with your `reason` injected "
            "into its seed and the prior point blackboard preserved (so the "
            "new surveyor sees what already happened)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "point_idx": {"type": "integer"},
                "reason": {"type": "string"},
                "focus_year": {"type": "integer"},
            },
            "required": ["point_idx", "reason"],
        },
    },
    {
        "name": "cross_check_claim",
        "description": (
            "Inspect evidence behind a claim across two points. Returns the "
            "matching claims/yaws from each point's blackboard so you can "
            "adjudicate a suspected inconsistency."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "point_a": {"type": "integer"},
                "point_b": {"type": "integer"},
            },
            "required": ["claim", "point_a", "point_b"],
        },
    },
    {
        "name": "finalize_street",
        "description": (
            "Write the corridor narrative + tier distribution. After this, "
            "no more dispatches. Call `done` next."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "narrative": {"type": "string",
                                "description": "2-4 paragraph corridor "
                                                "synthesis (markdown OK)"},
                "tier_distribution": {"type": "object",
                                        "description": "e.g. {Good:1,Fair:3}"},
            },
            "required": ["narrative"],
        },
    },
    {
        "name": "done",
        "description": "Wrap up. Use only after finalize_street.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------

def _build_seed(
    rs: RunState,
    sbb_summary: dict[str, Any],
    n_surveyor_slots: int,
    n_investigator_slots: int,
) -> str:
    wp_lines = "\n".join(
        f"  - point {wp.idx}: lat={wp.lat:.6f}, lon={wp.lon:.6f}, "
        f"distance_along={wp.distance_along_street_m:.1f}m"
        for wp in rs.street.waypoints
    )
    return (
        f"You are STREET CAPTAIN for **{rs.street.name}** "
        f"({len(rs.street.waypoints)} survey points, "
        f"{rs.street.length_m:.0f}m corridor).\n\n"
        f"Survey points (in geographic order along the centerline):\n"
        f"{wp_lines}\n\n"
        f"Concurrency caps: up to {n_surveyor_slots} Point Surveyors in "
        f"parallel per dispatch wave. Each surveyor in turn spawns up to "
        f"{n_investigator_slots} Year Investigators in parallel — peak "
        f"concurrent activity is ~{n_surveyor_slots*n_investigator_slots} "
        f"Opus calls.\n\n"
        f"Fleet budget cap: ${sbb_summary['fleet_budget_cap_usd']:.2f}. "
        f"Currently used: ${sbb_summary['fleet_budget_used_usd']:.2f}.\n\n"
        f"Workflow:\n"
        f"  1. plan_dispatch_batches (dry-run) to think about ordering.\n"
        f"  2. dispatch_surveyors([0, 1, 2], directive='...') — first wave.\n"
        f"  3. read_street_blackboard — see what came back.\n"
        f"  4. (optional) request_redo on any point that smells wrong.\n"
        f"  5. dispatch_surveyors([3, 4, 5], ...) — next wave.\n"
        f"  6. ...repeat until all points reported.\n"
        f"  7. finalize_street(narrative=..., tier_distribution=...).\n"
        f"  8. done.\n\n"
        f"You have 60 turns total. Keep dispatches geographic (lowest idx "
        f"first) unless you have a reason. Don't redo unless evidence "
        f"specifically contradicts neighboring points."
    )


def _make_dispatch(
    rs: RunState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    sbb: StreetBlackboard,
    point_blackboards: dict[int, PointBlackboard],
    captain_id: str,
):
    # Track which point indices have been dispatched / completed
    completed: set[int] = set()

    async def _ensure_pbb(idx: int) -> PointBlackboard:
        if idx not in point_blackboards:
            wp = rs.street.waypoints[idx]
            point_blackboards[idx] = PointBlackboard(
                rs.run_dir, idx, wp.lat, wp.lon
            )
        return point_blackboards[idx]

    async def dispatch(tname: str, targs: dict[str, Any], scratch: AgentScratch
                       ) -> dict[str, Any]:
        if tname == "read_street_blackboard":
            await sbb.update_budget(rs.budget_used_usd)
            snap = await sbb.get_summary_for_captain()
            return {
                "content": [{"type": "text",
                              "text": json.dumps(snap, indent=2, default=str)}],
                "summary": (f"street bb: "
                            f"{snap['n_points_completed']}/"
                            f"{snap['n_points_total']} pts; "
                            f"${snap['fleet_budget_used_usd']:.2f}/"
                            f"${snap['fleet_budget_cap_usd']:.2f}"),
                "is_error": False,
                "state_delta": {
                    "n_completed": snap["n_points_completed"],
                    "n_total": snap["n_points_total"],
                    "fleet_budget_used_usd": snap["fleet_budget_used_usd"],
                    "n_inconsistencies": len(snap["flagged_inconsistencies"]),
                },
                "side_effect": None,
            }

        if tname == "read_point_blackboard":
            try:
                pidx = int(targs.get("point_idx"))
            except (TypeError, ValueError):
                return _err("missing/invalid point_idx")
            pbb = point_blackboards.get(pidx)
            if pbb is None:
                return _err(f"no blackboard for point {pidx} (not yet dispatched)")
            snap = await pbb.snapshot()
            return {
                "content": [{"type": "text",
                              "text": json.dumps(snap, indent=2, default=str)}],
                "summary": (f"point {pidx} bb: "
                            f"{sum(len(v) for v in snap.get('claims_by_year', {}).values())} "
                            f"claims; final_grade="
                            f"{(snap.get('final_grade') or {}).get('tier', '?')}"),
                "is_error": False,
                "state_delta": {
                    "point_idx": pidx,
                    "has_grade": snap.get("final_grade") is not None,
                },
                "side_effect": None,
            }

        if tname == "plan_dispatch_batches":
            batch_size = int(targs.get("batch_size", 3))
            batch_size = max(1, min(3, batch_size))
            remaining = [
                wp.idx for wp in rs.street.waypoints
                if wp.idx not in completed
            ]
            waves: list[list[int]] = []
            for i in range(0, len(remaining), batch_size):
                waves.append(remaining[i:i + batch_size])
            text = json.dumps({
                "batch_size": batch_size,
                "remaining_points": remaining,
                "n_waves": len(waves),
                "proposed_waves": waves,
                "fleet_budget_remaining_usd": round(
                    rs.budget_remaining_usd, 4
                ),
            }, indent=2)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": (f"plan: {len(waves)} waves of <={batch_size} for "
                            f"{len(remaining)} remaining"),
                "is_error": False,
                "state_delta": {
                    "n_waves": len(waves),
                    "n_remaining": len(remaining),
                    "proposed_waves": waves,
                },
                "side_effect": None,
            }

        if tname == "dispatch_surveyors":
            point_ids = list(targs.get("point_ids") or [])
            try:
                point_ids = [int(p) for p in point_ids]
            except (TypeError, ValueError):
                return _err("invalid point_ids")
            if not point_ids:
                return _err("missing point_ids")
            if len(point_ids) > 3:
                point_ids = point_ids[:3]
            # Filter to valid + not-yet-completed
            n_total = len(rs.street.waypoints)
            point_ids = [p for p in point_ids if 0 <= p < n_total]
            new_dispatch = [p for p in point_ids if p not in completed]
            already_done = [p for p in point_ids if p in completed]
            if not new_dispatch:
                return _err(
                    f"all listed points already completed: {already_done}. "
                    f"Use request_redo if you want to re-do one."
                )
            directive = str(targs.get("directive") or "").strip()[:600]
            wave_idx = await sbb.append_dispatch(new_dispatch, directive)
            await scratch.trace({
                "record_type": "dispatch_order",
                "from_agent": captain_id,
                "dispatch_kind": "surveyors",
                "wave": wave_idx,
                "point_ids": new_dispatch,
                "directive": directive,
            })

            async def run_one(pidx: int) -> PointReport | Exception:
                async with rs.surveyor_sem:
                    pbb = await _ensure_pbb(pidx)
                    wp = rs.street.waypoints[pidx]
                    try:
                        return await run_point_surveyor(
                            rs=rs, parent_scratch=scratch,
                            point_idx=pidx, lat=wp.lat, lon=wp.lon,
                            captain_directive=directive or None,
                            redo_reason=None,
                            pbb=pbb, aclient=aclient,
                            mapillary_token=mapillary_token,
                        )
                    except Exception as e:
                        return e

            results = await asyncio.gather(*[run_one(p) for p in new_dispatch])
            summaries: list[dict[str, Any]] = []
            for pidx, r in zip(new_dispatch, results):
                if isinstance(r, Exception):
                    await scratch.trace({
                        "record_type": "system_note",
                        "note": f"surveyor wp{pidx} crashed: {r}",
                    })
                    summaries.append({
                        "point_idx": pidx,
                        "tier": "unknown",
                        "rationale": f"surveyor crashed: {r}",
                        "narrative_for_street": "",
                    })
                    completed.add(pidx)
                    await sbb.append_point_summary({
                        "point_idx": pidx,
                        "surveyor_id": f"S?-WP{pidx}",
                        "tier": "unknown",
                        "confidence": None,
                        "rationale": f"surveyor crashed: {r}",
                        "narrative_for_street": "",
                        "completed_ms": int(time.time() * 1000),
                    })
                else:
                    summary = r.to_summary()
                    summaries.append(summary)
                    completed.add(pidx)
                    await sbb.append_point_summary(summary)

            text = json.dumps({
                "wave": wave_idx,
                "dispatched": new_dispatch,
                "already_done": already_done,
                "summaries": summaries,
            }, indent=2, default=str)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": (f"wave {wave_idx}: "
                            f"{len(new_dispatch)} surveyors completed; "
                            f"tiers={[s.get('tier') for s in summaries]}"),
                "is_error": False,
                "state_delta": {
                    "wave": wave_idx,
                    "point_ids": new_dispatch,
                    "tiers": [s.get("tier") for s in summaries],
                },
                "side_effect": None,
            }

        if tname == "request_redo":
            try:
                pidx = int(targs.get("point_idx"))
            except (TypeError, ValueError):
                return _err("invalid point_idx")
            reason = str(targs.get("reason") or "").strip()[:300]
            focus_year = targs.get("focus_year")
            try:
                focus_year = int(focus_year) if focus_year is not None else None
            except (TypeError, ValueError):
                focus_year = None
            if not reason:
                return _err("reason required for request_redo")
            if pidx not in completed:
                return _err(f"point {pidx} hasn't completed yet — can't redo")
            if rs.budget_pct_used >= 0.90:
                return _err(
                    f"budget at {rs.budget_pct_used*100:.0f}%; redo "
                    f"refused. Finalize the street with what you have."
                )
            await sbb.append_redo(pidx, reason, focus_year)
            await scratch.trace({
                "record_type": "redo_order_issued",
                "from_agent": captain_id,
                "point_idx": pidx,
                "reason": reason,
                "focus_year": focus_year,
            })
            pbb = await _ensure_pbb(pidx)
            wp = rs.street.waypoints[pidx]
            async with rs.surveyor_sem:
                try:
                    report = await run_point_surveyor(
                        rs=rs, parent_scratch=scratch,
                        point_idx=pidx, lat=wp.lat, lon=wp.lon,
                        captain_directive=None,
                        redo_reason=reason,
                        pbb=pbb, aclient=aclient,
                        mapillary_token=mapillary_token,
                    )
                    summary = report.to_summary()
                    await sbb.append_point_summary(summary)
                except Exception as e:
                    summary = {
                        "point_idx": pidx,
                        "surveyor_id": f"S?-WP{pidx}",
                        "tier": "unknown",
                        "rationale": f"redo crashed: {e}",
                        "narrative_for_street": "",
                    }
                    await sbb.append_point_summary(summary)
            return {
                "content": [{"type": "text",
                              "text": json.dumps(summary, indent=2, default=str)}],
                "summary": (f"redo wp{pidx}: "
                            f"new tier={summary.get('tier')}"),
                "is_error": False,
                "state_delta": {
                    "point_idx": pidx,
                    "tier": summary.get("tier"),
                    "redone": True,
                },
                "side_effect": None,
            }

        if tname == "cross_check_claim":
            claim = str(targs.get("claim") or "")
            try:
                pa = int(targs.get("point_a"))
                pb = int(targs.get("point_b"))
            except (TypeError, ValueError):
                return _err("invalid point_a/point_b")
            pba = point_blackboards.get(pa)
            pbb_b = point_blackboards.get(pb)
            if pba is None or pbb_b is None:
                return _err(
                    f"both points must have a blackboard; have "
                    f"{list(point_blackboards.keys())}"
                )
            sa = await pba.snapshot()
            sb = await pbb_b.snapshot()
            text = json.dumps({
                "claim": claim,
                "point_a": pa,
                "point_a_claims_by_year": sa.get("claims_by_year", {}),
                "point_a_status_by_year": sa.get("status_by_year", {}),
                "point_a_grade": sa.get("final_grade"),
                "point_b": pb,
                "point_b_claims_by_year": sb.get("claims_by_year", {}),
                "point_b_status_by_year": sb.get("status_by_year", {}),
                "point_b_grade": sb.get("final_grade"),
            }, indent=2, default=str)
            return {
                "content": [{"type": "text", "text": text}],
                "summary": f"cross-check {claim[:40]}",
                "is_error": False,
                "state_delta": {
                    "claim": claim, "point_a": pa, "point_b": pb,
                },
                "side_effect": None,
            }

        if tname == "finalize_street":
            narrative = str(targs.get("narrative") or "").strip()
            tier_dist = targs.get("tier_distribution") or {}
            await sbb.set_narrative(narrative)
            (rs.run_dir / "summary.md").write_text(
                f"# {rs.street.name} — corridor synthesis\n\n"
                f"**Captain:** {captain_id}\n"
                f"**Points completed:** {len(completed)} of {len(rs.street.waypoints)}\n"
                f"**Tier distribution:** {tier_dist}\n"
                f"**Fleet cost:** ${rs.budget_used_usd:.2f} of ${rs.budget_cap_usd:.2f}\n\n"
                f"---\n\n{narrative}\n",
                encoding="utf-8",
            )
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "tier_distribution": tier_dist,
                    "narrative_chars": len(narrative),
                }, indent=2, default=str)}],
                "summary": (f"finalized: "
                            f"narrative {len(narrative)} chars; "
                            f"tier_dist={tier_dist}"),
                "is_error": False,
                "state_delta": {
                    "narrative_chars": len(narrative),
                    "tier_distribution": tier_dist,
                    "n_completed": len(completed),
                },
                "side_effect": None,
            }

        if tname == "done":
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": True, "stop_reason": "captain_done",
                }, indent=2)}],
                "summary": "captain done",
                "is_error": False,
                "state_delta": {"stop_reason": "captain_done"},
                "side_effect": "done",
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

async def run_captain(
    *,
    rs: RunState,
    aclient: anthropic.AsyncAnthropic,
    mapillary_token: str,
    model: str = "claude-opus-4-7",
    max_turns: int = 60,
    max_tokens_per_turn: int = 2500,
) -> dict[str, Any]:
    """Run the full hierarchy. Returns aggregate results."""
    captain_id = "captain"
    await rs.register_agent(
        agent_id=captain_id,
        agent_role="captain",
        parent_agent_id=None,
    )

    # Header (recorded BEFORE any sub-agent spawns — safe to write sync)
    rs.write_initial_artifacts()
    rs.trace_write_sync({
        "record_type": "hierarchy_run_header",
        "agent_id": captain_id,
        "agent_role": "captain",
        "worker_id": captain_id,
        "model": model,
        "street_name": rs.street.name,
        "street_slug": rs.street.slug,
        "n_waypoints": len(rs.street.waypoints),
        "polyline": rs.street.polyline,
        "n_candidates_in_corridor": len(rs.all_candidates),
        "budget_cap_usd": rs.budget_cap_usd,
        "n_surveyor_slots": rs.surveyor_sem._value,  # initial slots
        "n_investigator_slots_per_surveyor": (
            rs.investigator_sem_factory()._value
        ),
        "started_ts": int(time.time() * 1000),
    })
    # Mirror the legacy record_type so the v2 UI's existing handler still works
    rs.trace_write_sync({
        "record_type": "walker_run_header",
        "agent_id": captain_id,
        "agent_role": "captain",
        "worker_id": captain_id,
        "street_name": rs.street.name,
        "street_slug": rs.street.slug,
        "model": model,
        "n_waypoints": len(rs.street.waypoints),
        "polyline": rs.street.polyline,
        "ground_truth_per_waypoint": [
            {"waypoint_idx": wp.idx,
              "ground_truth_tier": wp.ground_truth_tier,
              "ground_truth_pci": wp.ground_truth_pci}
            for wp in rs.street.waypoints
        ],
        "n_candidates_in_corridor": len(rs.all_candidates),
        "budget_cap_usd": rs.budget_cap_usd,
        "started_ts": int(time.time() * 1000),
    })

    # Build per-point blackboards lazily inside dispatch (we don't know which
    # points the captain will actually dispatch first). The street blackboard
    # is built up-front.
    sbb = StreetBlackboard(
        rs.run_dir, rs.street.name,
        n_points_total=len(rs.street.waypoints),
        budget_cap_usd=rs.budget_cap_usd,
    )
    point_blackboards: dict[int, PointBlackboard] = {}

    scratch = AgentScratch(
        agent_id=captain_id,
        agent_role="captain",
        parent_agent_id=None,
        point_idx=None,
        year=None,
        run_state=rs,
    )

    sbb_summary = await sbb.get_summary_for_captain()
    seed = _build_seed(
        rs, sbb_summary,
        n_surveyor_slots=rs.surveyor_sem._value,
        n_investigator_slots=rs.investigator_sem_factory()._value,
    )

    dispatch = _make_dispatch(
        rs, aclient, mapillary_token, sbb, point_blackboards, captain_id,
    )

    system = compose_captain_system()
    result = await run_agent_loop(
        rs=rs,
        scratch=scratch,
        aclient=aclient,
        model=model,
        system_prompt=system,
        tool_schemas=CAPTAIN_TOOL_SCHEMAS,
        seed_user_text=seed,
        dispatch_tool=dispatch,
        max_turns=max_turns,
        max_tokens_per_turn=max_tokens_per_turn,
        keep_last_n_images=2,
        terminal_side_effects=("done",),
    )

    # Final budget mirror
    await sbb.update_budget(rs.budget_used_usd)

    # Mirror legacy walker_run_complete for the existing UI banner
    final_summary = await sbb.get_summary_for_captain()
    rs.trace_write_sync({
        "record_type": "walker_run_complete",
        "agent_id": captain_id,
        "agent_role": "captain",
        "worker_id": captain_id,
        "stop_reason": result["stop_reason"],
        "n_waypoints": len(rs.street.waypoints),
        "n_graded": sum(
            1 for s in final_summary["point_summaries"]
            if s.get("tier") and s.get("tier") != "unknown"
        ),
        "n_skipped": sum(
            1 for s in final_summary["point_summaries"]
            if not s.get("tier") or s.get("tier") == "unknown"
        ),
        "total_cost_usd": round(rs.budget_used_usd, 4),
        "budget_cap_usd": rs.budget_cap_usd,
    })

    rs.close()
    return {
        "stop_reason": result["stop_reason"],
        "turns_used": result["turns_used"],
        "captain_cost_usd": scratch.cost_usd_local,
        "fleet_cost_usd": rs.budget_used_usd,
        "n_points_completed": len(final_summary["point_summaries"]),
        "n_points_total": len(rs.street.waypoints),
        "tier_distribution": _tier_distribution(final_summary["point_summaries"]),
        "narrative": final_summary.get("captain_narrative_draft"),
    }


def _tier_distribution(summaries: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in summaries:
        t = s.get("tier") or "unknown"
        out[t] = out.get(t, 0) + 1
    return out
