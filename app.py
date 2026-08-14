from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from ai.deterministic import answer as deterministic_answer
from ai.compare import answer_pair
from ai.deterministic import answer_comparison, answer_xor, is_comparison_question
from ai.llm import ask_llm, generate_comparison, generate_review, provider_status
from analyzer.comparison import compare_metadata
from analyzer.classify import classify
from analyzer.drc import check_layout, load_rules
from analyzer.deck import load_deck, run as run_deck
from analyzer.density import density_map
from analyzer.diff import diff as structural_diff
from analyzer.edit import EditError, apply_to_bytes, describe as describe_edits
from analyzer.edit import grid_audit
from analyzer.lvs import compare as lvs_compare
from analyzer.netlist import default_recipe, extract as extract_netlist
from analyzer.stack3d import build_slabs, load_stack3d, mesh as mesh_slabs
from analyzer.edit import normalise as normalise_edits
from analyzer.plots import (change_hotspot, density_heatmap, density_profile,
                            difference_grid, difference_map, layout_view,
                            similarity_matrix, stack_3d)
from analyzer.present import findings, headline, nm, pct_of, split_primary, um2
from analyzer.xor_diff import compare_many
from analyzer.connectivity import (analyze_connectivity, default_stack, extract_nets,
                                   load_stack, stack_from_sidecar)
from analyzer.hierarchy import analyze_hierarchy, instance_tree
from analyzer.measurements import measure_layers, measure_vias, shape_outlines
from analyzer.parasitics import (compare_geometry, estimate_rc,
                                 load_process, wire_geometry)
from analyzer.pitch import analyze_pitch
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.sidecar_parser import analyze_sidecar
from analyzer.techparams import (compare_to_reference, find_reference,
                                load_reference, tech_parameters)
from ui.theme import (CSS, chips, hint, section, style_figure, swatch,
                      verdict_html)
from ui import tools as toolbench
from ui.viewer_data import editable_payload
from ui.workspace import (clear_focus, compare_panel, editor_panel,
                          focus_request, layout_panel, workspace)

# Explicit path: the no-argument form finds .env by inspecting the call stack,
# which raises when app.py is executed in an embedded or exec'd context.
load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="GDS Design Reviewer", page_icon="◧", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(CSS, unsafe_allow_html=True)

st.markdown(
    '<div class="title-row"><h1>◧ GDS Design Reviewer</h1>'
    '<span class="sub">layout-versus-layout comparison · deterministic geometry · '
    'measured, never guessed</span></div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("Analysis settings")
    use_sidecar = st.checkbox("Fuse semantic JSON sidecar when supplied", value=True)
    st.info("Numeric geometry is calculated by the parser. The AI only interprets the resulting metadata.")

    status = provider_status()
    if status["ready"]:
        st.success(f"AI enabled · {status['primary']}")
        if len(status["chain"]) > 1:
            st.caption("Fallback chain: " + " → ".join(status["chain"]))
    else:
        st.warning(f"AI narrative unavailable: {status['detail']}")
        st.caption("Deterministic Q&A works regardless — every number below is computed locally.")
    if status.get("models"):
        st.caption("Local models: " + ", ".join(status["models"]))
    st.caption("Numbers never come from the model. It only rephrases the metadata computed here.")

# The uploaders have to render to yield their values, so they cannot be skipped
# when the workspace is open - but they can be folded away, which is what keeps the
# expanded view at the top of the screen instead of below four file pickers.
_files_box = (st.expander("Files", expanded=False)
              if st.session_state.get("gv_focus") else st.container())
with _files_box:
    uploads = st.file_uploader("Upload GDS files — any number; every pair is compared",
                               type=["gds"], accept_multiple_files=True)
    sidecars = st.file_uploader("Optional semantic JSON sidecars", type=["json"],
                                accept_multiple_files=True)
    lyp_upload = st.file_uploader(
        "KLayout layer map (.lyp) — the bundled technology map is used unless you upload one",
        type=["lyp"], accept_multiple_files=False)
    stack_upload = st.file_uploader(
        "Optional connection stack (.json) — the one thing a .gds and .lyp cannot supply, "
        "needed for the net graph",
        type=["json"], accept_multiple_files=False, key="stack_upload")

if not uploads:
    st.markdown("### Try the included reference files")
    st.markdown("`NR2D1_1_RT_4.gds` and `NR2D1_2_RT_4.gds` are in `data/samples/`, each with its JSON sidecar.")
    st.markdown(
        "Upload the `.gds` **and** its `.json` together to get layer names, via counts and measured "
        "densities in one view."
    )
    st.stop()


# --- edited layouts ---------------------------------------------------------
# An edit produces a new file. It is held in session state under the upload's name,
# and every analysis below reads through `file_bytes`, so the moment an edit is
# written the design rule check, the connectivity and the classification are all
# re-run against what was actually written rather than against the original upload.
# The upload itself is never modified: it is the way back.
EDITED_KEY = "edited_files"


def edited_bytes(name: str) -> bytes | None:
    return (st.session_state.get(EDITED_KEY) or {}).get(name)


def file_bytes(upload) -> bytes:
    """The current contents of an upload: the edited version if there is one."""
    return edited_bytes(upload.name) or upload.getvalue()


def store_edit(name: str, data: bytes) -> None:
    st.session_state.setdefault(EDITED_KEY, {})[name] = data


def revert_edit(name: str) -> None:
    (st.session_state.get(EDITED_KEY) or {}).pop(name, None)


@st.cache_data(show_spinner=False)
def load_layermap(lyp_bytes: bytes | None, lyp_name: str | None):
    """Parse an uploaded .lyp, else fall back to the bundled technology map.

    The layer map is the default, not an optional extra. Without one a raw GDS can
    only say `layer_300`, and layer roles, via counts and every role aggregate are
    unavailable - so the common case of "user uploads just a .gds" would lose most
    of the analysis for no good reason.
    """
    if not lyp_bytes:
        bundled = default_layermap()
        if not bundled:
            return None, None
        try:
            return load_lyp(bundled), None
        except ValueError as exc:
            return None, f"the bundled layer map could not be read: {exc}"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / (lyp_name or "layers.lyp")
        path.write_bytes(lyp_bytes)
        try:
            return load_lyp(path), None
        except ValueError as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def load_connection_stack(stack_bytes: bytes | None, stack_name: str | None, layermap: dict | None):
    """Parse an uploaded connection stack. Needs the layer map to resolve names."""
    if not stack_bytes:
        return None, None
    if not layermap:
        return None, ("A connection stack needs a .lyp as well, so its layer names can be "
                      "resolved to layer numbers.")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / (stack_name or "stack.json")
        path.write_bytes(stack_bytes)
        try:
            return load_stack(path, layermap), None
        except (ValueError, KeyError) as exc:
            return None, str(exc)


def _pick_stack(layermap: dict | None, stack: dict | None,
                sidecar_metadata: dict | None = None):
    """Which connection stack to use, in precedence order.

    An uploaded stack wins. Failing that, a semantic sidecar can supply one: its
    via layers are named after their endpoints (`VIA_M0_M1`), which states the
    stack instead of leaving it to be guessed from geometry. Last comes the bundled
    technology stack. Shared so the net graph the page reports and the net shapes
    the viewer traces can never come from two different stacks.
    """
    if stack is not None:
        return stack
    if sidecar_metadata:
        derived = stack_from_sidecar(sidecar_metadata, layermap)
        if derived["usable_count"]:
            return derived
    return default_stack(layermap)


@st.cache_data(show_spinner=False)
def process_connectivity(gds_bytes: bytes, filename: str, layermap: dict | None,
                         stack: dict | None, sidecar_metadata: dict | None = None):
    """Physical connectivity. Tiers 1-2 always; the net graph only with a stack."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            # Each result is labelled with where its stack came from, so a net count
            # always says what it rests on.
            return analyze_connectivity(
                path, layermap,
                stack=_pick_stack(layermap, stack, sidecar_metadata))
        except Exception as exc:                     # never let this break the page
            return {"error": str(exc)}


@st.cache_data(show_spinner=False)
def process_net_shapes(gds_bytes: bytes, filename: str, layermap: dict | None,
                       stack: dict | None, sidecar_metadata: dict | None = None):
    """The same nets, carrying their polygons, for click-to-trace in the viewer.

    Kept separate from `process_connectivity` on purpose: those polygons are what
    the viewer highlights, and they have no business in the metadata the model is
    given - a digest with every polygon in it is a digest that gets truncated.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return {"nets": extract_nets(
                path, layermap, _pick_stack(layermap, stack, sidecar_metadata),
                collect_shapes=True)}
        except Exception:
            return None


@st.cache_data(show_spinner=False)
def process_classification(gds_bytes: bytes, filename: str, layermap: dict | None,
                           stack: dict | None, all_names: tuple[str, ...]):
    """Standard-cell classification: power delivery, technology, metals, height, tracks."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            outlines = shape_outlines(path, layermap, role_overrides=overrides)
            result = classify(outlines, path, list(all_names))
            result["pitch"] = analyze_pitch(outlines, filename)

            # Tech-file parameters: widths, spacings, extensions and the metal track
            # profiles, each measured to the definition in the design rule manual. A
            # stated tech file beside the layout is compared against, never
            # substituted for the measurement.
            params = tech_parameters(path, layermap)
            reference = find_reference(path)
            if reference:
                stated = load_reference(reference)
                params["reference"] = stated
                params["comparison"] = compare_to_reference(params, stated)
            result["tech_parameters"] = params
            return result, None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Checking design rules...")
def process_drc(gds_bytes: bytes, filename: str, layermap: dict | None, stack: dict | None):
    """Check the layout against the GENCHIP Design Rule Manual."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            return check_layout(shape_outlines(path, layermap, role_overrides=overrides)), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Reading layout geometry...")
