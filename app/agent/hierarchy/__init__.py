"""3-tier hierarchical multi-agent ecosystem for PavTrace.

Architecture:
  STREET CAPTAIN (1×, Opus 4.7) plans batches, dispatches surveyors, monitors
  the street blackboard, can issue redo orders.
    │
    ├─ POINT SURVEYOR (Opus 4.7, one per survey point, up to 3 concurrent)
    │  spawns year investigators, owns the discipline gate, reports up.
    │    │
    │    ├─ YEAR INVESTIGATOR (Opus 4.7, one per year per point, up to 2 conc.)
    │    │  investigates panos for a single year, writes claims to the
    │    │  per-point blackboard, reads sibling claims to coordinate yaws.
    │    │
    │    └─ ...

The legacy single-loop walker (run_street_walker) stays in tree as the
mode="single_walker" fallback. This package introduces mode="hierarchy".
"""
from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.blackboard import PointBlackboard, StreetBlackboard
from app.agent.hierarchy.captain import run_captain
from app.agent.hierarchy.point_surveyor import PointReport, run_point_surveyor
from app.agent.hierarchy.run_state import RunState
from app.agent.hierarchy.year_investigator import run_year_investigator

__all__ = [
    "AgentScratch",
    "PointBlackboard",
    "PointReport",
    "RunState",
    "StreetBlackboard",
    "run_captain",
    "run_point_surveyor",
    "run_year_investigator",
]
