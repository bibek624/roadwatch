"""Per-agent mutable scratch — instantiated once per agent loop iteration.

Each agent (captain / surveyor / investigator) has its OWN AgentScratch.
Agents do not share scratch — they communicate via the blackboard or by
returning a structured report to their parent.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent.hierarchy.run_state import RunState


@dataclass
class AgentScratch:
    """Per-agent state.

    Visit log + discipline-gate-strikes are populated for surveyors AND
    indirectly via investigators (each investigator updates its parent
    surveyor's visit_log when it record_look/record_zoom — see point_surveyor).
    Investigators don't run the discipline gate themselves; the surveyor does
    when it calls grade().
    """
    agent_id: str
    agent_role: str           # captain | surveyor | investigator
    parent_agent_id: str | None
    point_idx: int | None     # None for captain
    year: int | None          # None for captain/surveyor

    run_state: RunState

    # Conversation history sent to messages.create (we own pruning)
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Per-agent counters (turns + cost local subtotal)
    turns_used: int = 0
    cost_usd_local: float = 0.0

    # Visit log (surveyor-level) — same shape as WalkerState.visit_log[wp_idx]
    # but only for THIS surveyor's point. Investigators write here via the
    # surveyor's helper methods (record_peek / record_look / record_zoom).
    visit_log: dict[str, dict[str, Any]] = field(default_factory=dict)
    discipline_gate_strikes: int = 0

    # When this agent started (used by trace fields)
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    # -- visit log helpers (mirror walker_state.WalkerState) -----------------

    def _ensure_visit_entry(self, image_id: str, year: int | None) -> dict[str, Any]:
        entry = self.visit_log.setdefault(image_id, {
            "year": year, "peeked": False,
            "yaws_looked": set(), "yaws_zoomed": set(),
        })
        if entry.get("year") is None and year is not None:
            entry["year"] = year
        return entry

    def record_peek(self, image_id: str, year: int | None) -> None:
        e = self._ensure_visit_entry(image_id, year)
        e["peeked"] = True

    def record_look(self, image_id: str, year: int | None,
                    yaw_deg: float | None) -> None:
        e = self._ensure_visit_entry(image_id, year)
        if yaw_deg is not None:
            e["yaws_looked"].add(int(round(float(yaw_deg))))

    def record_zoom(self, image_id: str, year: int | None,
                    rendered_yaw_deg: float | None) -> None:
        e = self._ensure_visit_entry(image_id, year)
        if rendered_yaw_deg is not None:
            e["yaws_zoomed"].add(int(round(float(rendered_yaw_deg))))

    def visits_summary(self) -> dict[str, Any]:
        """Compact summary used by the discipline gate."""
        by_year: dict[int, list[dict[str, Any]]] = {}
        for iid, e in self.visit_log.items():
            yr = e.get("year")
            if yr is None:
                continue
            by_year.setdefault(yr, []).append({
                "image_id": iid,
                "peeked": e["peeked"],
                "yaws_looked": sorted(e["yaws_looked"]),
                "yaws_zoomed": sorted(e["yaws_zoomed"]),
            })
        return {
            "by_year": by_year,
            "n_distinct_panos_per_year": {y: len(v) for y, v in by_year.items()},
            "yaws_per_year": {
                y: sorted({yw for entry in v for yw in entry["yaws_looked"]})
                for y, v in by_year.items()
            },
        }

    # -- cost --------------------------------------------------------------

    async def add_cost(self, usd: float) -> None:
        if not usd or usd <= 0:
            return
        self.cost_usd_local += float(usd)
        await self.run_state.add_cost(usd)

    # -- trace --------------------------------------------------------------

    def _agent_fields(self) -> dict[str, Any]:
        """Fields injected on every trace event from this agent."""
        return {
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "parent_agent_id": self.parent_agent_id,
            "point_idx": self.point_idx,
            "year": self.year,
            # Mirror agent_id to worker_id so legacy fleet UI degrades gracefully
            "worker_id": self.agent_id,
        }

    async def trace(self, record: dict[str, Any]) -> None:
        merged = {**self._agent_fields(), **record}
        await self.run_state.trace_write(merged)