def process_outlines(gds_bytes: bytes, filename: str, layermap: dict | None,
                     stack: dict | None, identity: bool = False):
    """Shape outlines for the layout view. Cached: one read per file.

    `identity` is what turns the view into something editable: it adds, per shape,
    the cell it lives in and its outline in that cell's own database units. Off for
    the read-only views, because it roughly doubles the payload.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            return shape_outlines(path, layermap, role_overrides=overrides,
                                  include_identity=identity), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def process_extras(gds_bytes: bytes, filename: str, layermap: dict | None, stack: dict | None):
    """Hierarchy and geometric measurements. Both are GDS-only at their core, so
    they run whether or not a .lyp or stack is present; the .lyp only adds the
    role aggregates (total metal area and the like)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        out = {}
        try:
            out["hierarchy"] = analyze_hierarchy(path)
        except Exception as exc:
            out["hierarchy"] = {"error": str(exc)}
        try:
            meas = measure_layers(path, layermap, overrides)
            meas["vias"] = measure_vias(meas)
            out["measurements"] = meas
        except Exception as exc:
            out["measurements"] = {"error": str(exc)}
        return out


@st.cache_data(show_spinner="Extracting the netlist...")
def process_netlist(gds_bytes: bytes, filename: str, layermap: dict | None,
                    stack: dict | None, recipe: dict | None = None):
    """Devices and nets. Needs the connection stack; says so when it is missing."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return extract_netlist(path, layermap, stack, recipe=recipe), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Running LVS...")
def process_lvs(gds_bytes: bytes, filename: str, layermap: dict | None,
                stack: dict | None, schematic_bytes: bytes, schematic_name: str,
                recipe: dict | None = None):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        schematic = Path(td) / (schematic_name or "schematic.cir")
        schematic.write_bytes(schematic_bytes)
        try:
            return lvs_compare(path, layermap, stack, schematic, recipe=recipe), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Running the rule deck...")
def process_deck(gds_bytes: bytes, filename: str, layermap: dict | None,
                 deck_bytes: bytes, deck_name: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        deck_path = Path(td) / (deck_name or "deck.json")
        deck_path.write_bytes(deck_bytes)
        try:
            deck = load_deck(deck_path)
        except Exception as exc:
            return None, None, f"the deck could not be read: {exc}"
        try:
            return run_deck(path, layermap, deck), deck, None
        except Exception as exc:
            return None, deck, str(exc)


@st.cache_data(show_spinner="Measuring density...")
def process_density(gds_bytes: bytes, filename: str, layermap: dict | None,
                    layers: tuple[str, ...], window_nm: float, combine: bool):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return density_map(path, layermap, list(layers), window_nm, combine), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Building the 2.5D view...")
def process_stack3d(gds_bytes: bytes, filename: str, layermap: dict | None,
                    stack_bytes: bytes, stack_name: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        stack_path = Path(td) / (stack_name or "stack3d.json")
        stack_path.write_bytes(stack_bytes)
        try:
            stack = load_stack3d(stack_path)
        except Exception as exc:
            return None, None, f"the stack file could not be read: {exc}"
        try:
            slabs = build_slabs(path, layermap, stack)
            return slabs, mesh_slabs(slabs), None
        except Exception as exc:
            return None, None, str(exc)


@st.cache_data(show_spinner="Comparing structure...")
def process_diff(a_bytes: bytes, a_name: str, b_bytes: bytes, b_name: str,
                 layermap: dict | None):
    with tempfile.TemporaryDirectory() as td:
        first = Path(td) / a_name
        second = Path(td) / f"b_{b_name}"
        first.write_bytes(a_bytes)
        second.write_bytes(b_bytes)
        try:
            result = structural_diff(first, second, layermap)
            result["a"], result["b"] = a_name, b_name
            return result, None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Measuring wire geometry...")
def process_parasitics(gds_bytes: bytes, filename: str, layermap: dict | None,
                       stack: dict | None):
    """The layout side of R and C: lengths, widths, areas, coupling runs, vias."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return wire_geometry(path, layermap,
                                 role_overrides=(stack or {}).get("role_overrides")), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def process_rc(geometry: dict | None, process_bytes: bytes | None, process_name: str):
    """Ohms and farads, but only with the constants a layout cannot supply."""
    if not geometry or not process_bytes:
        return None, None
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / (process_name or "process.json")
        path.write_bytes(process_bytes)
        try:
            return estimate_rc(geometry, load_process(path)), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def process_grid(gds_bytes: bytes, filename: str, grid_nm: float = 1.0):
    """Off-grid shapes, so "is B still on grid?" has an answer rather than a caveat."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return grid_audit(path, grid_nm)
        except Exception:
            return None


@st.cache_data(show_spinner=False)
def process_tree(gds_bytes: bytes, filename: str):
    """Cell placements with their boxes, for the viewer's cell tree.

    Separate from process_extras because it is only needed by the viewer and reads
    no layer map - the cell structure is in the GDSII file regardless of any .lyp.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return instance_tree(path)
        except Exception:
            return None


@st.cache_data(show_spinner=False)
def process_gds(gds_bytes: bytes, filename: str, sidecar_bytes: bytes | None, sidecar_name: str | None,
                use_sidecar: bool, layermap: dict | None = None):
    """Analyze one upload. Fuses GDS geometry with sidecar semantics when both exist.

    Degrades one step at a time so an unusable sidecar never costs us a
    perfectly readable GDS: fused -> sidecar-only -> GDS-only.
    """
    with tempfile.TemporaryDirectory() as td:
        gds_path = Path(td) / filename
        gds_path.write_bytes(gds_bytes)
        if use_sidecar and sidecar_bytes:
            side_path = Path(td) / (sidecar_name or (Path(filename).stem + ".json"))
            side_path.write_bytes(sidecar_bytes)
            try:
                return analyze_pair(gds_path, side_path, filename, layermap=layermap)
            except Exception as fuse_exc:
                try:
                    return analyze_sidecar(side_path, filename)
                except Exception:
                    metadata = analyze_gds(gds_path, layermap=layermap)
                    metadata.setdefault("warnings", []).insert(0, (
                        f"The sidecar '{side_path.name}' could not be read ({fuse_exc}). "
                        "Falling back to GDSII geometry only, so layer names and via counts are unavailable."
                    ))
                    return metadata
        return analyze_gds(gds_path, layermap=layermap)


# Streamlit returns a single object here (accept_multiple_files=False), but guard
# anyway so an unexpected shape degrades to 'no layer map' instead of a crash.
_lyp_file = lyp_upload if hasattr(lyp_upload, "getvalue") else None
layermap, lyp_error = load_layermap(
    _lyp_file.getvalue() if _lyp_file else None, _lyp_file.name if _lyp_file else None)
if lyp_error:
    st.warning(f"Layer map ignored: {lyp_error}")
elif layermap:
    origin = "uploaded" if _lyp_file else "bundled by default"
    st.info(f"Layer map `{layermap['file']}` ({origin}) — {layermap['entry_count']} technology "
            f"layer names, so layer roles, via counts and role aggregates are all available.")

_stack_file = stack_upload if hasattr(stack_upload, "getvalue") else None
conn_stack, stack_error = load_connection_stack(
    _stack_file.getvalue() if _stack_file else None,
    _stack_file.name if _stack_file else None, layermap)
if stack_error:
    st.warning(f"Connection stack ignored: {stack_error}")
elif conn_stack:
    st.info(f"Connection stack loaded — {conn_stack['usable_count']} via/contact rule(s), "
            f"{len(conn_stack['same_level'])} same-level pair(s). The net graph is built from this.")
    for problem in conn_stack["problems"]:
        st.warning(f"Connection stack: {problem}")

# Match sidecars by filename stem; if there is exactly one of each, pair them.
sidecar_map = {Path(s.name).stem: s for s in sidecars}
metadata_list = []
for upload in uploads:
    side = sidecar_map.get(Path(upload.name).stem)
    if side is None and len(sidecars) == 1 and len(uploads) == 1:
        side = sidecars[0]
    try:
        metadata_list.append(process_gds(
            file_bytes(upload), upload.name,
            side.getvalue() if side else None,
            side.name if side else None,
            use_sidecar,
            layermap,
        ))
    except Exception as exc:
        st.error(f"Failed to analyze {upload.name}: {exc}")

if not metadata_list:
    st.stop()

connectivity_list = [
    process_connectivity(file_bytes(upload), upload.name, layermap, conn_stack,
                         # A fused/sidecar metadata object carries the via layer
                         # names that state the stack; a gds-only one does not.
                         meta if meta.get("metadata_source") in ("fused", "sidecar") else None)
    for upload, meta in zip(uploads[:len(metadata_list)], metadata_list)
]
# Attach to the metadata so the deterministic Q&A and the AI digest see it.
# st.cache_data hands back a fresh copy on every call, so this does not write
# connectivity into the cached analyzer output.
for _upload, _meta, _conn in zip(uploads[:len(metadata_list)], metadata_list, connectivity_list):
    _meta["connectivity"] = _conn
    _extra = process_extras(file_bytes(_upload), _upload.name, layermap, conn_stack)
    _meta["hierarchy"] = _extra["hierarchy"]
    _meta["measurements"] = _extra["measurements"]

sources = {m.get("metadata_source") for m in metadata_list}
if len(metadata_list) == 2 and len(sources) > 1:
    st.warning(
        "The two files were analyzed differently "
        f"({', '.join(sorted(str(s) for s in sources))}). Layer names and via semantics are not "
        "comparable across modes — upload the JSON sidecar for both files to get a meaningful diff."
    )


def render_layout_view(outlines: dict, key: str, colours: dict, title: str = "",
                       **analysis) -> dict | None:
    """One layout in the interactive viewer.

    Replaces the Plotly figure with a canvas component. The change that matters is
    not the renderer: it is that the layer panel, the zoom and the ruler now live
    inside the component, so none of them re-runs the script. Every toggle used to
    rebuild the whole figure, which is why the view jumped and the toolbar - a
    hover-revealed Plotly modebar sitting outside the plot's own hover region -
    disappeared as soon as the pointer moved toward it.
    """
    return layout_panel(outlines, key=key, colours=colours, title=title, **analysis)

