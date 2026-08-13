# What this platform can and cannot determine

Every feature is labelled with the information it requires. The labels are:

| Label | Meaning |
|---|---|
| **GDS-only** | Derivable from the `.gds` alone. Exact. |
| **LYP-only** | Derivable from the `.lyp` alone (naming and display, no geometry). |
| **GDS + LYP** | Needs both: geometry from the GDS, layer identity from the LYP. |
| **GDS + sidecar** | Needs the semantic JSON sidecar, which names layers by function. |
| **Requires PDK/DRC rules** | Needs a rule deck, process stack or technology file. |
| **Requires netlist/design intent** | Needs a netlist, schematic, LVS reference or spec. |
| **Not possible** | Not derivable from any combination of the above inputs. |

Two rules govern every output:

1. **A measurement is never reported as a rule violation.** "Observed M1 minimum
   width is 0.09 µm" is a measurement. "M1 minimum width DRC violation" requires a
   rule deck, and no rule deck is supplied.
2. **A physical fact is never reported as electrical intent.** "M0 and M1 are
   physically joined through VIA0" is derivable given a process stack. "M0 and M1
   are *supposed* to be joined" requires a netlist.

---

## The single most important limitation: GDSII has no Z axis

A GDS file records shapes on numbered layers. It does **not** record which layer
sits above which, nor which via layer bridges which two levels. A `.lyp` records
colours, fill patterns and names — not elevations. So the **vertical connection
stack is absent from both inputs**, and it is the gate on every net-level result.

This was established by measurement, not assumption. Three candidate ways to infer
the stack from geometry were tried on the sample technology, and each fails:

| Discriminator | Why it fails |
|---|---|
| Plan-view overlap (`interacting`) | In a dense standard cell nearly every connector layer overlaps nearly every conductor layer, because they are stacked. Treating overlap as connection collapsed a whole cell into one false net spanning 21 layers. |
| Full enclosure (`inside`) | A diffusion contact is wider than the fin it straddles, so nothing encloses it. Meanwhile a cell-spanning backside power rail *does* enclose vias it has no connection to. |
| Ubiquity (demote layers that are candidates for every connector) | The genuine local-interconnect layer (`M0`) is also a candidate for every connector, so demoting ubiquitous layers demotes the right answer. |

The concrete case: this technology uses backside power delivery (the `.lyp`
contains `BSPDN-PMOS-VIA`), so `BM0` underlies the entire cell. Geometry alone
therefore reports `N-VIAT` as joining `M0` and `BM0` with maximum confidence, and
that is wrong. Knowing `BM0` is on the far side of the wafer is technology
knowledge.

**Consequence:** the net graph is only computed from an explicitly supplied
connection stack (`data/samples/Titan_stack.json` shows the format), or from an
inferred stack that the caller explicitly accepts — in which case every result is
labelled provisional. It is never applied silently.

### One good way out: the sidecar names the stack

When a semantic JSON sidecar is present it names each via layer after its
endpoints — `VIA_M0_M1`, `VIA_M0_PMOSGate`, `VIA_Inteconnect_BSPowerRail`. That
*states* the stack instead of leaving it to be guessed from geometry, so the net
graph becomes available from **GDS + sidecar** with no PDK file. It is still a
naming convention rather than verified technology data, and is labelled as such.

This corrected the `.lyp`-only reading in two places, and both mattered:

1. The layers a `.lyp` calls `NDIFFCON`/`PDIFFCON` are named
   `NMOSInterconnect`/`PMOSInterconnect` in the sidecar. They are local
   interconnect **conductors**, not contacts. The `"CON"` name heuristic made them
   contacts bridging diffusion to `M0`, which shorted an entire cell into one net.
   A stack file can now correct any role via its `roles` block.
2. `N-VIAT`/`P-VIAT` land on those interconnect layers, not on the diffusion
   (nanosheet) layers directly.

With the corrected stack, results are physically sensible and the two independent
stack sources — the hand-transcribed file and the sidecar-derived one — produce
**identical net graphs on all four sample files**:

