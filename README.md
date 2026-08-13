# AI GDS Design Reviewer

A ready-to-run hackathon prototype that turns a GDSII layout into deterministic engineering metadata, a dashboard, a natural-language Q&A interface, and a two-GDS comparison view.

Every number shown comes from a deterministic parser. The language model only rephrases metadata it is given; it never counts, measures or infers a geometry fact.

## What it does

- Upload one or two `.gds` files.
- Parse GDSII with KLayout's Python API: top cell, cells, layers, polygon/shape/text counts, bounding box, merged-region area and layer density.
- Optionally fuse a technology-specific JSON sidecar, which supplies the semantics a raw GDSII stream does not carry: `layer_name`, `isVia`, `layerMap` and connectivity references.
- Answer the whole demo script deterministically, with **no** AI backend required.
- Fall back to a local LLM (Ollama) only for open-ended, audience-facing questions.
- Generate an AI design-review narrative.
- Compare two GDS files with layer-by-layer deltas, including layers added and removed.
- Analyse **physical connectivity** in three honestly-labelled tiers (see below), because a net graph needs information a `.gds` does not contain.

## Design rule checking needs the manual, which is not in this repository

Rule checking runs against the **GENCHIP Design Rule Manual**. That manual states it
may not be *"reproduced, transmitted, or translated, in any form or by any means"*
without written permission, and is subject to US export control law — so neither the
PDF nor the rules transcribed from it are committed here.

To enable it, put your own copy of the manual in `data/` and run:

```bash
pip install pypdf
python tools/extract_drm_rules.py
```

That regenerates `data/genchip_drm_rules.json` (71 rules across 14 sections) locally.
Both files are gitignored.

**Everything else works without it.** Geometry, per-layer measurement, connectivity,
the layout-versus-layout XOR comparison and the cell classification need only the
`.gds` and the `.lyp`. Without the manual, rule questions answer "no rules were
checked" and say why — they never guess.

See [CAPABILITIES.md](CAPABILITIES.md) for every feature labelled by the information it requires — GDS-only, LYP-only, GDS + LYP, GDS + sidecar, requires PDK/DRC rules, or requires netlist/design intent.

## Physical connectivity, and why it comes in tiers

**GDSII has no Z axis.** It records shapes on numbered layers; it does not record
which layer sits above which, nor which via bridges which two levels. A `.lyp`
carries colours and names, not elevations. So the vertical connection stack is
absent from both, and it gates every net-level result.

That is a measured conclusion, not a cautious assumption. Three ways to infer the
stack from geometry were tried and each fails: plan-view overlap collapses a cell
into one false net (in a stacked cell nearly everything overlaps); full enclosure
is too strict for a contact wider than its diffusion, yet a cell-spanning backside
power rail encloses vias it never connects to; and demoting "ubiquitous" layers
also demotes `M0`, which genuinely is a candidate for every connector.

| Tier | What it gives | Needs |
|---|---|---|
| 1 | Intra-layer connected components — shapes on one layer that touch are one physical conductor. Exact. | `.gds` only |
| 2 | Via/contact landing measurements: which conductors each via overlaps and which enclose it. Reported as *overlap*, never as *connection*. | `.gds` + `.lyp` |
| 3 | The net graph, floating conductors, per-net composition | a connection stack |

The stack can come from three places, and the tool always says which:

1. **A stack file** you supply (`--stack`, or the uploader). See
   `data/samples/Titan_stack.json`. Exact for that stack.
2. **The semantic sidecar**, whose via layers are named after their endpoints —
   `VIA_M0_M1`, `VIA_M0_PMOSGate`. That *states* the stack, so it is used
   automatically when a sidecar is present. A naming convention, not verified
   technology data, but not a geometric guess either.
3. **The bundled technology stack**, used when nothing else supplies one, so a
   lone `.gds` still gets a net graph. **Not PDK-verified**, and every result says
   so.
4. **An inference** from naming plus measured overlap, offered for review with a
   confidence and the unresolved alternatives. **Never applied automatically**, and
   every result built on it is labelled provisional.

### The layer map is a default, not an extra

`data/samples/Titan_layer_properties.lyp` is applied automatically unless you
upload your own. Without a layer map a raw GDS can only report `layer_300`, and
layer roles, via counts and every role aggregate are unavailable — so the common
case of uploading just a `.gds` would lose most of the analysis for no reason. An
uploaded `.lyp` always wins; the page states which one is in use.