def render_connectivity(conn: dict | None, idx: int) -> None:
    """The connectivity tab. Extracted so the per-layout panel stays readable."""
    if not conn:
        st.info("No connectivity analysis for this file.")
        return
    if conn.get("error"):
        st.error(f"Connectivity analysis failed: {conn['error']}")
    else:
        for w in conn.get("warnings", []):
            st.warning(w)

        st.markdown("#### Intra-layer connectivity · **GDS-only, exact**")
        st.caption(conn["intra_layer"]["basis"] +
                   ". Needs no process-stack knowledge, so these numbers are exact.")
        t1 = conn["intra_layer"]
        c = st.columns(3)
        c[0].metric("Conducting shapes", t1["total_shapes"])
        c[1].metric("Physical conductors", t1["total_components"])
        c[2].metric("Layers with abutting shapes", t1["layers_with_abutting_shapes"])
        t1df = pd.DataFrame(t1["layers"])
        if not t1df.empty:
            st.dataframe(t1df[[col for col in ["name", "layer", "datatype", "role",
                                               "shape_count", "component_count",
                                               "largest_component_area_um2",
                                               "smallest_component_area_um2"] if col in t1df]],
                         width="stretch", hide_index=True, key=f"intra_df_{idx}")

        land = conn["landings"]
        st.markdown("#### Via / contact landings · **GDS + LYP, measured**")
        if not land.get("available"):
            st.info(f"Not available: {land.get('reason')}")
        else:
            st.caption("Measured plan-view overlap and enclosure. **Overlap is not connection** — "
                       "GDSII has no Z axis, so which layers a via actually bridges is a "
                       "process fact that neither the .gds nor the .lyp contains.")
            rows = [{"connector": cn["name"], "role": cn["role"], "shapes": cn["shape_count"],
                     "conductor": o["name"], "interacting": o["shapes_interacting"],
                     "enclosed": o["shapes_enclosed"],
                     "interaction_ratio": o["interaction_ratio"],
                     "enclosure_ratio": o["enclosure_ratio"]}
                    for cn in land["connectors"] for o in cn["overlaps"]]
            if rows:
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                             key=f"land_df_{idx}")

        prop = conn.get("proposed_stack")
        if prop:
            st.markdown("#### Inferred connection stack · **inferred, needs confirmation**")
            st.caption(prop["caveat"])
            pdf = pd.DataFrame([{
                "connector": p["connector_name"],
                "proposed": " + ".join(x["name"] for x in p["connects"]) or "(undetermined)",
                "confidence": p["confidence"],
                "unresolved alternatives": ", ".join(p["unresolved_alternatives"]) or "—",
                "basis": p["reason"]} for p in prop["proposals"]])
            if not pdf.empty:
                st.dataframe(pdf, width="stretch", hide_index=True, key=f"prop_df_{idx}")
            if prop["same_level"]:
                st.caption("Layers measured to be one conductor level under two names: " +
                           "; ".join(f"{' + '.join(s['names'])} ({s['confidence']})"
                                     for s in prop["same_level"]))

        nets = conn.get("nets")
        st.markdown("#### Net graph · **requires a connection stack**")
        if not nets:
            msg = ("No connection stack supplied, so the net graph was not built. Upload a "
                   "connection stack `.json` to compute it.")
            if prop and prop["usable_count"]:
                msg += (" The inferred stack above is shown for review and is deliberately not "
                        "applied automatically.")
            else:
                msg += (" A stack also needs the `.lyp`, so that via and metal layers can be "
                        "identified.")
            st.info(msg)
        elif not nets.get("available"):
            st.info(f"Not available: {nets.get('reason')}")
        else:
            st.caption(f"Stack source: {conn.get('stack_source')}. {nets['basis']}.")
            s = nets["summary"]
            c = st.columns(4)
            c[0].metric("Nets", s["net_count"])
            c[1].metric("Multi-layer nets", s["multi_layer_net_count"])
            c[2].metric("Single-layer nets", s["single_layer_net_count"])
            c[3].metric("Floating nets", s["floating_net_count"])
            ndf = pd.DataFrame([{
                "net": n["net"], "shapes": n["shape_count"], "layers": n["layer_count"],
                "area_um2": n["area_um2"],
                # Spelled out: a bool column renders as 0/1, which reads like a
                # count of something rather than yes/no.
                "spans layers": "yes" if n["spans_multiple_layers"] else "no",
                "layer names": ", ".join(n["layers"])} for n in nets["nets"]])
            if not ndf.empty:
                st.dataframe(ndf, width="stretch", hide_index=True, key=f"nets_df_{idx}")
            # Stack-vs-layout disagreements are already surfaced in the warning
            # block at the top of this tab; only the informational ones are new.
            skipped = [i for i in conn.get("stack_vs_evidence") or []
                       if i["severity"] == "info"]
            if skipped:
                st.caption("Stack entries this layout does not use: "
                           + ", ".join(i["connector"] for i in skipped) + ".")

        with st.expander("What connectivity analysis cannot determine from a .gds and .lyp"):
            for key, text in conn.get("limitations", {}).items():
                st.markdown(f"- **{key.replace('_', ' ')}** — {text}")


# --- comparison, first: it is the question a reviewer opens the tool to answer ---
comparison = None
if len(metadata_list) == 2:
    comparison = compare_metadata(metadata_list[0], metadata_list[1])


@st.cache_data(show_spinner="Running layer-by-layer XOR...")
def run_xor(names: tuple[str, ...], blobs: tuple[bytes, ...], layermap: dict | None,
            tolerance_um: float, stack: dict | None):
    """Pairwise XOR across every uploaded layout.

    This is the check a physical-design engineer actually runs on a revision: not
    "did the count change?" but "what geometry differs, how much of it, and where?"
    """
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, blob in zip(names, blobs):
            p = Path(td) / name
            p.write_bytes(blob)
            paths.append(p)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            return compare_many(paths, layermap, tolerance_um, overrides), None
        except Exception as exc:
            return None, str(exc)


_xor_for_chat = None
_cell_area = (metadata_list[0].get("layout") or {}).get("bbox_area_um2")
_bbox = (metadata_list[0].get("layout") or {}).get("bbox_dbu") or {}
_dbu = (metadata_list[0].get("source") or {}).get("dbu_um") or 1.0
_cell_bbox = ([_bbox["left"] * _dbu, _bbox["bottom"] * _dbu,
               _bbox["right"] * _dbu, _bbox["top"] * _dbu] if _bbox else None)
# The technology's own colour per layer, so the panel swatches and the map agree
# with what the engineer sees in KLayout.
_idx_of = {u.name: n for n, u in enumerate(uploads)}
_layer_colours = {e["technology_name"]: e.get("fill_color")
                  for e in (layermap or {}).get("by_key", {}).values()
                  if e.get("fill_color")}

def pair_context(name_a: str, name_b: str, xor_detail: dict | None) -> dict:
    """Everything the comparison chat can answer from, for one pair.

    Assembled from the cached analyses the page has already run - nothing here reads
    a file that has not been read. It exists so a question about the *pair* is never
    answered from one file\'s metadata, which is the one mistake a comparison chat
    can make that looks like an answer.
    """
    def side(name: str) -> dict:
        index = _idx_of.get(name, 0)
        upload = uploads[index]
        data = file_bytes(upload)
        metadata = dict(metadata_list[index])
        outlines, _ = process_outlines(data, name, layermap, conn_stack)
        classification, _ = process_classification(data, name, layermap, conn_stack,
                                                   tuple(u.name for u in uploads))
        drc, _ = process_drc(data, name, layermap, conn_stack)
        if outlines:
            metadata["outlines"] = outlines
        if classification:
            metadata["classification"] = classification
            metadata["pitch"] = classification.get("pitch")
        parasitics, _ = process_parasitics(data, name, layermap, conn_stack)
        rc, _ = process_rc(parasitics, st.session_state.get("process_file"),
                           st.session_state.get("process_name", "process.json"))
        return {
            "file": name,
            "metadata": metadata,
            "drc": drc,
            "connectivity": metadata.get("connectivity"),
            "netlist": process_netlist(data, name, layermap,
                                       conn_stack or default_stack(layermap))[0],
            "grid": process_grid(data, name, 1.0),
            "parasitics": parasitics,
            "rc": rc,
        }

    return {"xor": xor_detail or {}, "a": side(name_a), "b": side(name_b)}


def answer_for(question: str, metadata: dict, history: list | None = None,
               pair: dict | None = None) -> str:
    """Answer one question, deterministic first and the model only as a fallback.

    Shared by the page chat and the expanded workspace so both give the same answer
    to the same question - the alternative is two ladders that drift apart, and the
    user discovering that the big view is less accurate than the small one.
    """
    # A pair context answers about *both* files. It is tried first and for every
    # question, not only ones that look comparative: "did any pin move" names no
    # comparison word and is unanswerable from one file.
    if pair:
        reply = answer_pair(pair, question)
        if reply:
            return reply
    if is_comparison_question(question) and _xor_for_chat is not None:
        reply = answer_xor(_xor_for_chat, question)
        if reply:
            return reply
    if comparison is not None and is_comparison_question(question):
        reply = answer_comparison(comparison, question)
        if reply:
            return reply
    reply = deterministic_answer(metadata, question)
    if reply:
        return reply
    # What the model is given matters most when nothing deterministic matched. In a
    # comparison, handing it one file's metadata is how "what changed?" gets answered
    # from a single layout - so both sides go, and the XOR with them.
    if pair:
        context = {
            "comparing": {"a": pair["a"].get("file"), "b": pair["b"].get("file")},
            "difference": (pair.get("xor") or {}).get("summary"),
            "layers_that_differ": [
                {"layer": row["name"], "regions": row["xor"]["count"],
                 "xor_area_um2": row["xor"]["area_um2"]}
                for row in (pair.get("xor") or {}).get("layers", [])
                if not row.get("identical")],
            "a": pair["a"].get("metadata"),
            "b": pair["b"].get("metadata"),
        }
    elif comparison and is_comparison_question(question):
        context = {"comparison": comparison}
    else:
        context = metadata
    return ask_llm(context, question, history=history or [])



