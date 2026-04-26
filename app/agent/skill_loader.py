"""Skill loader — composes the walker's system prompt from .claude/skills/pavement/.

The skills directory is the engineering source of truth for the agent's pavement
knowledge. This loader reads each .md file, strips the YAML frontmatter, and
assembles the bodies into a Claude system param (a list of text blocks with
cache_control on the right ones).

Two cache breakpoints (Anthropic supports 4 max):

  Block 1 — "core engineering knowledge" (cached, rarely changes):
    tier_rubric, distress_taxonomy, visual_confusers,
    deterioration_progression, treatment_signatures,
    climate_failure_modes, repair_priority_logic

  Block 2 — "operational discipline + geometry" (cached, stable per architecture):
    pano_anatomy, viewport_geometry, scan_plan,
    zoom_investigation, grade_discipline

A short ROLE preamble is prepended in Block 1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills" / "pavement"


# Skill files grouped per cache block, in load order.
# Order within block determines display order in the system prompt.
BLOCK_1_SKILLS: list[str] = [
    "tier_rubric",
    "distress_taxonomy",
    "visual_confusers",
    "deterioration_progression",
    "treatment_signatures",
    "climate_failure_modes",
    "repair_priority_logic",
]

BLOCK_2_SKILLS: list[str] = [
    "pano_anatomy",
    "viewport_geometry",
    "scan_plan",
    "zoom_investigation",
    "grade_discipline",
    "evidence_extraction",
]


ROLE_PREAMBLE = """You are a STREET WALKER agent surveying ONE city corridor for pavement condition. You walk the street end-to-end at fixed waypoint spacing. At each waypoint your job is:

  1. Find Mapillary 360° pano candidates near this exact location.
  2. ORIENT yourself in the chosen pano using `look_around`.
  3. Drill in with `look` — possibly multiple times — until you have a viewport that's PAVEMENT-DOMINATED.
  4. Grade the pavement condition.
  5. Advance to the next waypoint.

Mapillary is crowdsourced. Most candidates near a given waypoint will be PARTIALLY USABLE: car-mounted shots with hood occluding the lower half, pedestrian rigs photographing sidewalks, perpendicular angles, off-center forward yaws. Your job is to USE YOUR JUDGMENT — and to use the MULTIPLE LOOK TOOLS we give you — to find the angle that actually shows the road.

Your engineering knowledge is curated below as a set of skill modules. Each module gives you authoritative engineering content (sourced from FHWA, ASTM D6433, PASER, and other industry references) on one focused topic. Reference the modules' decision rules as you grade.

Quality > speed. 3-6 look calls per waypoint is acceptable when the view requires investigation. Grade only after self-critique (see grade_discipline)."""


def _strip_frontmatter(text: str) -> str:
    """Strip the leading YAML frontmatter (`---\\n...\\n---\\n`) from a skill file.
    Returns the body."""
    text = text.lstrip()
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    body = text[end + len("\n---"):]
    return body.lstrip("\n").rstrip() + "\n"


def _load_skill(name: str, skills_dir: Path = SKILLS_DIR) -> str:
    p = skills_dir / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Missing skill file: {p}")
    return _strip_frontmatter(p.read_text(encoding="utf-8"))


def _compose_block(skill_names: list[str], skills_dir: Path = SKILLS_DIR) -> str:
    parts: list[str] = []
    for name in skill_names:
        body = _load_skill(name, skills_dir).strip()
        # Keep each skill's # heading visible to the model
        parts.append(body)
    return "\n\n----------\n\n".join(parts) + "\n"


def compose_walker_system(skills_dir: Path = SKILLS_DIR) -> list[dict[str, Any]]:
    """Return the Anthropic-API-ready system param: a list of text blocks with
    cache_control on the two engineering-knowledge blocks."""
    block_1_body = ROLE_PREAMBLE + "\n\n----------\n\n" + _compose_block(
        BLOCK_1_SKILLS, skills_dir
    )
    block_2_body = _compose_block(BLOCK_2_SKILLS, skills_dir)
    return [
        {
            "type": "text",
            "text": block_1_body,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": block_2_body,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def assert_all_skills_present(skills_dir: Path = SKILLS_DIR) -> None:
    """Sanity-check that every expected skill file is present. Raises
    FileNotFoundError listing missing skills if any."""
    expected = BLOCK_1_SKILLS + BLOCK_2_SKILLS
    missing = [n for n in expected if not (skills_dir / f"{n}.md").exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing skill files in {skills_dir}: {missing}. "
            f"Expected: {expected}"
        )


if __name__ == "__main__":
    # Smoke test
    assert_all_skills_present()
    param = compose_walker_system()
    print(f"Composed {len(param)} cache blocks")
    for i, b in enumerate(param):
        kb = len(b["text"]) / 1024.0
        print(f"  Block {i+1}: {len(b['text']):,} chars ({kb:.1f} KB) "
              f"cache_control={b.get('cache_control')}")
    total_chars = sum(len(b["text"]) for b in param)
    print(f"Total: {total_chars:,} chars ({total_chars/1024:.1f} KB)")