Via counts follow from it: the map names the via layers (`VIA0`, `P-VIAG`, `DVB`),
so the count is derived from those names and cross-checks exactly against the
sidecar's explicit `isVia` flag on all four reference files (6, 9, 10, 10). A bare
`.gds` with the map disabled still reports `None`, never `0`.

Two things this never reports: a **short** or an **open**. Both are defined
relative to an intended netlist, and no netlist is supplied. What is reported
instead is the observable proxy — a via that overlaps no conductor at all.

A stack file may also correct the layer-role heuristic via a `roles` block. It has
to: reading a name ending in `CON` as a contact is right for most technologies and
wrong for this one, where `NDIFFCON` is the sidecar's `NMOSInterconnect` — a local
interconnect conductor. Treating it as a contact shorted a whole cell into one net.

## The three analysis modes

| Mode | Input | Geometry (area, density, cell size) | Semantics (layer names, vias) |
|---|---|---|---|
| `gds` | `.gds` (+ the default `.lyp`) | measured by KLayout, merged region | mask names, layer roles and via counts from the `.lyp`; with the map disabled, vias are **unavailable** |
| `sidecar` | `.json` only | derived from `xy`, unmerged polygon sum | from the sidecar |
| `fused` | `.gds` + `.json` | measured by KLayout, merged region | from the sidecar, plus `.lyp` mask names |

### Two naming vocabularies

A KLayout **`.lyp` layer-properties file** maps each `(layer, datatype)` to the
technology's own mask name, which is what lets a raw `.gds` report `BM0` instead of
`layer_300` with no sidecar at all. It is applied **by default** — see above — and
an uploaded one overrides the bundled map.

The sidecar and the `.lyp` are complementary, not competing, and the tool keeps
both rather than picking one:

| layer/datatype | `name` (sidecar: what it is for) | `technology_name` (.lyp: which mask) |
|---|---|---|
| 300/0 | `BSPowerRail` | `BM0` |
| 300/2 | `BSPowerRail` | `BM0-PIN` |
| 100/1 | `NmosNanoSheet` | `NDIFF-DUPLICATE` |
| 102/1 | `Diffusion_Break` *and* `NMOSGate` | `NPOLY-EXTENDED` |

Questions work in either vocabulary — "area of `BSPowerRail`" and "area of `BM0`"
both resolve. Auto-detected next to the GDS; `--layermap none` disables it.

Two things fall out of the reference `.lyp`. Its `-PIN` / `-DUPLICATE` suffixes
independently corroborate the datatype duplication the parser detects by unioning
regions. And `Diffusion_Break` mapping to `NPOLY-EXTENDED` + `PPOLY-EXTENDED` +
`DUMMY-GATE` confirms it really is three distinct mask layers, so summing their
areas (rather than unioning across them) is the physically correct treatment.

**Upload the `.gds` and its `.json` together.** That is the `fused` mode and the only one that answers every demo question. The app and CLI both select it automatically when both files are present.

A raw GDSII stream cannot tell you which geometry is a via, so with the layer map
disabled (`--layermap none`) the via count reads `n/a`, never `0` — a confident zero
would be a wrong answer, and this tool exists to be trusted about numbers. With a
layer map the via layers are named, so the count is derived from those names and
labelled with its source.

`fused` mode also cross-checks the two inputs and reports disagreement in a `consistency` block rather than silently preferring one. For the bundled reference files they agree exactly: 60 polygons, 70 shapes, 10 texts.

## Included reference files

- `data/samples/NR2D1_1_RT_4.gds` + `.json`
- `data/samples/NR2D1_2_RT_4.gds` + `.json`
- `data/samples/DCAP0_1_RT_4.gds` + `.json`, `DCAP0_2_RT_4.gds` + `.json`
- `data/samples/Titan_layer_properties.lyp` — technology layer map, **applied by default**
- `data/samples/Titan_stack.json` — connection stack, **applied by default** for the net graph (not PDK-verified)

Both report top structure `NR2D1`. Revision 2 adds an `M1` metal layer plus the `VIA_M0_M1` and `VIA_M0_PMOSInterconnect` vias that reach it, which makes them a good comparison demo.

## 1. Windows setup

Use Python 3.11 or 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Verify KLayout:

```powershell
python -c "import klayout.db as db; print('KLayout OK:', db.__version__)"
```

