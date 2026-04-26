---
name: tier_rubric
scope: 3-tier pavement condition scale (Good / Fair / Poor) for street-level optical assessment
references: [1, 2, 3]
---

# Tier rubric — what each pavement condition tier looks like

You assign exactly one of these tiers per waypoint: **Good**, **Fair**, **Poor**, or `unknown`.

We use 3 tiers (collapsed from the conventional 5+) because at street-level Mapillary resolution the boundaries between Good/Satisfactory and Poor/Failed are NOT reliably distinguishable from imagery alone. The collapse means each tier is broader and the agent commits to it more confidently — fewer borderline calls, fewer Fair-when-it-should-be-Poor mistakes.

## The 3-tier scale (+ unknown)

| Tier | Plain-language summary | Approx ASTM PCI band | Action horizon |
|---|---|---|---|
| **Good** | Essentially intact. New, recently treated, or aged-but-stable. Maintenance-only. | 70 – 100 | None / preventive only |
| **Fair** | Visible engineering distress. Surface needs intervention soon. | 40 – 69 | 1–3 years |
| **Poor** | Widespread structural distress, pothole/rutting/alligator/spalling. Action urgent. | 0 – 39 | ≤12 months / emergency |
| `unknown` | No usable pavement view in any analyzed viewport. | n/a | n/a |

## Per-tier attribute matrix — the decision rules

For each tier you should be able to answer YES to most rows. If a critical-row answer is NO, the segment is NOT that tier — drop to the next-worse tier.

### Good (PCI ≈ 70-100)

The "no real engineering concern" tier. Includes both pristine new pavement AND old-but-stable pavement.

| Attribute | What you should see |
|---|---|
| Surface uniformity | Mostly uniform; oxidation/greying tolerated, mild tonal mottling tolerated |
| Crack density | **0–4 short cracks per 5 m of lane**, mostly isolated (not interconnected) |
| Crack width | **< 6 mm**. Sealed cracks (intact crack-seal beads) count as Good. |
| Patches | **0–4 small intact patches** per ~30 m, edges flush, no spalling at boundaries |
| Rutting | **No visible rutting** in wheelpaths |
| Edge condition | Mostly intact; minor edge fraying tolerated |
| Markings | Visible (may be slightly worn); double-yellow + edge stripes readable |
| Raveling | **Light only** — surface texture intact, fines still binding aggregate |
| Drainage | Functional; no standing water suggesting failed drainage |
| **Disqualifiers — any of these → NOT Good** | • Cracks > 6 mm wide consistently • Alligator/block pattern anywhere • Any pothole • Any failing/spalled patch • Visible rutting • Widespread raveling (exposed stones) |

If you see a treatment signature on top of a previously-distressed surface (fresh slurry/microsurface, fresh thin overlay, mill-and-overlay, recent crack-seal beads still intact) — see [`treatment_signatures.md`](treatment_signatures.md) — grade **Good** based on the overlay's freshness, NOT the underlying distress.

### Fair (PCI ≈ 40-69)

The "visible engineering distress requiring action in 1–3 years" tier. Surface is showing structural fatigue but not yet failed.

| Attribute | What you should see |
|---|---|
| Surface uniformity | Mottled / patchwork appearance; multiple tonal generations visible |
| Crack density | **5–20 cracks per 5 m**, including some interconnected lengths |
| Crack width | Cracks **6–15 mm** wide, possibly with spalling at edges |
| Patches | Multiple patches; **1–2 may show edge separation, raveling at boundary, or sinking** |
| Rutting | Subtle rutting visible in wheelpaths (depth ~6–15 mm) |
| Edge condition | Edge fraying or breakage along curbs/gutters |
| Markings | Faded / partially worn |
| Raveling | Visible texture loss in wheelpaths or along the edge |
| Block-cracking | **Early block-cracking acceptable** — interconnected polygons forming but not yet branching into alligator |
| **Disqualifiers — any of these → drop to Poor** | • Any open pothole · any wide alligator field (interconnected branching cracks across full lane) · any deep rutting (>15 mm) · any spalled patch with material missing · any visible base layer beneath surface |

