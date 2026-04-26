---
name: visual_confusers
scope: false-positive traps — non-distresses that look like distresses
references: [1, 4]
---

# Visual confusers — what looks like a defect but isn't

Most prompt-engineering failures in pavement grading come from calling a non-defect a defect. Each pair below gives the **tells** that distinguish the real defect from its lookalike, and the **default rule** when you can't decide.

If you cannot resolve a confuser pair from the wide view, **zoom in** ([`zoom_investigation.md`](zoom_investigation.md)) before grading. A 3 mm crack at 8 m distance is 1 pixel wide at hfov=70 — invisible. The same crack at hfov=35 is 3-4 pixels — clearly distinguishable from paint or shadow.

## Paint vs crack

| Tell | Paint | Crack |
|---|---|---|
| Edge sharpness | **Crisp, uniform** edges | **Ragged, irregular** edges |
| Width consistency | Constant width along its length | **Variable** width, pinches and widens |
| Color | Solid white / yellow / blue (utility-locate) | Black or dark grey (sealed) or dark shadow (open) |
| Geometry | Straight line, defined endpoint, turns at right angles | Curves, branches, irregular path |
| Context | Functional position (lane line, crosswalk, arrow, stop bar) | Random orientation; often in wheelpaths |
| Surface | Sits ON TOP of asphalt; reflects light | INTO the asphalt; absorbs light |

**Default rule.** Anything in a known marking position (lane line, crosswalk, stop bar, parking-stall outline, bike-lane stripe, arrow, chevron, utility-locate spray) is paint until proven otherwise. Don't grade markings as cracks.

**Specifically watch for:**
- **Faded yellow lane lines** — worn paint can look like a longitudinal crack through the wheelpath. Look at parallel partner (the other side of the lane line should be there too). Paint comes in pairs / sets; cracks don't.
- **Utility-locate paint** — orange, blue, pink spray-painted markings indicating buried utilities. Always paint, never distress. Common before / during construction.
- **Crosswalk stripes** — multiple parallel transverse stripes, not transverse cracks. Crosswalks are PAINT EVENT every ~30-60 cm in a regular pattern.

## Manhole cover vs pothole

