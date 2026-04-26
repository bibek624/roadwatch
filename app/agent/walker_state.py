"""Street-walker state — Waypoint, Street, WalkerState.

The walker traverses a single corridor at fixed spacing. State tracks:
  - the ordered list of waypoints + per-waypoint ground-truth PCI/tier
  - which waypoint we're currently at
  - per-waypoint candidates considered / peeked / chosen
  - per-waypoint findings (predicted tier + rationale + cost)
  - aggregate cost
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.state import haversine_m


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Waypoint:
    idx: int
    lat: float
    lon: float
    distance_along_street_m: float
    # Ground-truth (looked up from LA PCI at construction time)
    segment_id: str | None
    segment_name: str | None
    ground_truth_pci: float | None
    ground_truth_tier: str | None  # Good/Satisfactory/Fair/Poor/Failed/None
    ground_truth_status: str | None  # LA's coarse 3-tier (Good/Fair/Poor)

    def to_public(self) -> dict[str, Any]:
        return {
            "waypoint_idx": self.idx,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "segment_id": self.segment_id,
            "segment_name": self.segment_name,
            "ground_truth_pci": (
                round(self.ground_truth_pci, 1)
                if self.ground_truth_pci is not None else None
            ),
            "ground_truth_tier": self.ground_truth_tier,
            "ground_truth_status": self.ground_truth_status,
            "distance_along_street_m": round(self.distance_along_street_m, 1),
        }


@dataclass
class Street:
    name: str
    slug: str
    polyline: list[tuple[float, float]]  # [(lon, lat), ...] full corridor centerline
    length_m: float
    waypoints: list[Waypoint]

    def to_geojson(self) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(c) for c in self.polyline],
                    },
                    "properties": {
                        "name": self.name,
                        "slug": self.slug,
                        "length_m": round(self.length_m, 1),
                        "n_waypoints": len(self.waypoints),
                    },
                },
            ],
        }


@dataclass
class WaypointCandidate:
    """One Mapillary candidate the walker considered at a waypoint."""
    image_id: str
    lat: float
    lon: float
    captured_at: str
    year: int
    age_years: float | None
    is_pano: bool
    compass_angle: float | None
    make: str | None
    model: str | None
    camera_type: str | None
    dist_from_waypoint_m: float

    def to_public(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "captured_at": self.captured_at,
            "year": self.year,
            "age_years": round(self.age_years, 1) if self.age_years is not None else None,
            "is_pano": self.is_pano,
            "compass_angle": (
                round(self.compass_angle, 1)
                if self.compass_angle is not None else None
            ),
            "make": self.make,
            "model": self.model,
            "camera_type": self.camera_type,
            "dist_from_waypoint_m": round(self.dist_from_waypoint_m, 1),
        }


@dataclass
class WaypointFinding:
    waypoint_idx: int
    predicted_tier: str | None
    confidence: float | None
    rationale: str | None
    chosen_image_id: str | None
    candidates_considered: int
    candidates_peeked: int
    viewports_used: int
    turns_used: int
    cost_usd: float
    stop_reason: str
    timestamp_ms: int
    # convenience copies of ground truth so artifacts are self-contained
    ground_truth_tier: str | None = None
    ground_truth_pci: float | None = None
    lat: float = 0.0
    lon: float = 0.0


# ---------------------------------------------------------------------------
# WalkerState
# ---------------------------------------------------------------------------

class WalkerState:
    """Mutable state for one walker run."""

    def __init__(
        self,
        run_dir: Path,
        street: Street,
        all_candidates: list[WaypointCandidate],
        budget_cap_usd: float = 30.0,
        per_waypoint_budget_usd: float = 0.60,
        per_waypoint_turn_cap: int = 6,
    ):
        self.run_dir = Path(run_dir)
        self.street = street
        # All panos in the corridor bbox, pre-fetched + enriched ONCE at startup.
        # `find_candidates` filters from this list at runtime (no Mapillary
        # round-trip per waypoint).
        self.all_candidates = all_candidates
        self.budget_cap_usd = budget_cap_usd
        self.per_waypoint_budget_usd = per_waypoint_budget_usd
        self.per_waypoint_turn_cap = per_waypoint_turn_cap

        self.current_waypoint_idx: int = 0
        self.budget_used_usd: float = 0.0
        # findings indexed by waypoint_idx (None until graded/skipped)
        self.findings_by_idx: dict[int, WaypointFinding] = {}
        # per-waypoint candidate cache (so the agent can re-query without
        # re-fetching from Mapillary — and we can write a richer trace)
        self.candidates_by_idx: dict[int, list[WaypointCandidate]] = {}
        # Haiku peek cache: image_id -> peek result dict
        self.peek_cache: dict[str, dict[str, Any]] = {}
        # Local equirect path cache: image_id -> Path
        self.equirect_cache: dict[str, Path] = {}
        # Thumb SHA cache used by the SHA-duplicate detector. Some Mapillary
        # contributors upload byte-identical panos under separate image_ids
        # with falsified captured_at timestamps — see Chestnut Ventura where
        # 2016/2025 pairs share SHA-256 to the byte. Hashing the thumb_1024
        # is enough to detect this. Cached so re-calls are free.
        self.thumb_sha_cache: dict[str, str] = {}
        # Per-waypoint visit log used by the temporal-discipline pre-grade gate.
        # Shape: {wp_idx: {image_id: {"year": int|None, "peeked": bool,
        #                              "yaws_looked": set[int],
        #                              "yaws_zoomed": set[int]}}}
        self.visit_log: dict[int, dict[str, dict[str, Any]]] = {}
        # Track how many times the temporal-discipline gate has fired per
        # waypoint so we can escape the loop after a few rejections (the agent
        # might be unable to satisfy the gate, e.g., genuine night-only year).
        self.discipline_gate_strikes: dict[int, int] = {}

    # -- accessors -----------------------------------------------------------

    @property
    def current_waypoint(self) -> Waypoint | None:
        if 0 <= self.current_waypoint_idx < len(self.street.waypoints):
            return self.street.waypoints[self.current_waypoint_idx]
        return None

    @property
    def is_finished(self) -> bool:
        return self.current_waypoint_idx >= len(self.street.waypoints)

    @property
    def findings(self) -> list[WaypointFinding]:
        return [self.findings_by_idx[i]
                for i in sorted(self.findings_by_idx)
                if self.findings_by_idx[i] is not None]

    def position_summary(self) -> dict[str, Any]:
        wp = self.current_waypoint
        if wp is None:
            return {
                "waypoint_idx": -1, "total_waypoints": len(self.street.waypoints),
                "is_finished": True,
            }
        n = len(self.street.waypoints)
        prior_findings = sum(
            1 for f in self.findings_by_idx.values()
            if f.predicted_tier is not None
        )
        out = wp.to_public()
        out.update({
            "total_waypoints": n,
            "distance_remaining_m": round(self.street.length_m - wp.distance_along_street_m, 1),
            "prior_findings_count": prior_findings,
            "budget_used_usd": round(self.budget_used_usd, 4),
            "budget_cap_usd": self.budget_cap_usd,
        })
        return out

    # -- mutations -----------------------------------------------------------

    def add_cost(self, usd: float) -> None:
        self.budget_used_usd += max(0.0, float(usd))

    # -- visit logging (drives the temporal-discipline pre-grade gate) -------

    def _ensure_visit_entry(self, wp_idx: int, image_id: str,
                             year: int | None) -> dict[str, Any]:
        wp_log = self.visit_log.setdefault(wp_idx, {})
        entry = wp_log.setdefault(image_id, {
            "year": year, "peeked": False,
            "yaws_looked": set(), "yaws_zoomed": set(),
        })
        if entry.get("year") is None and year is not None:
            entry["year"] = year
        return entry

    def record_peek(self, wp_idx: int, image_id: str, year: int | None) -> None:
        e = self._ensure_visit_entry(wp_idx, image_id, year)
        e["peeked"] = True

    def record_look(self, wp_idx: int, image_id: str, year: int | None,
                    yaw_deg: float | None) -> None:
        e = self._ensure_visit_entry(wp_idx, image_id, year)
        if yaw_deg is not None:
            e["yaws_looked"].add(int(round(float(yaw_deg))))

    def record_zoom(self, wp_idx: int, image_id: str, year: int | None,
                    rendered_yaw_deg: float | None) -> None:
        e = self._ensure_visit_entry(wp_idx, image_id, year)
        if rendered_yaw_deg is not None:
            e["yaws_zoomed"].add(int(round(float(rendered_yaw_deg))))

    def visits_summary(self, wp_idx: int) -> dict[str, Any]:
        """Compact summary used by the discipline gate + included in trace."""
        wp_log = self.visit_log.get(wp_idx, {})
        by_year: dict[int, list[dict[str, Any]]] = {}
        for iid, e in wp_log.items():
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

    def cache_candidates(self, idx: int, candidates: list[WaypointCandidate]) -> None:
        self.candidates_by_idx[idx] = candidates

    def cache_peek(self, image_id: str, result: dict[str, Any]) -> None:
        self.peek_cache[image_id] = result

    def record_finding(
        self,
        predicted_tier: str | None,
        confidence: float | None,
        rationale: str | None,
        chosen_image_id: str | None,
        candidates_peeked: int,
        viewports_used: int,
        turns_used: int,
        cost_usd: float,
        stop_reason: str,
    ) -> WaypointFinding:
        wp = self.current_waypoint
        assert wp is not None
        candidates = self.candidates_by_idx.get(wp.idx, [])
        f = WaypointFinding(
            waypoint_idx=wp.idx,
            predicted_tier=predicted_tier,
            confidence=confidence,
            rationale=rationale,
            chosen_image_id=chosen_image_id,
            candidates_considered=len(candidates),
            candidates_peeked=candidates_peeked,
            viewports_used=viewports_used,
            turns_used=turns_used,
            cost_usd=cost_usd,
            stop_reason=stop_reason,
            timestamp_ms=int(time.time() * 1000),
            ground_truth_tier=wp.ground_truth_tier,
            ground_truth_pci=wp.ground_truth_pci,
            lat=wp.lat,
            lon=wp.lon,
        )
        self.findings_by_idx[wp.idx] = f
        return f

    def advance(self) -> bool:
        """Move to the next waypoint. Returns False if we're at the end."""
        self.current_waypoint_idx += 1
        return not self.is_finished

    # -- persistence ---------------------------------------------------------

    def dump_findings_geojson(self) -> dict[str, Any]:
        features = []
        for idx in sorted(self.findings_by_idx):
            f = self.findings_by_idx[idx]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [f.lon, f.lat]},
                "properties": {
                    "waypoint_idx": f.waypoint_idx,
                    "predicted_tier": f.predicted_tier,
                    "ground_truth_tier": f.ground_truth_tier,
                    "ground_truth_pci": f.ground_truth_pci,
                    "match": (
                        f.predicted_tier == f.ground_truth_tier
                        if f.predicted_tier and f.ground_truth_tier else None
                    ),
                    "confidence": f.confidence,
                    "rationale": f.rationale,
                    "chosen_image_id": f.chosen_image_id,
                    "candidates_considered": f.candidates_considered,
                    "candidates_peeked": f.candidates_peeked,
                    "viewports_used": f.viewports_used,
                    "turns_used": f.turns_used,
                    "cost_usd": round(f.cost_usd, 5),
                    "stop_reason": f.stop_reason,
                },
            })
        return {"type": "FeatureCollection", "features": features}

    def dump_waypoints_geojson(self) -> dict[str, Any]:
        features = []
        for wp in self.street.waypoints:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [wp.lon, wp.lat]},
                "properties": wp.to_public(),
            })
        return {"type": "FeatureCollection", "features": features}

    def write_artifacts(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "street.geojson").write_text(
            json.dumps(self.street.to_geojson(), indent=2)
        )
        (self.run_dir / "waypoints.geojson").write_text(
            json.dumps(self.dump_waypoints_geojson(), indent=2)
        )
        (self.run_dir / "findings.geojson").write_text(
            json.dumps(self.dump_findings_geojson(), indent=2)
        )
        state = {
            "current_waypoint_idx": self.current_waypoint_idx,
            "budget_used_usd": round(self.budget_used_usd, 4),
            "n_waypoints": len(self.street.waypoints),
            "n_graded": sum(
                1 for f in self.findings_by_idx.values()
                if f.predicted_tier is not None and f.predicted_tier != "unknown"
            ),
            "n_skipped": sum(
                1 for f in self.findings_by_idx.values()
                if f.predicted_tier in (None, "unknown")
            ),
        }
        (self.run_dir / "state.json").write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Centerline + waypoint construction helpers
