---
name: repair_priority_logic
scope: How public-works inspectors prioritize repair queues; risk × consequence framework
references: [6, 7, 11]
---

# Repair priority logic — what makes one bad block more urgent than another

You produce per-waypoint pavement condition tiers. A public-works inspector reading your output uses those tiers to decide where to dispatch crews. This skill teaches you how that prioritization actually works, so the rationales you produce are useful at the decision-making level — not just at the description level.

## Risk × consequence framework

Every road defect has two dimensions for the dispatcher:

- **Severity** (consequence if untreated): how badly will this fail? Pothole > alligator > block crack > raveling.
- **Exposure** (likelihood of consequence affecting users): how much / what kind of traffic is here? Arterial > local > alley.

Priority = **severity × exposure**.

A single open pothole on a high-traffic arterial = top priority (severity high, exposure high).
A single open pothole on a low-traffic alley = lower priority despite same severity (low exposure).
Widespread alligator on an arterial = high priority (moderate severity over LARGE area, high exposure).
Widespread alligator on a residential street = medium priority (moderate severity, low exposure).

## What gets prioritized first (typical municipal logic)

Roughly in order of typical dispatch priority:

1. **Open potholes on any road with vehicular traffic** — safety / damage liability. Dispatched as 311-emergency calls regardless of corridor.
2. **Failed pavement on arterials / collectors** — ride quality + safety + economic impact (bus routes, freight corridors, emergency response).
3. **Severe alligator with rutting** — structural failure stage; deferring costs 4-7× more if reconstruction is needed [7].
4. **Failed segments at intersections** — exposed to braking forces, accelerated propagation; safety risk for pedestrians.
5. **Failed pavement on local / residential streets** — same severity but lower traffic. Goes onto the queue but lower urgency.
6. **Poor pavement at any tier** — needs preservation treatment (microsurface, mill-and-overlay) within the next 12 months.
7. **Fair pavement** — watch list. Treat at PCI 60-65 with thin overlay or microsurfacing for 4-7× cost savings vs reconstruction.
8. **Sat / Good** — preserve with fog seal or crack seal as needed; not on the immediate queue.

## Modifiers that bump priority

- **Pedestrian exposure** — segments with crosswalks, schools, transit stops, hospitals get bumped.
- **ADA compliance** — Title II of the ADA requires accessible curb ramps and intact sidewalks. Damaged curbs / sidewalks at corners trigger compliance obligations that can force priority.
- **Drainage failure adjacency** — pavement failures combined with clogged catch basins or visible standing water bump priority because each accelerates the other.
- **311 reports** — citizen-reported defects bump segments above silent failures of similar severity. (LA, NYC, Chicago, etc. all weight 311 reports in PMS scoring.)
- **Recent reconstruction adjacency** — a Failed segment immediately adjacent to a recently-reconstructed segment is anomalous and signals an underlying issue (e.g., the reconstruction missed a drainage problem). Bumps investigation priority.

## What this means for your rationale

Your `grade()` rationale should give the dispatcher useful priority information. Good rationales:

- "Widespread alligator across both wheelpaths; pieces visibly loose at intersection. Multiple patch failures at stop bar." → tells dispatcher: severity high, intersection exposure, urgent.
- "Block cracking covering full lane; no alligator, no rutting; faded markings." → tells dispatcher: aging pavement at typical Fair stage, candidate for microsurface, no immediate urgency.
- "Single 30 cm pothole in right wheelpath, exposed base course, surrounded by intact pavement." → tells dispatcher: localized failure, easy patch repair, dispatch within 24-48h.

Bad rationales:

- "Pavement looks weathered, possibly Fair." → no actionable info.
- "Some cracks." → not specific.
- "Fair condition." → just restates the tier.

## Spend-now-save-later economics

The "$1 spent now saves $4-7 later" rule from FHWA [7] is the economic driver of why preservation timing matters. The cheap treatments (crack seal, fog seal, slurry, microsurface) only work BEFORE the pavement reaches structural failure. By the time alligator + potholes appear, only mill-and-overlay or reconstruction work — at 5-15× the cost.

This is why the Fair tier matters disproportionately: a Fair pavement caught early (PCI 60-65) is treatable with a $5/sq.yd microsurface. The same pavement in 2-4 years is Poor (PCI 45-55) and requires $15/sq.yd mill-and-overlay. In 2-4 more years it's Failed and requires $50/sq.yd reconstruction.

The dispatcher's queue logic prioritizes Fair-tier segments NOT because they're the worst pavement, but because they're the segments where cost-deferral economics are most punitive.

## What you, the agent, do with this knowledge

You don't compute priority — that's the dispatcher's job from your tier output. But you can:

1. **Make rationales actionable.** Name specific defects (location, type, severity), name surrounding context (intersection, crosswalk, residential vs arterial — when visible), name visible drainage status if obvious.
2. **Be honest about ambiguity.** When you're between two tiers, say so in confidence and rationale, so the dispatcher can weight the segment appropriately.
3. **Note when a treatment is visible.** A recently-treated segment is post-decision; don't queue it unnecessarily. See [`treatment_signatures.md`](treatment_signatures.md).
4. **Flag unusual juxtapositions.** "Failed segment between two recently-reconstructed blocks" is unusual and worth noting — it suggests an underlying issue (drainage, subgrade) the dispatcher should investigate before patching.

## Sources

- [6] FHWA Pavement Preservation Program — preservation philosophy.
- [7] FHWA — *Pavement Preservation: Preserving Our Investment in Highways* (Public Roads, Jan/Feb 2000) — cost-deferral data.
- [11] FHWA — Reformulated Pavement Remaining Service Life Framework (FHWA-HRT-13-038).

Full bibliography: [`research/source_index.md`](../../../research/source_index.md).
