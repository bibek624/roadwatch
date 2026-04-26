"""Reusable primitives for the hierarchy — adapter pattern over street_walker.

The street_walker.py implementations are stateless (in the sense that they
take a `WalkerState` and read/write specific fields on it) and we want to
reuse them verbatim. This module defines a `_StateAdapter` that quacks like
`WalkerState` for the duration of a tool call, backed by `RunState` for
shared caches and `AgentScratch` for per-agent state (visit_log, etc.).

Async cost handling: street_walker's `state.add_cost(usd)` is SYNC. We can't
hold the run_state budget lock from a sync method — so the adapter accumulates
into a local `pending_cost_usd` field; the caller is responsible for awaiting
`scratch.add_cost(adapter.flush_pending_cost())` after the tool call returns.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.run_state import RunState
from app.agent.walker_state import Waypoint, WaypointCandidate

# Re-export primitives from street_walker so this module is the single source.
from app.agent.street_walker import (  # noqa: F401
    _check_temporal_discipline,
    _composite_with_minimap,
    _dedup_by_sha,
    _enrich_candidates_and_shas,
    _ensure_local_pano,
    _find_candidates_impl,
    _peek_candidate_impl,
    _record_and_pack_look,
    _record_and_pack_look_around,
    _record_and_pack_zoom,
    _render_look_around,
)


class _StateAdapter:
    """Quacks like WalkerState. Backed by RunState (shared) + AgentScratch
    (per-agent). Constructed per tool call.

    For investigators: the surveyor's AgentScratch is shared across child
    investigators (they all update the same visit_log) — see point_surveyor.
    For surveyors: their own AgentScratch.
    """

    def __init__(
        self,
        run_state: RunState,
        scratch: AgentScratch,
        *,
        current_point_idx: int,
        current_lat: float,
        current_lon: float,
        candidate_pool: list[WaypointCandidate] | None = None,
    ):
        self._rs = run_state
        self._sc = scratch
        self.run_dir: Path = run_state.run_dir
        self.street = run_state.street
        # Pool: defaults to run_state.all_candidates, but year investigators
        # get a pre-filtered slice (their year only) injected at spawn time.
        self.all_candidates: list[WaypointCandidate] = (
            candidate_pool if candidate_pool is not None
            else run_state.all_candidates
        )

        # Synthesize a Waypoint matching what street_walker expects
        self._wp = Waypoint(
            idx=current_point_idx,
            lat=current_lat,
            lon=current_lon,
            distance_along_street_m=0.0,
            segment_id=None,
            segment_name=None,
            ground_truth_pci=None,
            ground_truth_tier=None,
            ground_truth_status=None,
        )
        self.current_waypoint_idx: int = current_point_idx

        # Shared caches (NOT copied — the adapter is a thin view)
        self.peek_cache: dict[str, dict[str, Any]] = run_state.peek_cache
        self.equirect_cache: dict[str, Path] = run_state.equirect_cache
        self.thumb_sha_cache: dict[str, str] = run_state.thumb_sha_cache

        # Per-agent state mirrors (so the discipline gate sees the right
        # visit_log for THIS surveyor's point only)
        self.candidates_by_idx: dict[int, list[WaypointCandidate]] = {}
        # The discipline gate reads state.visit_log[wp_idx] — we expose ONE key.
        self.visit_log: dict[int, dict[str, dict[str, Any]]] = {
            current_point_idx: scratch.visit_log,
        }
        self.discipline_gate_strikes: dict[int, int] = {
            current_point_idx: scratch.discipline_gate_strikes,
        }

        # Local cost accumulator — flushed by caller post-tool-call
        self.pending_cost_usd: float = 0.0

    # -- WalkerState-shape API ----------------------------------------------

    @property
    def current_waypoint(self) -> Waypoint:
        return self._wp

    def cache_candidates(self, idx: int, candidates: list[WaypointCandidate]) -> None:
        self.candidates_by_idx[idx] = candidates

    def cache_peek(self, image_id: str, result: dict[str, Any]) -> None:
        self.peek_cache[image_id] = result

    def add_cost(self, usd: float) -> None:
        self.pending_cost_usd += max(0.0, float(usd))

    def record_peek(self, wp_idx: int, image_id: str, year: int | None) -> None:
        self._sc.record_peek(image_id, year)

    def record_look(self, wp_idx: int, image_id: str, year: int | None,
                    yaw_deg: float | None) -> None:
        self._sc.record_look(image_id, year, yaw_deg)

    def record_zoom(self, wp_idx: int, image_id: str, year: int | None,
                    rendered_yaw_deg: float | None) -> None:
        self._sc.record_zoom(image_id, year, rendered_yaw_deg)

    def visits_summary(self, wp_idx: int) -> dict[str, Any]:
        # The discipline gate calls this with wp_idx == current_point_idx.
        return self._sc.visits_summary()

    # discipline_gate_strikes lookup happens via attribute access in the gate:
    # state.discipline_gate_strikes.get(wp_idx, 0) — we expose a dict above.

    # find_candidates_impl writes back via state.thumb_sha_cache[iid] = sha.
    # That's already the shared cache, so all good.

    # -- helpers -------------------------------------------------------------

    def flush_pending_cost(self) -> float:
        usd = self.pending_cost_usd
        self.pending_cost_usd = 0.0
        return usd

    def sync_strikes_back(self) -> None:
        """Copy strike counter back into AgentScratch after a discipline-gate
        rejection (the gate mutates state.discipline_gate_strikes[wp_idx])."""
        self._sc.discipline_gate_strikes = self.discipline_gate_strikes.get(
            self.current_waypoint_idx, self._sc.discipline_gate_strikes,
        )


# ---------------------------------------------------------------------------
# Convenience: build the adapter inside a context manager that auto-flushes
# pending costs to the AgentScratch (and through it, the RunState budget).
# ---------------------------------------------------------------------------

class adapter_for:  # noqa: N801 — lowercase name reads cleaner at call site
    """Context manager for ergonomic adapter use:

        async with adapter_for(rs, scratch, point_idx=3, lat=..., lon=...) as adapter:
            cands, totals, supp = await _find_candidates_impl(
                adapter, mapillary_token=token, ...
            )
        # cost auto-flushed on __aexit__
    """
    def __init__(
        self,
        run_state: RunState,
        scratch: AgentScratch,
        *,
        point_idx: int,
        lat: float,
        lon: float,
        candidate_pool: list[WaypointCandidate] | None = None,
    ):
        self._adapter = _StateAdapter(
            run_state, scratch,
            current_point_idx=point_idx, current_lat=lat, current_lon=lon,
            candidate_pool=candidate_pool,
        )
        self._scratch = scratch

    async def __aenter__(self) -> _StateAdapter:
        return self._adapter

    async def __aexit__(self, exc_type, exc, tb) -> None:
        usd = self._adapter.flush_pending_cost()
        if usd > 0:
            await self._scratch.add_cost(usd)
        # Sync the strike counter back so it persists across calls
        self._adapter.sync_strikes_back()
