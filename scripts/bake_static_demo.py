"""Bake a single completed walker run into a static-deploy bundle.

Reads a `downloads/walker/<slug>/` directory (the live-app on-disk format)
and writes a frozen copy under `docs/data/<demo_slug>/` that the static
GitHub-Pages clones of `temporal_v2.html` + `decisions.html` can replay
with no backend.

Usage:
    python scripts/bake_static_demo.py \\
        --src ../pavtrace/downloads/walker/west_sunset_boulevard_1777239773 \\
        --dst docs/data/west_sunset \\
        --demo-slug west_sunset
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def _load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def bake(src: Path, dst: Path, demo_slug: str) -> None:
    src = src.resolve()
    dst = dst.resolve()
    if not src.is_dir():
        raise SystemExit(f"source dir not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "viewports").mkdir(exist_ok=True)

    config = _load_json(src / "config.json", {})
    street = _load_json(src / "street.geojson", {"type": "FeatureCollection", "features": []})
    waypoints = _load_json(src / "waypoints.geojson", {"type": "FeatureCollection", "features": []})
    street_bb = _load_json(src / "street_blackboard.json", {})
    synthesis = _load_json(src / "decisions_synthesis.json", None)

    # Per-point evidence — keyed by point_idx for the dashboard's drawer.
    per_point: dict[str, dict] = {}
    evidence_dir = src / "evidence"
    if evidence_dir.is_dir():
        for f in sorted(evidence_dir.iterdir()):
            if not f.name.endswith("_blackboard.json"):
                continue
            try:
                wp = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            idx = wp.get("point_idx")
            if isinstance(idx, int):
                per_point[str(idx)] = wp

    # Viewport file index — image_id → list of filenames on disk.
    viewport_index: dict[str, list[str]] = {}
    src_vp = src / "viewports"
    copied_vp = 0
    if src_vp.is_dir():
        for f in sorted(src_vp.iterdir()):
            if not f.is_file() or not f.name.lower().endswith(".jpg"):
                continue
            stem_id = f.name.split("_", 1)[0]
            if not stem_id:
                continue
            viewport_index.setdefault(stem_id, []).append(f.name)
            shutil.copy2(f, dst / "viewports" / f.name)
            copied_vp += 1

    bundle = {
        "slug": demo_slug,
        "config": config,
        "street": street,
        "waypoints": waypoints,
        "street_blackboard": street_bb,
        "per_point_evidence": per_point,
        "synthesis": synthesis,
        "viewport_index": viewport_index,
    }
    (dst / "bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
    )

    # Copy the trace verbatim — the replay UI reads it as JSONL.
    trace_src = src / "walker_trace.jsonl"
    if trace_src.is_file():
        shutil.copy2(trace_src, dst / "walker_trace.jsonl")

    bundle_size = (dst / "bundle.json").stat().st_size
    trace_size = (dst / "walker_trace.jsonl").stat().st_size if (dst / "walker_trace.jsonl").exists() else 0
    vp_size = sum(f.stat().st_size for f in (dst / "viewports").iterdir() if f.is_file())

    print(f"Baked {demo_slug}:")
    print(f"  bundle.json:        {bundle_size / 1024:.0f} KB")
    print(f"  walker_trace.jsonl: {trace_size / 1024 / 1024:.1f} MB")
    print(f"  viewports/:         {copied_vp} files, {vp_size / 1024 / 1024:.1f} MB")
    print(f"  total:              {(bundle_size + trace_size + vp_size) / 1024 / 1024:.1f} MB")
    print(f"  out: {dst}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Path to the source walker run dir")
    ap.add_argument("--dst", required=True, help="Output directory (under docs/data/<slug>)")
    ap.add_argument(
        "--demo-slug", default=None,
        help="Slug embedded in bundle.json (defaults to the dst dirname)",
    )
    args = ap.parse_args()
    src = Path(args.src)
    dst = Path(args.dst)
    demo_slug = args.demo_slug or dst.name
    bake(src, dst, demo_slug)


if __name__ == "__main__":
    main()