| File | Nets | Structure |
|---|---|---|
| `DCAP0_1`, `DCAP0_2` | 4 | Two mirrored capacitor terminals (gate + interconnect + M0 + M1), plus two backside power taps. Exactly a decap. |
| `NR2D1_1` | 7 | Two gate inputs, one interconnect net, two power taps, and two `PMOSInterconnect` stubs no via reaches. |
| `NR2D1_2` | 6 | The same, but the added `VIA_M0_PMOSInterconnect` connects one of those stubs — a real, reportable difference between revisions. |

---

## 1. File integrity and structure

| Feature | Availability | Status |
|---|---|---|
| Readable GDSII, record structure, `UNITS`/dbu | **GDS-only** | Done |
| Top cell(s), multiple/ambiguous top cells | **GDS-only** | Done — multiple tops are reported, not silently truncated |
| Cell count, hierarchy depth, instance placements | **GDS-only** | Done — depth, levels per cell, placements vs records |
| Empty cells, orphan cells, recursion | **GDS-only** | Done — `analyzer/hierarchy.py`; orphans are cells unreachable from the analysed top cell |
| Layer/datatype inventory, as-stored vs flattened counts | **GDS-only** | Done |
| Geometry fingerprint for change detection | **GDS-only** | Done — SHA over merged polygons, cross-checked against KLayout XOR |

## 2. Layer mapping and identity

| Feature | Availability | Status |
|---|---|---|
| Layer number/datatype → technology name | **LYP-only** | Done — 49 entries parsed |
| Display properties (colour, fill, visibility) | **LYP-only** | Parsed; not surfaced in the UI |
| Role from naming (drawing / pin / label / duplicate) | **LYP-only** | Done |
| Role from naming (metal / via / contact / poly / diffusion) | **LYP-only** | Done — a name heuristic, and labelled as one |
| Layers present in GDS but absent from LYP (and vice versa) | **GDS + LYP** | Done |
| **True layer purpose and process meaning** | **Requires PDK/DRC rules** | Not possible from a `.lyp`; names are a convention, not a contract |

## 3. Geometry measurement

| Feature | Availability | Status |
|---|---|---|
| Shape counts by type (box / polygon / path / text) | **GDS-only** | Done |
| Merged area per layer, union vs sum of parts | **GDS-only** | Done — verified against independent KLayout runs |
| Bounding boxes, width/height | **GDS-only** | Done |
| Per-layer-name totals across datatypes | **GDS + LYP** | Done — union within a layer number, summed across layer numbers |
| Observed minimum width / spacing | **GDS-only** | Done — KLayout width/space checks, cross-checked independently |
| Perimeter, as drawn and merged | **GDS-only** | Done — the two differ where shapes abut, and both are reported |
| Vertex counts, non-rectangular shape counts | **GDS-only** | Done |
| **Whether a measured width/spacing passes** | **Requires PDK/DRC rules** | Never claimed |

## 4. Metal layer analysis

| Feature | Availability | Status |
|---|---|---|
| Metal layer identification | **GDS + LYP** | Done |
| Per-metal area, shape count, density | **GDS + LYP** | Done |
| Observed minimum width, observed minimum spacing | **GDS-only** | Done — per layer and per role, labelled `observed_` |
| Wire direction / preferred routing direction | **GDS-only** | Partial — shape extents and array pitch are measured; no preferred-direction inference |
| **Width/spacing rule compliance** | **Requires PDK/DRC rules** | Never claimed |

## 5. Via analysis

| Feature | Availability | Status |
|---|---|---|
| Via layer identification | **LYP-only** | Done |
| Via count per layer, sizes, positions | **GDS + LYP** | Done |
| Which conductor layers each via overlaps / is enclosed by | **GDS + LYP** | Done — reported as *overlap*, never as *connection* |
| Vias overlapping no conductor at all | **GDS + LYP** | Done — stack-independent, so safe to report |
| Which two levels a via actually bridges | **Requires PDK/DRC rules** | Inferred with a confidence and alternatives; never asserted |
| **Via enclosure/overlap rule compliance** | **Requires PDK/DRC rules** | Never claimed |

## 6. Contact analysis