# ---------------------------------------------------------------------------

def polyline_length_m(coords: list[tuple[float, float]]) -> float:
    """coords are (lon, lat) pairs."""
    total = 0.0
    for i in range(1, len(coords)):
        a, b = coords[i - 1], coords[i]
        total += haversine_m(a[1], a[0], b[1], b[0])
    return total


def interpolate_along_polyline(
    coords: list[tuple[float, float]],
    target_distance_m: float,
) -> tuple[float, float]:
    """Given a polyline (lon, lat), return the point at `target_distance_m`
    along it. Returns (lon, lat)."""
    if not coords:
        raise ValueError("empty polyline")
    if target_distance_m <= 0:
        return coords[0]
    cum = 0.0
    for i in range(1, len(coords)):
        a, b = coords[i - 1], coords[i]
        seg_m = haversine_m(a[1], a[0], b[1], b[0])
        if cum + seg_m >= target_distance_m:
            # interpolate
            t = (target_distance_m - cum) / seg_m if seg_m > 0 else 0.0
            lon = a[0] + t * (b[0] - a[0])
            lat = a[1] + t * (b[1] - a[1])
            return (lon, lat)
        cum += seg_m
    return coords[-1]


def waypoints_from_polyline(
    polyline: list[tuple[float, float]],
    spacing_m: float,
) -> list[tuple[float, float, float]]:
    """Return list of (lon, lat, distance_along_m) sampled at uniform spacing.
    Always includes the start; ends at or before total_length."""
    L = polyline_length_m(polyline)
    points: list[tuple[float, float, float]] = []
    d = 0.0
    while d <= L + 1e-6:
        lon, lat = interpolate_along_polyline(polyline, d)
        points.append((lon, lat, d))
        d += spacing_m
    return points
