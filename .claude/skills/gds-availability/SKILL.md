---
name: gds-availability
description: Decide whether a requested layout fact is derivable from the available inputs, and label it correctly. Use before implementing any new analysis, or when asked "can we determine X from the GDS?".
---

# What is derivable from which inputs

Every feature must be labelled with the information it needs. Getting this wrong
is worse than not implementing the feature, because a confident wrong answer is
harder to detect than a missing one.

| Label | Meaning |
|---|---|
| **GDS-only** | Derivable from the `.gds` alone. Exact. |
| **LYP-only** | Names and display properties. No geometry. |
| **GDS + LYP** | Geometry from the GDS, layer identity from the LYP. |
| **GDS + sidecar** | Needs the semantic JSON, which names layers by function. |
| **Requires PDK/DRC rules** | Needs a rule deck, process stack or technology file. |
| **Requires netlist/design intent** | Needs a netlist, schematic, LVS reference or spec. |

Assume the user supplies **`.gds` + `.lyp` only**. The `.lyp` is bundled as a
default (`analyzer/layermap.py:BUNDLED_LAYERMAP`); the JSON sidecar usually is not
present. Design for that case first.

## The two rules that override everything

1. **A measurement is never a rule violation.** "Observed M1 minimum width is
   0.09 µm" is derivable. "M1 minimum width violation" needs a rule deck. Name
   fields `observed_*` and say so in the prose.
2. **A physical fact is never electrical intent.** "M0 and M1 are physically
   joined through VIA0" is derivable given a stack. "They are *supposed* to be
   joined" needs a netlist.

## GDSII has no Z axis

This is the single most important limitation, and it was established by
measurement, not caution. A GDS records shapes on numbered layers; it does not
record which layer is above which, nor which via bridges which two levels. A
`.lyp` carries colours and names, not elevations.

Three ways to infer the stack from geometry were tried and each fails:

- **Plan-view overlap** — in a dense cell nearly everything overlaps everything;
  this collapsed a whole cell into one false net spanning 21 layers.
- **Full enclosure** — a contact is wider than the fin it straddles so nothing
  encloses it, while a cell-spanning backside rail encloses vias it never touches.
- **Ubiquity** (demote layers that are candidates for every connector) — the real
  local-interconnect layer is *also* a candidate for every connector.

So the net graph needs a **connection stack**, which comes from one of three
places, in this precedence order — and the source is always recorded in
`stack_source`:

1. A user-supplied stack file (`--stack`, or the uploader). Exact for that stack.
2. The sidecar's via layer names (`VIA_M0_M1` states its endpoints).
3. The bundled technology stack (`analyzer/connectivity.py:BUNDLED_STACK`), which
   is **not PDK-verified** and says so wherever it is used.

## Never answer these

| Request | Why | What to give instead |
|---|---|---|
| Physical short | Defined against an intended netlist | Measured adjacency: which shapes touch |
| Physical open | Same | Connectors overlapping no conductor at all — that holds whatever the intent |
| DRC / rule pass-fail | No rule deck is supplied and no DRC engine runs | The measurement, labelled as observed |
| Health or risk score | A single score implies a threshold, and thresholds come from a rule deck | The individual measurements |
| A cell's *purpose* | Not recorded in GDSII | Naming may hint; say it is a hint |

For anything not implemented, refuse **by name** via `UNCOMPUTED_METRICS` in
`ai/deterministic.py`, and state that silence is not evidence of absence. A loose
regex branch answering with a nearby number is the failure mode to avoid: a
question about vertices once came back with a polygon count.

## Before implementing anything new

- [ ] Which label does it carry? If **Requires PDK** or **Requires netlist**, it
      is a refusal with an explanation, not a feature.
- [ ] Does it work with `.gds` + bundled `.lyp` alone? That is the real case.
- [ ] Is `None` used for "undeterminable", never `0`?
- [ ] Is the row added to `CAPABILITIES.md` with its label?
- [ ] Does `models/metadata.py` validate the new invariant?
