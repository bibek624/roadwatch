"""CLI runner for the 3-tier hierarchical PavTrace walker.

Mirrors `scripts/run_street_walker.py` but boots the captain instead of the
sequential walker. Use --limit-waypoints 1 for the smoke test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.hierarchy import RunState, run_captain  # noqa: E402
from scripts.run_street_walker import (  # noqa: E402
    build_street,
    prefetch_corridor_candidates,
)


async def main_async(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    mapillary_token = os.environ.get("MAPILLARY_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not mapillary_token or not anthropic_key:
        print("ERROR: MAPILLARY_TOKEN or ANTHROPIC_API_KEY not set",
              file=sys.stderr)
        return 2

    pci_path = ROOT / "data" / "la_pci" / "segments.geojson"
    if not pci_path.exists():
        print(f"ERROR: {pci_path} not found.", file=sys.stderr)
        return 2
    print("Loading LA PCI dataset...")
    pci_features = json.loads(pci_path.read_text()).get("features", [])

    print(f"Building street: {args.street_name}")
    street = build_street(args.street_name, pci_features,
                          waypoint_spacing_m=args.waypoint_spacing_m)
    if args.limit_waypoints:
        street.waypoints = street.waypoints[:args.limit_waypoints]
        print(f"  limited to first {len(street.waypoints)} waypoints")

    run_slug = args.slug or f"hier_{street.slug}_{int(time.time())}"
    run_dir = ROOT / "downloads" / "walker" / run_slug
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"  run dir: {run_dir.relative_to(ROOT)}\n")

    (run_dir / "config.json").write_text(json.dumps({
        "slug": run_slug,
        "street_name": args.street_name,
        "street_slug": street.slug,
        "length_m": street.length_m,
        "n_waypoints": len(street.waypoints),
        "waypoint_spacing_m": args.waypoint_spacing_m,
        "budget_cap_usd": args.budget,
        "model": args.model,
        "mode": "hierarchy",
        "n_surveyor_slots": args.n_surveyor_slots,
        "n_investigator_slots": args.n_investigator_slots,
    }, indent=2))

    print("Fetching Mapillary candidates in corridor bbox...")
    candidates = await prefetch_corridor_candidates(
        street, mapillary_token,
        buffer_m=args.candidate_buffer_m,
        max_age_years=args.max_age_years,
    )
    if not candidates:
        print("ERROR: no candidate panos in corridor.", file=sys.stderr)
        return 3
    print(f"  {len(candidates)} candidates loaded\n")

    rs = RunState(
        run_dir=run_dir, street=street, all_candidates=candidates,
        budget_cap_usd=args.budget,
        n_surveyor_slots=args.n_surveyor_slots,
        n_investigator_slots=args.n_investigator_slots,
    )

    aclient = anthropic.AsyncAnthropic(api_key=anthropic_key)
    print(f"=== Starting hierarchy run on {street.name} ===")
    print(f"    {len(street.waypoints)} pts · {len(candidates)} cands "
          f"· budget=${args.budget:.2f} "
          f"· {args.n_surveyor_slots} surveyors x "
          f"{args.n_investigator_slots} investigators\n", flush=True)

    result = await run_captain(
        rs=rs, aclient=aclient,
        mapillary_token=mapillary_token,
        model=args.model,
    )

    print("\n=== Hierarchy run complete ===")
    print(f"    stop_reason: {result['stop_reason']}")
    print(f"    points: {result['n_points_completed']}/{result['n_points_total']}")
    print(f"    tier distribution: {result['tier_distribution']}")
    print(f"    fleet cost: ${result['fleet_cost_usd']:.4f} of ${args.budget:.2f}")
    if result.get("narrative"):
        print(f"\n--- Captain narrative ---\n{result['narrative'][:600]}")
    print(f"\nArtifacts at {run_dir.relative_to(ROOT)}")
    return 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--street-name", default="SPRING ST")
    ap.add_argument("--waypoint-spacing-m", type=float, default=50.0)
    ap.add_argument("--limit-waypoints", type=int, default=1,
                    help="Smoke-test default of 1; raise for a real run.")
    ap.add_argument("--budget", type=float, default=4.0,
                    help="Fleet budget cap in USD.")
    ap.add_argument("--candidate-buffer-m", type=float, default=40.0)
    ap.add_argument("--max-age-years", type=float, default=12.0)
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--n-surveyor-slots", type=int, default=3)
    ap.add_argument("--n-investigator-slots", type=int, default=2)
    ap.add_argument("--slug", default=None)
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    rc = asyncio.run(main_async(args))
    sys.exit(rc)