## 2. Configure the AI (optional)

The dashboard and every question in the demo script work with no model at all. A model is only needed for open-ended narrative ("explain this to a non-expert").

### Why accuracy does not depend on the model

Every number in this tool is computed by the deterministic analyzer — KLayout for geometry, the sidecar for semantics. The model is never asked to count, measure, total, or convert anything; it receives a finished fact table and rephrases it. The system prompt states this explicitly ("every number you state must appear verbatim in the metadata"), a `null` field is defined as *unavailable* rather than zero, and DRC questions are answered locally before the model is ever consulted.

So a weaker model produces a less fluent narrative, not a wrong number. That is what makes the local fallback safe.

### Backend chain

`LLM_PROVIDER=auto` (the default) uses **Anthropic** when `ANTHROPIC_API_KEY` is set and falls back to a **local Ollama** model if the API is unreachable, rate-limited, or declines the request. Set `LLM_PROVIDER=anthropic`, `ollama`, `openai`, or `none` to pin one backend.

| Backend | Model | Notes |
|---|---|---|
| Anthropic (primary) | `claude-sonnet-5` | Configured default: strong narrative at $3 / $15 per million input / output tokens (introductory $2 / $10 through 2026-08-31), roughly 0.8 cents per question. Set `ANTHROPIC_MODEL=claude-opus-5` for the top tier at $5 / $25. |
| Ollama (fallback) | `qwen3:4b` | Runs on your RX 5600M, no network, no per-token cost. |

### Anthropic setup

