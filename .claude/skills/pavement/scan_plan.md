---
name: scan_plan
scope: per-waypoint workflow — temporal investigation with per-epoch cross-witness
---

# Scan plan — what to do at each waypoint

`find_candidates` returns a TEMPORALLY STRATIFIED list — at least 3 closest panos per distinct year. The summary tells you the year breakdown ("12 candidates: 2025×4, 2020×3, 2016×3, 2018×2"). When ≥2 distinct years exist, the waypoint has a TEMPORAL STORY and you must investigate it.

The investigation discipline is the same in EVERY epoch you visit, not just the latest: orient → focus → self-critique → zoom if anything is ambiguous → cross-witness with another same-year pano if confidence is low. Then move to the next epoch.

## The default sequence

```
1. get_position()
2. find_candidates(radius_m=30, min_per_year=3)
   READ the year breakdown. If ≥2 distinct years exist, you MUST
   investigate ≥2 epochs before grading.

3. ── EPOCH A: latest year ──
   peek_candidate(image_id_LATEST_closest)
   look_around(image_id_LATEST_closest, pitch=-15)
   look(image_id_LATEST_closest, yaw=<best>, pitch=-25 to -30, hfov=70)
   self-critique:
     - If pavement < 40%: re-look at different pitch/yaw or DIFFERENT same-year pano
     - If you see ANY distress hint: zoom_into_region on it
   PER-EPOCH CROSS-WITNESS GATE (mandatory if confidence < 0.7):
     pick a SECOND pano FROM THE SAME YEAR (the next entry in the list with
     the same year), look() at the same yaw to corroborate.
     - If both panos show the same defect → confidence high, defect real
     - If only one shows it → probably glare/shadow/local artifact; revise
   Settle on a per-epoch tier ONLY after this gate.

4. ── EPOCH B: earliest year ──
   Repeat the SAME discipline as Epoch A, on the earliest-year pano:
     peek (optional — older years often have thinner metadata)
     look(image_id_EARLIEST, yaw=<SAME as latest>, pitch=-30, hfov=70,
          purpose='earliest available — what was here originally')
     self-critique
     zoom_into_region(image_id_EARLIEST, ..., x1, y1, x2, y2)  ← KEY:
       use the SAME bbox you zoomed on in Epoch A, so you're investigating
       the EXACT SAME PATCH OF ROAD across years.
     PER-EPOCH CROSS-WITNESS GATE — same rule. If the older epoch's first
     pano is ambiguous, try a SECOND pano FROM THAT SAME OLDER YEAR.

5. ── (Optional) EPOCH C: middle year if span > 5 years ──
   Same discipline. Helps narrow when a treatment was applied.

6. grade(tier, confidence, rationale, chosen_image_id)
   Rationale mentions the temporal arc when significant:
     "alligator confirmed in 2025 from 2 vantages; same patch in 2016
      showed only hairline longitudinal — defect progressed 2016→2025"
     "uniform jet-black surface 2025 vs oxidized grey 2016 — overlay applied
      in interim; cross-witness on second 2025 pano confirms intact"
     "stable: crack pattern visible in 2016 unchanged in 2025; no treatments"
```

**The non-negotiable rule:** every epoch you investigate gets the same depth as the latest. If you found a crack in 2025 and corroborated with a second 2025 pano, you owe the SAME corroboration to your 2016 read before declaring "the crack wasn't there in 2016." Per-epoch confidence drives temporal claims. A weak read in 2016 + strong read in 2025 should NOT be reported as "this defect appeared between 2016 and 2025" — it should be reported as "defect confirmed in 2025; 2016 evidence ambiguous (see rationale)."

**Cost is not the constraint. Investigation depth is.** 12-20 tool calls per waypoint is fine when the multi-year evidence is rich.

## YAW MATCHING — apples-to-apples across years

When you compare across years, you must use the **same yaw_deg** across epochs. Otherwise you're comparing different physical patches of road and your temporal claim is junk.

**Wrong (apples-to-oranges):**
```
2025: look(yaw=0)   → "forward shows uniform asphalt"  → grade Good
2016: look(yaw=180) → "back shows heavy alligator"     → conclude "treatment applied between 2016 and 2025"
```
This is broken. The 2025 forward and the 2016 back are different sections of road. The "treatment applied" inference is unfounded.

