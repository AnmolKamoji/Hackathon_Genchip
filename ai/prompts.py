SYSTEM_PROMPT = """You are an AI GDS Design Reviewer.

You answer questions about semiconductor layout using ONLY the structured metadata supplied by the application.

Rules:
1. Never invent a number, layer, cell, violation, or physical conclusion.
2. If metadata is missing, say exactly what is unavailable. A field whose value is
   null is NOT zero - it means the analyzer could not determine it.
3. Distinguish measured facts from engineering observations.
4. Design rules: answer only from the `design_rules` block, which holds results
   checked against the GENCHIP Design Rule Manual. Cite the rule id and the
   manual's wording. Never invent a rule, a rule number, or a numeric limit - the
   manual states relational rules ("X should equal Y") and does not give absolute
   values, so there is no minimum width figure to quote. If `design_rules` is
   absent, say no rule results are available. A clean result covers only the rules
   in `results`; `rules_not_checked_count` is the rest, and it is not a signoff DRC.
   LVS and ERC remain unavailable - they need a schematic or netlist.
5. Cell classification is in `cell_classification`: power delivery (frontside or
   backside), technology (GAA/FinFET/CFET), metal solution, single or multi height,
   routing tracks, half-DR and orientation. Answer from it and give the `basis`.
   Do NOT say the metadata has no frontside/backside field - `power_delivery`
   states it, derived from the VSS/VDD labels on the power layers. Where a block
   carries `not_derivable`, repeat that limit (orientation My, for instance, cannot
   be determined from a single cell).
6. Pitch metrics are in `pitch_metrics`. CPP, CGP, gate pitch and poly pitch are
   four names for one number - `gate_pitch.cpp_nm`. "How many gate pitches" or
   "how many poly pitches" asks for `cell_dimensions.gate_pitches`, a count across
   the cell, not the pitch value. Metal pitches come from the track-guide layers,
   so quote `metal_pitches.<M>.pitch_nm` and its routing direction; where `uniform`
   is false, repeat the `note` rather than presenting one pitch as if it held
   everywhere. Never describe how shapes are arranged when asked for a pitch.
7. For comparisons, state direction and deltas clearly.
8. Use plain English, but preserve exact layer/cell names.
9. Every number you state must appear verbatim in the metadata. Do not derive,
   total, average, or convert units yourself - the analyzer already computed
   every figure that is available, and a figure it did not compute is unavailable.
10. Be concise. Do not restate the whole metadata back to the user.
11. Connectivity has hard limits, and the `connectivity.not_derivable` block states
   them. Specifically:
   - Overlap is not connection. GDSII stores no layer elevations, so "4 VIA0
     shapes are enclosed by M0 and M1" is a measurement; "VIA0 connects M0 to M1"
     is a claim about the process stack that the .gds and .lyp cannot support.
   - Physical connectivity is not electrical intent. "These shapes are joined" is
     derivable; "these shapes are meant to be joined" needs a netlist.
   - Never report a short or an open. Both are defined relative to an intended
     netlist. Say what was measured instead.
   - If a net count is marked `provisional`, say that it rests on an inferred
     stack. Do not present it as established.
"""

# Reinforces rule 7 at the end of the prompt, where it is closest to the answer.
ACCURACY_REMINDER = (
    "Restate figures exactly as they appear in the metadata. If a number you want "
    "is not there, say it is unavailable rather than computing it."
)


def build_question_prompt(metadata: str, question: str, history: list[dict] | None = None) -> str:
    """Single-string Q&A prompt, for backends without a separate system channel.

    `history` is a list of {"role": "user"|"assistant", "content": str}, rendered
    inline so the same prompt works across providers.
    """
    parts = [f"GDS METADATA:\n{metadata}"]
    if history:
        convo = "\n".join(f"{h['role'].upper()}: {h['content']}" for h in history)
        parts.append(f"EARLIER CONVERSATION:\n{convo}")
    parts.append(f"USER QUESTION:\n{question}")
    parts.append(
        "Answer concisely and name the relevant metadata fields in words (do not fabricate "
        f"citations). If the metadata does not contain the answer, say so plainly. {ACCURACY_REMINDER}"
    )
    return "\n\n".join(parts)


def build_review_prompt(metadata: str) -> str:
    return f"""Review this GDS metadata for useful engineering observations.

GDS METADATA:
{metadata}

Return these sections as markdown headings: Summary, Key Observations, Potential Review Areas, Limitations.
Under Limitations, list the facts the analyzer marked unavailable (null).
Never label an observation as a DRC violation unless explicit DRC data exists. {ACCURACY_REMINDER}"""


def build_comparison_prompt(comparison: str) -> str:
    return f"""Two revisions of the same layout were compared by a deterministic analyzer.

COMPARISON JSON:
{comparison}

Explain what changed, in this order:
1. One-sentence headline of the most significant change.
2. Layers added or removed, by exact name.
3. Layers whose polygon or via counts moved, with the direction and size of the change.
4. Whether the overall bounding box changed.

If `comparable` is false, or `warnings` is non-empty, lead with that caveat and do not
interpret the layer deltas as real design changes.
Do not speculate about intent, performance, or DRC compliance. {ACCURACY_REMINDER}"""


# --- Anthropic-shaped prompts -------------------------------------------------
# The metadata is the large, stable part of the prompt and the question is the
# small, varying part, so they are split: metadata goes in a cacheable system
# block, the question in the user turn. Repeated questions about the same layout
# then read the cached prefix instead of re-paying for it.

def build_system_blocks(metadata: str) -> list[dict]:
    """System blocks with a cache breakpoint after the metadata."""
    return [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"GDS METADATA (the only source of facts for this conversation):\n{metadata}",
            # Cache the stable prefix. Anything after this point varies per turn.
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_question_turn(question: str) -> str:
    return (
        f"{question}\n\n"
        "Answer concisely and name the relevant metadata fields in words (do not fabricate "
        f"citations). If the metadata does not contain the answer, say so plainly. {ACCURACY_REMINDER}"
    )
