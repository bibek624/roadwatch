---
name: deterioration_progression
scope: How asphalt distress evolves over time; service-life expectations
references: [4, 5, 6, 7, 11, 14]
---

# Deterioration progression — how distress evolves

This skill tells you what distress patterns are **plausible together** and what they imply about **age of treatment**. Use it to sanity-check your tier judgment: is the distress mix you're seeing consistent with how pavement actually fails?

## The S-curve

Asphalt PCI does NOT decline linearly with age. The canonical curve [11, 14]:

```
PCI
100 ┐
    │ ╲╲
 90 │   ╲╲       ← phase 1: slow oxidation, hairline cracks
    │     ╲╲
 80 │       ╲╲
    │         ╲      ← inflection ~ year 12-15 for typical asphalt
 70 │           ╲
    │            ╲
 60 │             ╲
    │              ╲╲   ← phase 2: rapid deterioration
 50 │                ╲╲
    │                  ╲╲
 40 │                    ╲╲
    │                      ╲
 30 │                       ╲
    │                        ╲
 20 │                          ╲ ← phase 3: failure
 10 │                            ╲
    │                             ╲
  0 └──────────────────────────────────── age (years)
    0    5   10   15   20   25   30
```

Three phases, each producing a characteristic distress mix:

### Phase 1 (PCI 75-100, age 0-12 years roughly): slow surface aging

What you see:
- Surface oxidation (asphalt greys from black with age)
- Hairline transverse cracks (thermal — first appear ~year 4-7 in temperate climates)
- Light raveling at surfaces edges
- Lane markings start to fade (~year 5-7 for low-traffic, ~year 2-4 for high-traffic)

What you DON'T see yet:
- Wheelpath cracking
- Alligator pattern
- Patches (because no need for repair yet)
- Rutting > 6 mm

This phase corresponds to **Good or Satisfactory** tiers.

### Phase 2 (PCI 40-75, age 12-22 years roughly): structural distress emerges

What you see:
- Block cracking developed across full lane (thermal + oxidation cumulative)
- Wheelpath longitudinal cracks (top-down fatigue)
- Early alligator pattern in wheelpaths (subgrade or thin-pavement failure)
- Patches appearing — utility cuts, pothole repairs from late phase 1
- Rutting becoming measurable (6-13 mm)
- Markings substantially worn

This phase corresponds to **Fair to Poor** tiers. This is where the curve accelerates — every year without intervention drops 3-8 PCI points (vs 1-2/yr in phase 1).

**Cost-of-deferral economic reality:** *"$1 spent now saves $4-7 later"* [7]. The curve's acceleration is why. A Fair pavement that gets a thin overlay (~$5/sq.yd) at year 15 stays serviceable to year 30. The same pavement deferred to year 18 needs mill-and-overlay (~$15/sq.yd). Deferred to year 22 needs reconstruction (~$50/sq.yd).

### Phase 3 (PCI 0-40, age 22+ years roughly, or premature failure under heavy load): structural failure

What you see:
- Severe alligator throughout wheelpaths
- Open potholes (alligator pieces dislodged)
- Deep rutting (> 25 mm)
- Edge breaks
- Failed patches everywhere — patches on patches on patches
- Base course visible at potholes

This phase corresponds to **Poor → Failed** tiers. By the time you see open potholes the structural section has failed; surface treatments don't restore it. Reconstruction needed.

## Crack progression — the canonical sequence

Cracks evolve in a predictable order on most asphalt pavements [4, 5]:

```
Hairline transverse  →  Block crack network  →  Longitudinal wheelpath  →  Alligator  →  Potholes
   (year 5-7)          (year 10-15 thermal)      (year 12-18 fatigue)     (year 15-22)    (year 18+)
```

(Timelines are typical for HMA in temperate-arid climates with moderate traffic. Heavy truck routes compress the timeline by 30-50%; freeze-thaw climates accelerate phase 3.)

This means a sanity check: **if you see open potholes, you should ALSO see surrounding alligator and severe distress.** A single isolated pothole on otherwise-Good pavement is suspicious — likely a utility-cut failure, not a Failed-tier surface. Grade based on whether the surrounding pavement matches the pothole's implied tier.

Conversely: **alligator without rutting is unusual.** Heavy traffic that produces alligator usually produces rutting too. If you see only alligator with no measurable rutting, the cause may be subgrade water (drainage failure) rather than load fatigue.

## Crack-widening rates

A crack doesn't just appear — it widens over time as the binder around it ages and as water+freeze cycles enter the crack:

| Climate | Typical hairline → 6 mm |
|---|---|
| Temperate (LA, mild) | 3-5 years |
| Hot-arid (LA, Phoenix, Vegas) | 2-3 years (faster oxidation) |
| Freeze-thaw (Chicago, Boston) | 1-2 years (water+freeze + thermal) |

So a 6 mm crack on a 4-year-old pavement is concerning (suggests poor mix or subgrade issue). A 6 mm crack on a 12-year-old pavement is normal aging.

## Service life expectations by treatment

| Original construction | Typical service life |
|---|---|
| New HMA pavement, full structural section | 18-25 years |
| Mill-and-overlay (2-3 inch) | 12-18 years |
| Thin overlay (1-1.5 inch) | 8-12 years |
| Microsurfacing | 5-8 years |
| Slurry seal | 4-7 years |
| Chip seal | 5-7 years |
| Fog seal | 2-4 years |
| Crack seal | 2-5 years (extends underlying life) |

Reference: FHWA preventive maintenance guidance + industry data [6, 7, 8, 10].

## Implication for grading

When you see a distress mix that matches Phase 1 (just hairlines + light raveling) → grade Good/Sat. When you see Phase 2 (block + wheelpath cracks + early patches) → grade Fair. When you see Phase 3 (alligator + potholes) → grade Poor or Failed.

When the mix doesn't fit a phase cleanly (e.g., new-looking surface but with one big pothole), the surface is likely **post-treatment**: an overlay that's failing prematurely, or a recent surface seal over advanced underlying distress. See [`treatment_signatures.md`](treatment_signatures.md) for what treatments look like.

## Sources

- [4] Pavement Interactive — distress reference desk; mechanism of progression.
- [5] Washington Asphalt Pavement Association — alligator cracking progression.
- [6] FHWA Pavement Preservation Program — preservation philosophy and timing.
- [7] FHWA — *Pavement Preservation: Preserving Our Investment in Highways* (Public Roads, Jan/Feb 2000) — origin of the "$1 now / $4 later" cost-deferral claim.
- [11] FHWA — Reformulated Pavement Remaining Service Life Framework (FHWA-HRT-13-038, Nov 2013).
- [14] VTTI — A Model for Predicting the Deterioration of Asphalt Pavement (Ravina, 2019) — Markov-style deterioration modeling.

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
