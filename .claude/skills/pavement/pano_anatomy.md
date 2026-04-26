---
name: pano_anatomy
scope: What a Mapillary 360° equirectangular panorama actually looks like
references: [18]
---

# Mapillary pano anatomy

You receive viewports rendered from Mapillary 360° equirectangular panoramas. The structure of the source image determines what's possible to see and where the carrier (camera-mounting platform) shows up. This skill teaches you that geometry so you can choose viewport parameters intelligently.

## The equirect coordinate system

A Mapillary equirect is a 2:1 image (typically 5760 × 2880 px for full resolution; 1024 × 512 for thumbnails) representing a full 360° × 180° sphere of view from one camera position.

```
        +90° pitch (zenith — straight up)
         ┌─────────────────────────┐
         │            sky          │
         │                         │
   horiz │ ────────── 0° ──────────│   ← horizon line is the middle row
         │                         │
         │           ground        │
         │   ╔═════════════════╗   │
         │   ║camera carrier   ║   │   ← carrier ALWAYS lives in the bottom band
         └───╨─────────────────╨───┘
        -90° pitch (nadir — straight down)
        
   yaw: -180° ──── 0° (forward) ─── +180°
```

Yaw 0 = forward (camera vehicle's travel direction). Pitch 0 = horizon. Pitch < 0 = looking down. Pitch > 0 = looking up.

## Capture rigs and where the carrier sits

Mapillary is crowdsourced. Different uploaders use different rigs, and the rig determines where its body sits in the equirect's bottom band.

### Car / SUV roof rig (the easiest to grade from)

- Bottom band of equirect = dark glossy car roof
- Often a sunroof outline visible
- The rig's antenna pokes up; sometimes visible as a vertical streak in viewports
- Forward viewport at pitch=-30 shows windshield top edge at the bottom of frame, with road visible past the hood

**Implication for viewport choice.** Pitch=-30 viewports will have the lower 30-40% as roof / hood. **Pitch up to -10 or -15** to clear the roof and see road further out.

### Pedestrian handheld 360 stick (very common in LA)

- Bottom shows the operator's HEAD, shoulders, arms with phone in hand
- Camera is at human height (~1.7 m), so the road horizon is much closer
- Operator may be on a sidewalk or crosswalk, NOT in the road
- Bottom-center viewport at any pitch < -45 shows the operator's body clearly

**Implication.** A pedestrian rig pano often does NOT show the road well even at horizon-level pitch — the operator may be standing on a sidewalk facing buildings. **Use `look_around` first to identify which yaw, if any, has road in view.** Sometimes the road is at yaw=180 (back) because the operator was photographing FROM the road side.

### Pedestrian chest / helmet mount

- Bottom shows the operator's torso, lap, jeans, shoes
- Looking down (pitch < -50) reveals dirt / gravel / sidewalk — usually not the road
- Operator is often walking on a hiking trail, dirt path, or pedestrian-only walkway

**Implication.** Many "Mapillary panos near LA street X" from chest-mount rigs are actually NOT on the street X — they're on adjacent sidewalks or parks. If `look_around` shows no asphalt road in any quadrant, this is likely a non-road capture. Grade `unknown`.

### Bicycle / scooter rig

- Bottom shows handlebars, helmet edge, cyclist's body
- Often in a bike lane (which IS road, just a marked lane)
- Camera mid-height (~1.0-1.2 m)

**Implication.** Bike-lane viewpoint is usable for grading the road; the bike-lane stripe is paint, not a defect. Just adjust pitch to clear the handlebars (pitch=-15 to -10).

### Tripod / static rig

- Bottom shows tripod legs forming a triangle, plus a small ground patch
- Camera positioned mid-height (~1.0-1.5 m)
- Operator typically not in frame (or only their feet)

**Implication.** Tripod panos tend to show clean horizons and may be the highest-quality capture available. If `peek_candidate` returns rig=tripod, prefer it.

## What pitch values reach what

| Pitch (looking down) | What's in your viewport |
|---|---|
| **0°** | Pure horizon. Far buildings + horizon line + nearest distance road in lower viewport. |
| **-10° to -15°** | "Sweet spot" for grading: road occupies most of the lower viewport, carrier is pushed below the frame, near-distance road (10-25 m ahead) visible clearly. |
| **-25° to -35°** | Standard pavement-forward angle. Road fills most of the frame; for car rigs the carrier hood is in the lower 20-30%. Good for general grading but watch for carrier. |
| **-45° to -55°** | Close-up pavement (3-8 m ahead). Carrier may dominate lower half. Useful for ZOOM if carrier is acceptable; not useful for orientation. |
| **-65° and lower** | Mostly carrier + immediate ground patch. Almost never useful. |
| **+5° to +15°** | Look up at signs / signals / overhead. Not relevant to pavement grading; useful for checking what's blocking the view. |

## Why the bottom band of any equirect is "carrier zone"

The equirect's bottom band (latitudes -90° to about -50°) renders the area DIRECTLY UNDERNEATH the camera. For any rig with a physical body, the carrier sits there. This is why our minimap (see [`viewport_geometry.md`](viewport_geometry.md)) crops out the band below -60°: nothing useful for pavement grading lives there.

## Implication for the scan plan

When you start a new pano, **assume the bottom 30% of any pitch=-30 viewport is non-pavement** (carrier or carrier-shadow region). Plan to pitch UP if you need cleaner pavement views; plan to pitch DOWN only when zooming on a confirmed close-distance defect.

`look_around(pitch=-15, hfov=80)` is the recommended first call: this pitch is near horizon, so all 4 cardinal-yaw tiles in the grid have road if road is present, AND none have severe carrier intrusion. From the look_around grid you can identify which yaw has the cleanest road, then `look()` that yaw at the standard pitch=-30.

## Sources

- [18] City of Los Angeles StreetsLA dataset — local LA capture mix verified empirically across 50-pano calibration set.
- Empirical: direct visual inspection of cached LA panos showed mixed rig types in the calibration set (car, pedestrian-handheld, pedestrian-chest, tripod). The "carrier-rig variety" guidance is from that direct observation.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
