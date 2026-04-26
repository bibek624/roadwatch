import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import mercantile
from shapely.geometry import Point, shape
from vt2geojson.tools import vt_bytes_to_geojson

TILE_URL = "https://tiles.mapillary.com/maps/vtp/mly1_public/2/{z}/{x}/{y}"
GRAPH_URL = "https://graph.mapillary.com/{image_id}"
TILE_ZOOM = 14
MAX_CAPTURES = 2000


def _geom(polygon: dict[str, Any]) -> dict[str, Any]:
    if polygon.get("type") == "Feature":
        return polygon["geometry"]
    return polygon


def _polygon_shape(polygon: dict[str, Any]):
    return shape(_geom(polygon))


def _bbox(polygon: dict[str, Any]) -> tuple[float, float, float, float]:
    coords = _geom(polygon)["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def _parse_captured_at(raw: Any) -> tuple[str, int] | None:
    """Mapillary captured_at may be ms-since-epoch (int) or ISO string. Returns (YYYY-MM-DD, year)."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
        else:
            s = str(raw)
            if s.isdigit():
                dt = datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d"), dt.year
    except (ValueError, OSError, OverflowError):
        return None


async def _fetch_tile(client: httpx.AsyncClient, tile: mercantile.Tile, token: str) -> bytes | None:
    url = TILE_URL.format(z=tile.z, x=tile.x, y=tile.y)
    try:
        r = await client.get(url, params={"access_token": token})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


async def fetch_captures(
    polygon: dict[str, Any], token: str, max_captures: int = MAX_CAPTURES,
    panos_only: bool = False,
) -> dict[str, Any]:
    """Fetch Mapillary captures inside a polygon.

    panos_only=True filters non-pano (flat camera) captures BEFORE the cap is
    applied — useful in dense urban areas where contributor cell-phone uploads
    dominate the first 2000 features and starve out the 360° pano fleet.
    """
    west, south, east, north = _bbox(polygon)
    tiles = list(mercantile.tiles(west, south, east, north, TILE_ZOOM))
    poly_shape = _polygon_shape(polygon)

    seen: set[str] = set()
    captures: list[dict[str, Any]] = []
    truncated = False

    limits = httpx.Limits(max_connections=16, max_keepalive_connections=16)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        sem = asyncio.Semaphore(12)

        async def worker(tile: mercantile.Tile):
            async with sem:
                return tile, await _fetch_tile(client, tile, token)

        results = await asyncio.gather(*(worker(t) for t in tiles))

    for tile, blob in results:
        if truncated or not blob:
            continue
        try:
            gj = vt_bytes_to_geojson(blob, tile.x, tile.y, tile.z, layer="image")
        except Exception:
            continue
        for feat in gj.get("features", []):
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            is_pano = bool(props.get("is_pano", False))
            if panos_only and not is_pano:
                continue
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            if not poly_shape.contains(Point(lon, lat)):
                continue
            image_id = str(props.get("id") or props.get("image_id") or "")
            if not image_id or image_id in seen:
                continue
            parsed = _parse_captured_at(props.get("captured_at"))
            if not parsed:
                continue
            date_str, year = parsed
            seen.add(image_id)
            captures.append({
                "image_id": image_id,
                "lat": lat,
                "lon": lon,
                "captured_at": date_str,
                "year": year,
                "is_pano": is_pano,
            })
            if len(captures) >= max_captures:
                truncated = True
                break

    years_available = sorted({c["year"] for c in captures})
    return {"captures": captures, "years_available": years_available, "truncated": truncated}


async def fetch_panos_near_point(
    lat: float,
    lon: float,
    token: str,
    radius_m: float = 30.0,
    max_age_years: float | None = 3.0,
    panos_only: bool = True,
    max_captures: int = 200,
) -> list[dict[str, Any]]:
    """Fetch panos within `radius_m` of (lat, lon).

    Mapillary's Graph API `closeto` endpoint caps at 25 m so we use the
    existing tile path: fetch the (one or two) zoom-14 tiles covering the
    point + a small buffer, parse, filter to the radius via haversine.
    Returns a list of dicts (same shape as fetch_captures().captures), sorted
    by descending captured_at (most recent first).
    """
    from datetime import datetime, timezone, timedelta
    import math

    # Build a tiny bbox around the point — radius_m on each side.
    # 1 degree latitude ≈ 111,320 m; longitude scales by cos(lat).
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.0001, math.cos(math.radians(lat))))
    poly = {
        "type": "Polygon",
        "coordinates": [[
            [lon - dlon, lat - dlat],
            [lon + dlon, lat - dlat],
            [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat],
            [lon - dlon, lat - dlat],
        ]],
    }
    cutoff_year = None
    if max_age_years is not None:
        cutoff_year = (datetime.now(timezone.utc)
                       - timedelta(days=int(365 * max_age_years))).year

    cap_data = await fetch_captures(
        poly, token, max_captures=max_captures, panos_only=panos_only
    )
    captures = cap_data.get("captures", [])

    EARTH_M = 6_371_000.0

    def hav(la1: float, lo1: float, la2: float, lo2: float) -> float:
        p1, p2 = math.radians(la1), math.radians(la2)
        dp = math.radians(la2 - la1)
        dl = math.radians(lo2 - lo1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * EARTH_M * math.asin(math.sqrt(a))

    out = []
    for c in captures:
        d = hav(lat, lon, c["lat"], c["lon"])
        if d > radius_m:
            continue
        if cutoff_year is not None and c.get("year", 0) < cutoff_year:
            continue
        c2 = dict(c)
        c2["dist_from_query_m"] = round(d, 1)
        out.append(c2)
    # Most recent first
    out.sort(key=lambda x: (x.get("captured_at") or ""), reverse=True)
    return out


DETAIL_FIELDS = (
    "thumb_1024_url,captured_at,geometry,is_pano,compass_angle,sequence,"
    "make,model,camera_type"
)


def _parse_detail(image_id: str, data: dict[str, Any]) -> dict[str, Any]:
    geom = data.get("geometry") or {}
    coords = geom.get("coordinates") or [0.0, 0.0]
    parsed = _parse_captured_at(data.get("captured_at"))
    captured_at = parsed[0] if parsed else ""
    compass = data.get("compass_angle")
    return {
        "image_id": image_id,
        "url": data.get("thumb_1024_url", ""),
        "captured_at": captured_at,
        "lat": coords[1],
        "lon": coords[0],
        "is_pano": bool(data.get("is_pano", False)),
        "compass_angle": float(compass) if compass is not None else None,
        "sequence": data.get("sequence"),
        "make": data.get("make"),
        "model": data.get("model"),
        "camera_type": data.get("camera_type"),
    }


async def _get_detail(client: httpx.AsyncClient, image_id: str, token: str) -> dict[str, Any]:
    r = await client.get(
        GRAPH_URL.format(image_id=image_id),
        params={"fields": DETAIL_FIELDS, "access_token": token},
    )
    r.raise_for_status()
    return _parse_detail(image_id, r.json())


async def fetch_image_detail(image_id: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await _get_detail(client, image_id, token)


async def fetch_image_detail_bulk(
    image_ids: list[str], token: str, concurrency: int = 12
) -> list[dict[str, Any]]:
    """Fetch Graph API detail for many image_ids in parallel.
    Returns a list aligned with input; failures become {"image_id": id, "error": "..."}."""
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        async def one(image_id: str) -> dict[str, Any]:
            async with sem:
                try:
                    return await _get_detail(client, image_id, token)
                except (httpx.HTTPError, ValueError) as e:
                    return {"image_id": image_id, "error": str(e)}
        return await asyncio.gather(*(one(i) for i in image_ids))
