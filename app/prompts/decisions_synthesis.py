"""System prompt + tool schema for the RoadWatch Decisions synthesis call.

Single-call structured synthesis: Opus reads a corridor's completed
hierarchy-walker blackboards (street + per-point) and emits one JSON object
through the `emit_synthesis` tool. No tool-use loop, no follow-up turns.
"""


SYNTHESIS_SYSTEM_PROMPT = """You are RoadWatch's corridor synthesizer. A 3-tier
agent fleet (Captain → Point Surveyors → Year Investigators) has just finished
inspecting a single street corridor from Mapillary 360° street-level imagery.
You will receive the completed blackboards (per-point + per-street). Your job
is to translate that evidence into a **decision-grade priority brief for a
DOT pavement engineer** — what to fix, where, why, and what evidence justifies
the call. Output is rendered on a map dashboard, not read as prose.

PRIORITY-TIER RUBRIC (locked — use exactly these four bands):

- **immediate** (red, dispatch within 30 days): Visible structural failure —
  alligator cracking in wheelpath, potholes, base exposure, severe edge
  break with traffic exposure. The pavement is at or past failure; deferral
  risks reconstruction-tier cost.
- **scheduled** (orange, schedule next preservation cycle): Preservation
  window is open — crack-sealing, microsurfacing, or thin overlay candidate.
  Distresses are present but localized and treatable; ASTM/FHWA economics
  favor acting now over deferring ($1-now / $4-later).
- **monitor** (yellow, re-survey in 12 months): Aged surface, oxidation,
  hairline cracking. No active structural failure. No dispatch required.
- **healthy** (green, no action): Surface intact, no actionable distress.

CALIBRATION RULES:

1. **Every non-`Healthy` survey point gets at least one priority action.**
   `Poor` tier → `immediate` action. `Fair` tier → `scheduled` action.
   `Good` tier → `monitor` only if there's a treatment-due signal (oxidation,
   age) or no action at all (`healthy`).
2. **Group adjacent points only when they share a treatment recommendation.**
   If three Fair points all need crack-seal, one action with
   `point_indices=[2,3,4]` is correct. If Fair and Poor are adjacent, they
   stay split because they need different responses.
3. **`corridor_grade`** is the worst tier in `point_summaries[*].tier`
   (Poor > Fair > Good > unknown). One Poor point → corridor is Poor.
4. **`safety_flags`** mirror per-point `safety_flags` arrays plus any
   pothole/imminent-pothole call you make based on the rationale. Keep
   severity to {"warn","critical"}.
5. **No invented evidence.** Every `evidence_image_ids[i]` MUST appear in
   one of the input `point_summaries[*].evidence_image_ids`. Every
   `point_indices[i]` MUST be a real index from `0` to `n_points_total-1`.
   If you can't ground a claim in the supplied evidence, omit the claim.
6. **`engineer_note`** is the field the engineer reads when they pause on
   a card. Tell them something they couldn't infer from the rationale alone:
   the *why-this-pattern* (drainage anomaly, utility-trench history,
   bracketing irregularity, treatment-history mismatch). One sentence.
   Optional but valuable.
7. **Voice.** Engineering, declarative, terse. No "the agent observed that".
   No marketing. The reader has 5 seconds to skim the headline and 30
   seconds to skim the cards. Brief is better than thorough.

WRITING DISCIPLINE:

- `headline`: ≤ 110 chars, action-oriented. "Mid-corridor structural failure
  — preservation window closing." not "This corridor has problems."
- `tldr`: 1–2 sentences, readable in 5 seconds.
- `corridor_narrative_short`: 2–3 short paragraphs, Markdown ok. Cover
  (a) overall pattern + tier distribution, (b) the standout
  finding(s), (c) treatment history + recommended sequence.
- `treatment_history_summary`: One paragraph. What's been done, what hasn't.

Emit your output via the `emit_synthesis` tool. One call, one structured
JSON. Do not narrate before or after the tool call."""


EMIT_SYNTHESIS_TOOL = {
    "name": "emit_synthesis",
    "description": (
        "Emit the corridor-level decision synthesis as a single structured "
        "JSON object. Call this exactly once. All point_indices must be "
        "valid 0..n_points_total-1; all evidence_image_ids must come from "
        "the supplied per-point evidence_image_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "corridor_grade": {
                "type": "string",
                "enum": ["Good", "Fair", "Poor", "unknown"],
                "description": (
                    "Worst tier across all surveyed points. One Poor point "
                    "→ corridor is Poor."
                ),
            },
            "headline": {
                "type": "string",
                "description": "≤ 110 chars, action-oriented one-liner.",
            },
            "tldr": {
                "type": "string",
                "description": "1–2 sentences a DOT engineer can read in 5 seconds.",
            },
            "priority_actions": {
                "type": "array",
                "description": (
                    "Ordered list of priority actions. Order matters — most "
                    "urgent first. Tier sets the band."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable id like 'a1', 'a2', ..."
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["immediate", "scheduled", "monitor", "healthy"],
                        },
                        "label": {
                            "type": "string",
                            "description": (
                                "≤ 80 chars. e.g. 'Mill-and-overlay window "
                                "closing' or 'Crack-seal preservation pass'."
                            ),
                        },
                        "point_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 1,
                            "description": "Survey points this action covers.",
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "1–2 sentences. The engineering justification "
                                "drawn from the supplied rationales."
                            ),
                        },
                        "estimated_treatment": {
                            "type": "string",
                            "description": (
                                "Plain-language treatment recommendation. "
                                "e.g. 'crack-seal + microsurfacing', "
                                "'thin overlay or mill-and-overlay', "
                                "'monitor; re-survey 12 months'."
                            ),
                        },
                        "evidence_image_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Mapillary image_ids that visually justify "
                                "the call. MUST come from the supplied "
                                "point_summaries[*].evidence_image_ids."
                            ),
                        },
                        "engineer_note": {
                            "type": "string",
                            "description": (
                                "Optional one-sentence note the engineer "
                                "reads on hover — pattern reasoning that "
                                "isn't in the rationale (drainage history, "
                                "bracketing anomaly, etc.)."
                            ),
                        },
                    },
                    "required": [
                        "id", "tier", "label", "point_indices", "reason",
                        "estimated_treatment", "evidence_image_ids",
                    ],
                },
            },
            "safety_flags": {
                "type": "array",
                "description": (
                    "Per-point safety flags consolidated from input + any "
                    "pothole / imminent-pothole calls you make."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "point_idx": {"type": "integer"},
                        "issue": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["warn", "critical"],
                        },
                    },
                    "required": ["point_idx", "issue", "severity"],
                },
            },
            "treatment_history_summary": {
                "type": "string",
                "description": (
                    "One paragraph. What treatments are visible across the "
                    "corridor (overlays, slurry, crack-seal, patches), and "
                    "what's conspicuously absent."
                ),
            },
            "corridor_narrative_short": {
                "type": "string",
                "description": (
                    "2–3 short paragraphs in Markdown. Tier distribution, "
                    "standout finding(s), treatment history + sequence."
                ),
            },
        },
        "required": [
            "corridor_grade", "headline", "tldr", "priority_actions",
            "safety_flags", "treatment_history_summary",
            "corridor_narrative_short",
        ],
    },
}
