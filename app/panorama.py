"""Equirectangular panorama -> rectilinear viewport extraction.

Mapillary equirectangular panos are oriented so that u_deg=0 (image center
column) points along the camera's travel direction. So for Tier 1 we can
render "forward" with u_deg=0 directly; compass_angle is only needed for
georeferenced use cases (e.g. BEV mosaic, cross-sequence alignment).

py360convert.e2p angle convention:
    u_deg: -left / +right   (yaw)
    v_deg: +up / -down      (pitch)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import py360convert
from PIL import Image

log = logging.getLogger(__name__)


@dataclass
class Viewport:
    name: str
    yaw: float           # deg, 0 = travel direction, + right
    pitch: float         # deg, 0 = horizon, + up
    hfov: float          # horizontal FOV deg (square output uses this for vfov too)
    out_hw: tuple[int, int] = (1024, 1024)
    path: str | None = None


# Category -> ordered list of viewport templates.
# Pitch negative = looking down; wider hfov = more peripheral context.
CATEGORY_RECIPES: dict[str, list[Viewport]] = {
    "near_miss": [
        Viewport("fwd_down", yaw=0, pitch=-35, hfov=95),
        Viewport("fwd", yaw=0, pitch=-5, hfov=90),
        Viewport("left_down", yaw=-90, pitch=-30, hfov=95),
        Viewport("right_down", yaw=90, pitch=-30, hfov=95),
    ],
    "sidewalk": [
        Viewport("fwd", yaw=0, pitch=-10, hfov=90),
        Viewport("left", yaw=-90, pitch=-5, hfov=90),
        Viewport("right", yaw=90, pitch=-5, hfov=90),
    ],
    "cpted": [
        Viewport("fwd", yaw=0, pitch=0, hfov=100),
        Viewport("left_wide", yaw=-75, pitch=-5, hfov=110),
        Viewport("right_wide", yaw=75, pitch=-5, hfov=110),
    ],
    "trucking": [
        Viewport("fwd_up", yaw=0, pitch=10, hfov=90),
        Viewport("fwd", yaw=0, pitch=-5, hfov=90),
    ],
    "transit": [
        Viewport("fwd", yaw=0, pitch=-5, hfov=90),
        Viewport("right", yaw=90, pitch=-10, hfov=100),
    ],
}
CATEGORY_RECIPES["default"] = CATEGORY_RECIPES["near_miss"]


def _wrap180(deg: float) -> float:
    d = ((deg + 180.0) % 360.0) - 180.0
    return d


def load_equirect(path: str) -> np.ndarray:
    """Load an equirectangular JPEG as uint8 HxWx3 RGB."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        return np.asarray(im, dtype=np.uint8)


def extract_viewport(equi: np.ndarray, vp: Viewport, yaw_offset: float = 0.0) -> np.ndarray:
    """Render one rectilinear perspective viewport from the equirect pano."""
    u_deg = _wrap180(vp.yaw + yaw_offset)
    # Square output: vfov == hfov.
    return py360convert.e2p(
        equi,
        fov_deg=(vp.hfov, vp.hfov),
        u_deg=u_deg,
        v_deg=vp.pitch,
        out_hw=vp.out_hw,
        in_rot_deg=0,
        mode="bilinear",
    )


def save_viewport(img: np.ndarray, path: str, quality: int = 90) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    Image.fromarray(img).save(path, format="JPEG", quality=quality, optimize=True)


def extract_viewports_for_category(
    pano_path: str,
    category: str,
    compass_angle: float | None = None,
    out_dir: str | None = None,
) -> list[Viewport]:
    """Render category-appropriate viewports from one equirectangular pano.

    `compass_angle` is accepted for forward compatibility but not applied to
    yaw — Mapillary panos already have travel direction at u_deg=0.
    """
    recipe = CATEGORY_RECIPES.get(category, CATEGORY_RECIPES["default"])
    equi = load_equirect(pano_path)

    if compass_angle is None:
        log.warning("no compass_angle provided; viewports rendered from pano-native frame "
                    "(forward = travel direction per Mapillary convention)")

    out: list[Viewport] = []
    for tpl in recipe:
        vp = Viewport(
            name=tpl.name, yaw=tpl.yaw, pitch=tpl.pitch, hfov=tpl.hfov,
            out_hw=tpl.out_hw,
        )
        img = extract_viewport(equi, vp, yaw_offset=0.0)
        if out_dir:
            p = os.path.join(out_dir, f"{vp.name}.jpg")
            save_viewport(img, p)
            vp.path = p
        out.append(vp)
    return out


# ---------------------------------------------------------------------------
# Pavement strips + distress overlays
# ---------------------------------------------------------------------------