| Feature | Availability | Status |
|---|---|---|
| Contact layer identification, counts, sizes | **GDS + LYP** | Done |
| Contact-to-diffusion / contact-to-metal overlap | **GDS + LYP** | Done, as measured overlap |
| **Contact rule compliance** | **Requires PDK/DRC rules** | Never claimed |

## 7. Physical connectivity — implemented in three tiers

| Feature | Availability | Status |
|---|---|---|
| **Tier 1** — intra-layer connected components (shapes that touch are one conductor) | **GDS-only** | Done, exact — cross-checked against from-scratch union-find on 106 (layer, shapes, components) triples, 0 mismatches |
| Per-layer fragmentation (shapes vs conductors) | **GDS-only** | Done |
| **Tier 2** — via/contact landing measurement (interaction + enclosure ratios) | **GDS + LYP** | Done, exact — cross-checked by an independent formulation on 122 pairs, 0 mismatches |
| Conductor layers that abut edge-to-edge (one level under two names, e.g. `NPOLY`/`PPOLY`) | **GDS + LYP** | Done, measured |
| **Tier 3** — net graph, connected components across layers | **Requires PDK/DRC rules** (the process stack), **or GDS + sidecar** | Done, from a supplied stack, a sidecar-derived stack, or an explicitly accepted inference |
| Connection stack from sidecar via layer names | **GDS + sidecar** | Done — cross-validated against the hand-written stack, identical nets on all 4 files |
| Layer role correction (`roles` block in the stack file) | **Requires PDK/DRC rules** | Done — the `"CON"` heuristic is wrong on this technology and must be correctable |
| Floating conductors (nets using no via or contact) | **Requires PDK/DRC rules** or **GDS + sidecar** | Done, under the stack in use |
| Stack plausibility check (whole cell collapsing to one net) | **GDS + LYP** | Done — flags an over-connecting stack rather than reporting "1 net" |
| Supplied stack vs measured geometry cross-check | **GDS + LYP** | Done |
| **Physical shorts** | **Requires netlist/design intent** | Always refused — a short is defined against an intended netlist |
| **Physical opens** | **Requires netlist/design intent** | Always refused; vias overlapping no conductor are offered as the observable proxy |
| **Electrical intent of any net** | **Requires netlist/design intent** | Always refused |
| Net names, pins, ports | **Requires netlist/design intent** | Not possible from GDS + LYP (text labels give hints only) |

## 8. Density and hotspots

| Feature | Availability | Status |
|---|---|---|
| Per-layer density over the cell bounding box | **GDS-only** | Done |
| Windowed density map / hotspot location | **GDS-only** | Not implemented — refused by name; whole-cell density per layer is exact |
| **Density rule compliance (min/max window density)** | **Requires PDK/DRC rules** | Never claimed |

## 9–12. Anomalies, patterns, complexity, utilization

| Feature | Availability | Status |
|---|---|---|
| Statistical outliers in size, count, density | **GDS-only** | Not implemented — refused by name |
| Array pitch / regular arrangement | **GDS-only** | Done — measured pitch per layer (row, column, grid or irregular) |
| Repeated patterns in raw geometry | **GDS-only** | Not implemented — refused by name |
| Cell complexity inputs (vertices, depth, layer count) | **GDS-only** | Done; no single complexity *score* is produced |
| Layer utilization ranking | **GDS + LYP** | Done — share of geometry and coverage per layer |
| **Whether an anomaly is a defect** | **Requires netlist/design intent** | Never claimed |

## 13–14. Instance placement and spatial analysis

| Feature | Availability | Status |
|---|---|---|
| Instance placements, transforms, array repetitions | **GDS-only** | Done — placements counted separately from records |
| Layer abutment / overlap measurement | **GDS + LYP** | Done — measured adjacency between conductor layers |
| Spatial distribution statistics | **GDS-only** | Not implemented — refused by name |

## 15–17. Via distribution, metal fragmentation, health scoring

| Feature | Availability | Status |
|---|---|---|
| Via count, size, spacing, pitch per layer | **GDS + LYP** | Done |
| Via spatial distribution statistics | **GDS + LYP** | Not implemented |
| Metal fragmentation (shapes per physical conductor) | **GDS-only** | Done — tier 1, exact |
| Composite "health score" | — | **Deliberately not implemented.** A single score implies a pass/fail threshold, which requires a rule deck. Individual measurements are reported instead. |