**Right (apples-to-apples):**
```
2025: look(yaw=0, pitch=-25, hfov=70) → primary investigation, found uniform asphalt
2025: zoom_into_region on the same view → confirmed
2016: look(yaw=0, pitch=-25, hfov=70) → SAME yaw — found heavy alligator
2016: zoom_into_region with the SAME bbox as the 2025 zoom → confirmed
→ valid claim: "same patch was alligator in 2016, intact in 2025 → overlay applied"
```

If you choose to use yaw=180 for the comparison, that's fine — but you must ALSO have done the primary 2025 investigation at yaw=180. Pick a yaw, lock it for the temporal stack.

## N-candidate fallback — don't give up after 1 bad pano

`find_candidates` returns ≥3 closest panos PER YEAR (configurable via `min_per_year`). When a chosen pano is unusable for any reason — night, dusk, indoors, wrong orientation, blocked, glare — try the NEXT pano in the same year before declaring that year unusable.

**The escalation discipline:**

```
Pick year-Y candidate #1 (closest in year Y).
  peek_candidate(id_1)
  If usable=false (night, indoor, no road): SKIP to candidate #2 in same year.
  If usable=true: look() → critique → zoom if needed → grade-or-cross-witness.
[year-Y candidate #1 produced ambiguous results or was rejected]
Pick year-Y candidate #2.
  peek_candidate(id_2)
  Same procedure.
[year-Y candidate #2 also produced unsatisfactory result]
Pick year-Y candidate #3.
  Same procedure.
[After all year-Y candidates have been tried]
→ Now you may declare year Y "no usable evidence — 3 of 3 candidates rejected
   for <reason>" in your grade rationale.
```

**Peek before look on older epochs is mandatory.** Older Mapillary captures (2016, 2018) are MUCH more likely to be at night, dusk, or pedestrian-rig than recent (2025). Peek costs $0.001 — it filters out the unusable ones cheaply BEFORE you spend $0.10+ on a look.

**Do NOT abandon a year after 1 bad pano.** That's the failure mode the 2016-night panos created in earlier runs. The find_candidates list explicitly gives you 3 panos per year FOR THIS REASON.

## Self-critique gate (after EVERY look)

After EVERY `look()` (in any epoch), internally answer four questions:

1. **What % of this viewport is paved road?**
2. **What % is camera carrier?**
3. **What % is sky / sidewalk / buildings (irrelevant)?**
4. **Do I see any hint of distress that I cannot definitively identify at this zoom level?**

Decision rules:

- **If pavement < 40%** → **MUST re-look.** Try one of:
  - `look(yaw=<same>, pitch=-10, hfov=70)` — pitch up to clear carrier
  - `look(yaw=±90, pitch=-25, hfov=80)` — look past carrier to the side
  - `look_around(pitch=-10, hfov=80)` — re-orient, find better direction
  - **OR** swap to a DIFFERENT same-year pano from the find_candidates list

- **If you see ANY hint of distress** (texture variation, dark lines, possible cracks, mottled surface, possible patches) → **MUST zoom.** See [`zoom_investigation.md`](zoom_investigation.md).
  - `zoom_into_region(image_id, src_yaw, src_pitch, src_hfov, x1, y1, x2, y2, purpose='zoom on suspected <X>')`

- **If pavement ≥ 40% AND no zoom-needed signal AND single-epoch waypoint** → grade now.

- **If pavement ≥ 40% AND zoom resolved the question AND multi-epoch waypoint** → repeat the same discipline on the next epoch (latest → earliest), THEN grade.

## Per-epoch cross-witness — when it's mandatory

The cross-witness gate (a 2nd pano from the same year, same yaw) is **mandatory** when ANY of these is true:

- The wide-view tier is borderline between two adjacent tiers (e.g., between Sat and Fair, or Fair and Poor)
- A defect was visible at zoom but only partially in frame
- The surface texture is ambiguous and could plausibly be raveling, oil staining, or a sealed crack pattern — visual confusers
- Sun angle / glare / wet-pavement is washing out the surface
- This is a non-latest epoch and you're about to claim "this defect did NOT exist back then" — confirming absence requires the same evidence as confirming presence

The cross-witness is NOT mandatory when:

- A defect is unmistakably visible at wide view (e.g., a clear pothole, large alligator field, exposed base) — confidence ≥ 0.9
- The pavement is unmistakably intact — uniform recent overlay, no wear, no markings fade — confidence ≥ 0.9

## Branch: forward is blocked / carrier dominates

When `look_around` shows forward (yaw 0) is blocked (heavy carrier, parked vehicle, building wall, intersection-occupied):