# MUST match constants in app.geoproject — if you change these, change there too.
#
# Geometry chosen to match the orientation probe (render_orientation_probe).
# Probe users / Opus both see the same projection, so picking a yaw in the
# probe maps 1:1 to picking the right strip. Steeper pitches were tried first
# (-35°/-50°/-55°) but produced strips dominated by the vehicle's own body.
# A gentler -25° pitch with a 3:2 aspect ratio keeps the forward vanishing
# point and distant road surface in view while still covering close pavement
# in the bottom ~40% of the frame.
STRIP_HFOV = 100.0
STRIP_W = 768
STRIP_H = 512

# All four viewports share the same pitch now. Previously we pitched backward
# and sides steeper to "see past" the vehicle body, but that just packed the
# frame with more vehicle for tall rigs. Keeping one pitch matches the probe
# exactly and lets Opus ignore bad viewports rather than us trying to engineer
# around physics we can't fix.
STRIP_PITCH_FORWARD = -25.0
STRIP_PITCH_BACKWARD = -25.0
STRIP_PITCH_SIDE = -25.0

# Pano-frame yaw offset per viewport, assuming u=0 is the vehicle's travel
# direction. The pipeline's stage_orient corrects per-pano deviations by
# baking a `forward_yaw_offset` into the caller side.
VIEWPORT_YAW_OFFSETS = {
    "forward":   0.0,
    "backward": 180.0,
    "left":    -90.0,
    "right":    90.0,
}

VIEWPORT_PITCHES = {
    "forward":  STRIP_PITCH_FORWARD,
    "backward": STRIP_PITCH_BACKWARD,
    "left":     STRIP_PITCH_SIDE,
    "right":    STRIP_PITCH_SIDE,
}

VIEWPORT_SUFFIXES = {"forward": "f", "backward": "b", "left": "l", "right": "r"}


def extract_pavement_strip(
    equi: np.ndarray,
    viewport: str = "forward",
    *,
    forward_yaw_offset: float = 0.0,
    hfov: float = STRIP_HFOV,
    pitch: float | None = None,
    out_w: int = STRIP_W,
    out_h: int = STRIP_H,
) -> np.ndarray:
    """Render one down-pitched rectilinear viewport from an equirectangular pano.

    `viewport` selects one of {forward, backward, left, right}. The final yaw
    passed to py360convert is `forward_yaw_offset + VIEWPORT_YAW_OFFSETS[viewport]`.

    `forward_yaw_offset` is the per-pano correction in degrees. Most Mapillary
    panos have travel direction at u=0 (offset=0), but some rigs capture with
    the pano rotated 180° (offset=180). stage_orient detects this per-sequence.
    """
    if viewport not in VIEWPORT_YAW_OFFSETS:
        raise ValueError(f"unknown viewport {viewport!r}")
    yaw = forward_yaw_offset + VIEWPORT_YAW_OFFSETS[viewport]
    if pitch is None:
        pitch = VIEWPORT_PITCHES[viewport]
    vfov = hfov * out_h / out_w
    return py360convert.e2p(
        equi,
        fov_deg=(hfov, vfov),
        u_deg=_wrap180(yaw),
        v_deg=pitch,
        out_hw=(out_h, out_w),
        in_rot_deg=0,
        mode="bilinear",
    )


def extract_pavement_strips(
    pano_path: str,
    out_dir: str | None = None,
    image_id: str | None = None,
    forward_yaw_offset: float = 0.0,
    viewports: list[str] | None = None,
) -> dict[str, str | None]:
    """Render 4-way pavement strips (forward/backward/left/right) from one pano.

    Returns dict keyed by viewport name with the saved path (or None if not
    saved). Keys are always present for every viewport requested; value is
    None if rendering/saving failed. Default `viewports` = all four.

    `forward_yaw_offset` rotates the entire 4-way frame — this is how we
    correct panos whose u=0 column is NOT aligned with travel direction.
    """
    if viewports is None:
        viewports = ["forward", "backward", "left", "right"]
    equi = load_equirect(pano_path)
    stem = image_id or os.path.splitext(os.path.basename(pano_path))[0]
    result: dict[str, str | None] = {v: None for v in viewports}
    for vp in viewports:
        try:
            img = extract_pavement_strip(equi, vp, forward_yaw_offset=forward_yaw_offset)
        except Exception as e:
            log.warning(f"strip render failed for {stem} viewport={vp}: {e}")
            continue
        if out_dir:
            suffix = VIEWPORT_SUFFIXES[vp]
            path = os.path.join(out_dir, f"{stem}_{suffix}.jpg")
            save_viewport(img, path)
            result[vp] = path
    return result