## 18–19. Risk scoring and AI layer

| Feature | Availability | Status |
|---|---|---|
| Natural-language explanation of measured metadata | **GDS + LYP** | Done — the model may only restate figures the analyzer computed |
| Anomaly prioritisation and root-cause hypotheses | **GDS + LYP** | Partial — hypotheses are labelled as such |
| **Risk score presented as a verdict** | **Requires PDK/DRC rules** | Never produced |

## 20–23. Querying, reporting, boundaries

| Feature | Availability | Status |
|---|---|---|
| Natural-language Q&A over the metadata | **GDS + LYP** | Done — deterministic answers first, model only for phrasing |
| Deterministic refusal of DRC/LVS/short/open questions | — | Done, and tested |
| Two-file comparison with geometry-level change detection | **GDS-only** | Done — verified against KLayout XOR |
| Report generation | **GDS + LYP** | Done |
| Explicit statement of what is not derivable | — | Done — carried in the metadata itself so the model sees it |

## 24. Tech-file parameters

Every parameter a tech file states, recovered from the layout so the question
"what is the gate extension in this cell?" is answered by measuring the cell.
Each measurement implements the definition in the GENCHIP Design Rule Manual rule
cited against it.

| Feature | Availability | Status |
|---|---|---|
| Poly and diffcon widths (3.2.1, 3.3.1) | **GDS + LYP** | Done — measured orthogonal to the derived poly direction |
| Diffusion width, power rail width (3.1.1, 3.12.1) | **GDS + LYP** | Done |
| N/P diffusion, poly-to-diffcon, gate cut, diffcon ETE spacings | **GDS + LYP** | Done — facing pairs only; abutting pairs are not a spacing |
| Gate and diffcon extension (3.2.2, 3.3.3) | **GDS + LYP** | Done — the *minimum* over every overlapping pair |
| Metal0/1/2 track profiles | **GDS + LYP** | Done — a cross-section that sums to the cell dimension |
| Via size, offset, enclosure, via extension (3.7.2, 3.9.2) | **GDS + LYP** | Done — extension recovered from the rule, not measured directly |
| Technology, power delivery, routing capability, orientation, height, tracks | **GDS + LYP** | Done — from the cell classifier |
| **Diffusion to Diff interconnect spacing** | **Requires a CFET layout** | Not measured — rule 3.13.5 is CFET-only and the layer is empty in GAA/FinFET |
| Comparison against a stated tech file | **Optional `<stem>.techparams.json`** | Done — measured and stated are reported side by side; a stated figure is never presented as a measurement |

Three readings this gets right that a naive one gets wrong:

- **Gate extension** measures 20.5 nm on an uncut gate, because the poly runs on to
  meet the poly of the opposite device. Only the cut column shows the real 12 nm, so
  the minimum over every poly/diffusion pair is what "minimum extension" means.
- **Gate cut spacing** exists only where the gate is actually cut. Three of the four
  gates in `AN2D1_2_RT_4.gds` run straight through, and counting them as
  zero-spacing pairs reports a gate cut spacing of 0 nm.
- **Routing capability** comes from the track guides, not the drawn wires. `AN2D1_2`
  routes on M0 and M1 only and is still a three-metal cell, exactly as its tech file
  states. Counting drawn metal made a standard cell's metal solution a function of
  how busy its logic was.

Where a parameter cannot be measured, the reason is reported rather than a zero. Six
unrelated pairs in `AN2D1_2_RT_4.gds` measure exactly 15 nm — the diffusion break and
poly and diffcon to BM0, because rule 3.1.6 ties the diffusion break to the poly
width — so any of them could be dressed up as the missing CFET spacing. None is.

## 25. Judging the answers

The tool measures a layout and then talks about it, and those are two places a wrong
number can appear. Section 24's parameters are checked against the tech file and the
DRC engine; this section is about grading the *answers*, including the ones the
Anthropic model writes.

