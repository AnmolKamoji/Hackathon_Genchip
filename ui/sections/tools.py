"""The tool bench, the expanded workspace and the editor.

These are the parts of the review surface that act on a file rather than describe
one: run LVS against a schematic, run your own rule deck, browse the netlist, open a
layout full screen with the chatbot beside it, draw on it and write a new GDSII.

They live here rather than in `app.py` for the same reason the six sections do - the
page is a conductor, not an implementation - and they are kept together because they
share one idea: a tool is opened from the viewer's own "More tools" menu and renders
under the viewer that asked for it.

Every analysis is cached on the file's bytes, so opening a tool costs one pass and
reopening it costs nothing. Nothing here measures anything itself.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from analyzer.classify import classify
from analyzer.connectivity import default_stack, extract_nets
from analyzer.deck import load_deck, run as run_deck
from analyzer.density import density_map
from analyzer.diff import diff as structural_diff
from analyzer.drc import check_layout
from analyzer.edit import EditError, apply_to_bytes, grid_audit
from analyzer.edit import normalise as normalise_edits
from analyzer.hierarchy import analyze_hierarchy, instance_tree
from analyzer.lvs import compare as lvs_compare
from analyzer.measurements import shape_outlines
from analyzer.netlist import default_recipe, extract as extract_netlist
from analyzer.parasitics import estimate_rc, load_process, wire_geometry
from analyzer.pitch import analyze_pitch
from analyzer.plots import density_heatmap, stack_3d
from analyzer.stack3d import build_slabs, load_stack3d, mesh as mesh_slabs
from ui import tools as toolbench
from ui.theme import section
from ui.viewer_data import editable_payload
from ui.workspace import (compare_panel, editor_panel, focus_request,
                          layout_panel, workspace)

# The tools the viewer's menu can ask the page for. The ids are the menu's; the
# labels are what the heading says.
TOOL_TABS = ["Technology", "DRC", "LVS", "Netlist", "Parasitics", "2.5D view",
             "Density map", "Diff", "Browse shapes", "Browse instances"]
TOOL_BY_ID = {"technology": "Technology", "drc": "DRC", "lvs": "LVS",
              "netlist": "Netlist", "parasitics": "Parasitics",
              "stack3d": "2.5D view", "density": "Density map",
              "diff": "Diff", "xor": "Diff", "shapes": "Browse shapes",
              "instances": "Browse instances"}

EDITED_KEY = "edited_files"


# --- edited layouts ---------------------------------------------------------
# An edit produces a new file, held in session state under the upload's name. Every
# read goes through `file_bytes`, so the moment an edit is written the analyses run
# against what was written rather than against the original upload. The upload
# itself is never modified: it is the way back.

def edited_bytes(name: str) -> bytes | None:
    return (st.session_state.get(EDITED_KEY) or {}).get(name)


def file_bytes(upload) -> bytes:
    """The current contents of an upload: the edited version if there is one."""
    return edited_bytes(upload.name) or upload.getvalue()


def store_edit(name: str, data: bytes) -> None:
    st.session_state.setdefault(EDITED_KEY, {})[name] = data


def revert_edit(name: str) -> None:
    (st.session_state.get(EDITED_KEY) or {}).pop(name, None)


# --- cached analyses --------------------------------------------------------

@st.cache_data(show_spinner="Reading layout geometry...")
def outlines_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                 stack: dict | None, identity: bool = False):
    """Flattened geometry. `identity` adds what the editor needs to name a shape."""
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
def classification_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                       stack: dict | None, all_names: tuple[str, ...]):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            outlines = shape_outlines(path, layermap, role_overrides=overrides)
            result = classify(outlines, path, list(all_names))
            result["pitch"] = analyze_pitch(outlines, filename)
            return result, None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Checking design rules...")
def drc_for(gds_bytes: bytes, filename: str, layermap: dict | None, stack: dict | None):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        overrides = (stack or {}).get("role_overrides") or None
        try:
            return check_layout(shape_outlines(path, layermap,
                                               role_overrides=overrides)), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def net_shapes_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                   stack: dict | None):
    """Nets carrying their polygons, so the viewer can trace one."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return {"nets": extract_nets(path, layermap,
                                         stack or default_stack(layermap),
                                         collect_shapes=True)}
        except Exception:
            return None