### Poor (PCI ≈ 0-39)

The "widespread structural failure" tier. Surface is visibly bad enough that a public-works dispatcher would prioritize it. **The under-call defense is here — when you see the disqualifiers above, do NOT hedge to Fair.**

| Attribute | What you should see |
|---|---|
| Surface uniformity | Highly mottled; multiple eras of patches, repairs, and untreated distress co-present |
| Cracks | Widespread network — alligator (interconnected branching) at ≥1 wheelpath OR full-lane block-cracking with branching |
| Crack width | Some cracks **> 15 mm** with spalled edges (chunks missing along crack lines) |
| Potholes | **Any open pothole** (depression with broken edges, material missing) → automatically Poor at minimum |
| Patches | Patches with widespread failure — edges separating, sinking centers, raveling-around-patch |
| Rutting | Visible rutting (channelized depressions in wheelpaths) > 15 mm depth |
| Edge condition | Edges crumbling or eroded; pavement-curb gap visible |
| Markings | Heavily faded / missing in stretches |
| Raveling | Heavy — pieces of aggregate visibly broken loose; surface no longer cohesive |
| Base exposure | Base layer or aggregate visible through a worn-through or potholed area |
| Treatment evidence | Multi-generation patches/seals showing the corridor has been failing repeatedly |

## Critical decision rules

These are absolute and override the tier-row attributes when triggered:

1. **Open pothole visible (any one) → Poor minimum.** Never grade Good or Fair if a pothole is in frame and confirmed via zoom.
2. **Alligator cracking (interconnected branching pattern across full wheelpath or lane) → Poor minimum.** Block-cracking alone (polygons without branching) can stay Fair.
3. **Base material exposed beneath worn surface → Poor minimum.**
4. **Visible rutting > 15 mm channelization in wheelpath → Poor minimum.**
5. **Wide cracks (> 15 mm) with spalled edges (material missing along crack) → Poor minimum.**
6. **Recently-treated surface (fresh dark overlay, fresh slurry texture, fresh mill-and-overlay edge at curb) → Good** regardless of the underlying surface's prior condition. The treatment is what's load-bearing.

## The "no Fair-hedge" rule (CRITICAL — read this twice)

**When uncertain between Fair and Poor, choose Poor.**

This is the opposite of the under-report rule for Good/Fair. The over-call cost (a Poor that should have been Fair) wastes one inspection visit. The under-call cost (a Poor graded as Fair) lets a structural failure stay on the watch-list and a pothole keeps growing. Public-works dispatchers prefer false positives at the Fair/Poor boundary.

**Specific anti-patterns to avoid:**

- "I see widespread cracking but it might be block not alligator" → **grade Poor.** If you can see widespread cracking, severity matters less than the fact of it.
- "I see a pothole but I'm not sure if it's deep" → **grade Poor.** Any visible pothole is Poor.
- "Surface looks weathered with mottled patches and longitudinal cracks but I don't see alligator" → **grade Fair if intact, Poor if any patch is failing.** Mottled multi-generation patching IS structural distress.
- "I see edge cracking and rutting but it's not severe" → **grade Fair.** Edge cracking + rutting are engineering signals not cosmetic.

## When to grade `unknown`

Use `unknown` only when:
- No paved road is visible in any analyzed viewport across all candidate panos checked
- All viewports have < 30% pavement after pitching/yawing/swapping panos
- All available panos are too dim/blurry/shadowed to assess (and you've checked AT LEAST 2 same-year alternatives)

`unknown` is honest. Don't use Fair as a "safe" hedge for things you can't see.

## Sources

[1] FHWA Distress Identification Manual for the LTPP, FHWA-HRT-13-092, 2014. Section 3 (asphalt distress severity definitions).
[2] ASTM D6433-21, *Standard Practice for Roads and Parking Lots Pavement Condition Index Surveys*. PCI bands.
[3] PASER Asphalt Manual, UW-Madison TIC. Visual condition rating reference.

Full bibliography: [`../../../research/source_index.md`](../../../research/source_index.md).
