"""Corridor decision synthesis — single Opus call that converts a completed
hierarchy walker run into a DOT-engineer priority brief.

Reads `street_blackboard.json` + `evidence/wp{idx:03d}_blackboard.json` from a
walker run directory, calls Opus 4.7 once with the `emit_synthesis` tool, and
caches the result as `decisions_synthesis.json` in the same run dir.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .prompts.decisions_synthesis import (
    EMIT_SYNTHESIS_TOOL,
    SYNTHESIS_SYSTEM_PROMPT,
)


SCHEMA_VERSION = 1
SYNTH_MODEL = "claude-opus-4-7"

# Per-million token prices for claude-opus-4-7 (1M-context tier).
# Source: Anthropic public pricing as of 2026-04. Used only to attribute an
# approximate per-call USD cost in the cache; UI displays it as ~ value.
_PRICE_INPUT_PER_MTOK = 15.0
_PRICE_OUTPUT_PER_MTOK = 75.0
_PRICE_CACHE_WRITE_PER_MTOK = 18.75
_PRICE_CACHE_READ_PER_MTOK = 1.5


def _load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _build_synthesis_input(run_dir: Path) -> dict:
    """Assemble the corridor evidence bundle the synthesizer reasons over."""
    config = _load_json(run_dir / "config.json", {})
    street_bb = _load_json(run_dir / "street_blackboard.json", {})
    point_summaries = list(street_bb.get("point_summaries") or [])
    point_summaries.sort(key=lambda p: p.get("point_idx", 0))

    n_points_total = int(street_bb.get("n_points_total")
                         or config.get("n_waypoints") or len(point_summaries))

    # Per-point detail (claims_by_year + cross_witness_yaws + status_by_year)
    # — keep just the fields a reasoning model can use for cross-year synthesis.
    per_point_detail: list[dict] = []
    for ps in point_summaries:
        idx = int(ps.get("point_idx", 0))
        wp_path = run_dir / "evidence" / f"wp{idx:03d}_blackboard.json"
        wp = _load_json(wp_path, {}) or {}
        per_point_detail.append({
            "point_idx": idx,
            "claims_by_year": wp.get("claims_by_year") or {},
            "cross_witness_yaws": wp.get("cross_witness_yaws") or {},
            "status_by_year": {
                yr: {
                    "usable": s.get("usable"),
                    "best_image_id": s.get("best_image_id"),
                    "summary": s.get("summary"),
                    "distresses": s.get("distresses") or [],
                    "treatments": s.get("treatments") or [],
                }
                for yr, s in (wp.get("status_by_year") or {}).items()
            },
        })

    return {
        "street_name": config.get("street_name") or street_bb.get("street_name"),
        "length_m": config.get("length_m"),
        "n_points_total": n_points_total,
        "waypoint_spacing_m": config.get("waypoint_spacing_m"),
        "point_summaries": point_summaries,
        "per_point_detail": per_point_detail,
    }


def _collect_known_image_ids(run_dir: Path, bundle: dict) -> set[str]:
    """Image ids that the synthesizer is allowed to cite as evidence: anything
    listed in point_summaries[*].evidence_image_ids OR claims_by_year[*][*].image_ids
    OR status_by_year[*].best_image_id, plus any file actually present in
    viewports/ (so the renderer can fall back gracefully)."""
    ids: set[str] = set()
    for ps in bundle.get("point_summaries") or []:
        for iid in ps.get("evidence_image_ids") or []:
            if iid:
                ids.add(str(iid))
        if ps.get("chosen_image_id"):
            ids.add(str(ps["chosen_image_id"]))
    for d in bundle.get("per_point_detail") or []:
        for yr_claims in (d.get("claims_by_year") or {}).values():
            for c in yr_claims or []:
                for iid in c.get("image_ids") or []:
                    if iid:
                        ids.add(str(iid))
        for s in (d.get("status_by_year") or {}).values():
            if s.get("best_image_id"):
                ids.add(str(s["best_image_id"]))
    # Files on disk under viewports/  (filename: <image_id>_y...)
    vp_dir = run_dir / "viewports"
    if vp_dir.is_dir():
        for f in vp_dir.iterdir():
            stem = f.name.split("_", 1)[0]
            if stem:
                ids.add(stem)
    return ids


def _validate(out: dict, bundle: dict, known_ids: set[str]) -> None:
    n = int(bundle.get("n_points_total") or 0)
    actions = out.get("priority_actions") or []
    if not isinstance(actions, list):
        raise ValueError("priority_actions must be a list")
    for i, a in enumerate(actions):
        for idx in a.get("point_indices") or []:
            if not isinstance(idx, int) or idx < 0 or idx >= n:
                raise ValueError(
                    f"priority_actions[{i}].point_indices contains "
                    f"out-of-range value {idx!r} (n_points_total={n})"
                )
        eids = a.get("evidence_image_ids") or []
        if not eids:
            raise ValueError(
                f"priority_actions[{i}].evidence_image_ids must be non-empty"
            )
        unknown = [e for e in eids if str(e) not in known_ids]
        if unknown:
            raise ValueError(
                f"priority_actions[{i}].evidence_image_ids reference "
                f"unknown image_ids: {unknown[:3]}"
            )

    flags = out.get("safety_flags") or []
    for j, sf in enumerate(flags):
        idx = sf.get("point_idx")
        if not isinstance(idx, int) or idx < 0 or idx >= n:
            raise ValueError(
                f"safety_flags[{j}].point_idx out of range: {idx!r}"
            )

    grade = out.get("corridor_grade")
    if grade not in {"Good", "Fair", "Poor", "unknown"}:
        raise ValueError(f"corridor_grade invalid: {grade!r}")


def _estimate_cost_usd(usage) -> float:
    """Price the call from the Anthropic usage object. Returns 0 if usage is
    missing fields. All values in USD."""
    try:
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cw_tok = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr_tok = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    except Exception:
        return 0.0
    cost = (
        in_tok * _PRICE_INPUT_PER_MTOK
        + out_tok * _PRICE_OUTPUT_PER_MTOK
        + cw_tok * _PRICE_CACHE_WRITE_PER_MTOK
        + cr_tok * _PRICE_CACHE_READ_PER_MTOK
    ) / 1_000_000.0
    return round(cost, 4)


async def synthesize_corridor(
    run_dir: Path,
    aclient,
    *,
    force: bool = False,
    model: str = SYNTH_MODEL,
) -> dict:
    """Synthesize a completed walker run into a decision brief. Cached as
    ``decisions_synthesis.json`` in the run dir.

    - If a cache file exists and ``force`` is False → return it as-is.
    - If the run has no ``street_blackboard.json`` or zero points → raises
      ``RuntimeError("survey_incomplete")`` so the caller can show an empty
      state, not fail mysteriously.
    - On Opus call success but schema validation failure → raises
      ``ValueError`` with a descriptive message; cache is NOT written.
    """
    run_dir = Path(run_dir)
    cache_path = run_dir / "decisions_synthesis.json"
    if cache_path.exists() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # corrupted cache — re-synthesize

    bundle = _build_synthesis_input(run_dir)
    if not bundle.get("point_summaries"):
        raise RuntimeError("survey_incomplete")

    known_ids = _collect_known_image_ids(run_dir, bundle)

    user_text = (
        "CORRIDOR EVIDENCE BUNDLE (JSON)\n"
        "==============================\n"
        f"Street: {bundle.get('street_name')!r}\n"
        f"Length: {bundle.get('length_m')} m\n"
        f"Survey points: {bundle.get('n_points_total')}\n"
        f"Spacing: {bundle.get('waypoint_spacing_m')} m\n\n"
        "point_summaries (Surveyor outputs — final per-point decisions):\n"
        + json.dumps(bundle["point_summaries"], indent=2, ensure_ascii=False)
        + "\n\nper_point_detail (Year Investigator claims + cross-witness yaws):\n"
        + json.dumps(bundle["per_point_detail"], indent=2, ensure_ascii=False)
        + "\n\nNow call `emit_synthesis` exactly once with the priority brief."
    )

    resp = await aclient.messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYNTHESIS_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[EMIT_SYNTHESIS_TOOL],
        tool_choice={"type": "tool", "name": "emit_synthesis"},
        messages=[{"role": "user", "content": user_text}],
    )

    tool_payload = None
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_synthesis":
            tool_payload = getattr(block, "input", None)
            break
    if not tool_payload:
        raise ValueError("Opus did not call emit_synthesis")

    # Defensive defaults — Opus occasionally omits required-but-empty fields
    # (e.g. no safety flags on a clean corridor). Backfill before validation
    # so downstream consumers never have to handle missing keys.
    tool_payload.setdefault("safety_flags", [])
    tool_payload.setdefault("priority_actions", [])

    _validate(tool_payload, bundle, known_ids)

    cost_usd = _estimate_cost_usd(getattr(resp, "usage", None))

    out = {
        "schema_version": SCHEMA_VERSION,
        "synthesized_at_ms": int(time.time() * 1000),
        "model": model,
        "cost_usd": cost_usd,
        "n_points_total": bundle.get("n_points_total"),
        **tool_payload,
    }

    cache_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