@st.cache_data(show_spinner=False)
def _xor_bytes(a_bytes: bytes, b_bytes: bytes, name: str, layermap: dict | None):
    """XOR two versions of one file. Cached on the bytes, so repeat views are free."""
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / f"before_{name}"
        b = Path(td) / f"after_{name}"
        a.write_bytes(a_bytes)
        b.write_bytes(b_bytes)
        from analyzer.xor_diff import xor_compare
        return xor_compare(a, b, layermap)


def _diff_against_upload(upload, edited: bytes) -> str | None:
    """One line describing how the edited file differs from the one uploaded."""
    try:
        result = _xor_bytes(upload.getvalue(), edited, upload.name, layermap)
    except Exception:
        return None
    summary = result.get("summary") or {}
    changed = summary.get("layers_changed")
    if not changed:
        # Merged geometry per layer, which is what gets manufactured: a shape drawn
        # inside another one on the same layer really is no difference.
        return "no difference in the merged geometry of any layer"
    return (f"{changed} layer(s) differ, "
            f"{um2(summary.get('total_xor_area_um2') or 0, 4)} of XOR area — "
            f"{um2(summary.get('total_area_removed_um2') or 0, 4)} removed, "
            f"{um2(summary.get('total_area_added_um2') or 0, 4)} added")


TOOL_TABS = ["Technology", "DRC", "LVS", "Netlist", "Parasitics", "2.5D view",
             "Density map", "Diff", "Browse shapes", "Browse instances"]
TOOL_BY_ID = {"technology": "Technology", "drc": "DRC", "lvs": "LVS",
              "netlist": "Netlist", "parasitics": "Parasitics",
              "stack3d": "2.5D view", "density": "Density map",
              "diff": "Diff", "xor": "Diff", "shapes": "Browse shapes",
              "instances": "Browse instances"}


def _handle_tool_event(event: dict | None, from_file: str | None = None) -> None:
    """Open the tool the viewer's menu asked for.

    The nonce guard matters here for the same reason it does for edits: a component
    keeps returning its last value, so without it every rerun would re-open the tool
    and the user could never leave it.
    """
    if not event or not isinstance(event, dict) or event.get("type") != "tool":
        return
    nonce = event.get("nonce")
    if nonce is not None and st.session_state.get("tool_last_event") == nonce:
        return
    st.session_state["tool_last_event"] = nonce
    wanted = TOOL_BY_ID.get(str(event.get("tool")))
    if wanted:
        # A separate key, not the radio's own. Streamlit refuses to let a widget's
        # key be written after that widget has been created in the same run, and the
        # viewer that raises this event is rendered below the selector.
        st.session_state["tool_request"] = wanted
        if from_file:
            st.session_state["tool_request_file"] = from_file
        st.rerun()


def _handle_edit_event(event: dict | None, upload, name: str) -> None:
    """Serve what the editor asked for: write the edits, or throw them away.

    The nonce is what makes this safe. A component keeps returning its last value on
    every rerun, so without it the same commit would be applied again on the next
    interaction - a two-nanometre move would walk across the layout.
    """
    if not event or not isinstance(event, dict):
        return
    nonce = event.get("nonce")
    if nonce is not None and st.session_state.get("ws_last_event") == nonce:
        return
    st.session_state["ws_last_event"] = nonce

    if event.get("type") == "discard":
        st.session_state["ws_revision"] = int(st.session_state.get("ws_revision", 0)) + 1
        st.rerun()

    if event.get("type") != "commit":
        return
    edits = normalise_edits(event.get("edits") or [])
    if not edits:
        return
    try:
        data, report = apply_to_bytes(file_bytes(upload), name, edits,
                                      layermap=layermap,
                                      grid_nm=event.get("gridNm"))
    except EditError as exc:
        # Atomic: nothing was written, so the layout on screen is still the file.
        st.session_state["ws_edit_error"] = str(exc)
        st.session_state["ws_revision"] = int(st.session_state.get("ws_revision", 0)) + 1
        st.rerun()
        return
    store_edit(name, data)
    st.session_state["ws_edit_error"] = None
    st.session_state["ws_edit_report"] = report
    # What the edit changed against the file that was uploaded, measured by the same
    # XOR the comparison page uses. This is the review an editor usually cannot give
    # you: KLayout will happily save a change without telling you what it was.
    st.session_state["ws_edit_diff"] = _diff_against_upload(upload, data)
    st.session_state["ws_revision"] = int(st.session_state.get("ws_revision", 0)) + 1
    st.rerun()


# --- expanded workspace ----------------------------------------------------
# Rendered before the page body and then the script stops, so the workspace is
# the whole screen rather than something appended below a page the user has to
# scroll past. It reuses the same panels as the inline views: one viewer, one
# answering path, so the big view cannot disagree with the small one.
_focus = focus_request()
if _focus and _focus.get("kind") == "compare":
    # Computed before the workspace renders, because the chat beside the drawings
    # needs it: a question about a difference answered from one file's metadata is
    # the exact failure this tool exists to avoid. run_xor is cached, so this is the
    # same result the comparison section below shows, not a second opinion.
    _ws_res, _ws_err = run_xor(
        tuple(u.name for u in uploads), tuple(file_bytes(u) for u in uploads),
        layermap, st.session_state.get("xor_tol", 0.0), conn_stack)
    if _ws_res:
        for _ws_pair in _ws_res["pairs"]:
            if _ws_pair["a"] == _focus.get("a") and _ws_pair["b"] == _focus.get("b"):
                _xor_for_chat = _ws_pair.get("detail") or {
                    "comparable": False, "reason": _ws_pair.get("reason", "unknown")}
                break

if _focus:
    def _render_focus() -> None:
        if _focus["kind"] == "compare":
            oa, ea = process_outlines(file_bytes(uploads[_idx_of[_focus["a"]]]),
                                      _focus["a"], layermap, conn_stack)
            ob, eb = process_outlines(file_bytes(uploads[_idx_of[_focus["b"]]]),
                                      _focus["b"], layermap, conn_stack)
            if ea or eb or not oa or not ob:
                st.error(f"Could not read the geometry: {ea or eb}")
                return
            # The XOR is computed here rather than read from the page below,
            # which has not run yet - the workspace deliberately renders before the
            # page body so it owns the screen. run_xor is cached, so this is the
            # same result the comparison section will show, not a second opinion.
            _res, _err = run_xor(
                tuple(u.name for u in uploads),
                tuple(file_bytes(u) for u in uploads),
                layermap, st.session_state.get("xor_tol", 0.0), conn_stack)
            xor_detail = {}
            if _res:
                for _pair in _res["pairs"]:
                    if _pair["a"] == _focus["a"] and _pair["b"] == _focus["b"]:
                        xor_detail = _pair.get("detail") or {}
                        break
            # Side by side here too, at the height the workspace gives it. The
            # chatbot takes the right-hand quarter, so the two viewers are narrower
            # than on the page - but they are still equal, which is the point.
            compare_panel(xor_detail, oa, ob, _layer_colours, _focus["a"], _focus["b"],
                          key="ws_cmp", height=700, expandable=False)
        else:
            name = _focus.get("title") or uploads[0].name
            idx = _idx_of.get(name, 0)
            editing = bool(st.session_state.get("ws_editing"))
            data = file_bytes(uploads[idx])
            outlines, error = process_outlines(data, name, layermap, conn_stack,
                                               identity=editing)
            if error or not outlines:
                st.error(f"Could not read the geometry: {error}")
                return
            # The expanded view gets exactly what the inline one gets. Dropping the
            # analysis here would leave the big view with empty Rules, Nets and
            # Cells tabs - the one place a reviewer is most likely to look for them.
            drc, _ = process_drc(data, name, layermap, conn_stack)
            cls, _ = process_classification(data, name, layermap, conn_stack,
                                            tuple(u.name for u in uploads))
            analysis = dict(
                drc=drc,
                connectivity=process_net_shapes(
                    data, name, layermap, conn_stack,
                    metadata_list[idx] if metadata_list[idx].get("metadata_source")
                    in ("fused", "sidecar") else None),
                pitch=(cls or {}).get("pitch"),
                hierarchy=metadata_list[idx].get("hierarchy"),
                tree=process_tree(data, name),
            )
            if not editing:
                layout_panel(outlines, key="ws_lv", colours=_layer_colours, title=name,
                             height=760, expandable=False, **analysis)
                return
            # Editing: the same viewer, mounted as a component so it can send the
            # journal back. Every check above was re-run on the *edited* file, so
            # the markers beside the drawing describe what is on screen.
            event = editor_panel(
                outlines, key="ws_edit", colours=_layer_colours, title=name,
                editable=editable_payload(outlines, layermap,
                                          (cls or {}).get("tech_parameters")),
                revision=int(st.session_state.get("ws_revision", 0)),
                height=760, **analysis)
            _handle_edit_event(event, uploads[idx], name)

    _focus_name = _focus.get("title") or _focus.get("a")
    _focus_meta = next((m for m in metadata_list
                        if m["source"]["file"] == _focus_name),
                       metadata_list[0])
    # The per-layout section attaches these as it renders, and it has not run: the
    # workspace stops the script before it. Without them the expanded chat answers
    # "no pitch metrics are available" for a file the page below reports a pitch
    # for, which reads as the two views disagreeing.
    _focus_cls, _ = process_classification(
        file_bytes(uploads[_idx_of.get(_focus_name, 0)]),
        _focus_meta["source"]["file"], layermap, conn_stack,
        tuple(u.name for u in uploads))
    if _focus_cls:
        _focus_meta["classification"] = _focus_cls
        _focus_meta["pitch"] = _focus_cls.get("pitch")
        if _focus_cls.get("tech_parameters"):
            _focus_meta["tech_parameters"] = _focus_cls["tech_parameters"]
    _focus_drc, _ = process_drc(file_bytes(uploads[_idx_of.get(_focus_name, 0)]),
                                _focus_meta["source"]["file"], layermap, conn_stack)
    if _focus_drc:
        _focus_meta["drc"] = _focus_drc
    def _edit_bar(slot) -> None:
        """Edit mode, and what comes with it: the file out, and the way back.

        The download is the whole point of an editor. A change you cannot take away
        with you is a drawing, not an edit - so the button is beside the toggle
        rather than somewhere further down the page.
        """
        editing = bool(st.session_state.get("ws_editing"))
        if slot.toggle("Edit layout", value=editing, key="ws_editing",
                       help="Draw, move, reshape and delete. Changes are written to "
                            "a new file with KLayout; the upload is never modified."):
            pass
        edited = edited_bytes(_focus_name)
        if edited:
            slot.download_button("Download edited .gds", edited,
                                 file_name=f"edited_{_focus_name}",
                                 mime="application/octet-stream",
                                 key="ws_download", width="stretch")
            if slot.button("Revert to the upload", key="ws_revert", width="stretch"):
                revert_edit(_focus_name)
                st.session_state["ws_revision"] = int(
                    st.session_state.get("ws_revision", 0)) + 1
                st.rerun()

    if _focus["kind"] == "layout":
        if st.session_state.get("ws_edit_error"):
            st.error(f"Nothing was written. {st.session_state['ws_edit_error']}")
        report = st.session_state.get("ws_edit_report")
        if report and report.get("applied"):
            st.success(f"{report['applied']} change(s) written to a new file — "
                       "every check on this page has been re-run on it.")
            for warning in report.get("warnings") or []:
                st.warning(warning)
            grid = report.get("off_grid") or {}
            if grid.get("added"):
                # Worth a warning of its own: an off-grid vertex looks right on
                # screen and reads right in a report, and the mask writer rounds it
                # somewhere else.
                st.warning(
                    f"{grid['added']} shape(s) this edit wrote have a vertex off the "
                    f"{grid['grid_nm']:g} nm grid: "
                    + ", ".join(f"{row['layer']} ({row['shapes']})"
                                for row in grid.get("layers") or []))
            diff = st.session_state.get("ws_edit_diff")
            if diff:
                st.caption(f"Against the upload: {diff}")

    _ws_pair_context = (pair_context(_focus["a"], _focus["b"], _xor_for_chat)
                        if _focus.get("kind") == "compare" else None)
    workspace(_focus, _render_focus,
              lambda q: answer_for(q, _focus_meta,
                                   history=st.session_state.get("ws_chat", [])[-6:],
                                   pair=_ws_pair_context),
              edit_bar=_edit_bar if _focus["kind"] == "layout" else None)
    st.stop()