| Tool | What it does |
|---|---|
| `tools/oracle.py` | An independent fact sheet. Reads the layout with **gdstk** and parses the .lyp with a plain XML reader, importing nothing from `analyzer/` and not using KLayout at all. |
| `tools/judge.py` | Grades answers against that fact sheet on three axes, and self-tests first. |
| `tools/render.py` | Renders the layout to PNG through KLayout's own `LayoutView`, with the .lyp colours. |

The independence is the point. An answer key built with the same library as the
analyzer would confirm the analyzer's own mistakes, so the key comes from a second GDS
reader with its own parser. When the two agree, two codebases that share no code agree.

The judge grades three things separately, because they fail separately:

1. **Correctness** — does the answer state the value the oracle measured? A fluent,
   perfectly grounded answer can still be the answer to a different question.
2. **Grounding** — is every number in the answer present in the metadata? Catches
   figures derived, summed or unit-converted by the model.
3. **Restraint** — does it avoid what a `.gds` and `.lyp` cannot support? No DRC
   verdict, no short or open, no LVS result, no electrical intent, and no stated
   tech-file figure passed off as a measurement.

The restraint scan is negation-aware, because a correct refusal names the claim it is
refusing. "I cannot say whether the layout is DRC clean" and "DRC clean status is
unavailable" are refusals; "the layout is DRC clean, though I cannot check the timing"
is an overclaim with an unrelated hedge attached. All three are pinned by tests.

```
python tools/judge.py                  # deterministic answers, no API cost
python tools/judge.py --self-test      # only the negative controls
python tools/judge.py --model --restraint-only --gds data/samples/AN2D1_2_RT_4.gds
```

`--model` costs API credit — one call per question per file — so it refuses a run over
40 calls without `--yes` and prints the narrower commands instead.

**Results.** 14 negative controls behave correctly, so the judge can fail things.
Deterministic answers: **164/164 across all five samples**, 5 questions deferred to the
model because no local branch claims them. Model answers on `AN2D1_2_RT_4.gds`:
**28/28 correctness** against the independent oracle and **7/7 restraint**.

An image is for sanity, not measurement — a pixel is about 0.15 nm at these zoom
levels. A test asserts that no module in `analyzer/` reads one.

---

## 26. The layout viewer

A canvas viewer (`ui/viewer.js`, `ui/viewer.css`, mounted by `ui/viewer.py`) replaces
the Plotly figures. It runs inside an iframe with the whole geometry payload, so a
layer toggle, a zoom or a ruler never re-runs the Python script — which is what made
the old view jump back and its hover-revealed toolbar vanish on the way to it.

**What it does that KLayout does.** Pan, wheel-zoom at the cursor, box zoom, fit,
view history; a layer list with per-layer visibility, solo, colour swatch,
layer/datatype, shape counts and a filter; dither patterns, fill/outline, opacity,
labels, grid, scale bar and a live coordinate readout; rulers with vertex and edge
snapping; an area box; a shape probe reporting the measured size, area, centre,
origin and vertex count; a cell tree with placements, instance boundaries and a
hierarchy-depth control; saved views; PNG export; keyboard shortcuts with a help
overlay.

**What it does that KLayout does not.**

| Tool | What it answers |
|---|---|
| Rule marker browser | Every DRM result is a row; clicking one isolates the layers *that check read* and zooms to them. Visited state, waive, failures-only, `N`/`Shift+N` to step. |
| Net tracing | Click a shape to highlight the whole physical net; shift-click a second for a same-net / different-net verdict. Says "physical, not intent" on every answer. |
| Routing-grid overlay | Draws the track centres from the track-guide layers, so "is this wire on grid?" is answered by looking (`T`). |
| Find by measured size | `w<21`, `h>50`, `a<300` over the analyzer's own numbers — the question KLayout needs a DRC script for. Enter steps through the hits. |
| Difference browser | In a comparison, every XOR region is a row, largest first, with its area and extent; stepping through them is `N`. Picking one forces a compare mode that can show it. |
| Auto-measure | Double-clicking a shape drops two rulers matching its measured width and height. |
| Share a view | Copies zoom, centre, layer set and overlays as a string that restores exactly that view. |
| Chatbot beside the drawing | The expanded workspace puts the same Q&A path next to the layout, with the same enriched metadata the page uses. |