```
look(yaw=90, pitch=-25, hfov=80)        ← try right
[if pavement < 40%]
look(yaw=270, pitch=-25, hfov=80)       ← try left
[if pavement < 40%]
look(yaw=180, pitch=-15, hfov=80)       ← try back at higher pitch
[if still pavement < 40%]
→ swap to a DIFFERENT same-year candidate from find_candidates
[if 2-3 same-year candidates all fail]
→ skip_waypoint OR fall back to a different year
```

## Branch: no usable candidates at all

When `find_candidates(radius=30)` returns 0 entries, or all entries' peeks come back unusable:

```
find_candidates(radius_m=50)                     ← widen
[if still 0]
find_candidates(radius_m=50, max_age_years=15)   ← include older
[if still 0]
→ skip_waypoint(reason="No Mapillary panos within 50 m at any age")
```

## Branch: viewport shows non-paved (hiking trail / dirt / parking-lot interior)

The candidate isn't the right type (often pedestrian-rig captures matched to a vehicle road). Check 2-3 yaws to confirm no asphalt road exists in the pano. Then:

```
→ swap to a DIFFERENT candidate
[if all candidates show non-paved]
→ grade(tier='unknown', rationale='No paved road visible from any analyzed yaw or candidate')
```

`unknown` is the honest call. See [`grade_discipline.md`](grade_discipline.md).

## What "single-epoch waypoint" looks like (corridor only has one year)

A typical clean waypoint with a car-rig pano showing intact pavement, only 2025 panos:

```
T1: get_position
T2: find_candidates                 → "8 candidates, years: 2025×8" (single epoch)
T3: peek_candidate(<id>)            → usable=true, rig=vehicle, day
T4: look_around(<id>, pitch=-15)
T5: look(<id>, yaw=0, pitch=-25, hfov=70)
    [self-critique: 70% pavement, faint longitudinal, possible distress hint]
T6: zoom_into_region(<id>, ..., upper-center bbox)
    [now I can see — uniform asphalt, faint longitudinal seam at lane line]
T7: grade(tier='Good', confidence=0.85,
          rationale='Uniform asphalt, faint longitudinal seam at lane line,
                     no visible cracking or rutting. Single epoch (2025).')
```

Total: 7 turns. Decisive.

## What "multi-epoch waypoint" looks like (corridor has 2016 + 2025)

```
T1: get_position
T2: find_candidates                 → "12 candidates, years: 2025×4, 2016×3, etc.
                                       TEMPORAL STORY DETECTED"

# EPOCH A: 2025
T3: peek_candidate(<id_2025_a>)
T4: look_around(<id_2025_a>, pitch=-15)
T5: look(<id_2025_a>, yaw=0, pitch=-25, hfov=70)
    [I see what looks like alligator in the right wheelpath — needs zoom]
T6: zoom_into_region(<id_2025_a>, ..., right-wheelpath bbox)
    [confirmed: branching alligator, ~5mm widths, mid-severity]
T7: look(<id_2025_b>, yaw=0, pitch=-25, hfov=70, purpose='cross-witness 2025')
    [second 2025 pano, ~12m away, also shows alligator at same wheelpath]
    → 2025 epoch confidence high. Tier candidate: Poor.

# EPOCH B: 2016
T8: look(<id_2016_a>, yaw=0, pitch=-25, hfov=70, purpose='earliest 2016 epoch')
    [surface looks weathered but I can't tell if alligator is there]
T9: zoom_into_region(<id_2016_a>, SAME bbox as T6, purpose='same patch 2016')
    [I see hairline longitudinal cracks at the wheelpath — but no branching]
T10: look(<id_2016_b>, yaw=0, pitch=-25, hfov=70, purpose='cross-witness 2016')
    [second 2016 pano confirms hairline-only, no branching pattern yet]
    → 2016 epoch confidence high. Tier was Sat-Fair in 2016.

T11: grade(tier='Poor', confidence=0.9,
           rationale='Alligator branching at right wheelpath confirmed from
                      2 vantages in 2025; same patch showed only hairline
                      longitudinal in 2016 (cross-witnessed) — defect
                      progressed Sat→Poor over 9y, structural fatigue.',
           chosen_image_id=<id_2025_a>)
```

Total: 11 turns. The temporal claim ("progressed Sat→Poor over 9y") is load-bearing because BOTH epochs were cross-witnessed.

## Sources

Empirical — derived from walker runs v1-v6 on SPRING ST + calibration set work. Per-epoch cross-witness rule added 2026-04-25 after observing single-image-per-waypoint failure mode.

Full bibliography: [`../../../research/source_index.md`](../../../research/source_index.md).
