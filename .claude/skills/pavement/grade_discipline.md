---
name: grade_discipline
scope: When to grade vs skip vs unknown; under-report rule; confidence calibration
references: [2, 4]
---

# Grade discipline — when to commit and how to be honest

This skill governs the FINAL grade decision. The other skills tell you what each defect / tier looks like; this one tells you when to actually call `grade()`.

## The order of operations

Before calling `grade()`, you must satisfy in order:

1. **You have at least one viewport with ≥ 40% paved road visible.** If the carrier (vehicle hood / person / bike) dominates the bottom of every viewport and you've already tried pitching up + side yaws + alternative candidates → `skip_waypoint`. Don't grade from a 20%-pavement viewport.

2. **You have zoomed in on any suspected distress.** If your wide view shows ANY hint of distress (texture variation, dark lines, mottled surface, possible cracks/patches) you must `zoom_into_region` to confirm what you're seeing. See [`zoom_investigation.md`](zoom_investigation.md). Grading "looks weathered, no major distress" without zooming is a calibrated failure mode.

3. **You have applied the visual-confusers filter.** If a feature might be paint, manhole, shadow, oil, sealcoat — apply [`visual_confusers.md`](visual_confusers.md) defaults before calling it a defect.

4. **TEMPORAL pre-grade gate (mandatory if find_candidates returned ≥2 distinct years):**
   a. You have looked at the LATEST year (primary investigation).
   b. You have looked at the EARLIEST year AT THE SAME YAW as the latest. If the first earliest-year candidate was unusable (night, indoor, blocked), you tried the 2nd, then the 3rd before declaring the year unusable.
   c. If you suspect a treatment was applied between epochs OR a defect appeared/disappeared, you have zoomed on the SAME bbox in BOTH epochs.
   d. Your rationale must specify which yaw/bbox you used for cross-temporal comparison, OR explicitly note "older epoch unusable — graded from latest only" with the reason.

5. **You have committed to ONE tier with a confidence and a specific rationale.** No "Fair-to-Poor" hedges; pick one. The rationale should reference SPECIFIC observations ("alligator pattern across right wheelpath", "rectangular patches with edge separation", "transverse cracks every ~6 m"), not vague tier-words ("looks weathered").

## Anti-patterns to AVOID at grade time

These are failure modes observed in prior runs:

- **"Stark contrast" claims with mismatched yaw.** If you graded 2025 from yaw=0 (forward) and looked at 2016 yaw=180 (back), you CANNOT claim "the surface improved" or "treatment applied between years." That comparison is junk — different physical patches of road. Either redo with matching yaw, or strike the temporal claim from your rationale.
- **Giving up on a year after 1 bad pano.** Older Mapillary captures often skew night/dusk/pedestrian. Try at least 2 candidates from a year (peek the next one) before declaring "year unusable." find_candidates returned ≥3 per year specifically so you have fallbacks.
- **Asymmetric investigation.** If you spent 6 looks on 2025 and 1 look on 2016 and are about to claim a temporal arc — STOP. Either spend equivalent depth on 2016, or remove the temporal claim from your rationale. The agent's confidence in 2016 must match its confidence in 2025 for any "between-year" claim.
- **Discipline degradation across waypoints.** If WP0 was deeply investigated and WP4 only 1 pano — you've drifted. The same per-epoch discipline applies at every waypoint, not just the first.

## The 3-tier decision rules

We use **Good / Fair / Poor / unknown** — finer splits aren't reliable at street-level Mapillary resolution. The collapsed scale forces commitment.

**Between Good and Fair:**
- 0–4 short cracks per 5m, no rutting, no potholes, no patches failing → **Good**
- ≥5 cracks per 5m OR mottled patchwork OR edge fraying OR raveling → **Fair**

**Between Fair and Poor — the CRITICAL boundary, no hedging:**
- Any open pothole → **Poor**
- Alligator (interconnected branching cracks across full wheelpath) → **Poor**
- Wide cracks (>15mm) with spalled edges → **Poor**
- Deep rutting (>15mm channelization) → **Poor**
- Base material visible through worn surface → **Poor**
- Multi-generation patches with widespread failure → **Poor**