# --- the tool bench ----------------------------------------------------------
# Opened from the viewer's "More tools" menu and from nowhere else. There is no
# permanent section for it: a row of tools sitting on the page whether or not
# anyone wants one is exactly the clutter the menu exists to remove.
#
# A tool that needs an input the layout cannot supply names the input and shows its
# format instead of falling back on a guess.
_requested = st.session_state.pop("tool_request", None)
if _requested in TOOL_TABS:
    st.session_state["tool_open"] = _requested
    _from_file = st.session_state.pop("tool_request_file", None)
    if _from_file:
        st.session_state["tool_file"] = _from_file



def tool_panel(for_file: str) -> None:
    """The open tool, if it was opened from this layout's viewer.

    Nothing renders when no tool is open, which is the point: the tools live in the
    viewer's menu, and the page shows one only while it is being used.
    """
    chosen = st.session_state.get("tool_open")
    if not chosen or (st.session_state.get("tool_file") or for_file) != for_file:
        return
    bar = st.columns([5, 1.1])
    bar[0].markdown(section(f"{chosen} — {for_file}"), unsafe_allow_html=True)
    if bar[1].button("✕ Close", key=f"tool_close_{for_file}", width="stretch",
                     help="Close this tool. Reopen it from More tools in the viewer."):
        st.session_state.pop("tool_open", None)
        st.rerun()
    render_tool(chosen, for_file)


def render_tool(chosen: str, tool_file: str) -> None:
    """Render one tool, under the viewer whose menu asked for it.

    A function rather than a section, because where it appears matters: the menu
    says a tool opens below the drawing, and a tool that opened three screens away
    would be a menu that lies.
    """
    tool_names = [u.name for u in uploads]
    if tool_file not in tool_names:
        tool_file = tool_names[0]
    tool_idx = _idx_of.get(tool_file, 0)
    tool_bytes = file_bytes(uploads[tool_idx])
    tool_meta = metadata_list[tool_idx]

    if chosen == "Technology":
        st.markdown("**What is loaded, and what each input unlocks.**")
        toolbench.technology_panel({
            "layermap": {"loaded": bool(layermap),
                         "source": (lyp_upload.name if lyp_upload else
                                    "Titan_layer_properties.lyp (bundled)")},
            "stack": {"loaded": bool(conn_stack),
                      "source": (stack_upload.name if stack_upload
                                 else (default_stack(layermap) or {}).get("source", "bundled"))},
            "recipe": {"loaded": bool(st.session_state.get("device_recipe")),
                       "source": st.session_state.get("device_recipe_name") or
                                 "proposed from the layer map"},
            "deck": {"loaded": bool(st.session_state.get("deck_file")),
                     "source": st.session_state.get("deck_name") or "—"},
            "schematic": {"loaded": bool(st.session_state.get("schematic_file")),
                          "source": st.session_state.get("schematic_name") or "—"},
            "stack3d": {"loaded": bool(st.session_state.get("stack3d_file")),
                        "source": st.session_state.get("stack3d_name") or "—"},
            "drm": {"loaded": bool(load_rules()),
                    "source": "data/genchip_drm_rules.json" if load_rules() else "not present"},
        })
        st.markdown("**The proposed device recipe** — what the netlist and LVS use unless "
                    "you replace it.")
        st.json(default_recipe(layermap, conn_stack or default_stack(layermap)))

    if chosen == "DRC":
        st.markdown("**The bundled catalogue** — the rules transcribed from the design "
                    "rule manual in this repository.")
        _bundled_drc, _bundled_error = process_drc(tool_bytes, tool_file, layermap, conn_stack)
        if _bundled_error:
            st.error(_bundled_error)
        elif _bundled_drc and _bundled_drc.get("available") is False:
            st.info(f"**Unavailable.** {_bundled_drc['reason']}")
        elif _bundled_drc:
            _s = _bundled_drc["summary"]
            _c = st.columns(4)
            _c[0].metric("Rules checked", _s["rules_checked"])
            _c[1].metric("Violations", _s["violation"])
            _c[2].metric("Passed", _s["pass"])
            _c[3].metric("Not checked", _s["not checked"])
            st.caption(f"The manual has {_s['rules_in_manual']} rules; "
                       f"{_s['rules_not_checked']} of them need information a .gds and "
                       ".lyp do not carry.")
        st.divider()
        st.markdown("**Your own deck** — any technology, run with the same engine.")
        _deck_upload = st.file_uploader("Design rule deck (.json)", type=["json"],
                                        key="deck_upload")
        if _deck_upload is not None:
            st.session_state["deck_file"] = _deck_upload.getvalue()
            st.session_state["deck_name"] = _deck_upload.name
        _deck_bytes = st.session_state.get("deck_file")
        _deck_result = _deck = None
        if _deck_bytes:
            _deck_result, _deck, _deck_error = process_deck(
                tool_bytes, tool_file, layermap, _deck_bytes,
                st.session_state.get("deck_name", "deck.json"))
            if _deck_error:
                st.error(_deck_error)
        toolbench.deck_panel(_deck_result, _deck, lambda: None)

    if chosen == "LVS":
        _schematic_upload = st.file_uploader(
            "Schematic netlist (SPICE / CDL)", type=["cir", "sp", "spice", "cdl", "net", "txt"],
            key="schematic_upload")
        if _schematic_upload is not None:
            st.session_state["schematic_file"] = _schematic_upload.getvalue()
            st.session_state["schematic_name"] = _schematic_upload.name
        _schematic_bytes = st.session_state.get("schematic_file")
        if not _schematic_bytes:
            toolbench.lvs_panel(None, lambda: None)
        else:
            _lvs, _lvs_error = process_lvs(
                tool_bytes, tool_file, layermap, conn_stack or default_stack(layermap),
                _schematic_bytes, st.session_state.get("schematic_name", "schematic.cir"),
                st.session_state.get("device_recipe"))
            if _lvs_error:
                st.error(f"LVS could not run: {_lvs_error}")
            else:
                toolbench.lvs_panel(_lvs, lambda: None)

    if chosen == "Netlist":
        _netlist, _netlist_error = process_netlist(
            tool_bytes, tool_file, layermap, conn_stack or default_stack(layermap),
            st.session_state.get("device_recipe"))
        if _netlist_error:
            st.error(f"The netlist could not be extracted: {_netlist_error}")
        else:
            toolbench.netlist_panel(_netlist, Path(tool_file).stem)

    if chosen == "Parasitics":
        st.markdown("**The layout side of R and C.** Lengths, widths, areas, coupling "
                    "runs and via counts are measured here; the constants that turn "
                    "them into ohms and farads are not in a GDSII.")
        geometry, geometry_error = process_parasitics(tool_bytes, tool_file, layermap,
                                                     conn_stack)
        if geometry_error:
            st.error(geometry_error)
        elif geometry:
            process_upload = st.file_uploader(
                "Process constants (.json) — sheet resistance and capacitance per layer",
                type=["json"], key="process_upload")
            if process_upload is not None:
                st.session_state["process_file"] = process_upload.getvalue()
                st.session_state["process_name"] = process_upload.name
            rc, rc_error = process_rc(geometry,
                                      st.session_state.get("process_file"),
                                      st.session_state.get("process_name", "process.json"))
            if rc_error:
                st.error(f"The process file could not be used: {rc_error}")
            toolbench.parasitics_panel(geometry, rc)

    if chosen == "2.5D view":
        _stack3d_upload = st.file_uploader("Layer stack for the 2.5D view (.json)",
                                           type=["json"], key="stack3d_upload")
        if _stack3d_upload is not None:
            st.session_state["stack3d_file"] = _stack3d_upload.getvalue()
            st.session_state["stack3d_name"] = _stack3d_upload.name
        _stack3d_bytes = st.session_state.get("stack3d_file")
        if not _stack3d_bytes:
            toolbench.stack3d_panel(None, None, lambda: None)
        else:
            _slabs, _meshes, _stack3d_error = process_stack3d(
                tool_bytes, tool_file, layermap, _stack3d_bytes,
                st.session_state.get("stack3d_name", "stack3d.json"))
            if _stack3d_error:
                st.error(_stack3d_error)
            else:
                toolbench.stack3d_panel(_slabs, stack_3d(_meshes or [],
                                                         (_slabs or {}).get("height_nm", 0)),
                                        lambda: None)

    if chosen == "Density map":
        _outlines_for_density, _ = process_outlines(tool_bytes, tool_file, layermap, conn_stack)
        _layer_names = [row["name"] for row in (_outlines_for_density or {}).get("layers", [])]
        _pick = st.multiselect("Layers", _layer_names,
                               default=[n for n in _layer_names if n in ("M0", "M1", "M2")][:3]
                               or _layer_names[:2], key="density_layers")
        _window = st.select_slider("Window", options=[25, 50, 100, 200, 500, 1000],
                                   value=100, format_func=lambda v: f"{v} nm",
                                   key="density_window")
        _combine = st.checkbox("Combine the chosen layers into one map", key="density_combine")
        if _pick:
            _density, _density_error = process_density(
                tool_bytes, tool_file, layermap, tuple(_pick), float(_window), _combine)
            if _density_error:
                st.error(_density_error)
            elif _density:
                _shown = list(_density["layers"])[0] if _density["layers"] else None
                _which = st.selectbox("Map", list(_density["layers"]), key="density_shown") \
                    if len(_density["layers"]) > 1 else _shown
                toolbench.density_panel(_density, density_heatmap(_density, _which)
                                        if _which else None)
        else:
            st.info("Choose at least one layer.")

    if chosen == "Diff":
        st.markdown("**Structural diff** — cells, shapes, instances and texts, compared "
                    "one for one. This is not the XOR.")
        # Uploading the same file twice gives two entries with one name, so the "other"
        # list can be empty even with two uploads. Selecting nothing then reads as a
        # crash rather than as "there is nothing to compare".
        _distinct = sorted(set(tool_names))
        if len(_distinct) < 2:
            st.info("Upload a second, different layout to compare against.")
        else:
            _dcols = st.columns(2)
            _da = _dcols[0].selectbox("A", _distinct, index=0, key="diff_a")
            _others = [n for n in _distinct if n != _da]
            _db = _dcols[1].selectbox("B", _others, index=0, key="diff_b")
            _diff, _diff_error = process_diff(file_bytes(uploads[_idx_of[_da]]), _da,
                                              file_bytes(uploads[_idx_of[_db]]), _db, layermap)
            if _diff_error:
                st.error(_diff_error)
            else:
                toolbench.diff_panel(_diff)

    if chosen == "Browse shapes":
        _outlines_for_browse, _browse_error = process_outlines(
            tool_bytes, tool_file, layermap, conn_stack)
        if _browse_error:
            st.error(_browse_error)
        else:
            toolbench.browse_shapes(_outlines_for_browse or {})

    if chosen == "Browse instances":
        toolbench.browse_instances(process_tree(tool_bytes, tool_file))



