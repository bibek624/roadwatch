---
name: climate_failure_modes
scope: regional / climate-driven distress patterns; what's plausible at a given location
references: [4, 15, 16, 17]
---

# Climate failure modes — what's plausible where

Asphalt pavements in different climates fail in different ways. Knowing the climate of the segment lets you sanity-check whether the distress mix you see is consistent with the failure modes that climate produces. This skill is **advisory** — use it to interpret ambiguous distress patterns, not to dictate the tier.

## The four canonical climate regimes

### 1. Freeze-thaw climates (e.g., Boston, Chicago, Denver, Minneapolis, Buffalo)

**Defining feature.** Water enters cracks → freezes → expands by 9% → forces crack widening → thaws → repeats. Compounds with thermal cracking from large temperature swings.

**Dominant distresses:**
- **Transverse cracking** — frequent, evenly-spaced (every 4-12 m), often densest of any climate. The asphalt contracts in extreme cold, exceeds tensile capacity, splits.
- **Block cracking** — block sizes tend to be smaller and more uniform than in hot climates.
- **Pothole proliferation** — every spring thaw produces a new wave of potholes from cracks that widened over winter.
- **Alligator cracking** — water saturating subgrade in spring weakens support; repeated freeze-thaw within wheelpaths produces alligator faster than load alone would.
- **Joint/edge breaks** — water entry at edges + freeze cycle accelerates edge deterioration.

**What's NOT typical here:** heavy rutting (cold pavement is too stiff to flow plastically), severe bleeding (low temperatures keep binder firm).

**Implication.** A pavement in a freeze-thaw climate showing dense transverse cracking + scattered potholes + alligator in wheelpaths is **textbook Phase 2-3** for the climate. This pattern is age-typical, not premature failure.

### 2. Hot-arid climates (LA Basin, Phoenix, Vegas, Tucson, Albuquerque, Inland Empire)

**Defining feature.** UV radiation + high pavement temperatures (often 50-65°C surface in summer) accelerate binder oxidation. The asphalt binder hardens and embrittles faster than in temperate climates.

**Dominant distresses:**
- **Block cracking** — wide-spread, often on lower-traffic streets where cumulative thermal/oxidation stress is the only loading. **Block cracking is the LA / hot-arid signature distress.**
- **Surface oxidation** — asphalt greys / lightens with age. Visible on blocks of pavement that are 8+ years old and untreated.
- **Raveling and surface weathering** — binder oxidation reduces aggregate adhesion; aggregate loss is gradual.
- **Bleeding** — in heaviest-traffic areas with marginal mix design, hot summers force binder up. Less common than in temperate climates.
- **Rutting** — possible on heavy-truck routes (binder softens in heat).

**What's NOT typical here:** freeze-thaw potholes (no freezing). Pothole development in hot-arid climates usually requires structural failure (alligator → dislodgement) rather than crack-water-freeze.

**Implication for LA grading.** When you see block cracking + fading + light raveling without alligator or potholes — that's typical aging, grade Sat-to-Fair depending on density. Block cracking in LA is the rule, not pathology. **Don't over-call block cracking as Poor unless block density is severe AND combined with other distresses (rutting, raveling, edge breaks).**

LA-specific: Santa Ana wind events deposit fine dust + erosion patterns; this looks like surface wear but is environmental. The Pacific marine layer keeps overnight temperatures mild, slowing binder embrittlement vs true desert (Phoenix is hotter / drier than LA).

### 3. Coastal / salt-exposed climates (Pacific Coast Highway, Atlantic seaboard, Great Lakes shores)

**Defining feature.** Chloride ions (sea salt, de-icing salt) penetrate asphalt cracks and degrade aggregate-binder bond. Plus humidity-driven binder stripping.

**Dominant distresses:**
- **Edge raveling** — particularly intense on shoreline-facing edges.
- **Edge breaks** — coastal erosion + salt undermining creates edge crumbling.
- **Stripping** — internal aggregate-binder bond failure; manifests as raveling or unexplained rutting.
- **Spalling** — pieces of pavement breaking off, especially at edges.

**Implication.** A coastal segment showing edge-concentrated distress (edges much worse than the lane interior) is exhibiting climate-typical patterns.

### 4. Heavy-truck routes (industrial corridors, port access, freight highways)

**Not strictly a climate, but a load regime that overlays on any climate.**

**Defining feature.** Repeated heavy axle loads (trucks 18 kips per axle vs cars 1-2 kips) accelerate fatigue.

**Dominant distresses:**
- **Wheelpath rutting** — bulging shoulders, sinking wheelpaths. Most diagnostic of heavy-truck failure.
- **Alligator cracking in wheelpaths** — fatigue crack network in the loaded zones.
- **Shoving and corrugation** — at intersections where braking + acceleration concentrate.
- **Slippage cracking** — at intersections, crescent-shaped cracks from layered shear.

**Implication.** A street near a port, industrial area, or freight corridor showing wheelpath-concentrated distress (alligator + rutting only in wheelpaths, not in surrounding lane areas) is exhibiting heavy-truck failure. Grade based on severity; the cause helps interpret but doesn't change the tier.

## How to use this skill while grading

**It's a sanity-check overlay, not a primary input.** Apply in this order:

1. Identify the visible distress mix (per [`distress_taxonomy.md`](distress_taxonomy.md)).
2. Apply the rubric to assign a tier (per [`tier_rubric.md`](tier_rubric.md)).
3. Cross-check with this skill: "given that this is LA / hot-arid, is the distress mix climate-plausible or unusual?" If unusual, pause and look harder — maybe there's a treatment failure or hidden issue.

For LA specifically (our current study area):

- **Expected:** block cracking + oxidation + faded markings + light raveling on aging streets.
- **Suspicious:** densely-spaced transverse cracks (more typical of cold climates) → check for thin-overlay reflection cracking from underlying rigid pavement.
- **Suspicious:** severe rutting on a low-traffic residential street (no truck loading explanation) → check for subgrade water issue.
- **Expected:** alligator + rutting on industrial-area streets (port-adjacent, loading-zone roads).

## Don't use this to over-diagnose

The agent is a triage spotter, not a forensic pavement engineer. Don't try to write rationales like "block cracking suggests UV-driven binder oxidation typical of LA's hot-arid climate." That's overreach. Just call the visible distress and the tier; let the climate context inform your confidence and choice between adjacent tiers.

Example use:

> Wide view: I see transverse cracks every ~5 m and some block-cracking pattern. No alligator, no rutting. *Climate context: LA, hot-arid → block + transverse without alligator is the typical Phase 2 signature here, not anomalous.* Given the absence of alligator/rutting and the typical-for-climate pattern → **grade Fair** with confidence 0.8.

## Sources

- [4] Pavement Interactive — distress mechanism reference.
- [15] Tensar International — Types of Road Cracking.
- [16] The Constructor — Alligator Cracking Causes and Control.
- [17] Strata Global — Crocodile Cracking Definition and Causes.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