Every figure the viewer shows was measured in Python and shipped with the geometry;
nothing is re-derived from screen pixels. 71 browser tests drive the real document in
headless Chromium — picking, zoom anchoring, layer toggles, snapping, marker
cross-probe, net probe, the finder, the cell tree and the difference browser — and any
JavaScript error or `console.error` fails the test that provoked it.

---

## 27. Editing the layout

The viewer can change a layout and write a new GDSII. The division of labour is the
same one the rest of the tool uses: **the browser records what you did; KLayout
writes the file.** Nothing in between guesses.

An edit names the shape it changes — the cell it lives in, its outline in *that
cell's* own database units, and its rank among identical siblings — so a journal
applied to a file that has moved on is refused rather than applied to the wrong
polygon. Applying is atomic: one bad operation writes nothing.

**Why it is not done in JavaScript.** A polygon on a canvas is a float; a polygon in
a GDSII is an integer on the database grid. Rounding one to the other in the browser
puts off-grid vertices in the file, which is how a layout stops being manufacturable
without anything looking wrong. And every shape the viewer draws was flattened
through the hierarchy, so the rectangle under the pointer may live in a child cell
shared by twenty placements — editing it by screen position would silently change all
twenty. A displacement asked for in screen coordinates is rotated into the cell's own
frame before it is written, or a 90° instance moves sideways.

**Tools.** Select (click, shift-click, rubber band), move (drag or arrow keys by the
grid step), rectangle, polygon, wire (a centre line plus the width the layer already
measures), label, vertex and edge handles, rotate, mirror, copy, paste, duplicate,
step-and-repeat, merge, subtract, delete, cell placement including arrays, undo/redo,
and `Ctrl+S` to apply. Snapping goes to edges and vertices first, then to the
**routing tracks** read off the track-guide layers, then to the design grid.

**Four things KLayout does not do:**

| | |
|---|---|
| Checks follow the edit | Applying re-runs the design rule check, connectivity, pitch and classification on the file that was just written, so the markers beside the drawing describe what you made. |
| The write reports itself | Every apply says how the file now differs from the one you uploaded, measured by the same XOR the comparison page uses. |
| Off-grid is caught | Vertices *this edit* added off the grid you were drawing on are counted and named, separately from the ones the file already had. |
| Shared cells warn first | An edit to a cell placed more than once says how many placements it reaches — before it is made. |

The upload is never modified. An edited file lives in the session, is offered as a
download, and can be reverted in one click.

**Verification.** 37 engine tests cover the operations and, more importantly, the
refusals: a stale target, a target whose file has moved on, an unknown layer, a
degenerate polygon, a self-placement, an array with no step, and a batch where one
operation fails and nothing is written. A round-trip test drives the real editor in
headless Chromium, applies the journal it produced with KLayout, and compares the
written file against the browser's preview **polygon by polygon across 34 layers** —
the one assertion that catches the two sides drifting apart.

---

## Verification standard used

Every numeric claim in this document was checked against a **separately written**
computation, never against the analyzer's own output:

- Tier-1 components vs from-scratch union-find over pairwise shape interaction:
  **106 (layer, shapes, components) triples, 0 mismatches**.
- Tier-2 landings vs an independent formulation (set difference for enclosure,
  region growing for interaction): **122 (connector, conductor) pairs,
  0 mismatches**.
- The two stack sources against each other — a stack transcribed by hand against
  the `.lyp` names, and one parsed from the sidecar's via names: **identical net
  graphs on all 4 sample files**.
- Layer areas vs independent KLayout runs: **48 groups, 0 mismatches**.
- Geometry change detection vs KLayout XOR: **exact agreement**.
- Deterministic answers vs the contextual fact-checker, across 24 configurations
  (4 files × 2 metadata modes × 3 stack sources): **892 claims, 0 unsupported**,
  with **24 negative-control self-tests** proving the checker catches fabricated
  figures rather than passing everything.
- All 24 configurations validated against the metadata and connectivity schema
  contracts.