st.markdown(section("Per-layout detail"), unsafe_allow_html=True)

MODE_LABEL = {
    "gds": "GDSII geometry + layer map",
    "sidecar": "semantic sidecar only — areas are an unmerged polygon sum",
    "fused": "GDSII geometry fused with the semantic sidecar",
}

for idx, metadata in enumerate(metadata_list):
    d, layout = metadata["design"], metadata["layout"]
    conn = connectivity_list[idx]
    nets = (conn or {}).get("nets") or {}
    net_count = nets.get("summary", {}).get("net_count") if nets.get("available") else None

    with st.expander(
            f"**{metadata['source']['file']}** — {d['top_cell']}, "
            f"{d['polygon_count']} polygons, "
            f"{layout.get('width_um')} × {layout.get('height_um')} µm"
            + (f", {net_count} nets" if net_count is not None else ""),
            expanded=len(metadata_list) == 1):
        st.caption(MODE_LABEL.get(metadata.get("metadata_source", ""), ""))
        for w in metadata.get("warnings", []):
            st.warning(w)

        cls, cls_error = process_classification(
            file_bytes(uploads[idx]), uploads[idx].name, layermap, conn_stack,
            tuple(u.name for u in uploads))
        if cls:
            metadata["classification"] = cls
            metadata["pitch"] = cls.get("pitch")
            st.markdown(f'<div class="verdict" style="--vc:#58a6ff">'
                        f'<div class="vtitle"><span class="vicon">◧</span>'
                        f'<span>{cls["headline"]}</span></div>'
                        f'<div class="vdetail">'
                        f'{cls["technology"]["basis"]}; power delivery from the labels on the '
                        f'power layers.</div></div>', unsafe_allow_html=True)
            c = st.columns(5)
            c[0].metric("Power", (cls["power_delivery"]["power_delivery"] or "unknown").capitalize())
            c[1].metric("Technology", cls["technology"]["technology"])
            c[2].metric("Routing", {"SingleMetalSolution": "1 metal",
                                    "TwoMetalSolution": "2 metal",
                                    "ThreeMetalSolution": "3 metal"}.get(
                                        cls["metal_solution"]["metal_solution"], "unknown"))
            c[3].metric("Height", (cls["cell_height"].get("height") or "?").capitalize())
            c[4].metric("M0 tracks",
                        f"{cls['routing_tracks'].get('tracks_used', '?')}"
                        f"/{cls['routing_tracks'].get('tracks', '?')}")
            pitch = cls.get("pitch") or {}
            gp = (pitch.get("gate_pitch") or {}).get("cpp_nm")
            dims = pitch.get("cell_dimensions") or {}
            mp = pitch.get("metal_pitches") or {}
            if gp:
                st.markdown(section("Pitch metrics"), unsafe_allow_html=True)
                pc = st.columns(6)
                pc[0].metric("Gate pitch (CPP)", f"{gp:g} nm",
                             help="Also called CGP or the poly pitch — the poly-to-poly spacing.")
                pc[1].metric("Cell width", f"{dims.get('gate_pitches', '?')} CPP",
                             help=dims.get("width_basis", ""))
                for slot, metal in zip(pc[2:5], ("M0", "M1", "M2")):
                    entry = mp.get(metal) or {}
                    slot.metric(f"{metal} pitch",
                                f"{entry['pitch_nm']:g} nm" if entry.get("pitch_nm") else "n/a",
                                help=entry.get("note") or entry.get("source") or "")
                gear = (pitch.get("gear_ratio") or {}).get("gear_ratio")
                pc[5].metric("Gear ratio", f"{gear:g}" if gear else "n/a",
                             help="CPP / M1 pitch — the device grid against the routing grid.")
                if dims.get("rt_in_filename") is not None:
                    ok = dims.get("rt_matches_measured_tracks")
                    st.markdown(hint(
                        f"The filename declares RT {dims['rt_in_filename']} and "
                        f"{dims.get('signal_tracks')} M0 signal tracks were measured — "
                        + ("they agree." if ok else "<b>they disagree.</b>")),
                        unsafe_allow_html=True)

            # --- tech-file parameters ---------------------------------------
            params = cls.get("tech_parameters") or {}
            if params.get("parameters"):
                comparison = params.get("comparison") or {}
                st.markdown(section("Tech file parameters"), unsafe_allow_html=True)
                if comparison:
                    st.markdown(hint(comparison.get("headline", "")),
                                unsafe_allow_html=True)

                stated_lookup = {row["parameter"]: row for row in
                                 (comparison.get("agree") or [])
                                 + (comparison.get("disagree") or [])
                                 + (comparison.get("stated_only") or [])}
                rows = []
                for name, record in params["parameters"].items():
                    value = record.get("value")
                    if isinstance(value, list):
                        shown = ", ".join(f"{v:g}" for v in value)
                    elif isinstance(value, dict):
                        shown = ", ".join(f"{k} {v}" for k, v in value.items())
                    elif value is None:
                        shown = "—"
                    elif isinstance(value, bool):
                        shown = "yes" if value else "no"
                    elif isinstance(value, float):
                        shown = f"{value:g}"
                    else:
                        shown = str(value)
                    stated = stated_lookup.get(name) or {}
                    against = stated.get("stated")
                    # Agreement, disagreement and "stated but not measurable" are
                    # three different situations and must not read the same.
                    if against is None:
                        verdict = ""
                    elif not record.get("available"):
                        verdict = "tech file only"
                    elif any(r["parameter"] == name
                             for r in comparison.get("disagree") or []):
                        verdict = "DISAGREES"
                    else:
                        verdict = "matches"
                    rows.append({
                        "Parameter": name,
                        "Measured": shown,
                        "Unit": record.get("unit") or "",
                        "Rule": record.get("drm_rule") or "",
                        "vs tech file": verdict,
                    })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                st.markdown(hint(
                    "Every figure is measured from this layout to the definition in the "
                    "cited manual rule. A blank measurement is a parameter the geometry "
                    "cannot express — the reason is given below, and a tech-file figure "
                    "for it is that file's number, not a measurement of this cell."),
                    unsafe_allow_html=True)
                with st.expander("How each parameter was measured"):
                    for name, record in params["parameters"].items():
                        st.markdown(f"**{name}** (rule {record.get('drm_rule', '?')}) — "
                                    f"{record.get('basis', '')}")

            st.markdown(chips(
                ("power · from GDS labels", "exact"),
                ("technology · from diffusion geometry", "measured"),
                ("pitches · from the track-guide layers", "measured"),
                (f"tech parameters · {params.get('measured_count', 0)} measured",
                 "measured"),
                (f"orientation {cls['orientation'].get('orientation') or '?'} · "
                 f"{cls['orientation'].get('confidence')}", "inferred")),
                unsafe_allow_html=True)
            with st.expander("How each classification was reached"):
                for key in ("power_delivery", "technology", "metal_solution",
                            "routing_tracks", "cell_height", "half_dr", "orientation"):
                    block = cls[key]
                    st.markdown(f"**{key.replace('_', ' ')}** — {block.get('basis', '')}")
                    if block.get("not_derivable"):
                        st.markdown(f'<div class="hint">{block["not_derivable"]}</div>',
                                    unsafe_allow_html=True)
        elif cls_error:
            st.warning(f"Cell classification unavailable: {cls_error}")

        cols = st.columns(5)
        cols[0].metric("Cells", d["cell_count"])
        cols[1].metric("Polygons", d["polygon_count"])
        cols[2].metric("Vias", d["via_count"] if d.get("via_count") is not None else "n/a")
        cols[3].metric("Cell area", um2(layout.get("bbox_area_um2"), 4))
        cols[4].metric("Nets", net_count if net_count is not None else "n/a")
        if d.get("via_count") is None:
            st.caption("Vias read `n/a` because nothing here labels them — supply the `.lyp` "
                       "(its via layer names make the count derivable) or the JSON sidecar.")
        elif d.get("via_count_source"):
            st.caption(f"{d['via_count']} vias on {', '.join(d.get('via_layer_names') or [])}"
                       + (f"; {d['contact_count']} contacts counted separately"
                          if d.get("contact_count") else "")
                       + f". Derived from {d['via_count_source']}.")

        cons = metadata.get("consistency")
        if cons and not cons["agrees"]:
            st.warning(f"GDS and sidecar disagree: {cons['count_mismatches']}")

        meas = metadata.get("measurements") or {}
        agg = meas.get("role_aggregates") or {}
        if agg:
            st.markdown(section("By layer role"), unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "role": role,
                "layers": ", ".join(a["layers"]),
                "shapes": a["shape_count"],
                "area": um2(a["total_area_um2"], 4),
                "% of cell": pct_of(a["total_area_um2"], layout.get("bbox_area_um2")),
                "min width": nm(a.get("observed_min_width_um")),
                "min space": nm(a.get("observed_min_space_um")),
            } for role, a in sorted(agg.items())]), width="stretch", hide_index=True,
                key=f"roles_{idx}")
            st.markdown(
                chips(("geometry · exact", "exact"),
                      ("min width / spacing · measured, not a rule check", "measured"),
                      ("which layers are metal · from the .lyp", "GDS + LYP")),
                unsafe_allow_html=True)

        tabs = st.tabs(["Layout", "Design rules", "Layers", "Connectivity", "Cells",
                        "AI review"])

        # Fetched before the tabs because the viewer in the Layout tab turns rule
        # results into clickable markers and the cell tree into a navigator. All
        # cached calls.
        drc_for_view, _ = process_drc(file_bytes(uploads[idx]), uploads[idx].name,
                                      layermap, conn_stack)
        hierarchy_for_view = metadata.get("hierarchy")
        tree_for_view = process_tree(file_bytes(uploads[idx]), uploads[idx].name)
        # Net polygons, so a click in the viewer can highlight a whole net. The
        # page's own connectivity block deliberately carries no polygons.
        nets_for_view = process_net_shapes(
            file_bytes(uploads[idx]), uploads[idx].name, layermap, conn_stack,
            metadata if metadata.get("metadata_source") in ("fused", "sidecar") else None)

        with tabs[0]:
            outlines, outline_error = process_outlines(
                file_bytes(uploads[idx]), uploads[idx].name, layermap, conn_stack)
            if outline_error:
                st.error(f"Could not read the geometry for drawing: {outline_error}")
            elif outlines:
                _event = render_layout_view(
                    outlines, f"lv{idx}", _layer_colours,
                    title=uploads[idx].name,
                    drc=drc_for_view, connectivity=nets_for_view,
                    pitch=(cls or {}).get("pitch"),
                    hierarchy=hierarchy_for_view,
                    tree=tree_for_view,
                    interactive=True)
                _handle_tool_event(_event, uploads[idx].name)
                tool_panel(uploads[idx].name)

        with tabs[1]:
            drc, drc_error = process_drc(file_bytes(uploads[idx]), uploads[idx].name,
                                        layermap, conn_stack)
            if drc_error:
                st.error(f"Design rule check failed: {drc_error}")
            elif drc and drc.get("available") is False:
                # The catalogue is transcribed from a manual that may not be
                # redistributed, so a fresh clone will not have it.
                metadata["drc"] = drc
                st.info(f"**Design rule checking is unavailable.** {drc['reason']}")
                st.markdown(hint(
                    "Place the manual in <code>data/</code> and run "
                    "<code>python tools/extract_drm_rules.py</code> to enable it. Everything "
                    "else — geometry, connectivity, XOR comparison and classification — is "
                    "unaffected."), unsafe_allow_html=True)
            elif drc:
                metadata["drc"] = drc
                s_ = drc["summary"]
                tech = drc["technology"]
                state = "identical" if not s_["violation"] else "base-layers"
                head = (f"No violations of the {s_['rules_checked']} rules checked"
                        if not s_["violation"] else
                        f"{s_['violation']} violation(s) of the checked rules")
                st.markdown(verdict_html(
                    state, head,
                    f"Checked against the {drc['source']}. Technology read as "
                    f"<b>{tech['used']}</b> — {tech['basis']}."), unsafe_allow_html=True)

                cols = st.columns(4)
                cols[0].metric("Pass", s_["pass"])
                cols[1].metric("Violations", s_["violation"])
                cols[2].metric("Not evaluable", s_["not checked"])
                cols[3].metric("Rules checked", f"{s_['rules_checked']}/{s_['rules_in_manual']}")
                st.markdown(chips(
                    ("relational rules · checkable from geometry", "exact"),
                    ("technology · inferred", "inferred"),
                    (f"{s_['rules_not_checked']} rules not checked", "unavailable"),
                    ("LVS / ERC · requires a netlist", "requires netlist")),
                    unsafe_allow_html=True)

                if drc["violations"]:
                    st.markdown(section("Violations"), unsafe_allow_html=True)
                    for v in drc["violations"]:
                        st.markdown(
                            f"**{v['id']}** — {v['detail']}<br>"
                            f'<span class="hint">Manual {v["section"]}: “{v["rule"]}”</span>',
                            unsafe_allow_html=True)

                st.markdown(section("All checked rules"), unsafe_allow_html=True)
                st.dataframe(pd.DataFrame([{
                    "rule": r["id"], "status": r["status"], "finding": r["detail"],
                    "manual wording": r["rule"],
                } for r in drc["results"]]), width="stretch", hide_index=True,
                    key=f"drc_{idx}")
                st.caption(drc["caveat"])
                with st.expander(f"The {len(drc['rules_not_checked'])} rules not checked"):
                    st.dataframe(pd.DataFrame(drc["rules_not_checked"]), width="stretch",
                                 hide_index=True, key=f"drc_missing_{idx}")
                with st.expander("What a rule check still cannot tell you"):
                    for k, v in drc["not_derivable"].items():
                        st.markdown(f"- **{k.replace('_', ' ')}** — {v}")

        with tabs[2]:
            rows = metadata.get("layers", [])
            primary, derived = split_primary(rows, layermap)
            show_all = st.checkbox(
                f"Include the {len(derived)} pin / label / duplicate copies",
                key=f"showall_{idx}",
                help="These repeat another layer's geometry, so they double every total.")
            shown = rows if show_all else primary
            mrows = {(r["layer"], r["datatype"]): r for r in (meas.get("layers") or [])}
            table = []
            for r in shown:
                mr = mrows.get((r["layer"], r["datatype"]), {})
                table.append({
                    "layer": r["name"], "l/d": f"{r['layer']}/{r['datatype']}",
                    "role": mr.get("role", ""), "polygons": r["polygon_count"],
                    "vias": r.get("via_count"), "labels": r["text_count"],
                    "area": um2(r["area_um2"], 4),
                    "% of cell": (f"{r['density_percent']:.2f}%"
                                  if r.get("density_percent") is not None else "n/a"),
                    "min width": nm(mr.get("observed_min_width_um")),
                    "min space": nm(mr.get("observed_min_space_um")),
                })
            st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True,
                         key=f"layers_{idx}")
            prof = style_figure(density_profile(shown))
            if prof is not None:
                st.plotly_chart(prof, width="stretch", key=f"dens_{idx}")

        with tabs[3]:
            render_connectivity(conn, idx)

        with tabs[4]:
            hier = metadata.get("hierarchy") or {}
            if hier and not hier.get("error"):
                st.caption(f"{hier['depth_description']}. {hier['cell_count_total']} cell(s) in "
                           f"the file, {hier['cell_count_in_scope']} reachable from "
                           f"`{hier['top_cell']}`.")
                for w in hier.get("warnings", []):
                    st.warning(w)
            st.dataframe(pd.DataFrame(metadata.get("cells", [])), width="stretch",
                         hide_index=True, key=f"cells_{idx}")

        with tabs[5]:
            review_key = f"review_text_{idx}"
            if st.button("Generate AI design review", key=f"review_{idx}"):
                with st.spinner("Reviewing metadata..."):
                    st.session_state[review_key] = generate_review(metadata)
            if st.session_state.get(review_key):
                st.markdown(st.session_state[review_key])