def render_orientation_probe(
    pano_path: str,
    out_path: str,
    pitch: float = STRIP_PITCH_FORWARD,
    hfov: float = STRIP_HFOV,
    tile_w: int = STRIP_W // 2,
    tile_h: int = STRIP_H // 2,
) -> str:
    """Render a single composite image containing FOUR rectilinear crops at
    (u=0, u=90, u=180, u=270) arranged in a 2x2 grid. Used by stage_orient to
    let a cheap Haiku call pick which yaw is 'forward' for this pano.
    """
    from PIL import Image as PILImage
    equi = load_equirect(pano_path)
    vfov = hfov * tile_h / tile_w
    tiles = []
    for yaw in [0, 90, 180, 270]:
        img = py360convert.e2p(equi, fov_deg=(hfov, vfov), u_deg=_wrap180(yaw),
                               v_deg=pitch, out_hw=(tile_h, tile_w),
                               in_rot_deg=0, mode="bilinear")
        tiles.append(img)
    # stitch 2x2 grid
    grid = np.zeros((tile_h * 2, tile_w * 2, 3), dtype=np.uint8)
    grid[:tile_h, :tile_w] = tiles[0]          # top-left  = u=0
    grid[:tile_h, tile_w:] = tiles[1]          # top-right = u=90
    grid[tile_h:, :tile_w] = tiles[2]          # bot-left  = u=180
    grid[tile_h:, tile_w:] = tiles[3]          # bot-right = u=270
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    PILImage.fromarray(grid).save(out_path, format="JPEG", quality=85, optimize=True)
    return out_path


# ---- overlay rendering --------------------------------------------------

# Severity -> RGBA stroke color (PIL uses RGB, we fold alpha into a separate layer)
_SEVERITY_COLORS = {
    "minor":    (255, 215, 0),    # gold
    "moderate": (255, 140, 0),    # dark orange
    "severe":   (220, 20, 60),    # crimson
}
_SEVERITY_DEFAULT = (200, 200, 200)


def _severity_color(severity: str) -> tuple[int, int, int]:
    return _SEVERITY_COLORS.get((severity or "").lower(), _SEVERITY_DEFAULT)


def draw_distress_overlay(
    strip_path: str,
    distresses: list[dict],
    out_path: str,
    *,
    stroke_width: int = 3,
    draw_labels: bool = True,
) -> str:
    """Render a PNG with bboxes drawn on one pavement strip.

    `distresses` should already be filtered to this viewport (forward OR
    backward). Each entry needs `bbox`, `type`, `severity`, and optionally
    `approx_distance_m`.

    Returns `out_path`.
    """
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img = PILImage.open(strip_path).convert("RGBA")
    W, H = img.size
    overlay = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for d in distresses:
        bbox = d.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x1, y1, x2, y2 = [float(v) for v in bbox]
        x1, x2 = max(0, min(x1, x2)), min(W, max(x1, x2))
        y1, y2 = max(0, min(y1, y2)), min(H, max(y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        color = _severity_color(d.get("severity", ""))
        # translucent fill + solid stroke
        draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=stroke_width,
                       fill=color + (40,))
        if draw_labels:
            dtype = str(d.get("type", "")).replace("_", " ")
            dist = d.get("approx_distance_m")
            label = f"{dtype}" + (f" @ {dist}m" if dist else "")
            # measure text
            try:
                tb = draw.textbbox((0, 0), label, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
            except AttributeError:
                tw, th = draw.textsize(label, font=font)
            ly = max(0, y1 - th - 4)
            draw.rectangle([x1, ly, x1 + tw + 6, ly + th + 4],
                           fill=(0, 0, 0, 180))
            draw.text((x1 + 3, ly + 2), label, fill=(255, 255, 255, 255), font=font)

    composed = PILImage.alpha_composite(img, overlay).convert("RGB")
    composed.save(out_path, format="PNG", optimize=True)
    return out_path


# ---------------------------------------------------------------------------
# Tier 2 / Tier 3 stubs — advertised roadmap, not yet implemented.
# ---------------------------------------------------------------------------

def build_sequence_strip(
    image_ids: list[str],
    category: str,
    metadata: dict,
    window: int = 5,
) -> str:
    """Tier 2: tile the matching viewport from N consecutive panos in one
    Mapillary sequence into a single JPEG for temporal-agreement reasoning.

    A hazard visible in 3 consecutive frames is real; a one-frame blob is noise.
    """
    raise NotImplementedError("Tier 2 sequence-strip stitching not implemented yet")


def build_bev_mosaic(
    image_ids: list[str],
    metadata: dict,
    camera_height_m: float = 2.5,
) -> str:
    """Tier 3: reproject downward forward crops from consecutive panos onto
    the ground plane using GPS deltas + assumed camera height, then composite
    a synthetic top-down mosaic of the road surface — the 'digital twin'.
    """
    raise NotImplementedError("Tier 3 BEV ground mosaic not implemented yet")