@st.cache_data(show_spinner=False)
def tree_for(gds_bytes: bytes, filename: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return instance_tree(path)
        except Exception:
            return None


@st.cache_data(show_spinner=False)
def hierarchy_for(gds_bytes: bytes, filename: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return analyze_hierarchy(path)
        except Exception:
            return None


@st.cache_data(show_spinner="Extracting the netlist...")
def netlist_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                stack: dict | None):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return extract_netlist(path, layermap, stack), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Running LVS...")
def lvs_for(gds_bytes: bytes, filename: str, layermap: dict | None,
            stack: dict | None, schematic_bytes: bytes, schematic_name: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        schematic = Path(td) / (schematic_name or "schematic.cir")
        schematic.write_bytes(schematic_bytes)
        try:
            return lvs_compare(path, layermap, stack, schematic), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Running the rule deck...")
def deck_for(gds_bytes: bytes, filename: str, layermap: dict | None,
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
def density_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                layers: tuple[str, ...], window_nm: float, combine: bool):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return density_map(path, layermap, list(layers), window_nm, combine), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Building the 2.5D view...")
def stack3d_for(gds_bytes: bytes, filename: str, layermap: dict | None,
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
def diff_for(a_bytes: bytes, a_name: str, b_bytes: bytes, b_name: str,
             layermap: dict | None):
    with tempfile.TemporaryDirectory() as td:
        first, second = Path(td) / a_name, Path(td) / f"b_{b_name}"
        first.write_bytes(a_bytes)
        second.write_bytes(b_bytes)
        try:
            result = structural_diff(first, second, layermap)
            result["a"], result["b"] = a_name, b_name
            return result, None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner="Measuring wire geometry...")
def parasitics_for(gds_bytes: bytes, filename: str, layermap: dict | None,
                   stack: dict | None):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return wire_geometry(path, layermap,
                                 role_overrides=(stack or {}).get("role_overrides")), None
        except Exception as exc:
            return None, str(exc)


@st.cache_data(show_spinner=False)
def rc_for(geometry: dict | None, process_bytes: bytes | None, process_name: str):
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
def grid_for(gds_bytes: bytes, filename: str, grid_nm: float = 1.0):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return grid_audit(path, grid_nm)
        except Exception:
            return None


# --- what the viewer asks for -----------------------------------------------

def handle_tool_event(event: dict | None, from_file: str | None = None,
                      owner: str | None = None) -> None:
    """Open the tool the viewer's menu asked for.

    Two things travel with the request: which *file* to run on, and which *viewer*
    asked. They are not the same - the comparison's two halves run on two different
    files, and a tool asked for there has to appear under the comparison rather than
    inside a collapsed expander somewhere up the page.

    The nonce guard matters: a component keeps returning its last value, so without
    it every rerun would reopen the tool and the user could never leave it.
    """
    if not event or not isinstance(event, dict) or event.get("type") != "tool":
        return
    nonce = event.get("nonce")
    if nonce is not None and st.session_state.get("tool_last_event") == nonce:
        return
    st.session_state["tool_last_event"] = nonce
    wanted = TOOL_BY_ID.get(str(event.get("tool")))
    if wanted:
        # A separate key from the one the panel reads, because Streamlit refuses to
        # let a widget's key be written after that widget exists in the same run.
        st.session_state["tool_request"] = wanted
        # The dual viewer names the side's file itself; a single viewer is the file.
        st.session_state["tool_request_file"] = event.get("file") or from_file
        st.session_state["tool_request_owner"] = owner or from_file
        st.rerun()


def handle_edit_event(event: dict | None, upload, name: str,
                      layermap: dict | None) -> None:
    """Write the edits the editor committed, or throw them away."""
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
        # Atomic: nothing was written, so what is on screen is still the file.
        st.session_state["ws_edit_error"] = str(exc)
        st.session_state["ws_revision"] = int(st.session_state.get("ws_revision", 0)) + 1
        st.rerun()
        return
    store_edit(name, data)
    st.session_state["ws_edit_error"] = None
    st.session_state["ws_edit_report"] = report
    st.session_state["ws_revision"] = int(st.session_state.get("ws_revision", 0)) + 1
    st.rerun()


# --- the tools --------------------------------------------------------------

def render_tool(chosen: str, tool_file: str, uploads, layermap: dict | None,
                stack: dict | None) -> None:
    """One tool, under the viewer whose menu asked for it."""
    names = tuple(u.name for u in uploads)
    if tool_file not in names:
        tool_file = names[0]
    index = names.index(tool_file)
    data = file_bytes(uploads[index])

    if chosen == "Technology":
        st.markdown("**What is loaded, and what each input unlocks.**")
        toolbench.technology_panel({
            "layermap": {"loaded": bool(layermap),
                         "source": (layermap or {}).get("file", "bundled")},
            "stack": {"loaded": bool(stack),
                      "source": (stack or {}).get("source", "bundled")},
            "recipe": {"loaded": False, "source": "proposed from the layer map"},
            "deck": {"loaded": bool(st.session_state.get("deck_file")),
                     "source": st.session_state.get("deck_name") or "—"},
            "schematic": {"loaded": bool(st.session_state.get("schematic_file")),
                          "source": st.session_state.get("schematic_name") or "—"},
            "stack3d": {"loaded": bool(st.session_state.get("stack3d_file")),
                        "source": st.session_state.get("stack3d_name") or "—"},
            "drm": {"loaded": True, "source": "data/genchip_drm_rules.json"},
        })
        st.markdown("**The proposed device recipe** — what the netlist and LVS use "
                    "unless you replace it.")
        st.json(default_recipe(layermap, stack))

    elif chosen == "DRC":
        st.markdown("**The bundled catalogue** — the rules transcribed from the "
                    "design rule manual in this repository.")
        result, error = drc_for(data, tool_file, layermap, stack)
        if error:
            st.error(error)
        elif result and result.get("available") is False:
            st.info(f"**Unavailable.** {result['reason']}")
        elif result:
            summary = result["summary"]
            columns = st.columns(4)
            columns[0].metric("Rules checked", summary["rules_checked"])
            columns[1].metric("Violations", summary["violation"])
            columns[2].metric("Passed", summary["pass"])
            columns[3].metric("Not checked", summary["not checked"])
            st.caption(f"The manual has {summary['rules_in_manual']} rules; "
                       f"{summary['rules_not_checked']} of them need information a "
                       ".gds and .lyp do not carry.")
        st.divider()
        st.markdown("**Your own deck** — any technology, run with the same engine.")
        upload = st.file_uploader("Design rule deck (.json)", type=["json"],
                                  key="deck_upload")
        if upload is not None:
            st.session_state["deck_file"] = upload.getvalue()
            st.session_state["deck_name"] = upload.name
        deck_bytes = st.session_state.get("deck_file")
        deck_result = deck = None
        if deck_bytes:
            deck_result, deck, deck_error = deck_for(
                data, tool_file, layermap, deck_bytes,
                st.session_state.get("deck_name", "deck.json"))
            if deck_error:
                st.error(deck_error)
        toolbench.deck_panel(deck_result, deck, lambda: None)

    elif chosen == "LVS":
        upload = st.file_uploader(
            "Schematic netlist (SPICE / CDL)",
            type=["cir", "sp", "spice", "cdl", "net", "txt"], key="schematic_upload")
        if upload is not None:
            st.session_state["schematic_file"] = upload.getvalue()
            st.session_state["schematic_name"] = upload.name
        schematic = st.session_state.get("schematic_file")
        if not schematic:
            toolbench.lvs_panel(None, lambda: None)
        else:
            result, error = lvs_for(data, tool_file, layermap,
                                    stack or default_stack(layermap), schematic,
                                    st.session_state.get("schematic_name", "s.cir"))
            if error:
                st.error(f"LVS could not run: {error}")
            else:
                toolbench.lvs_panel(result, lambda: None)

    elif chosen == "Netlist":
        result, error = netlist_for(data, tool_file, layermap,
                                    stack or default_stack(layermap))
        if error:
            st.error(f"The netlist could not be extracted: {error}")
        else:
            toolbench.netlist_panel(result, Path(tool_file).stem)

    elif chosen == "Parasitics":
        st.markdown("**The layout side of R and C.** Lengths, widths, areas, coupling "
                    "runs and via counts are measured here; the constants that turn "
                    "them into ohms and farads are not in a GDSII.")
        geometry, error = parasitics_for(data, tool_file, layermap, stack)
        if error:
            st.error(error)
        elif geometry:
            upload = st.file_uploader(
                "Process constants (.json) — sheet resistance and capacitance per layer",
                type=["json"], key="process_upload")
            if upload is not None:
                st.session_state["process_file"] = upload.getvalue()
                st.session_state["process_name"] = upload.name
            rc, rc_error = rc_for(geometry, st.session_state.get("process_file"),
                                  st.session_state.get("process_name", "process.json"))
            if rc_error:
                st.error(f"The process file could not be used: {rc_error}")
            toolbench.parasitics_panel(geometry, rc)

    elif chosen == "2.5D view":
        upload = st.file_uploader("Layer stack for the 2.5D view (.json)",
                                  type=["json"], key="stack3d_upload")
        if upload is not None:
            st.session_state["stack3d_file"] = upload.getvalue()
            st.session_state["stack3d_name"] = upload.name
        stack_bytes = st.session_state.get("stack3d_file")
        if not stack_bytes:
            toolbench.stack3d_panel(None, None, lambda: None)
        else:
            slabs, meshes, error = stack3d_for(
                data, tool_file, layermap, stack_bytes,
                st.session_state.get("stack3d_name", "stack3d.json"))
            if error:
                st.error(error)
            else:
                toolbench.stack3d_panel(
                    slabs, stack_3d(meshes or [], (slabs or {}).get("height_nm", 0)),
                    lambda: None)

    elif chosen == "Density map":
        outlines, _ = outlines_for(data, tool_file, layermap, stack)
        layer_names = [row["name"] for row in (outlines or {}).get("layers", [])]
        chosen_layers = st.multiselect(
            "Layers", layer_names,
            default=[n for n in layer_names if n in ("M0", "M1", "M2")][:3]
            or layer_names[:2], key="density_layers")
        window = st.select_slider("Window", options=[25, 50, 100, 200, 500, 1000],
                                  value=100, format_func=lambda v: f"{v} nm",
                                  key="density_window")
        combine = st.checkbox("Combine the chosen layers into one map",
                              key="density_combine")
        if not chosen_layers:
            st.info("Choose at least one layer.")
        else:
            result, error = density_for(data, tool_file, layermap,
                                        tuple(chosen_layers), float(window), combine)
            if error:
                st.error(error)
            elif result:
                which = (st.selectbox("Map", list(result["layers"]), key="density_shown")
                         if len(result["layers"]) > 1 else
                         (list(result["layers"])[0] if result["layers"] else None))
                toolbench.density_panel(result,
                                        density_heatmap(result, which) if which else None)

    elif chosen == "Diff":
        st.markdown("**Structural diff** — cells, shapes, instances and texts, "
                    "compared one for one. This is not the XOR.")
        distinct = sorted(set(names))
        if len(distinct) < 2:
            st.info("Upload a second, different layout to compare against.")
        else:
            columns = st.columns(2)
            first = columns[0].selectbox("A", distinct, index=0, key="diff_a")
            second = columns[1].selectbox("B", [n for n in distinct if n != first],
                                          index=0, key="diff_b")
            result, error = diff_for(file_bytes(uploads[names.index(first)]), first,
                                     file_bytes(uploads[names.index(second)]), second,
                                     layermap)
            if error:
                st.error(error)
            else:
                toolbench.diff_panel(result)

    elif chosen == "Browse shapes":
        outlines, error = outlines_for(data, tool_file, layermap, stack)
        if error:
            st.error(error)
        else:
            toolbench.browse_shapes(outlines or {})

    elif chosen == "Browse instances":
        toolbench.browse_instances(tree_for(data, tool_file))


def tool_panel(for_file: str, uploads, layermap: dict | None,
               stack: dict | None, owner: str | None = None) -> None:
    """The open tool, if it was opened from this viewer.

    Nothing renders when no tool is open, which is the point: the tools live in the
    viewer's menu and the page shows one only while it is being used.
    """
    owner = owner or for_file
    requested = st.session_state.pop("tool_request", None)
    if requested in TOOL_TABS:
        st.session_state["tool_open"] = requested
        came_from = st.session_state.pop("tool_request_file", None) or for_file
        st.session_state["tool_file"] = came_from
        st.session_state["tool_owner"] = (
            st.session_state.pop("tool_request_owner", None) or came_from)

    chosen = st.session_state.get("tool_open")
    if not chosen or st.session_state.get("tool_owner", for_file) != owner:
        return
    on_file = st.session_state.get("tool_file") or for_file
    bar = st.columns([5, 1.1])
    bar[0].markdown(section(f"{chosen} — {on_file}"), unsafe_allow_html=True)
    if bar[1].button("✕ Close", key=f"tool_close_{owner}", width="stretch",
                     help="Close this tool. Reopen it from More tools in the viewer."):
        st.session_state.pop("tool_open", None)
        st.rerun()
    render_tool(chosen, on_file, uploads, layermap, stack)


# --- the expanded workspace -------------------------------------------------

def render_focus(uploads, layermap: dict | None, stack: dict | None,
                 colours: dict[str, str], answer: Callable[[str], str],
                 xor_for: Callable[[str, str], dict[str, Any]] | None = None) -> bool:
    """Draw the expanded workspace, if one was asked for. True when it took over.

    It renders before the page body and stops the script, so the workspace owns the
    screen instead of being appended below a page the user has to scroll past.
    """
    focus = focus_request()
    if not focus:
        return False

    names = tuple(u.name for u in uploads)
    index_of = {name: i for i, name in enumerate(names)}

    def render_view() -> None:
        if focus["kind"] == "compare":
            a_bytes = file_bytes(uploads[index_of[focus["a"]]])
            b_bytes = file_bytes(uploads[index_of[focus["b"]]])
            oa, ea = outlines_for(a_bytes, focus["a"], layermap, stack)
            ob, eb = outlines_for(b_bytes, focus["b"], layermap, stack)
            if ea or eb or not oa or not ob:
                st.error(f"Could not read the geometry: {ea or eb}")
                return
            detail = xor_for(focus["a"], focus["b"]) if xor_for else {}
            compare_panel(detail or {}, oa, ob, colours, focus["a"], focus["b"],
                          key="ws_cmp", height=700, expandable=False)
            return

        name = focus.get("title") or names[0]
        index = index_of.get(name, 0)
        editing = bool(st.session_state.get("ws_editing"))
        data = file_bytes(uploads[index])
        outlines, error = outlines_for(data, name, layermap, stack, identity=editing)
        if error or not outlines:
            st.error(f"Could not read the geometry: {error}")
            return
        drc, _ = drc_for(data, name, layermap, stack)
        classification, _ = classification_for(data, name, layermap, stack, names)
        analysis = dict(
            drc=drc,
            connectivity=net_shapes_for(data, name, layermap, stack),
            pitch=(classification or {}).get("pitch"),
            hierarchy=hierarchy_for(data, name),
            tree=tree_for(data, name),
        )
        if not editing:
            event = layout_panel(outlines, key="ws_lv", colours=colours, title=name,
                                 height=760, expandable=False, interactive=True,
                                 **analysis)
            handle_tool_event(event, name)
            tool_panel(name, uploads, layermap, stack)
            return
        event = editor_panel(
            outlines, key="ws_edit", colours=colours, title=name,
            editable=editable_payload(outlines, layermap,
                                      (classification or {}).get("tech_parameters")),
            revision=int(st.session_state.get("ws_revision", 0)),
            height=760, **analysis)
        handle_edit_event(event, uploads[index], name, layermap)

    focus_name = focus.get("title") or focus.get("a")

    def edit_bar(slot) -> None:
        """Edit mode, and what comes with it: the file out, and the way back."""
        st.session_state.setdefault("ws_editing", False)
        slot.toggle("Edit layout", key="ws_editing",
                    help="Draw, move, reshape and delete. Changes are written to a "
                         "new file with KLayout; the upload is never modified.")
        edited = edited_bytes(focus_name)
        if edited:
            slot.download_button("Download edited .gds", edited,
                                 file_name=f"edited_{focus_name}",
                                 mime="application/octet-stream",
                                 key="ws_download", width="stretch")
            if slot.button("Revert to the upload", key="ws_revert", width="stretch"):
                revert_edit(focus_name)
                st.session_state["ws_revision"] = int(
                    st.session_state.get("ws_revision", 0)) + 1
                st.rerun()

    if focus["kind"] == "layout":
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
                st.warning(
                    f"{grid['added']} shape(s) this edit wrote have a vertex off the "
                    f"{grid['grid_nm']:g} nm grid: "
                    + ", ".join(f"{row['layer']} ({row['shapes']})"
                                for row in grid.get("layers") or []))

    workspace(focus, render_view, answer,
              edit_bar=edit_bar if focus["kind"] == "layout" else None)
    return True
