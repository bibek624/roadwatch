"""Shared atomic state for one hierarchical run (Captain + Surveyors + Investigators).

This is the OUTER STATE — one instance per `run_captain` invocation. Every agent
has a reference to it. Atomicity is enforced by `asyncio.Lock`s.

What lives here:
  - Street + the immutable pre-fetched candidate pool
  - Atomic budget counter + cap
  - Two semaphores (surveyor slots, investigator slots) — see plan
  - The TraceWriter + its lock (multi-writer safe)
  - Pure caches reusable across agents (peek_cache, equirect_cache, thumb_sha_cache)
  - The street blackboard + a per-point blackboard registry
  - Stop signal (file flag + asyncio.Event)
  - Agent registry — `agents_by_id` for the /hierarchy endpoint snapshot
  - A global Semaphore guarding `aclient.messages.create` calls (rate-limit backstop)

What does NOT live here:
  - Per-agent scratch (`messages`, `visit_log`, `discipline_gate_strikes`) —
    that's `AgentScratch`, instantiated per agent inside its loop.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.trace import TraceWriter
from app.agent.walker_state import Street, WaypointCandidate


@dataclass
class AgentRegistration:
    """One row in the live agent registry, used by /api/temporal/runs/{slug}/hierarchy."""
    agent_id: str
    agent_role: str       # captain | surveyor | investigator
    parent_agent_id: str | None
    point_idx: int | None
    year: int | None
    state: str            # spawned | running | completed | failed
    spawned_ms: int
    completed_ms: int | None = None
    cost_usd: float = 0.0
    turns_used: int = 0
    output_summary: str | None = None
    color_seed: str | None = None  # for UI hashing — same as agent_id

    def to_public(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "parent_agent_id": self.parent_agent_id,
            "point_idx": self.point_idx,
            "year": self.year,
            "state": self.state,
            "spawned_ms": self.spawned_ms,
            "completed_ms": self.completed_ms,
            "cost_usd": round(self.cost_usd, 5),
            "turns_used": self.turns_used,
            "output_summary": self.output_summary,
        }


class RunState:
    """One instance per hierarchical run."""

    def __init__(
        self,
        run_dir: Path,
        street: Street,
        all_candidates: list[WaypointCandidate],
        *,
        budget_cap_usd: float = 12.0,
        n_surveyor_slots: int = 3,
        n_investigator_slots: int = 2,
        n_global_api_slots: int = 6,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.street = street
        self.all_candidates = all_candidates  # pre-fetched & enriched ONCE

        self.budget_cap_usd = float(budget_cap_usd)
        self.budget_used_usd: float = 0.0
        self._budget_lock = asyncio.Lock()

        # Concurrency caps. Per-agent semaphores are owned by parent agents.
        self.surveyor_sem = asyncio.Semaphore(n_surveyor_slots)
        self.investigator_sem_factory = lambda: asyncio.Semaphore(n_investigator_slots)
        self.api_sem = asyncio.Semaphore(n_global_api_slots)

        # Pure caches (reusable across agents, no race)
        self.peek_cache: dict[str, dict[str, Any]] = {}
        self.equirect_cache: dict[str, Path] = {}
        self.thumb_sha_cache: dict[str, str] = {}

        # Trace
        self.trace = TraceWriter(self.run_dir / "walker_trace.jsonl")
        self.trace_lock = asyncio.Lock()
        self._t0_ms = int(time.time() * 1000)

        # Stop signal — file flag (UI) + asyncio Event (budget hard cap)
        self.stop_flag_path = self.run_dir / "_stop_requested.flag"
        self.stop_event = asyncio.Event()

        # Agent registry (id -> registration). Captain is registered up-front.
        self.agents_by_id: dict[str, AgentRegistration] = {}
        self._registry_lock = asyncio.Lock()

        # Counters used to mint unique agent ids
        self._surveyor_counter = 0
        self._investigator_counters: dict[int, int] = {}

    # -- budget --------------------------------------------------------------

    async def add_cost(self, usd: float) -> None:
        if not usd or usd <= 0:
            return
        async with self._budget_lock:
            self.budget_used_usd += float(usd)
            if self.budget_used_usd >= 0.95 * self.budget_cap_usd:
                self.stop_event.set()

    @property
    def budget_remaining_usd(self) -> float:
        return max(0.0, self.budget_cap_usd - self.budget_used_usd)

    @property
    def budget_pct_used(self) -> float:
        if self.budget_cap_usd <= 0:
            return 0.0
        return self.budget_used_usd / self.budget_cap_usd

    # -- stop signal ---------------------------------------------------------

    def should_stop(self) -> bool:
        if self.stop_event.is_set():
            return True
        if self.stop_flag_path.exists():
            self.stop_event.set()
            return True
        return False

    # -- agent registry ------------------------------------------------------

    async def register_agent(
        self,
        agent_id: str,
        agent_role: str,
        parent_agent_id: str | None = None,
        point_idx: int | None = None,
        year: int | None = None,
    ) -> AgentRegistration:
        async with self._registry_lock:
            reg = AgentRegistration(
                agent_id=agent_id,
                agent_role=agent_role,
                parent_agent_id=parent_agent_id,
                point_idx=point_idx,
                year=year,
                state="spawned",
                spawned_ms=int(time.time() * 1000),
                color_seed=agent_id,
            )
            self.agents_by_id[agent_id] = reg
            return reg

    async def update_agent(
        self,
        agent_id: str,
        *,
        state: str | None = None,
        cost_usd: float | None = None,
        turns_used: int | None = None,
        output_summary: str | None = None,
        completed: bool = False,
    ) -> None:
        async with self._registry_lock:
            reg = self.agents_by_id.get(agent_id)
            if reg is None:
                return
            if state is not None:
                reg.state = state
            if cost_usd is not None:
                reg.cost_usd = float(cost_usd)
            if turns_used is not None:
                reg.turns_used = int(turns_used)
            if output_summary is not None:
                reg.output_summary = output_summary
            if completed:
                reg.completed_ms = int(time.time() * 1000)

    # -- agent id minting ----------------------------------------------------

    def mint_surveyor_id(self, point_idx: int) -> str:
        n = self._surveyor_counter
        self._surveyor_counter += 1
        return f"S{n}-WP{point_idx}"

    def mint_investigator_id(self, point_idx: int, year: int) -> str:
        cnt = self._investigator_counters.get(point_idx, 0)
        self._investigator_counters[point_idx] = cnt + 1
        return f"I{cnt}-WP{point_idx}-Y{year}"

    # -- trace helpers (with lock) -------------------------------------------

    async def trace_write(self, record: dict[str, Any]) -> None:
        async with self.trace_lock:
            self.trace.write(record)

    def trace_write_sync(self, record: dict[str, Any]) -> None:
        """Lock-free variant for the captain's own header — only safe before any
        sub-agent has been spawned."""
        self.trace.write(record)

    # -- artifacts -----------------------------------------------------------

    def write_initial_artifacts(self) -> None:
        """Mirror what WalkerState.write_artifacts does for street + waypoints —
        the v2 UI expects these files at /file/{path}."""
        (self.run_dir / "street.geojson").write_text(
            json.dumps(self.street.to_geojson(), indent=2)
        )
        features = []
        for wp in self.street.waypoints:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [wp.lon, wp.lat]},
                "properties": wp.to_public(),
            })
        (self.run_dir / "waypoints.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": features}, indent=2)
        )

    def close(self) -> None:
        try:
            self.trace.close()
        except Exception:
            pass