**When uncertain between Fair and Poor → choose Poor.** This is the "no Fair-hedge" rule. The over-call cost (a Poor that should have been Fair) wastes one inspection visit. The under-call cost (a Poor as Fair) lets a structural failure stay on the watch-list. Public-works dispatchers prefer false positives at the Fair/Poor boundary.

```
0–4 isolated cracks, intact patches, no rutting    → Good
Mottled surface, several cracks, edge fraying      → Fair
Any pothole, alligator, spalling, exposed base     → Poor
Uncertain between Fair and Poor                    → Poor (NO HEDGE)
```

## When to grade `unknown`

Use `unknown` when:

- **No paved road in any analyzed viewport.** Mapillary captures from staircases, hiking trails, plazas, parking lot interiors, sidewalks-only — the road this segment refers to genuinely isn't visible. `unknown` is the correct call.
- **All viewports have < 40% pavement** even after pitching up + side yaws + multiple candidates. The carrier or other obstructions dominate.
- **View is so dim / blurry / shadowed** that no surface judgement is possible.

`unknown` is HONEST and not a failure. Do NOT default to `Sat` when you can't see the pavement — that produces false positives in the demo's accuracy table.

## When to `skip_waypoint` instead of `grade unknown`

Both result in "no useful grade for this waypoint." The distinction:

| Use this | When |
|---|---|
| `grade(tier='unknown', ...)` | You DID look at one or more viewports of one or more candidates and concluded no usable pavement is visible. The pano is the issue. |
| `skip_waypoint(reason)` | You couldn't even find a usable candidate at this waypoint (no panos within radius, all candidates rejected at peek/look without proceeding to grade). The location is the issue. |

Practically: if you've called `look()` at all, prefer `grade(tier='unknown')`. If you only called `find_candidates` (and then nothing), prefer `skip_waypoint`. Both terminate the waypoint cleanly.

## Confidence calibration

The `confidence` field on `grade()` is a 0.0-1.0 number. Calibrate honestly:

| Confidence range | Meaning |
|---|---|
| **0.90 - 1.00** | Multiple viewports unambiguously show this tier. Distress (or its absence) is unmistakable at zoom. Tier-defining attributes match the rubric across the entire visible road. |
| **0.70 - 0.90** | Tier matches the rubric but one or two attributes are ambiguous (e.g., is that a hairline crack or a bit of paint?). Most graders would agree but a few would call adjacent tier. |
| **0.50 - 0.70** | Borderline. For Good/Fair you may pick the better tier; for Fair/Poor you MUST pick Poor (no hedge). |
| **0.30 - 0.50** | Significant ambiguity. **Consider calling `unknown` instead.** |
| **< 0.30** | Don't grade. Call `unknown` or skip. |

A high-confidence Good is more useful than a 0.4-confidence Fair.

## Anti-patterns to avoid (calibrated against)

These behaviors fired in earlier iterations and are now explicit don'ts:

1. **"Looks weathered, grade Good"** without zooming on the weathering. The wide view doesn't resolve cracks at 8m distance. **Always zoom on suspected distress before grading.**
2. **Calling Fair when you saw clear alligator pattern** because "I'm not sure how widespread it is." If alligator is in any wheelpath, grade Poor.
3. **Calling Fair when you can't see the road** because "Fair feels like a safe middle answer." Fair requires VISIBLE engineering distress. No road visible → unknown.
4. **Stopping at the first viewport** when it shows 50% car body. Pitch up, look around, switch candidates. Don't grade from carrier-dominated views.
5. **Giving up on a year after 1-2 bad panos.** find_candidates returned ≥6 per year and you can call it again with year_filter=<year> to get more closest panos. Don't declare a year "unusable" without trying ≥3 alternatives.

## Final commit checklist

Before calling `grade()`, verbally confirm to yourself:

- [ ] I have a viewport with ≥ 40% pavement.
- [ ] I zoomed on any feature I called a defect.
- [ ] I applied confuser filters (paint, manhole, shadow, oil, sealcoat).
- [ ] My tier matches the rubric attribute matrix, not just my gut.
- [ ] My rationale names specific observations, not just adjectives.
- [ ] My confidence reflects how solidly the rubric matches.

If any are NO, take one more action before grading.

## Sources

- [2] ASTM D6433-21 — severity definitions that anchor the rubric tiers.
- [4] Pavement Interactive — practical guidance on distress identification.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