# --- chat ---------------------------------------------------------------------
st.divider()
st.header("💬 Ask the Layout")

if len(metadata_list) == 2:
    st.caption(
        f"Questions answer against **{metadata_list[0]['source']['file']}**. "
        "Comparison questions (\"what changed?\") use both files."
    )



if len(uploads) >= 2:
    xor_result, xor_error = run_xor(
        tuple(u.name for u in uploads), tuple(file_bytes(u) for u in uploads),
        layermap, st.session_state.get("xor_tol", 0.0), conn_stack)

    if xor_error:
        st.error(f"XOR comparison failed: {xor_error}")
    elif xor_result:
        names = xor_result["files"]
        labels = [f"{p['a']} → {p['b']}" for p in xor_result["pairs"]]
        # With more than two uploads this is the choice of *which* two to compare.
        # It was label-less, which made it look like a filter rather than the
        # control that decides what the whole section below is about.
        pick = st.selectbox(
            "Which two layouts to compare", labels, key="xor_pair",
            help="Every pair of uploaded layouts is available. The first file is "
                 "the one differences are measured from: red is only in it, green "
                 "only in the second.") if len(labels) > 1 else labels[0]
        pair = xor_result["pairs"][labels.index(pick)]
        detail = pair.get("detail")
        _xor_for_chat = detail or {"comparable": False,
                                   "reason": pair.get("reason", "unknown")}

        # 1. The verdict. One line, before anything else.
        head = headline(detail, _cell_area)
        st.markdown(verdict_html(head["state"], head["headline"], head.get("detail", "")),
                    unsafe_allow_html=True)

        # 2. The two layouts, side by side, whether or not they differ. Two
        # identical files still have to be shown: "they are the same" is easier to
        # believe with both drawings in front of you than with an empty section.
        oa, ea = process_outlines(file_bytes(uploads[_idx_of[pair["a"]]]),
                                  pair["a"], layermap, conn_stack)
        ob, eb = process_outlines(file_bytes(uploads[_idx_of[pair["b"]]]),
                                  pair["b"], layermap, conn_stack)
        if ea or eb or not oa or not ob:
            st.error(f"Could not read the geometry for drawing: {ea or eb}")
        elif detail and detail.get("comparable"):
            compare_panel(detail, oa, ob, _layer_colours,
                          pair["a"], pair["b"], key="cmp")

        if detail and detail.get("comparable") and not detail["summary"]["identical"]:
            s = detail["summary"]
            # 3. Four numbers, not fourteen.
            c = st.columns(4)
            c[0].metric("Layers changed", f"{s['layers_changed']}/{s['layers_compared']}")
            c[1].metric("Regions", s["difference_regions"])
            c[2].metric("XOR area", um2(s["total_xor_area_um2"]),
                        help="Total geometry present in one layout and not the other")
            c[3].metric("Removed → Added",
                        f"{um2(s['total_area_removed_um2'], 3)} → {um2(s['total_area_added_um2'], 3)}")

            changed = sorted((x for x in detail["layers"] if not x["identical"]),
                             key=lambda r: -r["xor"]["area_um2"])
            st.markdown(hint(
                "<b>A</b> and <b>B</b> are side by side above, each with its own zoom, "
                "pan and layers. In the overlay below them, <b>A+B</b> draws both, "
                "<b>XOR</b> leaves only the differences, and the wipe and blink tools "
                "compare them in place. Red is only in the first file, green only in "
                "the second."), unsafe_allow_html=True)

            # 4. What to look at first, largest difference first.
            st.markdown(section("Largest differences — biggest first"),
                        unsafe_allow_html=True)
            top = findings(detail, _cell_area, limit=6)
            st.dataframe(pd.DataFrame([{
                "layer": f["layer"], "change": f["change"], "size": f["size"],
                "area": um2(f["area_um2"], 3), "of cell": f["share_of_cell"],
                "at (µm)": f"{f['at_um'][0]}, {f['at_um'][1]}"} for f in top]),
                width="stretch", hide_index=True, key="findings")
            st.markdown(
                chips(("XOR geometry · exact", "exact"),
                      ("layer roles · inferred from names", "inferred"),
                      ("intent · requires netlist", "requires netlist"),
                      ("rule compliance · requires PDK", "requires PDK")),
                unsafe_allow_html=True)

            # Everything else is available, but out of the way.
            with st.expander(f"All {s['layers_changed']} changed layers, and settings"):
                st.select_slider(
                    "Edge-shift tolerance — differences at or below this are binned separately",
                    options=[0.0, 0.005, 0.01, 0.02, 0.05], key="xor_tol",
                    format_func=lambda v: "off" if not v else nm(v))
                changed = [r for r in detail["layers"] if not r["identical"]]
                st.dataframe(pd.DataFrame([{
                    "layer": r["name"], "role": r["role"],
                    "regions": r["xor"]["count"],
                    "xor area": um2(r["xor"]["area_um2"], 3),
                    "largest": um2(r["xor"]["largest_area_um2"], 3),
                    "removed": r["removed"]["count"], "added": r["added"]["count"],
                    "Δ area": um2(r["area_delta_um2"], 3),
                    "edge shifts": r.get("at_or_below_tolerance_count"),
                    "only in": ("B" if not r["present_in_a"] else
                                "A" if not r["present_in_b"] else ""),
                } for r in sorted(changed, key=lambda r: -r["xor"]["area_um2"])]),
                    width="stretch", hide_index=True, key="xor_changed")

                text_rows = [r for r in detail["layers"]
                             if r.get("texts_added") or r.get("texts_removed")]
                if text_rows:
                    st.markdown("**Label changes** — no geometry impact, but LVS sees them")
                    st.dataframe(pd.DataFrame([{
                        "layer": r["name"],
                        "added": ", ".join(r.get("texts_added") or []) or "—",
                        "removed": ", ".join(r.get("texts_removed") or []) or "—",
                    } for r in text_rows]), width="stretch", hide_index=True, key="xor_text")

                st.caption(detail["mask_impact"]["caveat"])
                st.markdown("**Not derivable from an XOR**")
                for k, v in detail["not_derivable"].items():
                    st.markdown(f"- *{k.replace('_', ' ')}* — {v}")

        # Three or more layouts is a revision family, not a pair. The question
        # becomes "which of these differs from the golden one, and where?", so the
        # views are reference-based rather than pairwise.
        if len(names) > 2:
            st.divider()
            st.markdown(section(f"All {len(names)} layouts — reference comparison"),
                        unsafe_allow_html=True)
            reference = st.selectbox(
                "Reference layout — everything is compared back to this",
                names, key="xor_reference",
                help="A revision family is reviewed against one golden database.")

            c = st.columns(3)
            ref_pairs = [p for p in xor_result["pairs"]
                         if reference in (p["a"], p["b"]) and p.get("comparable")]
            same = [p for p in ref_pairs if p["identical"]]
            worst = max(ref_pairs, key=lambda p: p["total_xor_area_um2"], default=None)
            c[0].metric("Match the reference", f"{len(same)} of {len(ref_pairs)}")
            if worst:
                other = worst["b"] if worst["a"] == reference else worst["a"]
                c[1].metric("Furthest from reference", other)
                c[2].metric("Its XOR area", um2(worst["total_xor_area_um2"]))
            if same:
                st.success("Identical to the reference: " + ", ".join(
                    (p["b"] if p["a"] == reference else p["a"]) for p in same))

            grid = style_figure(difference_grid(xor_result, reference, _cell_bbox))
            if grid is not None:
                st.plotly_chart(grid, width="stretch", key="diffgrid")
                st.caption(f"Each panel is one layout against **{reference}**, on shared axes and "
                           f"to scale. Red = missing relative to the reference, green = extra. "
                           f"A region that lights up in every panel is where all the churn is.")

            hot = style_figure(change_hotspot(xor_result, _cell_bbox, reference=reference))
            if hot is not None:
                st.plotly_chart(hot, width="stretch", key="hotspot")
                st.caption("Difference area accumulated over a grid of the cell, so a "
                           "repeatedly-edited region stands out however the revisions are paired.")

            with st.expander("Pairwise matrix — every combination"):
                heat = style_figure(similarity_matrix(xor_result))
                if heat is not None:
                    st.plotly_chart(heat, width="stretch", key="simmatrix")
                c = st.columns(2)
                if xor_result["most_similar_pair"]:
                    c[0].metric("Closest pair", " ↔ ".join(xor_result["most_similar_pair"]))
                if xor_result["most_different_pair"]:
                    c[1].metric("Furthest pair", " ↔ ".join(xor_result["most_different_pair"]))
                if xor_result["identical_pairs"]:
                    st.success("Identical: " + "; ".join(
                        " ↔ ".join(p) for p in xor_result["identical_pairs"]))

# --- per-file detail, collapsed: needed sometimes, not first ------------------
st.divider()
if "chat" not in st.session_state:
    st.session_state.chat = []

for msg in st.session_state.chat:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

question = st.chat_input("Ask a question about the uploaded GDS")
if question:
    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    metadata = metadata_list[0]
    # With two layouts open, the page chat answers about the pair as well - the
    # comparison workspace should not be the only place that can say what changed.
    _page_pair = (pair_context(uploads[0].name, uploads[1].name, _xor_for_chat)
                  if len(uploads) == 2 else None)
    with st.chat_message("assistant"):
        reply = answer_for(question, metadata, history=st.session_state.chat[:-1][-6:],
                           pair=_page_pair)
        st.markdown(reply)
    st.session_state.chat.append({"role": "assistant", "content": reply})

if st.session_state.chat and st.button("Clear conversation"):
    st.session_state.chat = []
    st.rerun()

st.caption("Suggested questions: " + " · ".join([
    "Give me a summary of this GDS.",
    "How many polygons are there?",
    "Which layers are used?",
    "How many vias are present?",
    "What is the largest cell?",
    "Which layer has the highest density?",
    "What changed between the two layouts?",
]))

