import json
import os
from pathlib import Path

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .mapillary import fetch_captures, fetch_image_detail
from .models import AnalyzeResponse, ImageDetail, ImagesResponse, PolygonRequest
from .osm import fetch_roads

load_dotenv()
MAPILLARY_TOKEN = os.getenv("MAPILLARY_TOKEN", "")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
PAVEMENT_RUNS_DIR = BASE_DIR / "downloads" / "pavement"

app = FastAPI(title="PavTrace")


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: PolygonRequest):
    try:
        roads = await fetch_roads(req.polygon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Overpass error: {e}")
    return {"roads": roads, "road_count": len(roads["features"])}


@app.post("/api/images", response_model=ImagesResponse)
async def images(req: PolygonRequest):
    if not MAPILLARY_TOKEN:
        raise HTTPException(status_code=500, detail="MAPILLARY_TOKEN not set")
    try:
        return await fetch_captures(req.polygon, MAPILLARY_TOKEN)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mapillary error: {e}")


@app.get("/api/image/{image_id}", response_model=ImageDetail)
async def image(image_id: str):
    if not MAPILLARY_TOKEN:
        raise HTTPException(status_code=500, detail="MAPILLARY_TOKEN not set")
    try:
        detail = await fetch_image_detail(image_id, MAPILLARY_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mapillary error: {e}")
    # Rewrite the image URL to go through our proxy so pannellum (canvas) can read pixels.
    detail["url"] = f"/api/image/{image_id}/pixels"
    return detail


@app.get("/api/image/{image_id}/pixels")
async def image_pixels(image_id: str):
    """Proxy the Mapillary thumb so the image is served same-origin (CORS-safe for WebGL)."""
    if not MAPILLARY_TOKEN:
        raise HTTPException(status_code=500, detail="MAPILLARY_TOKEN not set")
    try:
        detail = await fetch_image_detail(image_id, MAPILLARY_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mapillary error: {e}")
    src = detail.get("url")
    if not src:
        raise HTTPException(status_code=404, detail="no image url")
    client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        upstream = await client.get(src)
        upstream.raise_for_status()
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"image fetch error: {e}")
    content_type = upstream.headers.get("content-type", "image/jpeg")

    async def gen():
        try:
            yield upstream.content
        finally:
            await client.aclose()

    return StreamingResponse(gen(), media_type=content_type, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Pavement assessment UI routes
# ---------------------------------------------------------------------------

def _pavement_run_dir(slug: str) -> Path:
    """Resolve a safe run directory under downloads/pavement/<slug>/. Rejects
    traversal attempts."""
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    p = (PAVEMENT_RUNS_DIR / safe).resolve()
    try:
        p.relative_to(PAVEMENT_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {slug}")
    return p


@app.get("/api/pavement/runs")
async def list_pavement_runs():
    """List available pavement runs by slug."""
    if not PAVEMENT_RUNS_DIR.exists():
        return {"runs": []}
    runs = []
    for p in sorted(PAVEMENT_RUNS_DIR.iterdir()):
        if not p.is_dir():
            continue
        config_path = p / "config.json"
        cfg = {}
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
            except Exception:
                cfg = {}
        runs.append({
            "slug": p.name,
            "street": cfg.get("street"),
            "city": cfg.get("city"),
            "postcode": cfg.get("postcode"),
            "started_at": cfg.get("started_at"),
            "has_ratings": (p / "ratings.json").exists(),
            "has_geojson": (p / "points.geojson").exists(),
        })
    return {"runs": runs}


@app.get("/api/pavement/{slug}/bundle")
async def pavement_bundle(slug: str):
    """Return the full GeoJSON + ratings bundle for a run, ready for the UI."""
    run_dir = _pavement_run_dir(slug)

    def _load_json(name: str, default):
        p = run_dir / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    bundle = {
        "slug": slug,
        "config": _load_json("config.json", {}),
        "street": _load_json("street.geojson", {"type": "FeatureCollection", "features": []}),
        "polygon": _load_json("polygon.geojson", None),
        "points": _load_json("points.geojson", {"type": "FeatureCollection", "features": []}),
        "distresses": _load_json("distresses.geojson", {"type": "FeatureCollection", "features": []}),
        "segments": _load_json("segments.geojson", {"type": "FeatureCollection", "features": []}),
        "temporal": _load_json("temporal.json", {}),
    }
    return JSONResponse(bundle)


@app.get("/api/pavement/{slug}/file/{filepath:path}")
async def pavement_file(slug: str, filepath: str):
    """Serve a single file from a run directory (strips, overlays, etc.).
    Guards against path traversal."""
    run_dir = _pavement_run_dir(slug)
    # Normalize and ensure result stays inside run_dir
    target = (run_dir / filepath).resolve()
    try:
        target.relative_to(run_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes run dir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {filepath}")
    return FileResponse(target)


@app.get("/pavement")
async def pavement_index_no_slug():
    """Loader page — picks the first (or user-selected) run."""
    return FileResponse(STATIC_DIR / "pavement.html")


@app.get("/pavement/{slug}")
async def pavement_index(slug: str):
    return FileResponse(STATIC_DIR / "pavement.html")


# ---------------------------------------------------------------------------
# Agent survey UI routes (the hackathon hero demo)
# ---------------------------------------------------------------------------

AGENT_RUNS_DIR = BASE_DIR / "downloads" / "agent"


def _agent_run_dir(slug: str) -> Path:
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    p = (AGENT_RUNS_DIR / safe).resolve()
    try:
        p.relative_to(AGENT_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {slug}")
    return p


@app.get("/api/agent/runs")
async def list_agent_runs():
    if not AGENT_RUNS_DIR.exists():
        return {"runs": []}
    runs = []
    for p in sorted(AGENT_RUNS_DIR.iterdir()):
        if not p.is_dir():
            continue
        cfg = {}
        cfg_path = p / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text())
            except Exception:
                cfg = {}
        state = {}
        state_path = p / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                state = {}
        runs.append({
            "slug": p.name,
            "name": cfg.get("name", p.name),
            "description": cfg.get("description", ""),
            "budget_cap_usd": cfg.get("budget_cap_usd"),
            "turn_cap": cfg.get("turn_cap"),
            "turns_used": state.get("turns_used"),
            "budget_used_usd": state.get("budget_used_usd"),
            "findings_count": state.get("findings_count"),
            "has_trace": (p / "agent_trace.jsonl").exists(),
        })
    return {"runs": runs}


@app.get("/api/agent/{slug}/bundle")
async def agent_bundle(slug: str):
    """Return all static artifacts for a run in one response.
    The trace itself is served by /api/agent/{slug}/trace as JSONL."""
    run_dir = _agent_run_dir(slug)

    def _load(name: str, default):
        p = run_dir / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    bundle = {
        "slug": slug,
        "config": _load("config.json", {}),
        "polygon": _load("polygon.geojson", None),
        "network": _load("network.geojson", {"type": "FeatureCollection", "features": []}),
        "roads": _load("roads.geojson", {"type": "FeatureCollection", "features": []}),
        "primaries": _load("primaries.json", []),
        "findings": _load("findings.geojson", {"type": "FeatureCollection", "features": []}),
        "state": _load("state.json", {}),
    }
    return JSONResponse(bundle)


@app.get("/api/agent/{slug}/trace")
async def agent_trace(slug: str):
    """Stream the agent_trace.jsonl. UI parses line-by-line."""
    run_dir = _agent_run_dir(slug)
    path = run_dir / "agent_trace.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="trace not found")
    return FileResponse(path, media_type="application/x-ndjson")


@app.get("/api/agent/{slug}/file/{filepath:path}")
async def agent_file(slug: str, filepath: str):
    """Serve a single file from a run directory (viewports, panos, thumbnails, probes)."""
    run_dir = _agent_run_dir(slug)
    target = (run_dir / filepath).resolve()
    try:
        target.relative_to(run_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes run dir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {filepath}")
    return FileResponse(target)


@app.get("/api/agent/{slug}/trace/tail")
async def agent_trace_tail(slug: str, offset: int = 0):
    """Live tail of agent_trace.jsonl. Client passes back `next_offset` each
    poll to receive only newly-appended complete lines. Tolerates the run dir
    or trace file not yet existing (returns exists=false)."""
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    run_dir = (AGENT_RUNS_DIR / safe).resolve()
    try:
        run_dir.relative_to(AGENT_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    path = run_dir / "agent_trace.jsonl"
    if not path.is_file():
        return {"lines": [], "next_offset": offset, "exists": False}
    size = path.stat().st_size
    if offset >= size:
        return {"lines": [], "next_offset": size, "exists": True}
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return {"lines": [], "next_offset": offset, "exists": True}
    complete = chunk[: last_nl + 1]
    next_offset = offset + (last_nl + 1)
    parsed: list[dict] = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            continue
    return {"lines": parsed, "next_offset": next_offset, "exists": True}


@app.get("/agent")
async def agent_index():
    return FileResponse(STATIC_DIR / "agent.html")


@app.get("/agent/{slug}")
async def agent_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "agent.html")


@app.get("/live")
async def live_index():
    return FileResponse(STATIC_DIR / "live.html")


@app.get("/live/{slug}")
async def live_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "live.html")


# ---------------------------------------------------------------------------
# Fleet (parallel-Worker) live UI
# ---------------------------------------------------------------------------

@app.get("/api/fleet/{slug}/trace/tail")
async def fleet_trace_tail(slug: str, offset: int = 0):
    """Live tail of fleet_trace.jsonl (multi-worker parallel run). Same shape
    as /api/agent/{slug}/trace/tail, just reads a different filename."""
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    run_dir = (AGENT_RUNS_DIR / safe).resolve()
    try:
        run_dir.relative_to(AGENT_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    path = run_dir / "fleet_trace.jsonl"
    if not path.is_file():
        return {"lines": [], "next_offset": offset, "exists": False}
    size = path.stat().st_size
    if offset >= size:
        return {"lines": [], "next_offset": size, "exists": True}
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return {"lines": [], "next_offset": offset, "exists": True}
    complete = chunk[: last_nl + 1]
    next_offset = offset + (last_nl + 1)
    parsed: list[dict] = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            continue
    return {"lines": parsed, "next_offset": next_offset, "exists": True}


@app.get("/fleet")
async def fleet_index():
    return FileResponse(STATIC_DIR / "fleet.html")


@app.get("/fleet/{slug}")
async def fleet_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "fleet.html")


# ---------------------------------------------------------------------------
# Calibration set viewer
# ---------------------------------------------------------------------------

CALIBRATION_DIR = BASE_DIR / "data" / "calibration"


def _calibration_dir(slug: str) -> Path:
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    p = (CALIBRATION_DIR / safe).resolve()
    try:
        p.relative_to(CALIBRATION_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside calibration dir")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"calibration set not found: {slug}")
    return p


@app.get("/api/calibration/runs")
async def list_calibration_runs():
    if not CALIBRATION_DIR.exists():
        return {"runs": []}
    runs = []
    for p in sorted(CALIBRATION_DIR.iterdir()):
        if not p.is_dir():
            continue
        m = p / "manifest.json"
        meta = {}
        if m.exists():
            try:
                meta = json.loads(m.read_text())
            except Exception:
                meta = {}
        runs.append({
            "slug": p.name,
            "n_total": meta.get("n_total"),
            "ground_truth_source": meta.get("ground_truth_source"),
            "bbox": meta.get("bbox"),
        })
    return {"runs": runs}


@app.get("/api/calibration/{slug}/bundle")
async def calibration_bundle(slug: str):
    cdir = _calibration_dir(slug)

    def _load(name: str, default):
        p = cdir / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    return JSONResponse({
        "slug": slug,
        "manifest": _load("manifest.json", {}),
        "segments": _load("segments.geojson", {"type": "FeatureCollection", "features": []}),
        "panos": _load("panos.geojson", {"type": "FeatureCollection", "features": []}),
    })


@app.get("/api/calibration/{slug}/thumb/{pano_id}")
async def calibration_thumb(slug: str, pano_id: str):
    cdir = _calibration_dir(slug)
    # Sanitize pano_id to digits/letters only — Mapillary ids are numeric strings
    if not pano_id or not pano_id.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid pano_id")
    target = (cdir / "thumbs" / f"{pano_id}.jpg").resolve()
    try:
        target.relative_to(cdir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes calibration dir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="thumb not found")
    return FileResponse(target)


@app.get("/calibration")
async def calibration_index():
    return FileResponse(STATIC_DIR / "calibration.html")


@app.get("/calibration/{slug}")
async def calibration_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "calibration.html")


# ---------------------------------------------------------------------------
# Street walker UI
# ---------------------------------------------------------------------------

WALKER_RUNS_DIR = BASE_DIR / "downloads" / "walker"


def _walker_run_dir(slug: str) -> Path:
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    p = (WALKER_RUNS_DIR / safe).resolve()
    try:
        p.relative_to(WALKER_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"walker run not found: {slug}")
    return p


@app.get("/api/walker/runs")
async def list_walker_runs():
    if not WALKER_RUNS_DIR.exists():
        return {"runs": []}
    runs = []
    for p in sorted(WALKER_RUNS_DIR.iterdir()):
        if not p.is_dir():
            continue
        cfg = {}
        cfg_path = p / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text())
            except Exception:
                cfg = {}
        state = {}
        state_path = p / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                state = {}
        runs.append({
            "slug": p.name,
            "street_name": cfg.get("street_name"),
            "length_m": cfg.get("length_m"),
            "n_waypoints": cfg.get("n_waypoints"),
            "n_graded": state.get("n_graded"),
            "n_skipped": state.get("n_skipped"),
            "budget_used_usd": state.get("budget_used_usd"),
            "budget_cap_usd": cfg.get("budget_cap_usd"),
        })
    return {"runs": runs}


@app.get("/api/walker/{slug}/bundle")
async def walker_bundle(slug: str):
    rd = _walker_run_dir(slug)

    def _load(name: str, default):
        p = rd / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    return JSONResponse({
        "slug": slug,
        "config": _load("config.json", {}),
        "street": _load("street.geojson", {"type": "FeatureCollection", "features": []}),
        "waypoints": _load("waypoints.geojson", {"type": "FeatureCollection", "features": []}),
        "findings": _load("findings.geojson", {"type": "FeatureCollection", "features": []}),
        "state": _load("state.json", {}),
    })


@app.get("/api/walker/{slug}/trace/tail")
async def walker_trace_tail(slug: str, offset: int = 0):
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    rd = (WALKER_RUNS_DIR / safe).resolve()
    try:
        rd.relative_to(WALKER_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    path = rd / "walker_trace.jsonl"
    if not path.is_file():
        return {"lines": [], "next_offset": offset, "exists": False}
    size = path.stat().st_size
    if offset >= size:
        return {"lines": [], "next_offset": size, "exists": True}
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return {"lines": [], "next_offset": offset, "exists": True}
    complete = chunk[: last_nl + 1]
    next_offset = offset + (last_nl + 1)
    parsed: list[dict] = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            continue
    return {"lines": parsed, "next_offset": next_offset, "exists": True}


@app.get("/api/walker/{slug}/file/{filepath:path}")
async def walker_file(slug: str, filepath: str):
    rd = _walker_run_dir(slug)
    target = (rd / filepath).resolve()
    try:
        target.relative_to(rd.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes run dir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {filepath}")
    return FileResponse(target)


@app.get("/walker")
async def walker_index():
    return FileResponse(STATIC_DIR / "walker.html")


@app.get("/walker/{slug}")
async def walker_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "walker.html")


# ---------------------------------------------------------------------------
# Temporal walker — polygon-driven entry point
# ---------------------------------------------------------------------------
# User flow:
#   1. Draw polygon on /temporal map
#   2. POST /api/temporal/streets-in-polygon → list of named streets in polygon
#      with their LineString geometries
#   3. POST /api/temporal/start with chosen street → launches the walker as a
#      background task, returns slug
#   4. UI flips to live-trace mode pointing at /api/walker/<slug>/...

import asyncio  # noqa: E402
import re as _re  # noqa: E402
import time as _time  # noqa: E402

from .osm import fetch_roads as _fetch_roads_for_polygon  # noqa: E402

# Track running walker tasks so we don't double-launch the same slug
_TEMPORAL_TASKS: dict[str, "asyncio.Task[None]"] = {}


def _slugify(s: str) -> str:
    s2 = (s or "").lower()
    s2 = _re.sub(r"[^a-z0-9]+", "_", s2).strip("_")
    return s2 or "street"


def _group_streets_by_name(roads_fc: dict) -> list[dict]:
    """Group OSM ways inside the polygon by `name` tag. Returns one entry per
    distinct named street with combined LineString segments + total length.
    Unnamed ways are dropped."""
    import math
    R = 6_371_000.0

    def hav(la1, lo1, la2, lo2):
        p1, p2 = math.radians(la1), math.radians(la2)
        dp = math.radians(la2 - la1)
        dl = math.radians(lo2 - lo1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(min(1.0, math.sqrt(a)))

    by_name: dict[str, dict] = {}
    for f in roads_fc.get("features", []):
        props = f.get("properties") or {}
        name = (props.get("name") or "").strip()
        if not name:
            continue
        geom = f.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        seg_len = 0.0
        for i in range(1, len(coords)):
            a, b = coords[i - 1], coords[i]
            seg_len += hav(a[1], a[0], b[1], b[0])
        slot = by_name.setdefault(name, {
            "name": name,
            "highway": props.get("highway"),
            "segments": [],
            "length_m": 0.0,
        })
        slot["segments"].append(coords)
        slot["length_m"] += seg_len
    out = []
    for name, s in by_name.items():
        out.append({
            "name": name,
            "highway": s["highway"],
            "length_m": round(s["length_m"], 1),
            "n_segments": len(s["segments"]),
            "segments": s["segments"],
        })
    out.sort(key=lambda x: x["length_m"], reverse=True)
    return out


@app.post("/api/temporal/streets-in-polygon")
async def temporal_streets_in_polygon(req: PolygonRequest):
    """Given a user-drawn polygon, return all named streets inside it
    (one entry per distinct `name`, with all LineString segments combined)."""
    try:
        roads = await _fetch_roads_for_polygon(req.polygon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Overpass error: {e}")
    streets = _group_streets_by_name(roads)
    return {"streets": streets, "n_streets": len(streets)}


@app.post("/api/temporal/start")
async def temporal_start(payload: dict):
    """Launch the walker on a chosen street. Payload:
        {
          "name": "MAIN ST",
          "segments": [[[lon,lat],...], ...],   # LineString rings
          "waypoint_spacing_m": 50,
          "budget_cap_usd": 10.0,
          "max_age_years": 10,                    # default 10 for temporal
          "per_waypoint_turn_cap": 12,
          "limit_waypoints": 4,                   # smoke-test cap
          "slug": "optional_custom_slug",
          "mode": "single_walker" | "hierarchy",  # default single_walker
          "n_surveyor_slots": 3,                  # hierarchy only
          "n_investigator_slots": 2,              # hierarchy only
        }
    Returns: {"slug": "...", "n_waypoints": N, "n_candidates": M}
    """
    name = (payload.get("name") or "").strip()
    segments = payload.get("segments") or []
    if not name or not segments:
        raise HTTPException(status_code=400, detail="name + segments required")

    mode = str(payload.get("mode") or "single_walker").strip()
    if mode not in {"single_walker", "hierarchy"}:
        raise HTTPException(
            status_code=400,
            detail=f"invalid mode {mode!r}; valid: single_walker | hierarchy",
        )

    waypoint_spacing_m = float(payload.get("waypoint_spacing_m", 50.0))
    budget_cap_usd = float(payload.get("budget_cap_usd", 30.0))
    max_age_years = float(payload.get("max_age_years", 12.0))
    # Generous defaults — let Opus investigate as deeply as it wants. Cost
    # is not a constraint per the user's directive.
    per_waypoint_turn_cap = int(payload.get("per_waypoint_turn_cap", 40))
    per_waypoint_budget = float(payload.get("per_waypoint_budget_usd", 6.00))
    limit_waypoints_in = payload.get("limit_waypoints")
    limit_waypoints = int(limit_waypoints_in) if limit_waypoints_in else None
    requested_slug = (payload.get("slug") or "").strip() or None
    n_surveyor_slots = int(payload.get("n_surveyor_slots", 3))
    n_investigator_slots = int(payload.get("n_investigator_slots", 2))

    mapillary_token = os.environ.get("MAPILLARY_TOKEN") or MAPILLARY_TOKEN
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not mapillary_token or not anthropic_key:
        raise HTTPException(
            status_code=500,
            detail="Server missing MAPILLARY_TOKEN or ANTHROPIC_API_KEY",
        )

    # Imports are local to avoid loading anthropic SDK at app start
    import anthropic  # type: ignore
    from .agent.street_walker import run_street_walker
    from .agent.walker_state import (
        Street,
        Waypoint,
        WalkerState,
        polyline_length_m,
        waypoints_from_polyline,
    )
    from scripts.run_street_walker import (
        chain_segments_by_endpoints,
        prefetch_corridor_candidates,
    )

    # Build polyline from chosen segments (chain them by endpoint adjacency)
    fake_segs = [
        {"geometry": {"type": "LineString", "coordinates": list(coords)}}
        for coords in segments
    ]
    polyline, _ = chain_segments_by_endpoints(fake_segs)
    if len(polyline) < 2:
        raise HTTPException(status_code=400, detail="could not chain segments")
    length_m = polyline_length_m(polyline)
    waypoint_pts = waypoints_from_polyline(polyline, spacing_m=waypoint_spacing_m)

    waypoints: list[Waypoint] = []
    for i, (lon, lat, dist_m) in enumerate(waypoint_pts):
        waypoints.append(Waypoint(
            idx=i, lat=lat, lon=lon,
            distance_along_street_m=dist_m,
            segment_id=None, segment_name=name,
            ground_truth_pci=None, ground_truth_tier=None,
            ground_truth_status=None,
        ))
    if limit_waypoints:
        waypoints = waypoints[:limit_waypoints]

    slug = requested_slug or f"{_slugify(name)}_{int(_time.time())}"
    if slug in _TEMPORAL_TASKS and not _TEMPORAL_TASKS[slug].done():
        raise HTTPException(status_code=409, detail=f"slug already running: {slug}")
    run_dir = WALKER_RUNS_DIR / slug
    run_dir.mkdir(parents=True, exist_ok=True)

    street = Street(
        name=name, slug=_slugify(name),
        polyline=polyline, length_m=length_m,
        waypoints=waypoints,
    )
    (run_dir / "config.json").write_text(json.dumps({
        "slug": slug,
        "street_name": name,
        "street_slug": street.slug,
        "length_m": length_m,
        "n_waypoints": len(waypoints),
        "waypoint_spacing_m": waypoint_spacing_m,
        "budget_cap_usd": budget_cap_usd,
        "per_waypoint_budget_usd": per_waypoint_budget,
        "per_waypoint_turn_cap": per_waypoint_turn_cap,
        "model": "claude-opus-4-7",
        "max_age_years": max_age_years,
        "mode": mode,
        "n_surveyor_slots": n_surveyor_slots if mode == "hierarchy" else None,
        "n_investigator_slots": (
            n_investigator_slots if mode == "hierarchy" else None
        ),
        "started_via": "polygon_picker",
    }, indent=2))

    # Write the street + waypoints geojson IMMEDIATELY (before prefetch) so the
    # UI can render the map while prefetch is still happening.
    (run_dir / "street.geojson").write_text(
        json.dumps(street.to_geojson(), indent=2)
    )
    waypoints_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point",
                              "coordinates": [wp.lon, wp.lat]},
                "properties": wp.to_public(),
            }
            for wp in waypoints
        ],
    }
    (run_dir / "waypoints.geojson").write_text(json.dumps(waypoints_fc, indent=2))

    # Status file so the UI can poll the prefetch phase
    def _write_status(**fields):
        (run_dir / "_temporal_status.json").write_text(
            json.dumps({"slug": slug, "ts_ms": int(_time.time() * 1000), **fields})
        )

    _write_status(phase="prefetching",
                  message="fetching Mapillary candidates in corridor bbox...")

    aclient = anthropic.AsyncAnthropic(api_key=anthropic_key)

    async def _runner():
        try:
            # Phase 1: prefetch (slow part — Mapillary tile + Graph API)
            candidates = await prefetch_corridor_candidates(
                street, mapillary_token,
                buffer_m=60.0,
                max_age_years=max_age_years,
            )
            if not candidates:
                _write_status(
                    phase="errored",
                    error=(
                        "No 360° panoramic images found near this street. "
                        "RoadWatch requires Mapillary 360° panos for its "
                        "look/zoom tools — flat perspective photos cannot be used. "
                        "Try the Sunset Strip (West Hollywood), a major LA arterial, "
                        "or any street with confirmed Mapillary pano coverage."
                    ),
                )
                return

            # Compute year breakdown for UI
            from collections import Counter
            yhist = Counter(c.year for c in candidates)
            _write_status(phase="running",
                          n_candidates=len(candidates),
                          year_breakdown=dict(sorted(yhist.items(), reverse=True)),
                          message=f"{len(candidates)} candidates in corridor — agent starting")

            if mode == "hierarchy":
                from .agent.hierarchy import RunState, run_captain
                rs = RunState(
                    run_dir=run_dir, street=street, all_candidates=candidates,
                    budget_cap_usd=budget_cap_usd,
                    n_surveyor_slots=n_surveyor_slots,
                    n_investigator_slots=n_investigator_slots,
                )
                await run_captain(
                    rs=rs, aclient=aclient,
                    mapillary_token=mapillary_token,
                    model="claude-opus-4-7",
                )
            else:
                state = WalkerState(
                    run_dir=run_dir, street=street, all_candidates=candidates,
                    budget_cap_usd=budget_cap_usd,
                    per_waypoint_budget_usd=per_waypoint_budget,
                    per_waypoint_turn_cap=per_waypoint_turn_cap,
                )
                await run_street_walker(
                    run_dir=run_dir,
                    state=state,
                    aclient=aclient,
                    mapillary_token=mapillary_token,
                    model="claude-opus-4-7",
                    max_total_turns=400,
                )
            _write_status(phase="completed")
        except Exception as exc:  # noqa: BLE001
            (run_dir / "runner_error.txt").write_text(
                f"{type(exc).__name__}: {exc}"
            )
            _write_status(phase="errored", error=f"{type(exc).__name__}: {exc}")

    task = asyncio.create_task(_runner())
    _TEMPORAL_TASKS[slug] = task

    # Return slug IMMEDIATELY — prefetch is happening in the background.
    return {
        "slug": slug,
        "n_waypoints": len(waypoints),
        "length_m": round(length_m, 1),
        "phase": "prefetching",
    }


@app.get("/api/temporal/runs/{slug}/status")
async def temporal_status(slug: str):
    """Phase-by-phase status: prefetching / running / completed / errored.
    Reads from disk so it survives server restarts and is observable by the
    UI without needing the in-memory task registry."""
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    rd = (WALKER_RUNS_DIR / safe).resolve()
    try:
        rd.relative_to(WALKER_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not rd.is_dir():
        return {"slug": slug, "phase": "unknown"}
    status_path = rd / "_temporal_status.json"
    if status_path.exists():
        try:
            return json.loads(status_path.read_text())
        except Exception:
            pass
    # Legacy: completed run from before status-file was added
    if (rd / "state.json").exists():
        return {"slug": slug, "phase": "completed_prior"}
    return {"slug": slug, "phase": "unknown"}


@app.post("/api/temporal/runs/{slug}/stop")
async def temporal_stop(slug: str):
    """Request a graceful stop. Walker checks the flag at the top of each
    turn loop iteration and exits cleanly — preserving artifacts already
    written. Idempotent."""
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    rd = (WALKER_RUNS_DIR / safe).resolve()
    try:
        rd.relative_to(WALKER_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not rd.is_dir():
        raise HTTPException(status_code=404, detail="run not found")
    (rd / "_stop_requested.flag").write_text("requested")
    return {"slug": slug, "stop_requested": True}


# ---------------------------------------------------------------------------
# Hierarchy mode endpoints (mirror /api/walker/* under /api/temporal/runs/*)
# ---------------------------------------------------------------------------

def _temporal_run_dir(slug: str) -> "Path":
    safe = slug.replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise HTTPException(status_code=400, detail="invalid slug")
    rd = (WALKER_RUNS_DIR / safe).resolve()
    try:
        rd.relative_to(WALKER_RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug outside runs dir")
    if not rd.is_dir():
        raise HTTPException(status_code=404, detail="run not found")
    return rd


@app.get("/api/temporal/runs/{slug}/trace/tail")
async def temporal_trace_tail(slug: str, offset: int = 0):
    """Alias of /api/walker/{slug}/trace/tail — same JSONL file, hierarchy-
    or single-walker-agnostic. Lets the v2 UI poll one endpoint regardless
    of mode."""
    return await walker_trace_tail(slug, offset)


@app.get("/api/temporal/runs/{slug}/file/{filepath:path}")
async def temporal_file(slug: str, filepath: str):
    return await walker_file(slug, filepath)


@app.get("/api/temporal/runs/{slug}/hierarchy")
async def temporal_hierarchy(slug: str):
    """Live hierarchy snapshot — built from the trace tail. Returns:
        {
          "captain": {agent_id, state, cost_usd, turns_used, ...},
          "surveyors": [{agent_id, point_idx, state, tier, ..., investigators: [...]}],
        }
    Returns an empty hierarchy when the trace doesn't exist yet."""
    rd = _temporal_run_dir(slug)
    trace_path = rd / "walker_trace.jsonl"
    if not trace_path.is_file():
        return {"captain": None, "surveyors": [], "n_total_agents": 0}

    captain: dict | None = None
    surveyors: dict[str, dict] = {}     # agent_id -> dict
    investigators: dict[str, dict] = {} # agent_id -> dict

    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                aid = rec.get("agent_id")
                role = rec.get("agent_role")
                rt = rec.get("record_type")
                if not aid or not role:
                    continue
                if role == "captain":
                    if captain is None:
                        captain = {
                            "agent_id": aid, "state": "spawned",
                            "cost_usd": 0.0, "turns_used": 0,
                        }
                    if rt == "agent_completed":
                        captain["state"] = "completed"
                        captain["stop_reason"] = rec.get("stop_reason")
                    if "cost_usd" in rec and rec.get("record_type") == "turn_assistant":
                        captain["cost_usd"] = (captain.get("cost_usd", 0.0)
                                                + float(rec.get("cost_usd") or 0.0))
                        captain["turns_used"] = max(
                            captain.get("turns_used", 0),
                            int(rec.get("turn") or 0),
                        )
                elif role == "surveyor":
                    s = surveyors.setdefault(aid, {
                        "agent_id": aid,
                        "parent_agent_id": rec.get("parent_agent_id"),
                        "point_idx": rec.get("point_idx"),
                        "state": "spawned",
                        "cost_usd": 0.0,
                        "turns_used": 0,
                        "tier": None,
                        "investigators": [],
                    })
                    if rt == "agent_completed":
                        s["state"] = "completed"
                        s["stop_reason"] = rec.get("stop_reason")
                    if rt == "report_up" and rec.get("to_agent") == "captain":
                        rep = rec.get("report") or {}
                        s["tier"] = rep.get("tier")
                    if rec.get("record_type") == "turn_assistant":
                        s["cost_usd"] = (s.get("cost_usd", 0.0)
                                          + float(rec.get("cost_usd") or 0.0))
                        s["turns_used"] = max(
                            s.get("turns_used", 0),
                            int(rec.get("turn") or 0),
                        )
                elif role == "investigator":
                    inv = investigators.setdefault(aid, {
                        "agent_id": aid,
                        "parent_agent_id": rec.get("parent_agent_id"),
                        "point_idx": rec.get("point_idx"),
                        "year": rec.get("year"),
                        "state": "spawned",
                        "cost_usd": 0.0,
                        "turns_used": 0,
                        "usable": None,
                    })
                    if rt == "agent_completed":
                        inv["state"] = "completed"
                        inv["stop_reason"] = rec.get("stop_reason")
                    if rt == "report_up":
                        rep = rec.get("report") or {}
                        inv["usable"] = rep.get("usable")
                        inv["yaws_covered"] = rep.get("yaws_covered")
                    if rec.get("record_type") == "turn_assistant":
                        inv["cost_usd"] = (inv.get("cost_usd", 0.0)
                                            + float(rec.get("cost_usd") or 0.0))
                        inv["turns_used"] = max(
                            inv.get("turns_used", 0),
                            int(rec.get("turn") or 0),
                        )
    except Exception:
        pass

    # Attach investigators to surveyors by parent_agent_id
    for aid, inv in investigators.items():
        parent = inv.get("parent_agent_id")
        if parent and parent in surveyors:
            surveyors[parent]["investigators"].append(inv)

    surveyors_list = sorted(
        surveyors.values(),
        key=lambda s: (s.get("point_idx") if s.get("point_idx") is not None else 999),
    )
    for s in surveyors_list:
        s["investigators"].sort(key=lambda i: i.get("year") or 0)

    return {
        "captain": captain,
        "surveyors": surveyors_list,
        "n_total_agents": (
            (1 if captain else 0) + len(surveyors_list) + len(investigators)
        ),
    }


@app.get("/api/temporal/runs/{slug}/blackboard/street")
async def temporal_blackboard_street(slug: str):
    rd = _temporal_run_dir(slug)
    p = rd / "street_blackboard.json"
    if not p.is_file():
        return {"exists": False}
    try:
        return {"exists": True, **json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        return {"exists": True, "error": "could not parse blackboard"}


@app.get("/api/temporal/runs/{slug}/blackboard/wp/{point_idx}")
async def temporal_blackboard_point(slug: str, point_idx: int):
    rd = _temporal_run_dir(slug)
    p = rd / "evidence" / f"wp{int(point_idx):03d}_blackboard.json"
    if not p.is_file():
        return {"exists": False, "point_idx": point_idx}
    try:
        return {"exists": True, **json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        return {"exists": True, "error": "could not parse blackboard"}


@app.get("/temporal")
async def temporal_index():
    # Serve v2 by default
    return FileResponse(STATIC_DIR / "temporal_v2.html")


@app.get("/temporal/{slug}")
async def temporal_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "temporal_v2.html")


@app.get("/temporal_v1")
async def temporal_v1_index():
    """Fallback to v1 UI if needed."""
    return FileResponse(STATIC_DIR / "temporal.html")


@app.get("/temporal_v1/{slug}")
async def temporal_v1_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "temporal.html")


# ---------------------------------------------------------------------------
# Decisions dashboard — DOT-engineer view of completed corridor runs
# ---------------------------------------------------------------------------
# - GET  /decisions               → list of completed corridors
# - GET  /decisions/{slug}        → per-corridor decision dashboard
# - GET  /api/decisions/runs      → JSON enumeration of dashboardable runs
# - POST /api/decisions/{slug}/synthesize  → idempotent Opus synthesis call;
#         ?force=1 re-runs and overwrites the cache.
#
# Designed to be additive: no existing routes change, and viewport / evidence
# JPEGs are served via the existing /api/walker/{slug}/file/{filepath:path}.

def _tier_rank(tier: str | None) -> int:
    return {"unknown": 0, "Good": 1, "Fair": 2, "Poor": 3}.get(str(tier or ""), 0)


def _tier_distribution(point_summaries: list[dict]) -> dict:
    out = {"Good": 0, "Fair": 0, "Poor": 0, "unknown": 0}
    for ps in point_summaries:
        t = str(ps.get("tier") or "unknown")
        if t not in out:
            t = "unknown"
        out[t] += 1
    return out


def _completed_ms(street_bb: dict) -> int | None:
    pss = street_bb.get("point_summaries") or []
    if not pss:
        return None
    completes = [
        int(p.get("completed_ms"))
        for p in pss
        if isinstance(p.get("completed_ms"), (int, float))
    ]
    return max(completes) if completes else None


@app.get("/api/decisions/runs")
async def list_decisions_runs():
    """Enumerate walker runs that have a non-empty street_blackboard.json
    (i.e. at least one point graded). Sorted newest-first by completed_ms."""
    if not WALKER_RUNS_DIR.exists():
        return {"runs": []}
    out: list[dict] = []
    for p in sorted(WALKER_RUNS_DIR.iterdir()):
        if not p.is_dir():
            continue
        sb_path = p / "street_blackboard.json"
        if not sb_path.exists():
            continue
        try:
            sb = json.loads(sb_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pss = sb.get("point_summaries") or []
        if not pss:
            continue
        cfg = {}
        cfg_path = p / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        # Only show hierarchy-mode runs (the dashboard is built around the
        # hierarchy blackboard schema). Legacy/single-walker runs are skipped.
        if str(cfg.get("mode", "hierarchy")) != "hierarchy":
            continue
        synth_exists = (p / "decisions_synthesis.json").exists()
        out.append({
            "slug": p.name,
            "street_name": cfg.get("street_name") or sb.get("street_name"),
            "length_m": cfg.get("length_m"),
            "n_points_total": int(sb.get("n_points_total") or len(pss)),
            "n_points_graded": len(pss),
            "tier_distribution": _tier_distribution(pss),
            "completed_ms": _completed_ms(sb),
            "synthesis_cached": synth_exists,
        })
    out.sort(key=lambda r: r.get("completed_ms") or 0, reverse=True)
    return {"runs": out}


@app.get("/api/decisions/{slug}/bundle")
async def decisions_bundle(slug: str):
    """Everything the dashboard needs to render a single corridor: config,
    street geometry, waypoints, full street_blackboard, and the cached
    synthesis if one exists."""
    rd = _walker_run_dir(slug)

    def _load(name: str, default):
        p = rd / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    # Per-point evidence — keyed by point_idx for easy UI lookup.
    evidence_dir = rd / "evidence"
    per_point: dict[str, dict] = {}
    if evidence_dir.is_dir():
        for f in sorted(evidence_dir.iterdir()):
            if not f.is_file() or not f.name.endswith("_blackboard.json"):
                continue
            try:
                wp = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            idx = wp.get("point_idx")
            if isinstance(idx, int):
                per_point[str(idx)] = wp

    # Build a lookup of available viewport files per image_id so the UI can
    # render the *actual* file on disk without listing the whole dir client-side.
    viewport_index: dict[str, list[str]] = {}
    vp_dir = rd / "viewports"
    if vp_dir.is_dir():
        for f in sorted(vp_dir.iterdir()):
            if not f.is_file():
                continue
            stem_id = f.name.split("_", 1)[0]
            if stem_id:
                viewport_index.setdefault(stem_id, []).append(f.name)

    return JSONResponse({
        "slug": slug,
        "config": _load("config.json", {}),
        "street": _load("street.geojson", {"type": "FeatureCollection", "features": []}),
        "waypoints": _load("waypoints.geojson", {"type": "FeatureCollection", "features": []}),
        "street_blackboard": _load("street_blackboard.json", {}),
        "per_point_evidence": per_point,
        "synthesis": _load("decisions_synthesis.json", None),
        "viewport_index": viewport_index,
    })


@app.post("/api/decisions/{slug}/synthesize")
async def decisions_synthesize(slug: str, force: int = 0):
    """Run (or return cached) corridor synthesis. Idempotent unless force=1."""
    rd = _walker_run_dir(slug)
    sb = rd / "street_blackboard.json"
    if not sb.exists():
        raise HTTPException(status_code=409, detail="survey_incomplete")

    # Cache hit short-circuit (same logic as decisions.synthesize_corridor,
    # duplicated here to avoid loading anthropic SDK if we don't need to).
    cache_path = rd / "decisions_synthesis.json"
    if cache_path.exists() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # fall through to re-synthesize

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")

    import anthropic  # type: ignore

    from .decisions import synthesize_corridor

    aclient = anthropic.AsyncAnthropic(api_key=anthropic_key)
    try:
        out = await synthesize_corridor(rd, aclient, force=bool(force))
    except RuntimeError as exc:
        if str(exc) == "survey_incomplete":
            raise HTTPException(status_code=409, detail="survey_incomplete")
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"synthesis_invalid: {exc}")
    return out


@app.get("/decisions")
async def decisions_index():
    return FileResponse(STATIC_DIR / "decisions.html")


@app.get("/decisions/{slug}")
async def decisions_index_slug(slug: str):
    return FileResponse(STATIC_DIR / "decisions.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
