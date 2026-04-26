"""Role-specific system-prompt composers for Captain / Surveyor / Investigator.

Each role gets its own subset of the 16 skill files (13 existing + 3 new),
arranged into 2 cache_control blocks. The first call per role per run pays
the cache write; subsequent same-role calls amortize the read.

The 3 new role-specific skills are SHORT (~1.5 KB each) and pinned to the
front of block 2 so a Sunday tweak to one only invalidates that role's cache.

A non-cached role preamble (per-agent identity) is prepended so it doesn't
bust cache.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.skill_loader import _compose_block, _load_skill, SKILLS_DIR


# ---------------------------------------------------------------------------
# Captain
# ---------------------------------------------------------------------------

CAPTAIN_BLOCK_1 = ["tier_rubric", "deterioration_progression", "repair_priority_logic"]
CAPTAIN_BLOCK_2 = ["cross_point_synthesis"]

CAPTAIN_ROLE = """You are the STREET CAPTAIN — the top-level agent of a 3-tier hierarchy investigating ONE city street for pavement condition.

You do not look at panos directly. You dispatch Point Surveyors (max 3 in parallel) to specific survey points on this street. Each surveyor spawns its own Year Investigators (one per year of imagery, max 2 in parallel) to inspect the panos and produce structured per-year evidence.

Your tools: read_street_blackboard, read_point_blackboard, plan_dispatch_batches, dispatch_surveyors, request_redo, cross_check_claim, finalize_street, done.

Each `dispatch_surveyors` call BLOCKS until all spawned surveyors finish — only then can you dispatch the next wave.

Your output: a corridor-level narrative for a public-works engineer + flagged inconsistencies + final tier distribution.

Stay terse. Engineer-grade language. Don't speculate about distress types you can't see — your surveyors and investigators do that. You synthesize."""


# ---------------------------------------------------------------------------
# Surveyor
# ---------------------------------------------------------------------------

SURVEYOR_BLOCK_1 = ["tier_rubric", "grade_discipline", "deterioration_progression"]
SURVEYOR_BLOCK_2 = ["scan_plan", "evidence_extraction", "temporal_reconciliation"]

SURVEYOR_ROLE = """You are a POINT SURVEYOR — the middle tier of a 3-tier hierarchy. You own ONE survey point on a street.

Your tools: get_point_brief, enumerate_candidates_by_year, dispatch_year_investigators, read_point_blackboard, request_more_evidence, cross_witness_check, grade, report_to_captain.

Each `dispatch_year_investigators` call BLOCKS until all spawned investigators finish, then writes their reports to your point blackboard. You then read the blackboard to reconcile across years and grade.

The pre-grade temporal-discipline gate WILL refuse sloppy grades — see temporal_reconciliation. You have 2 strikes per point. Use them.

Output: tier (Good/Fair/Poor/unknown) + rationale + structured evidence + ≤300-char narrative for the captain's corridor synthesis."""


# ---------------------------------------------------------------------------
# Year Investigator
# ---------------------------------------------------------------------------

INVESTIGATOR_BLOCK_1 = [
    "distress_taxonomy", "visual_confusers", "treatment_signatures",
]
INVESTIGATOR_BLOCK_2 = [
    "pano_anatomy", "viewport_geometry", "scan_plan", "zoom_investigation",
    "year_investigator_brief",
]

INVESTIGATOR_ROLE = """You are a YEAR INVESTIGATOR — the bottom tier of a 3-tier hierarchy. You investigate ONE year of panos at ONE survey point.

Your tools: peek_candidate, look_around, look, zoom_into_region, read_sibling_claims, post_claim, report_year_findings.

You do NOT enumerate candidates — your parent surveyor already filtered them to your year. You do NOT grade — you only produce structured per-year evidence. Sibling investigators (other years at this same point) post their claims to a shared point blackboard, and you should call read_sibling_claims on turn 1 to align with them.

Your job: peek your candidates, look_around to orient, look at the cleanest yaw (matching sibling anchors when available), zoom into anything ambiguous, post claims, report year findings."""


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

def _compose_role(
    role_preamble: str,
    block1_skills: list[str],
    block2_skills: list[str],
    skills_dir: Path = SKILLS_DIR,
) -> list[dict[str, Any]]:
    """Two ephemerally-cached blocks. The role preamble is prepended in block 1
    so a role tweak invalidates only that role's cache (not all 3)."""
    block_1_body = role_preamble + "\n\n----------\n\n" + _compose_block(
        block1_skills, skills_dir
    )
    block_2_body = _compose_block(block2_skills, skills_dir)
    return [
        {"type": "text", "text": block_1_body,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": block_2_body,
         "cache_control": {"type": "ephemeral"}},
    ]


def compose_captain_system(skills_dir: Path = SKILLS_DIR) -> list[dict[str, Any]]:
    return _compose_role(
        CAPTAIN_ROLE, CAPTAIN_BLOCK_1, CAPTAIN_BLOCK_2, skills_dir
    )


def compose_surveyor_system(skills_dir: Path = SKILLS_DIR) -> list[dict[str, Any]]:
    return _compose_role(
        SURVEYOR_ROLE, SURVEYOR_BLOCK_1, SURVEYOR_BLOCK_2, skills_dir
    )


def compose_investigator_system(skills_dir: Path = SKILLS_DIR) -> list[dict[str, Any]]:
    return _compose_role(
        INVESTIGATOR_ROLE, INVESTIGATOR_BLOCK_1, INVESTIGATOR_BLOCK_2, skills_dir
    )


def assert_all_role_skills_present(skills_dir: Path = SKILLS_DIR) -> None:
    """Sanity-check: every skill referenced by every role exists."""
    expected = set(
        CAPTAIN_BLOCK_1 + CAPTAIN_BLOCK_2
        + SURVEYOR_BLOCK_1 + SURVEYOR_BLOCK_2
        + INVESTIGATOR_BLOCK_1 + INVESTIGATOR_BLOCK_2
    )
    missing = [n for n in sorted(expected) if not (skills_dir / f"{n}.md").exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing role skill files in {skills_dir}: {missing}"
        )


if __name__ == "__main__":
    assert_all_role_skills_present()
    for label, fn in [
        ("captain", compose_captain_system),
        ("surveyor", compose_surveyor_system),
        ("investigator", compose_investigator_system),
    ]:
        param = fn()
        total = sum(len(b["text"]) for b in param)
        print(f"{label:13s} {len(param)} blocks, {total:,} chars ({total/1024:.1f} KB)")
