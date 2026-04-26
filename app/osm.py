import math
from typing import Any

import httpx
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import transform

OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

DRIVABLE_HIGHWAYS = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|service"

METERS_PER_DEG_LAT = 111_111.0


def _polygon_coords(polygon: dict[str, Any]) -> list[tuple[float, float]]:
    """Extract outer-ring coords as (lon, lat) pairs from a GeoJSON Polygon or Feature."""
    geom = polygon
    if polygon.get("type") == "Feature":
        geom = polygon["geometry"]
    if geom.get("type") != "Polygon":
        raise ValueError("Expected GeoJSON Polygon geometry")
    return [(lon, lat) for lon, lat in geom["coordinates"][0]]


def _poly_string(polygon: dict[str, Any]) -> str:
    """Overpass poly filter expects 'lat lon lat lon ...'."""
    coords = _polygon_coords(polygon)
    return " ".join(f"{lat} {lon}" for lon, lat in coords)


def build_overpass_query(polygon: dict[str, Any]) -> str:
    poly = _poly_string(polygon)
    return (
        f"[out:json][timeout:25];\n"
        f'way["highway"~"{DRIVABLE_HIGHWAYS}"](poly:"{poly}");\n'
        f"out geom;"
    )


def overpass_to_geojson(data: dict[str, Any]) -> dict[str, Any]:
    features = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "id": el.get("id"),
                "highway": tags.get("highway"),
                "name": tags.get("name"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


async def fetch_roads(polygon: dict[str, Any]) -> dict[str, Any]:
    import logging
    log = logging.getLogger("pavtrace.osm")
    query = build_overpass_query(polygon)
    headers = {"User-Agent": "PavTrace/0.1 (+https://github.com/pavtrace)"}
    errors: list[str] = []
    timeout = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for url in OVERPASS_ENDPOINTS:
            try:
                log.warning(f"Overpass: trying {url}")
                r = await client.post(url, data={"data": query})
                log.warning(f"Overpass: {url} -> HTTP {r.status_code} ({len(r.content)} bytes)")
                if r.status_code in (429, 502, 503, 504):
                    errors.append(f"{url}: HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                return overpass_to_geojson(r.json())
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                log.warning(f"Overpass: {url} failed -> {msg}")
                errors.append(f"{url}: {msg}")
                continue
    raise RuntimeError("Overpass mirrors all failed: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Street-by-name geocoding (Overpass)
# ---------------------------------------------------------------------------

# Ordered substitution rules. Each tuple is (pattern, options_to_try). We
# apply rules cumulatively — i.e. "S Chestnut St" expands to every 2^k
# combination of its applicable rules.
# Order matters: prefix rules (S/N/E/W) first, then suffix rules (St/Ave/etc.)
_STREET_RULES: list[tuple[str, list[str]]] = [
    ("S ", ["South "]),
    ("N ", ["North "]),
    ("E ", ["East "]),
    ("W ", ["West "]),
    (" St",   [" Street", " St."]),
    (" Ave",  [" Avenue", " Ave."]),
    (" Blvd", [" Boulevard", " Blvd."]),
    (" Rd",   [" Road", " Rd."]),
    (" Dr",   [" Drive", " Dr."]),
    (" Ln",   [" Lane", " Ln."]),
    (" Pkwy", [" Parkway", " Pkwy."]),
    (" Hwy",  [" Highway", " Hwy."]),
]


def _street_variants(name: str) -> list[str]:
    """Generate all reasonable variants by layering every applicable
    substitution rule. "S Chestnut St" -> {"S Chestnut St",
    "South Chestnut St", "S Chestnut Street", "South Chestnut Street", ...}.
    Applies each rule only where the pattern is actually present."""
    variants = {name}
    for pattern, replacements in _STREET_RULES:
        # only expand if pattern is present somewhere in any current variant
        new = set(variants)
        for v in variants:
            if pattern in v:
                for repl in replacements:
                    new.add(v.replace(pattern, repl))
        variants = new
    return sorted(variants, key=len, reverse=True)


def build_street_query(
    name: str, city: str, postcode: str | None = None,
    search_bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """Build an Overpass query for all ways matching `name` within `city`.

    - `name` is expanded to multiple variants (S→South, St→Street, etc.) and
      OR'd into a single case-insensitive regex.
    - `postcode` is NOT filtered at the Overpass level (addr:postcode is rarely
      set on ways); it's a hint the caller can use to narrow the area if
      needed.
    - Area lookup uses `name="<city>"` without admin_level filter to be more
      forgiving (Ventura is admin_level=8, but some cities vary).
    """
    variants = _street_variants(name)
    # Escape regex special characters inside each variant.
    def _esc(s: str) -> str:
        for ch in r".^$*+?|()[]{}\\":
            s = s.replace(ch, "\\" + ch)
        return s
    escaped = [_esc(v) for v in variants]
    name_regex = "^(" + "|".join(escaped) + ")$"
    # Overpass requires quotes inside the regex to be escaped
    name_regex = name_regex.replace('"', r'\"')

    area_clause = f'area[name="{city}"]->.searchArea;'
    in_area = "(area.searchArea)"
    filters = f'["name"~"{name_regex}",i]'

    bbox_clause = ""
    if search_bbox is not None:
        s, w, n, e = (
            search_bbox[1], search_bbox[0], search_bbox[3], search_bbox[2]
        )
        bbox_clause = f"({s},{w},{n},{e})"

    query = (
        f"[out:json][timeout:25];\n"
        f"{area_clause}\n"
        f"(\n"
        f'  way["highway"~"{DRIVABLE_HIGHWAYS}"]{filters}{in_area};\n'
        + (f'  way["highway"~"{DRIVABLE_HIGHWAYS}"]{filters}{bbox_clause};\n'
           if bbox_clause else "")
        + f");\n"
        f"out geom;"
    )
    return query


async def fetch_street_by_name(
    name: str, city: str, postcode: str | None = None,
    search_bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Query Overpass for ways whose `name` matches `name` inside `city`.

    Returns a FeatureCollection of LineString features. Tries all mirrors and
    returns the first NON-EMPTY result (mirror indexes sometimes lag, so an
    empty response from one mirror is not authoritative).
    """
    import logging
    log = logging.getLogger("pavtrace.osm")
    query = build_street_query(name, city, postcode=postcode, search_bbox=search_bbox)
    headers = {"User-Agent": "PavTrace/0.1 (+https://github.com/pavtrace)"}
    errors: list[str] = []
    last_result: dict[str, Any] | None = None
    # overpass-api.de has the most complete area index — try it first
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.ru/api/interpreter",
    ]
    timeout = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for url in mirrors:
            try:
                log.warning(f"Overpass street-by-name: {url}  name={name!r} city={city!r}")
                r = await client.post(url, data={"data": query})
                if r.status_code in (429, 502, 503, 504):
                    errors.append(f"{url}: HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                gj = overpass_to_geojson(r.json())
                if gj.get("features"):
                    return gj
                # empty — record and try next mirror
                last_result = gj
                errors.append(f"{url}: 0 features")
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}: {e}")
                continue
    if last_result is not None:
        # all mirrors returned empty; surface that (caller will error out)
        return last_result
    raise RuntimeError("Overpass mirrors all failed: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Buffer LineString(s) -> Polygon for Mapillary capture fetch
# ---------------------------------------------------------------------------

def _to_local_meters(linestrings: list[LineString], ref_lat: float):
    """Project WGS84 lon/lat to a local equirectangular (meters) frame around
    ref_lat, for meter-accurate buffering. Returns (projected, inverse_fn)."""
    lat_scale = METERS_PER_DEG_LAT
    lon_scale = METERS_PER_DEG_LAT * math.cos(math.radians(ref_lat))

    def fwd(x, y, z=None):
        return (x * lon_scale, y * lat_scale) if z is None else (x * lon_scale, y * lat_scale, z)

    def inv(x, y, z=None):
        return (x / lon_scale, y / lat_scale) if z is None else (x / lon_scale, y / lat_scale, z)

    projected = MultiLineString([transform(fwd, ls) for ls in linestrings])
    return projected, inv


def buffer_street_to_polygon(
    street_geojson: dict[str, Any], width_m: float = 8.0,
) -> dict[str, Any]:
    """Buffer the centerlines by `width_m` meters on each side → one GeoJSON
    Polygon (outer ring only; interior rings unioned away).
    """
    lines: list[LineString] = []
    all_lats: list[float] = []
    for feat in street_geojson.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lines.append(LineString(coords))
        all_lats.extend(c[1] for c in coords)
    if not lines:
        raise ValueError("No LineString features to buffer — street not found")
    ref_lat = sum(all_lats) / len(all_lats)
    projected, inv = _to_local_meters(lines, ref_lat)
    buffered_m = projected.buffer(width_m, cap_style=2, join_style=2)
    # unproject back to lon/lat
    buffered = transform(inv, buffered_m)
    geom = mapping(buffered)
    # If the buffer produced a MultiPolygon (disjoint ways), keep the unioned
    # convex hull to give Mapillary one polygon. Otherwise return as-is.
    if geom["type"] == "MultiPolygon":
        # Convert to a Polygon covering the union by taking the convex hull.
        hull = buffered.convex_hull
        geom = mapping(hull)
    if geom["type"] != "Polygon":
        raise ValueError(f"Unexpected buffered geometry type: {geom['type']}")
    return geom


def street_geojson_to_linestrings(street_geojson: dict[str, Any]) -> list[LineString]:
    """Helper: pull out the raw LineString geometries for downstream use
    (e.g. road-centerline clamping in geoproject)."""
    out: list[LineString] = []
    for feat in street_geojson.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") == "LineString":
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                out.append(LineString(coords))
    return out