- Pitch metrics vs KLayout measured three other ways — centre-to-centre from
  polygon extents, same-side edge-to-edge from `Region.edges()`, and
  `space_check` + `width_check` — over 3 identical runs on every sample:
  **CPP 45 nm confirmed by 6 to 12 independent measurements per file**, M0 21 nm,
  M1 30 nm, M2 28 nm, cell width an exact whole multiple of CPP, and the M0 track
  grid closing on the cell height.
- Tech-file parameters vs KLayout's DRC engine (`width_check`,
  `separation_check`, boolean subtraction for the extensions), 3 identical runs on
  every sample: **11 to 12 parameters confirmed per file, 0 mismatches**, with rule
  3.2.6 (2 x poly-to-diffcon + diffcon width + poly width) closing on the measured
  45 nm gate pitch.
- Tech-file parameters vs a tech file supplied independently of this repository for
  `AN2D1_2_RT_4.gds`: **26 of 26 comparable parameters agree, 0 disagree**. The
  figures were not produced by any code here, so the agreement is not a module
  confirming itself.
- Every measurement vs `tools/oracle.py`, an independent fact sheet built with
  **gdstk** rather than KLayout and its own .lyp parser: **agreement on the tech
  parameters, the gate pitch, all three metal pitches, the top cell and the polygon
  count, on all five samples**.
- Answers graded by `tools/judge.py` against that oracle on correctness, grounding and
  restraint: **164/164 deterministic**, and **35/35 on the model path** for
  `AN2D1_2_RT_4.gds`, with **14 negative controls** proving the judge fails wrong
  answers rather than passing everything.
- **717 automated tests**, including 33 that drive the real Streamlit app with the
  real sample files, and `verify_setup.py`'s 15 end-to-end checks.
