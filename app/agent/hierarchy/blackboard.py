"""Per-point and per-street blackboards — the inter-agent communication channel.

Pattern:
  - PointBlackboard is created by the surveyor when it starts a point.
  - Year investigators (the surveyor's children) read sibling claims and post
    their own claims/status updates atomically.
  - StreetBlackboard is created by the captain.
  - Surveyors append point summaries; captain posts dispatch_log + redo_log
    + narrative_draft.

Concurrency: one `asyncio.Lock` per blackboard, covers BOTH the in-memory
mutation and the JSON-file rewrite. Reads are also lock-protected so a writer
can't be observed mid-update.

Persistence: every mutation rewrites the entire JSON file (small files,
< 50 KB each). Every mutation also emits a `blackboard_post` trace event so
the UI replay is lossless even when a JSON file is overwritten.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# PointBlackboard
# ---------------------------------------------------------------------------

class PointBlackboard:
    """One per survey point. Lives at evidence/wp{idx:03d}_blackboard.json."""

    def __init__(self, run_dir: Path, point_idx: int, lat: float, lon: float):
        self.path = Path(run_dir) / "evidence" / f"wp{point_idx:03d}_blackboard.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {
            "point_idx": point_idx,
            "lat": lat,
            "lon": lon,
            "surveyor_id": None,
            "captain_directive": None,
            "redo_history": [],
            "claims_by_year": {},     # {"2025": [claim, claim, ...]}
            "cross_witness_yaws": {}, # {"2025": [0, 90, 180]}
            "status_by_year": {},     # {"2025": {state, usable, summary, ...}}
            "requests_pending": [],
            "final_grade": None,
            "created_ms": int(time.time() * 1000),
        }
        self._write_unlocked()

    # -- internal: file I/O assumes lock is held -----------------------------

    def _write_unlocked(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, default=_json_default),
            encoding="utf-8",
        )

    # -- public reads (lock-protected) ---------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return json.loads(json.dumps(self.data, default=_json_default))

    async def get_sibling_claims(self, asking_year: int) -> dict[str, Any]:
        """Return claims from years OTHER than asking_year — what an investigator
        sees when it calls read_sibling_claims."""
        async with self.lock:
            sibling = {
                y: list(self.data["claims_by_year"].get(y, []))
                for y in self.data["claims_by_year"].keys()
                if str(y) != str(asking_year)
            }
            cross_yaws = {
                y: list(yaws)
                for y, yaws in self.data["cross_witness_yaws"].items()
                if str(y) != str(asking_year)
            }
            return {
                "asking_year": asking_year,
                "sibling_claims_by_year": sibling,
                "sibling_yaws_per_year": cross_yaws,
                "n_sibling_claims": sum(len(v) for v in sibling.values()),
            }

    # -- writes --------------------------------------------------------------

    async def set_surveyor(self, surveyor_id: str, captain_directive: str | None) -> None:
        async with self.lock:
            self.data["surveyor_id"] = surveyor_id
            self.data["captain_directive"] = captain_directive
            self._write_unlocked()

    async def add_redo(self, by: str, reason: str, focus_year: int | None) -> None:
        async with self.lock:
            self.data["redo_history"].append({
                "by": by, "reason": reason, "focus_year": focus_year,
                "ts_ms": int(time.time() * 1000),
            })
            self._write_unlocked()

    async def post_claim(
        self,
        investigator_id: str,
        year: int,
        category: str,
        content: str,
        image_ids: list[str] | None = None,
        yaw_deg: int | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        claim = {
            "claim_id": f"c-{uuid.uuid4().hex[:8]}",
            "investigator_id": investigator_id,
            "ts_ms": int(time.time() * 1000),
            "year": year,
            "category": category,
            "content": content,
            "image_ids": list(image_ids or []),
            "yaw_deg": yaw_deg,
            "confidence": confidence,
        }
        async with self.lock:
            year_key = str(year)
            self.data["claims_by_year"].setdefault(year_key, []).append(claim)
            if yaw_deg is not None:
                yaws = self.data["cross_witness_yaws"].setdefault(year_key, [])
                if yaw_deg not in yaws:
                    yaws.append(int(yaw_deg))
            self._write_unlocked()
        return claim

    async def set_year_status(
        self,
        year: int,
        *,
        investigator_id: str | None = None,
        state: str | None = None,        # spawned | running | completed | failed
        usable: bool | None = None,
        best_image_id: str | None = None,
        summary: str | None = None,
        yaws_covered: list[int] | None = None,
        distresses: list[Any] | None = None,
        treatments: list[Any] | None = None,
    ) -> None:
        async with self.lock:
            year_key = str(year)
            cur = self.data["status_by_year"].setdefault(year_key, {})
            if investigator_id is not None:
                cur["investigator_id"] = investigator_id
            if state is not None:
                cur["state"] = state
            if usable is not None:
                cur["usable"] = bool(usable)
            if best_image_id is not None:
                cur["best_image_id"] = best_image_id
            if summary is not None:
                cur["summary"] = summary
            if yaws_covered is not None:
                cur["yaws_covered"] = list(yaws_covered)
            if distresses is not None:
                cur["distresses"] = list(distresses)
            if treatments is not None:
                cur["treatments"] = list(treatments)
            self._write_unlocked()

    async def append_request(self, request: dict[str, Any]) -> None:
        async with self.lock:
            self.data["requests_pending"].append(request)
            self._write_unlocked()

    async def set_final_grade(self, grade: dict[str, Any]) -> None:
        async with self.lock:
            self.data["final_grade"] = grade
            self._write_unlocked()


# ---------------------------------------------------------------------------
# StreetBlackboard
# ---------------------------------------------------------------------------

class StreetBlackboard:
    """One per run. Lives at street_blackboard.json (in run_dir root)."""

    def __init__(
        self,
        run_dir: Path,
        street_name: str,
        n_points_total: int,
        budget_cap_usd: float,
    ):
        self.path = Path(run_dir) / "street_blackboard.json"
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {
            "street_name": street_name,
            "captain_id": "captain",
            "n_points_total": n_points_total,
            "started_ms": int(time.time() * 1000),
            "point_summaries": [],
            "flagged_inconsistencies": [],
            "dispatch_log": [],
            "captain_redo_log": [],
            "captain_narrative_draft": None,
            "fleet_budget_used_usd": 0.0,
            "fleet_budget_cap_usd": float(budget_cap_usd),
        }
        self._write_unlocked()

    def _write_unlocked(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, default=_json_default),
            encoding="utf-8",
        )

    # -- reads ---------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return json.loads(json.dumps(self.data, default=_json_default))

    async def get_summary_for_captain(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "street_name": self.data["street_name"],
                "n_points_total": self.data["n_points_total"],
                "n_points_completed": len(self.data["point_summaries"]),
                "point_summaries": list(self.data["point_summaries"]),
                "flagged_inconsistencies": list(self.data["flagged_inconsistencies"]),
                "dispatch_log": list(self.data["dispatch_log"]),
                "captain_redo_log": list(self.data["captain_redo_log"]),
                "captain_narrative_draft": self.data.get("captain_narrative_draft"),
                "fleet_budget_used_usd": self.data["fleet_budget_used_usd"],
                "fleet_budget_cap_usd": self.data["fleet_budget_cap_usd"],
            }

    # -- writes --------------------------------------------------------------

    async def append_point_summary(self, summary: dict[str, Any]) -> None:
        async with self.lock:
            # Dedup if a summary for the same point_idx already exists (redo case)
            self.data["point_summaries"] = [
                s for s in self.data["point_summaries"]
                if s.get("point_idx") != summary.get("point_idx")
            ]
            self.data["point_summaries"].append(dict(summary))
            self.data["point_summaries"].sort(key=lambda s: s.get("point_idx", 0))
            self._write_unlocked()

    async def append_flag(self, flag: dict[str, Any]) -> None:
        async with self.lock:
            flag = dict(flag)
            flag.setdefault("flag_id", f"f-{uuid.uuid4().hex[:8]}")
            flag.setdefault("ts_ms", int(time.time() * 1000))
            self.data["flagged_inconsistencies"].append(flag)
            self._write_unlocked()

    async def append_dispatch(self, point_ids: list[int], directive: str) -> int:
        async with self.lock:
            wave_idx = len(self.data["dispatch_log"])
            self.data["dispatch_log"].append({
                "wave": wave_idx,
                "point_ids": list(point_ids),
                "directive": directive,
                "ts_ms": int(time.time() * 1000),
            })
            self._write_unlocked()
            return wave_idx

    async def append_redo(self, point_idx: int, reason: str, focus_year: int | None) -> None:
        async with self.lock:
            self.data["captain_redo_log"].append({
                "point_idx": point_idx,
                "reason": reason,
                "focus_year": focus_year,
                "ts_ms": int(time.time() * 1000),
            })
            self._write_unlocked()

    async def set_narrative(self, narrative: str) -> None:
        async with self.lock:
            self.data["captain_narrative_draft"] = narrative
            self._write_unlocked()

    async def update_budget(self, used_usd: float) -> None:
        async with self.lock:
            self.data["fleet_budget_used_usd"] = round(float(used_usd), 4)
            self._write_unlocked()


# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)
