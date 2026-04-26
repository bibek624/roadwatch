---
name: viewport_geometry
scope: yaw / pitch / hfov conventions; minimap interpretation; zoom math
---

# Viewport geometry — your camera-control vocabulary

This skill defines what `yaw_deg`, `pitch_deg`, and `hfov_deg` do when you call `look()` or `look_around()`, and how to read the minimap inset that comes back with every `look()`.

## yaw, pitch, hfov

When you call `look(image_id, yaw_deg=Y, pitch_deg=P, hfov_deg=H, purpose=...)`:

- **`yaw_deg`** rotates left-right around the vertical axis. **`yaw=0` is camera-forward** (the vehicle's travel direction in Mapillary's convention). +90 = right, -90 (or 270) = left, ±180 = back.
- **`pitch_deg`** tilts up-down. **`pitch=0` is horizon.** Negative = looking down, positive = looking up.
- **`hfov_deg`** is the horizontal field of view in degrees. Larger = wider scene; smaller = zoomed in. Vertical FOV scales with output aspect ratio (vfov = hfov × out_h / out_w).

The output is rendered at 768 × 512 px (a 3:2 ratio).

## The minimap inset (every `look()` returns this)

Every `look()` returns the rendered viewport WITH a small minimap pasted at the bottom-right. The minimap is a **CROPPED slice of the source equirect** — only the pavement-relevant pitch band (**+10° down to -60°**) is shown. Sky (above +10°) and the camera-carrier zone (below -60°, where vehicle hood / pedestrian body / handlebars always live) are CROPPED OUT. Every minimap pixel is road-relevant context.

A **RED RECTANGLE** marks where the current viewport samples within this band.

### Reading rectangle POSITION

| Rectangle position | What it tells you |
|---|---|
| Upper third of minimap | You're pitched at or near horizon (pitch ≈ 0 to -10°) → road in lower half of viewport, often the cleanest framing |
| Middle third | You're pitched moderately down (pitch ≈ -15 to -35°) → road fills most of viewport, carrier may show in bottom |
| Touching the BOTTOM edge | You're pitched into the carrier zone (pitch < -45°) → bottom of viewport is almost certainly carrier (hood / person / bike). **Pitch up.** |
| **No rectangle visible** | Your viewport is entirely outside the pavement band (looking very far up or straight down). Re-look at a saner pitch. |

### Reading rectangle SIZE

| Rectangle size (horizontal) | What it tells you |
|---|---|
| LARGE (covers ~30%+ of minimap width) | Wide hfov (≥ 70°). Good for context / orient / scan. |
| MEDIUM | Standard view (hfov ≈ 60-80°). |
| SMALL (covers ~10% of width) | Zoomed in (hfov ≤ 40°). Good for confirming a specific defect. |

When you zoom on a suspected defect, expect a small rectangle — that's your visual confirmation that you have a high-detail view.

### Reading rectangle WRAP

If you look at yaw near ±180° with a wide hfov, the viewport's horizontal range crosses the equirect seam. The minimap shows TWO red rectangles — one near the left edge, one near the right edge. That's normal; the union of the two rectangles is the actual coverage.

### Horizontal axis of the minimap

The minimap shows the full 360° horizontally:
- yaw 0° (forward) at the **center**
- yaw +90° (right) at the **right of center** (75% across)
- yaw ±180° (back) at the **edges** (0% and 100%)
- yaw -90° / 270° (left) at the **left of center** (25% across)

So a red rectangle at the right edge of the minimap = looking back-right; a rectangle at the center = looking forward.

## Zoom math (why hfov matters quantitatively)

A given crack at a given distance projects to a finite number of pixels on your viewport. Narrowing hfov increases pixel density:

| hfov_deg | Pixels per degree | Effective magnification vs hfov=70 |
|---|---|---|
| 100° | 7.7 px/° | 0.7× |
| 70° (default) | 11.0 px/° | 1.0× |
| 50° | 15.4 px/° | 1.4× |
| 40° | 19.2 px/° | 1.7× |
| 35° | 21.9 px/° | 2.0× |
| 30° | 25.6 px/° | 2.3× |

A 5 mm crack at 8 m horizontal distance subtends about 0.036°. At hfov=70 that's ~0.4 pixels — invisible. At hfov=35 the same crack is ~0.8 pixels — still subpixel but the surrounding texture region is now sampled with 2× the detail, making the crack pattern resolvable.

For confirming a defect: target hfov around 30-45.

## Picking the right viewport — quick reference

| Goal | Recommended call |
|---|---|
| **Orient** at a new candidate | `look_around(pitch=-15, hfov=80)` — see all 4 directions |
| **Wide scan** of the cleanest direction | `look(yaw=<chosen>, pitch=-25, hfov=70)` |
| **Pitch up** to clear vehicle hood | `look(yaw=<same>, pitch=-10, hfov=70)` |
| **Look past the carrier** to side road | `look(yaw=±90, pitch=-25, hfov=80)` |
| **Drill into a region of your previous view** (zoom on a defect, skip the carrier band) | `zoom_into_region(image_id=<same>, source_yaw=, source_pitch=, source_hfov=, x1, y1, x2, y2, ...)` — point at the bbox in normalized coords. **Preferred over `look(narrow hfov)`** because it lets you skip the carrier band rather than re-rendering the same scene smaller. |
| **Close-up pavement** for raveling/texture | `look(yaw=<same>, pitch=-50, hfov=40)` (carrier may show) — or use `zoom_into_region` on the upper-third of a wider view |
| **Check overhead** for signs / signals | `look(yaw=<chosen>, pitch=+5, hfov=80)` |

## Don'ts

- **Don't go below pitch=-65** unless you have a specific reason to inspect right under the camera. The carrier dominates and the rendered area is tiny.
- **Don't go above pitch=+15** for pavement grading. You're looking at sky/buildings.
- **Don't go below hfov=30** — the rendered viewport becomes a tiny patch with high distortion at the edges.
- **Don't look at the same (yaw, pitch, hfov) twice on the same image_id**. The render is cached and you'll just spend a turn re-displaying what you already saw.

## Sources

Empirical / engineering — derived from `app/agent/street_walker.py:render_view` (uses `py360convert.e2p`) and direct visual inspection of rendered viewports across 4 walker runs (v1-v4) on SPRING ST.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