- Live model answers spot-checked against the fact-checker: the boundaries held
  under leading questions ("the vias overlap both M0 and M1, so VIA0 connects
  them, correct?" was rejected), and two wording defects the live test exposed —
  a sidecar-derived stack being described as a geometric guess, and net identities
  missing from the digest — were fixed.

---

# The 59-topic checklist, answered

Run empirically: each topic was posed as a natural question against all four sample
files in both `gds` and `fused` mode (8 configurations), and every numeric claim in
every answer was audited by `tools/claimcheck.py`.

**Result: 384 answers produced locally, 88 deferred to the model, 656 numeric claims
audited, 0 unsupported.**

"Local" means a deterministic answer computed by the analyzer. "Model" means the
figures are in the metadata and Claude phrases them — it cannot invent a number,
because the accuracy checks apply to its output too.

| # | Topic | Availability | Status |
|---|---|---|---|
| 1 | GDS File Integrity & Basic Validation | GDS-only | Model, from warnings + consistency |
| 2 | GDS Format & Version | GDS-only | Model, from `technology.raw_version` |
| 3 | Database Unit & User Unit | GDS-only | Model, from `source.dbu_um` |
| 4 | Top-Level Cell | GDS-only | **Local** |
| 5 | Cell & Hierarchy | GDS-only | **Local** |
| 6 | Cell Reference Validation | GDS-only | **Local** |
| 7 | Recursive Hierarchy Detection | GDS-only | **Local** |
| 8 | Empty & Orphan Cell Detection | GDS-only | **Local** |
| 9 | Cell Instance Analysis | GDS-only | **Local** — placements vs records |
| 10 | Hierarchy Depth | GDS-only | **Local** |
| 11 | Layer Identification & Mapping | GDS + LYP | **Local** |
| 12 | GDS–LYP Layer Consistency | GDS + LYP | Model, from the `consistency` block |
| 13 | Layer Usage | GDS + LYP | **Local** |
| 14 | Layer Statistics | GDS + LYP | Model, from the per-layer rows |
| 15 | Geometry Extraction | GDS-only | Model, from the per-layer rows |
| 16 | Polygon Analysis | GDS-only | **Local** — discloses datatype duplication |
| 17 | Path Analysis | GDS-only | **Local** — widths, or "no paths present" |
| 18 | Bounding Box | GDS-only | **Local** |
| 19 | Area & Perimeter | GDS-only | **Local** — as-drawn *and* merged perimeter |
| 20 | Vertex & Polygon Complexity | GDS-only | **Local** |
| 21 | Metal Layer Analysis | GDS + LYP | Model, from role aggregates |
| 22 | Metal Width | GDS-only | **Local** — observed, never a verdict |
| 23 | Metal Spacing | GDS-only | **Local** — observed, never a verdict |
| 24 | Metal Area | GDS + LYP | **Local** — summed over metal layers |
| 25 | Metal Density | GDS-only | **Local** |
| 26 | Metal Overlap & Intersection | GDS + LYP | **Local** — measured overlap |
| 27 | Floating Metal Detection | needs a stack | **Local** — under the stack in use |
| 28 | Via Layer Analysis | LYP | Model, from role aggregates |
| 29 | Via Size | GDS + LYP | **Local** |
| 30 | Via Spacing | GDS + LYP | **Local** |
| 31 | Via Area | GDS + LYP | **Local** |
| 32 | Via Array Analysis | GDS + LYP | **Local** — measured pitch |
| 33 | Via-to-Metal Connectivity | GDS + LYP | **Local** — overlap, not connection |
| 34 | Via Enclosure | GDS + LYP | **Local** |
| 35 | Contact Analysis | GDS + LYP | Model, from the landing measurements |
| 36 | Contact-to-Metal Connectivity | GDS + LYP | **Local** |
| 37 | Physical Connectivity | tiered | **Local** |
| 38 | Connected Component Analysis | GDS-only | **Local** — exact |
| 39 | **Physical Open Detection** | **needs netlist** | **Refused**, with the proxy offered |
| 40 | **Physical Short Detection** | **needs netlist** | **Refused**, with the reason |
| 41 | Disconnected Geometry | GDS-only | **Local** |
| 42 | Floating Geometry | needs a stack | **Local** |
| 43 | Layer-to-Layer Interaction | GDS + LYP | **Local** — measured adjacency |
| 44 | Connectivity Graph | needs a stack | **Local** — nodes, edges, nets |
| 45 | Density & Hotspot | GDS-only (whole-cell) | **Local** for density; windowed map **not implemented** |
| 46 | Geometry Anomaly Detection | GDS-only | **Not implemented** — refused by name |
| 47 | Layout Pattern Analysis | GDS-only | **Not implemented** — refused by name |
| 48 | Repeated Structure Analysis | GDS-only | **Not implemented** for raw geometry; array *pitch* is measured (#32) |
| 49 | Symmetry Analysis | GDS-only | **Not implemented** — refused by name |
| 50 | Cell Complexity | GDS-only | Model, from hierarchy + vertex counts |
| 51 | Layout Complexity | GDS-only | Model, from the same |
| 52 | Layer Utilization | GDS + LYP | **Local** |
| 53 | Instance Placement | GDS-only | **Local** |
| 54 | Layout Spatial Analysis | GDS-only | **Not implemented** — refused by name |
| 55 | Geometric Outlier Detection | GDS-only | **Not implemented** — refused by name |
| 56 | Via Distribution | GDS + LYP | Partial — pitch and spacing measured; no distribution statistics |
| 57 | Metal Fragmentation | GDS-only | **Local** — exact |
| 58 | Layout Health Scoring | — | **Deliberately not produced** |
| 59 | Physical Layout Risk Scoring | — | **Deliberately not produced** |

## Where this disagrees with the premise

Two topics on the list are **not** achievable from GDS + LYP, and the tool refuses
them rather than guessing:

* **#39 Physical Open** and **#40 Physical Short.** A short means two nets that
  should be separate are joined; an open means a net that should be continuous is
  broken. Both are defined against an *intended* netlist, and no netlist is
  supplied. What is reported instead is the observable proxy — a via or contact
  overlapping no conductor at all — which holds whatever the intent turns out to be.

Six more are honest gaps: **#46, #47, #49, #54, #55** are not implemented (the
geometry is available, so they are missing features, not missing inputs), and
**#58, #59** are refused on principle, since a single score implies a pass/fail
threshold and thresholds come from a rule deck.

Every one of those eight answers names what is missing and states plainly that
silence is not evidence of absence.