| Tell | Manhole cover | Pothole |
|---|---|---|
| Material | METAL (cast iron, gray to rust-orange) with circular pattern | Asphalt rubble + base course (brown / gravel) |
| Shape | Round, perfect circle, ~60-90 cm diameter | Irregular, jagged edges |
| Edges | Flush with pavement (sometimes slightly recessed) | Vertical drops, sharp asphalt edges |
| Surface | Patterned (concentric ridges, grid, manufacturer's mark) | Crumbled, irregular |
| Surrounding | Often a paving ring around the cover | Often surrounded by alligator cracking |

**Default rule.** Round + metal + patterned = manhole. Irregular + crumbly + base-visible = pothole.

**Specifically watch for:**
- **Settled / depressed manhole frame**: the cover itself is intact and metal, but the surrounding pavement has settled, creating a depression around the frame. This IS a defect (utility cut / patch failure family), grade as patch failure if the depression is significant. Don't call it a pothole.
- **Damaged manhole cover**: bent, cracked, or partially missing. Hazardous; call it `damaged_sign` family or note in rationale, but it's not pavement distress.

## Shadow vs distress

Shadows are the most common false-positive on cracks and depressions.

| Tell | Shadow | Real distress |
|---|---|---|
| Edge | Soft, gradient, fades to zero | Hard, fixed, pixel-bounded |
| Position | Moves/changes with light direction | Static |
| Depth | NO depth — pavement is uniform under the dark area | Has actual depth or surface texture change |
| Source | Usually traceable (pole, sign, tree, building, vehicle) | No external cause |
| Edges of "feature" | Match the edges of the casting object | Asphalt-shaped (not pole / tree / vehicle shape) |

**Default rule.** If the dark feature's shape matches a nearby tall object's silhouette, it's shadow. **Mentally subtract shadows** before grading. If you cannot tell, zoom in: at higher resolution, real cracks resolve as 3-D textural changes; shadows remain flat darkness.

## Oil stain / grease vs raveling

| Tell | Oil stain | Raveling |
|---|---|---|
| Color | Dark patch with diffuse edge; sometimes rainbow sheen | Lighter / greyer than surrounding (binder gone, aggregate exposed) |
| Texture | Surface smoother / glossy in stain area | Surface ROUGHER, exposed aggregate visible |
| Shape | Drip pattern, often elongated under cars (parking) | Areal, often along wheelpaths |
| Position | Under parking spaces, intersections (where vehicles stop) | Wheelpaths, edges |

**Default rule.** Drips / sheens at parking spaces = oil. Pitted texture with exposed aggregate = raveling.

## Sealcoat sheen vs raveling

A sealcoat or fog seal applied recently looks DARK AND UNIFORM with a slight sheen. This is the OPPOSITE of raveling.

| Tell | Sealcoat sheen | Raveling |
|---|---|---|
| Color | Uniformly dark, often almost black | Light/grey, exposed aggregate visible |
| Texture | Smooth, uniform | Pitted, rough |
| Sheen | Slight gloss when fresh | None — surface is rough |

**Default rule.** Sealcoat → grade Good or Sat (the surface was just preserved). Don't penalize. See [`treatment_signatures.md`](treatment_signatures.md).

## Sand / dust / debris vs degraded surface

| Tell | Loose surface debris | Degraded surface |
|---|---|---|
| Pattern | Light cover; transient; concentrated near gutters or wind-shadows | Embedded; areal; doesn't move |
| Texture | Powdery / loose grains visible | Hard surface with holes/cracks |
| Edges | Diffuse, gradients into adjacent area | Defined |

**Default rule.** Loose powder = debris (cosmetic; not a tier issue). Embedded coarseness with binder loss = raveling.

## Reflection / wet patch vs depression

After rain or in cool morning light, water can pool or sit on pavement that may not actually be depressed.

| Tell | Wet pavement | Depression |
|---|---|---|
| Boundary | Diffuse — drying edge slowly retreats | Defined — fixed in place |
| Color | Mirror-like reflection | Uniform asphalt color |
| Persistence | Dries / shifts with weather | Always there |

**Default rule.** Reflective / glossy pavement on a damp morning → wet, not necessarily depressed. Depression requires you can see surface curvature (or water still pooling after surrounding pavement has dried).

## Patch (intentional repair) vs alligator cracking

A patch — even a discolored, visible-edged patch — is NOT alligator cracking unless the patch ITSELF is cracking.

| Tell | Patch (intact) | Alligator |
|---|---|---|
| Boundary | Clear rectangular edges defining the patch | Random irregular crack network |
| Surface | Uniform within the patch boundary | Multiple interconnected pieces |
| Position | Anywhere — utility cuts often in lanes | Concentrated in wheelpaths |

**Default rule.** Tonal contrast bounded by a clear rectangle = patch. Multi-sided pieces with no rectangular boundary = alligator. A patch with internal alligator = patch failure (high severity).

## Concrete joint vs concrete crack

If the surface is concrete (PCC) not asphalt:

| Tell | Joint (intentional) | Crack |
|---|---|---|
| Pattern | Regular spacing every ~3-4.5 m (10-15 ft) for transverse joints | Irregular, random |
| Width | Narrow, often sealed with dark sealant | Variable, often unsealed |
| Geometry | Perpendicular to centerline (transverse) or parallel (longitudinal) | Often diagonal, irregular |

**Default rule.** Joints are intentional and don't count as distress. Spalling AT joints does count.

## When in doubt — the under-report rule

If you've zoomed in once and still can't decide whether a feature is a defect or a confuser, **assume it's the confuser** and grade accordingly. False positives waste inspector time. False negatives are also bad but less so than fabricating distress.

This rule does NOT apply when you see CLEAR widespread distress (alligator, deep ruts, potholes). Those are NOT confusers and should drive your tier choice. The under-report rule is for **borderline single features**, not for systemic distress.

## Sources

- [1] FHWA Distress Identification Manual for the LTPP — distress vs construction-feature distinctions.
- [4] Pavement Interactive — distress reference desk; practical confuser guidance.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
