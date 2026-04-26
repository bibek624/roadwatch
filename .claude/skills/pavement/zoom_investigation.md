---
name: zoom_investigation
scope: when to zoom on suspected distress, how to zoom effectively
references: [4]
---

# Zoom investigation — confirm distress with detail BEFORE grading

A wide-angle view can mask defects that are unmistakable at zoom. This skill governs when zoom is required and how to zoom effectively.

## When to zoom (a hard requirement, not a suggestion)

If your wide viewport (hfov ≥ 70) shows ANY of these → **you MUST zoom before grading:**

- Dark lines running across the pavement (could be cracks, could be paint, could be shadow — you can't tell at this resolution)
- Mottled / blotchy / weathered texture
- Possible patches (rectangular tonal differences)
- Surface that "looks weathered" but you can't pinpoint specific defects
- Anything that triggers "might be a defect, hard to tell"

The zoom is what distinguishes a confident grade from a vibe-based grade. See [`grade_discipline.md`](grade_discipline.md) for why this matters.

## How to zoom — TWO tools, choose the right one

### Tool A — `zoom_into_region` (PREFERRED for drilling into the previous view)

When you've just done a `look()` and want to zoom INTO a specific area of that view — **always prefer `zoom_into_region`**.

```
zoom_into_region(
    image_id=<same>,
    source_yaw_deg=<copy from previous look's caption>,
    source_pitch_deg=<copy>,
    source_hfov_deg=<copy>,
    x1=..., y1=..., x2=..., y2=...,    # bbox in your previous view's pixel coords
    purpose='zoom on suspected <X>',
)
```

**Why this is preferred over `look(narrow hfov)`**: a car-rig pano renders the camera-vehicle hood across the bottom 40-50% of the viewport. If you `look()` again at narrower hfov but the same yaw/pitch, the hood STILL fills the bottom of your zoom — so you've zoomed into the same scene, including the same hood. Zoom factor = 2× but you're now seeing 2× the hood.

`zoom_into_region` lets you **point at the upper portion of your previous view** (where the road actually is) and the system computes the right (yaw, pitch, hfov) to render that region without the hood.

**Bbox examples that work for the carrier-dominated case:**

```
# Skip the carrier: zoom into the upper half of the previous view
zoom_into_region(... x1=0.20, y1=0.05, x2=0.80, y2=0.45,
                 purpose='zoom on road in upper half above carrier')
# This gives ~1.7× zoom into a region above the hood

# Tight 3× zoom on a defect at upper-left of previous view
zoom_into_region(... x1=0.10, y1=0.10, x2=0.45, y2=0.40,
                 purpose='3× zoom on alligator pattern in left wheelpath')

# Zoom on a crack you spotted at center-right
zoom_into_region(... x1=0.55, y1=0.30, x2=0.85, y2=0.55,
                 purpose='zoom on right-wheelpath longitudinal crack')
```

**Bbox-width to zoom-factor mapping:**

| bbox width (x2 - x1) | Zoom factor | Use case |
|---|---|---|
| 0.25 | ~4× | Tight detail on a small feature (hairline crack, single patch edge) |
| 0.33 | ~3× | Confirm severity (alligator vs block) |
| 0.50 | ~2× | Standard zoom |
| 0.67 | ~1.5× | Clear context, mild magnification |

**Read the new minimap.** After `zoom_into_region`, the new viewport has its own minimap inset. The red rectangle should be SMALLER than your previous view's rectangle (you zoomed in) and POSITIONED somewhere different (you re-aimed). If the new minimap rectangle is at the TOP of the band → your zoom is now horizon-level / above-carrier (good). If it's still touching the bottom → you accidentally aimed back into the carrier zone; pick a different bbox.

### Tool B — `look(narrow hfov)` (for changing direction)

Use `look(image_id, yaw=<DIFFERENT>, pitch=-25, hfov=35-45)` ONLY when you want to look at a **different direction** (not in your current frame at all). Examples:

- "I see possible cracks in my forward view; let me also check the back" → `look(yaw=180, pitch=-25, hfov=70)` — fresh wide view, then if needed `zoom_into_region` on it.
- "I want to confirm the road extends past where I can see" → look at yaw=±90 to see the side road.

**Don't use `look()` to "zoom" by narrowing hfov on the same yaw/pitch — that re-renders the same scene including the hood. Use `zoom_into_region` instead.**

### Three knobs (when you DO use `look()` for a new direction)

1. **yaw** — different from your previous view. You're exploring, not zooming.
2. **pitch** — choose for the new direction. -10 to -15 to clear carrier, -25 to -30 standard, -45+ for close-up.
3. **hfov** — 70-80 for general scan, 40-50 for moderate detail. Use `zoom_into_region` instead of look-with-narrow-hfov.

### Pixel arithmetic

A 5 mm crack at 8 m horizontal distance subtends ~0.036°. At 768-px-wide rendering:

| hfov | px-per-degree | crack px | Visibility |
|---|---|---|---|
| 70 | 11.0 | 0.4 | Invisible |
| 50 | 15.4 | 0.55 | Marginal |
| 40 | 19.2 | 0.7 | Resolvable in context |
| 30 | 25.6 | 0.9 | Easily resolved |

Zooming doesn't make the crack physically wider — it puts more sensor pixels per real-world degree. The crack-vs-paint vs crack-vs-shadow disambiguation becomes mechanical at the right hfov.

## Common zoom patterns

### Pattern 1: zoom on the wheelpath

You suspect alligator cracking in the right wheelpath of a back view (yaw=180).

```
look(image_id, yaw=180, pitch=-30, hfov=40,
     purpose='zoom on suspected alligator in right wheelpath')
```

Why pitch=-30 vs -25: the wheelpath at the same yaw is a few meters closer to the camera at -30, so the cracks are bigger relative to the frame.

### Pattern 2: zoom on a patch boundary

You see what looks like a rectangular patch but the boundary is blurry.

```
look(image_id, yaw=<same>, pitch=-35, hfov=45,
     purpose='zoom on patch boundary to confirm patch vs tonal variation')
```

The patch boundary should resolve as a clean line / seam at zoom. If it stays diffuse / gradient, it's tonal variation from weathering, not a patch.

### Pattern 3: zoom on a crack near the lane line

Is that a longitudinal crack, or just the worn lane line?

```
look(image_id, yaw=<same>, pitch=-30, hfov=35,
     purpose='zoom on possible longitudinal crack near edge stripe')
```

At zoom: paint has crisp uniform edges; cracks have ragged variable edges. See [`visual_confusers.md`](visual_confusers.md) tells.

### Pattern 4: zoom on possible pothole

You see a dark spot. Pothole or shadow or oil?

```
look(image_id, yaw=<same>, pitch=-40, hfov=40,
     purpose='zoom on dark spot — pothole vs shadow')
```

A pothole resolves at zoom as a sharp-edged hole with visible base material at the bottom. A shadow stays uniform-dark and edges remain diffuse. Oil stays glossy / sheen-y.

### Pattern 5: zoom on intersection / stop-bar area

Cracking around stop bars and intersections is common (heavy braking + acceleration loads). You see possible cracking near the stop bar.

```
look(image_id, yaw=<same>, pitch=-35, hfov=35,
     purpose='zoom on cracks around STOP marking')
```

This was the pattern that revealed alligator on SPRING ST WP2 in the v4 walker run.

## When NOT to zoom

- The wide view shows clearly intact / clearly defective pavement and you have high confidence already → grade. Don't pad turns.
- You've already zoomed once on this region and it confirmed the distress — additional zooms have diminishing returns. Move on / grade.
- The question is "is there ANY distress" and the wide view shows uniform clean asphalt → grade Good or Sat. Zoom isn't proving a negative.

## Anti-pattern: routine zoom

Don't zoom on every viewport "to be safe." Routine zooming wastes turns. The zoom is for **specific suspected distress**, not for "let me get a better look in general."

## How many zooms per waypoint

Typical: 0-1 zooms.

- 0 zooms: wide view was decisive (clearly intact OR clearly defective).
- 1 zoom: wide view showed possible distress; one zoom confirmed.
- 2+ zooms: multiple suspect regions in the same view. Zoom each. Common at intersection waypoints with both wheelpath cracking AND patches near the stop bar.

3+ zooms on the same image_id is rare but not wrong. If you're truly investigating a complex scene, do it.

## Confidence after zoom

The zoom should change your confidence:

- Wide view: "looks weathered, possible cracks" — confidence ~0.5
- Zoom confirms alligator: confidence → 0.85+
- Zoom shows it was just paint / shadow: confidence on a Good/Sat grade → 0.85+

If after zooming you're STILL not sure, that's a signal the imagery quality is genuinely too poor — consider a different candidate, or grade `unknown`.

## Sources

- [4] Pavement Interactive — distress reference desk (visual cues that zoom resolves).
- Empirical — SPRING ST walker v4 run showed Poor-tier upgrades on WP1 and WP2 directly attributable to zoom investigation revealing alligator pattern that wide views missed.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