1. Create a key at [console.anthropic.com](https://console.anthropic.com) → **API keys**.
2. Put it in `.env` (never in a source file, and never paste it into a chat):
   ```text
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. `pip install -r requirements.txt` (installs the `anthropic` SDK).

The metadata is sent as a **cacheable system block** with the question in the user turn, so asking several questions about the same layout re-reads a cached prefix at ~10% of input cost instead of re-sending it. Effort defaults to `low` — the analyzer already did the thinking.

**Data note:** with the Anthropic (or OpenAI) backend, the layout metadata — including technology layer names such as `NmosNanoSheet` and `BSPowerRail` — leaves your machine. If that matters for a given design, run `LLM_PROVIDER=ollama` and nothing is transmitted.

### Install Ollama (fallback, or primary if you prefer local-only)

Download the Windows installer from <https://ollama.com/download>, then pull the recommended model:

```powershell
ollama pull qwen3:4b
```

### Why `qwen3:4b` on a Radeon RX 5600M

The RX 5600M has **6 GB of VRAM**, which is the binding constraint:

| Model | Q4_K_M weights | Fits 6 GB with an 8k context? |
|---|---|---|
| `qwen3:4b` | ~2.6 GB | **Yes, comfortably** — recommended |
| `gemma3:4b` | ~3.3 GB | Yes |
| `qwen3:8b` | ~5.2 GB | Weights fit, but the KV cache spills to CPU and it gets slow |
| `gpt-oss:20b` | ~14 GB | No |

A 4B model is sufficient here precisely because of the architecture: the deterministic analyzer computes every number, so the model's only job is to phrase facts it has been handed. Prompts are also digested and hard-capped so a large layout cannot overflow the context window.

If generation is slow, keep `OLLAMA_NUM_CTX=8192` and prefer `qwen3:4b` over an 8B model.

### Running the code in WSL2 while the model uses the GPU

This is the recommended setup on an AMD laptop, and it needs almost no configuration.

**Why the split:** WSL2 exposes only `/dev/dxg` (DirectX paravirtualisation). There is no `/dev/kfd` and no `/dev/dri`, which is what ROCm/HIP needs, so Ollama running *inside* WSL falls back to CPU and ignores the RX 5600M. Ollama running *on Windows* uses the GPU normally.

So: **Ollama server on Windows, codebase and Streamlit in WSL, connected over HTTP.**

**If `.wslconfig` has `networkingMode=mirrored`** (Windows 11 22H2 / build 22621+), WSL shares the Windows network namespace and `127.0.0.1` already reaches the Windows Ollama. Nothing to configure — no `OLLAMA_HOST`, no `0.0.0.0` binding, no firewall rule:

```bash
curl http://127.0.0.1:11434/api/version    # from inside WSL
```

To enable it, put this in `C:\Users\<you>\.wslconfig` and run `wsl --shutdown`:

```ini
[wsl2]
networkingMode=mirrored
```

**On default (NAT) networking**, `localhost` does not reach Windows. Then:

1. On Windows, let Ollama listen on all interfaces and restart it:
   ```powershell
   setx OLLAMA_HOST 0.0.0.0:11434
   ```
2. In WSL, point at the vEthernet gateway:
   ```bash
   export OLLAMA_HOST=http://$(ip route show default | awk '{print $3}'):11434
   ```
3. Allow inbound TCP 11434 through Windows Firewall for private networks.

The app tries `OLLAMA_HOST`, then `127.0.0.1`, then a WSL NAT gateway, and validates that whatever answers really is an Ollama server before sending it anything. The sidebar reports which backend was found, which models are installed, and the exact `ollama pull` command if the requested model is missing.

### Confirming the GPU is actually being used

```powershell
ollama run qwen3:4b "hello"
ollama ps
```

`ollama ps` prints a `PROCESSOR` column. `100% GPU` means the model is fully resident in VRAM; a `CPU/GPU` split means it spilled and will be slow — drop to a smaller model or lower `OLLAMA_NUM_CTX`.

This Ollama build ships both a ROCm/HIP backend (`lib/ollama/rocm/ggml-hip.dll`) and a Vulkan backend (`lib/ollama/vulkan/ggml-vulkan.dll`). ROCm on Windows does not officially cover Navi 10 (gfx1010, the RX 5600M), so if ROCm declines the card, Vulkan is the working path — the AMD Windows driver exposes full Vulkan on this GPU. Check the server log for the chosen backend, and if it fell back to CPU try:

```powershell
$env:OLLAMA_VULKAN=1 ; ollama serve
```

### Configure

Copy `.env.example` to `.env` and adjust. Defaults: `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=qwen3:4b`, host auto-detected.

To use a hosted model instead, set `LLM_PROVIDER=openai`, `OPENAI_API_KEY` and `OPENAI_MODEL`. To turn the AI off entirely, set `LLM_PROVIDER=none`.

Do not commit `.env`.

## 3. Run the tests

```powershell
python -m pytest
```

## 4. CLI analysis

The sidecars are found automatically next to each GDS file:

```powershell
python analyze.py data/samples/NR2D1_1_RT_4.gds data/samples/NR2D1_2_RT_4.gds
```

Outputs:

```text
reports/
  NR2D1_1_RT_4.metadata.json
  NR2D1_2_RT_4.metadata.json
  comparison.json
```

Options:

- `--sidecar-dir DIR` — look for `<stem>.json` in `DIR` instead of beside the GDS.
- `--mode {auto,gds,sidecar}` — `auto` fuses when both files exist (default); `gds` ignores sidecars; `sidecar` ignores GDS geometry.
- `--layermap FILE` — `.lyp` technology layer map; auto-detected, `none` disables.
- `--stack FILE` — connection stack for the net graph. Without it, a sidecar can
  supply one from its via layer names; failing that, tiers 1 and 2 still run.
- `--no-connectivity` — skip connectivity analysis.
- `--out DIR`, `--quiet`

With a stack, the per-file output adds a connectivity line:

```text
connectivity: 60 conducting shapes -> 54 within-layer conductors (exact, GDS-only)
              7 net(s) under the supplied stack, 2 floating
```

## 5. Run the dashboard

```powershell
streamlit run app.py
```

Open the local URL shown in the terminal.

## Architecture

```text
GDS ---------> KLayout parser -----> measured geometry
                                       (area, density, bbox, cells)
                                             |
                     (join on layer, datatype) + consistency check
                                             |
JSON sidecar -> sidecar parser -----> technology semantics
                                       (layer_name, isVia, layerMap)
                                             |
                                             v
                                   deterministic metadata
                                             |
        +--------------+--------------+--------------+
        v              v              v              v
    dashboard   deterministic     comparison    LLM narrative
                    Q&A            deltas      (rephrasing only)
```

The LLM is explicitly instructed never to calculate or invent GDS facts, and that a `null` field means *unavailable*, not zero.

## Important limitations

- A raw GDSII file carries no semantics of its own. A `.lyp` supplies mask names; via-ness and connectivity remain sidecar-only facts and are never guessed from layer names.
- A layer name spanning several layer numbers describes distinct mask layers, so its area is the **sum** of each layer's merged coverage. Where two names share one `(layer, datatype)`, the reported area is flagged as an upper bound (`area_is_exclusive_to_this_name: false`) because the inputs do not say which shape belongs to which name.
- `polygon_count` is flattened across instance placements (what is drawn); `polygon_record_count` is as-stored records. The sidecar cross-check compares records with records, so a hierarchical design is not mistaken for a mismatched pairing.
- Sidecar geometry is only measured when its coordinates parse completely. One malformed vertex makes that layer's area `null` rather than a plausible-looking wrong number.
- A multi-structure sidecar cannot give an overall bounding box, because placement offsets are not recorded per element; those fields are `null` with a warning.
- **AI observations are not DRC results.** The prototype never claims a DRC violation; no DRC engine is run.
- **A via overlapping two metals is not proof they are connected.** GDSII stores no layer elevations, so which levels a via bridges comes from the connection stack, never from the overlap. Tier-2 output is labelled as overlap throughout.
- **Shorts and opens are never reported.** Both are defined relative to an intended netlist. A via overlapping no conductor at all is reported instead, since that conclusion holds whatever the stack turns out to be.
- **A net count is only as good as its stack.** The source is always recorded (`stack_source`), and a layout collapsing into a single all-layer net is flagged as an over-connecting stack rather than reported as one net.
- Layer *roles* (metal / via / contact) are inferred from `.lyp` names and can be wrong: `NDIFFCON` reads as a contact but is local interconnect in these samples. A stack file's `roles` block corrects it.
- `sidecar` mode areas are an unmerged sum over polygons, so overlapping shapes on one layer are double counted. `technology.area_method` records which method produced the numbers.
- Where two sidecar layer names share one `(layer, datatype)` pair — `Diffusion_Break`/`NMOSGate` at `102/1` and `Diffusion_Break`/`PMOSGate` at `103/1` in these samples — a merged area cannot be attributed to one name. Those rows are flagged `sidecar_unmerged_subset` rather than being given the group's area.
- Comparing a `gds`-mode file against a `sidecar`- or `fused`-mode file is refused with a warning, because layer identity is not comparable across modes.

## Suggested hackathon demo

1. Upload `NR2D1_1_RT_4.gds`, `NR2D1_1_RT_4.json`, **and** `Titan_layer_properties.lyp`.
2. Show the dashboard: layer table, polygon chart, density chart.
3. Ask: `Give me a summary of this GDS.`
4. Ask: `How many polygons are on M0?`
5. Ask: `How many vias are present?`
6. Ask: `Which layer has the highest density?`
7. Add `NR2D1_2_RT_4.gds` + `NR2D1_2_RT_4.json`.
8. Show the comparison deltas and the layers-added table.
9. Ask: `What changed between the two layouts?` — answered deterministically, naming `M1` and `VIA_M0_M1`.
10. Ask: `Explain these changes to a non-layout engineer.` — this one goes to the model.
11. Generate the AI design review.

Steps 1-9 need no AI backend at all, which makes the demo safe to run offline.

### Connectivity demo

1. Upload `DCAP0_1_RT_4.gds` + its `.json` + `Titan_layer_properties.lyp`, and open
   the **Connectivity** tab. The sidecar supplies the stack, so the net graph is
   built: **4 nets** — two mirrored capacitor terminals plus two backside power
   taps, which is exactly what a decap should be.
2. Ask: `How many nets does this design have?` — answered deterministically, and it
   says where the stack came from.
3. Ask: `Are there any shorts?` — refused, with the reason. Same for opens, which
   offers the measurable proxy instead.
4. Remove the `.json` and keep only the `.gds` + `.lyp`. The net graph disappears
   and the inferred stack is shown for review, with per-connector confidence and
   the alternatives geometry cannot separate. Tiers 1 and 2 still report exact
   numbers.
5. Upload `NR2D1_1` and `NR2D1_2` together: revision 1 has two `PMOSInterconnect`
   stubs no via reaches; revision 2 adds `VIA_M0_PMOSInterconnect` and one stops
   floating.

The point of step 4 is the interesting one for judges: the tool declines to give a
net count it cannot justify, and explains precisely what is missing.

## Future extensions

- KLayout DRC execution and result ingestion.
- Technology/layer-map configuration files.
- Polygon-level click-through and layout image preview.
- Hierarchy graph and connectivity-aware questions.
- Export PDF/HTML review reports.
