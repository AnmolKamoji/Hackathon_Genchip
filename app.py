"""GDS Design Reviewer - the review page.

This module orchestrates and renders nothing itself. Every figure on screen was
measured in `analyzer/` and arrived inside one of two documents:

    FileAnalysisDocument   one per uploaded GDS, built once and cached
    ComparisonDocument     one per selected A/B pair, built once and cached

The six sections read those documents. None of them recalculates a value that
already exists in one, which is what stops two sections disagreeing about the same
metric.

The two authoritative inputs - the technology layer map and the design rule
catalogue - are loaded automatically. The only upload control on the page is for
GDS files.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from analyzer.compare_engine import build_comparison
from analyzer.connectivity import default_stack
from analyzer.document import build_document
from analyzer.drc import rules_available
from analyzer.edit import grid_audit
from analyzer.layermap import default_layermap, load_lyp
from analyzer.netlist import extract as extract_netlist
from analyzer.parasitics import wire_geometry
from analyzer.xor_diff import compare_many, xor_compare
from ui.sections import (changes, chat, comparison, impact, inspect, revision,
                         summary, tools)
from ui.theme import CSS
from ui.workspace import compare_panel, layout_panel

load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="GDS Design Reviewer", page_icon="◧", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(CSS, unsafe_allow_html=True)

st.markdown(
    '<div class="title-row"><h1>◧ GDS Design Reviewer</h1>'
    '<span class="sub">deterministic geometry · measured, never guessed</span></div>',
    unsafe_allow_html=True)


# --- the two authoritative inputs, loaded automatically ----------------------

@st.cache_resource(show_spinner=False)
def technology_layer_map():
    """The KLayout .lyp. Not uploaded: it is the technology's own definition.

    Without it a raw GDS can only say `layer_300`, and layer roles, via counts, pin
    identification and device extraction are all undeterminable. The specification
    is explicit that semantic analysis stops with an error rather than continuing
    on guessed semantics.
    """
    bundled = default_layermap()
    if not bundled:
        return None, "no technology layer map is bundled with this installation"
    try:
        return load_lyp(bundled), None
    except ValueError as exc:
        return None, str(exc)


layermap, layermap_error = technology_layer_map()
if layermap_error or not layermap:
    st.error(f"**Semantic analysis is unavailable.** The technology layer map could "
             f"not be loaded: {layermap_error}. Layer roles, via counts, pins and "
             f"device extraction all depend on it, and guessing them from raw layer "
             f"numbers would produce confident wrong answers.")
    st.stop()

st.caption(f"Layer map `{layermap['file']}` — {layermap['entry_count']} technology "
           f"layer names, loaded automatically. "
           + ("Design rule catalogue loaded." if rules_available()
              else "**No design rule catalogue**, so no rule will be checked — this "
                   "is not a clean rule result."))

uploads = st.file_uploader("Upload GDS files", type=["gds"],
                           accept_multiple_files=True)
if not uploads:
    st.info("Upload one or more `.gds` files. `data/samples/` holds five real "
            "standard cells; `NR2D1_1_RT_4.gds` and `NR2D1_2_RT_4.gds` are two "
            "revisions of the same cell.")
    st.stop()


# --- analysis: once per file, cached on its bytes ----------------------------

@st.cache_data(show_spinner="Analyzing the layout...")
def analyse(gds_bytes: bytes, filename: str, all_names: tuple[str, ...]):
    """One FileAnalysisDocument. Parsed once; every section reads the result."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        return build_document(path, layermap, stack=default_stack(layermap),
                              all_filenames=list(all_names))


stack = default_stack(layermap)


def outlines_of(gds_bytes: bytes, filename: str):
    """Flattened geometry for the viewer. Cached separately: the polygons are large
    and belong nowhere near the analysis document the sections read.

    One cache, shared with the tool bench and the expanded workspace, so opening a
    layout full screen re-reads nothing.
    """
    return tools.outlines_for(gds_bytes, filename, layermap, stack)[0]


@st.cache_data(show_spinner="Comparing the two layouts...")
def pair_xor(a_bytes: bytes, a_name: str, b_bytes: bytes, b_name: str):
    with tempfile.TemporaryDirectory() as td:
        first, second = Path(td) / a_name, Path(td) / f"b_{b_name}"
        first.write_bytes(a_bytes)
        second.write_bytes(b_bytes)
        try:
            return xor_compare(first, second, layermap)
        except Exception as exc:
            return {"comparable": False, "reason": str(exc)}


@st.cache_data(show_spinner="Comparing every layout against the reference...")
def family_xor(names: tuple[str, ...], blobs: tuple[bytes, ...]):
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, blob in zip(names, blobs):
            path = Path(td) / name
            path.write_bytes(blob)
            paths.append(path)
        try:
            return compare_many(paths, layermap, 0.0)
        except Exception:
            return None


# --- the chat's extra analyses ----------------------------------------------
# Netlist, grid and wire geometry: nothing on the page displays them, only a
# question can ask for them, and then only sometimes. Defined here because the
# expanded workspace's chat needs them too, and that renders before the page body.

@st.cache_data(show_spinner=False)
def chat_extras(gds_bytes: bytes, filename: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        out = {}
        try:
            out["netlist"] = extract_netlist(path, layermap, stack)
        except Exception:
            out["netlist"] = None
        try:
            out["grid"] = grid_audit(path, 1.0)
        except Exception:
            out["grid"] = None
        try:
            # The stack's role overrides matter here: they make NDIFFCON and
            # PDIFFCON local interconnect rather than contacts, which is what this
            # technology draws. Without them the chat would quote a wire length
            # 0.14 µm shorter than the Parasitics tool on the same page.
            out["parasitics"] = wire_geometry(
                path, layermap, role_overrides=(stack or {}).get("role_overrides"))
        except Exception:
            out["parasitics"] = None
        return out


names = tuple(u.name for u in uploads)
documents = []
for upload in uploads:
    try:
        documents.append(analyse(tools.file_bytes(upload), upload.name, names))
    except Exception as exc:
        st.error(f"Failed to analyze {upload.name}: {exc}")
if not documents:
    st.stop()

_colours = {e["technology_name"]: e.get("fill_color")
            for e in layermap.get("by_key", {}).values() if e.get("fill_color")}
_by_name = {doc["file"]["name"]: doc for doc in documents}


def _extras_for(*file_names: str) -> dict:
    """The chat's extra analyses for the named files, with the geometry attached."""
    out = {}
    for name in file_names:
        index = names.index(name)
        data = tools.file_bytes(uploads[index])
        out[name] = dict(chat_extras(data, name))
        # Already cached from the viewer, so this costs nothing to reuse.
        out[name]["outlines"] = outlines_of(data, name)
        # Ohms and farads only once the Parasitics tool has been given the process
        # constants. Until then the chat answers R and C from the geometry alone,
        # which is the honest half of the question.
        out[name]["rc"] = tools.rc_for(
            out[name].get("parasitics"), st.session_state.get("process_file"),
            st.session_state.get("process_name", "process.json"))[0]
    return out


def _answer(question: str) -> str:
    """One question, answered against whatever the expanded view is showing."""
    focus = tools.focus_request() or {}
    if focus.get("kind") == "compare":
        a_doc, b_doc = _by_name[focus["a"]], _by_name[focus["b"]]
        detail = _pair_xor_by_name(focus["a"], focus["b"])
        return chat.answer_for(
            question, chat.enriched_metadata(a_doc),
            history=st.session_state.get("ws_chat", [])[-6:],
            pair=chat.pair_context(a_doc, b_doc, detail,
                                   _extras_for(focus["a"], focus["b"])),
            xor=detail if detail.get("comparable") else None)
    doc = _by_name.get(focus.get("title")) or documents[0]
    return chat.answer_for(question, chat.enriched_metadata(doc),
                           history=st.session_state.get("ws_chat", [])[-6:])


def _pair_xor_by_name(a_name: str, b_name: str) -> dict:
    ia, ib = names.index(a_name), names.index(b_name)
    return pair_xor(tools.file_bytes(uploads[ia]), a_name,
                    tools.file_bytes(uploads[ib]), b_name)


# The expanded workspace owns the screen when it is open, so it renders before the
# page body and stops the script rather than being appended below it.
if tools.render_focus(uploads, layermap, stack, _colours, _answer,
                      xor_for=_pair_xor_by_name):
    st.stop()


def _layout_viewer(doc, index):
    name = uploads[index].name
    data = tools.file_bytes(uploads[index])
    outlines = outlines_of(data, name)
    if not outlines:
        return
    # The analyses the document already holds become the viewer's markers, cell tree
    # and routing grid; only the net *shapes* are read here, because a document
    # carries the net graph but not the polygons needed to trace one.
    event = layout_panel(outlines, key=f"lv{index}", colours=_colours,
                         title=name, expandable=True, interactive=True,
                         drc=doc.get("rules"),
                         connectivity=tools.net_shapes_for(data, name, layermap, stack),
                         pitch=(doc.get("classification") or {}).get("pitch"),
                         hierarchy=doc.get("hierarchy"),
                         tree=tools.tree_for(data, name))
    tools.handle_tool_event(event, name)
    tools.tool_panel(name, uploads, layermap, stack)


# --- 1. GDS Summary ---------------------------------------------------------
summary.render(documents)

# --- 2. Inspect File --------------------------------------------------------
inspect.render(documents, viewer=_layout_viewer)

# --- 3-6: everything below needs two files ----------------------------------
if len(uploads) < 2:
    st.info("Upload a second GDS file for the comparison, revision analysis and "
            "change list.")
    st.markdown("---")
    chat.render(documents)                      # one file: ask about that file
    st.stop()

st.markdown("---")
index_a, index_b = comparison.selectors(list(names))
if index_a == index_b:
    st.info("Choose two different files to compare.")
    st.stop()

a_doc, b_doc = documents[index_a], documents[index_b]
xor = _pair_xor_by_name(uploads[index_a].name, uploads[index_b].name)


@st.cache_data(show_spinner=False)
def comparison_document(a_name: str, b_name: str, _a, _b, _xor):
    """The one ComparisonDocument. Sections 3-6 all read this object."""
    return build_comparison(_a, _b, _xor)


doc = comparison_document(uploads[index_a].name, uploads[index_b].name,
                          a_doc, b_doc, xor)


def _comparison_viewer():
    oa = outlines_of(tools.file_bytes(uploads[index_a]), uploads[index_a].name)
    ob = outlines_of(tools.file_bytes(uploads[index_b]), uploads[index_b].name)
    if not (oa and ob):
        return
    # Each half's tool menu runs on that half's file, and the result appears here
    # rather than inside the Inspect expander for whichever file was picked.
    event = compare_panel(xor if xor.get("comparable") else {}, oa, ob, _colours,
                          uploads[index_a].name, uploads[index_b].name, key="cmp",
                          expandable=True, interactive=True)
    tools.handle_tool_event(event, uploads[index_a].name, owner="cmp")
    tools.tool_panel(uploads[index_a].name, uploads, layermap, stack, owner="cmp")


family = (family_xor(names, tuple(tools.file_bytes(u) for u in uploads))
          if len(uploads) > 2 else None)

comparison.render(doc, documents, index_a, index_b, xor_all=family,
                  viewer=_comparison_viewer)

st.markdown("---")
revision.render(doc)

st.markdown("---")
changes.render(doc)

st.markdown("---")
# Integration Impact is hidden. The risk flags are still computed and still ride in
# the comparison document; only the section that displayed them is not rendered.
impact.render_ai_summary(doc, ai_enabled=bool(os.getenv("ANTHROPIC_API_KEY")))


# --- Ask the Layout ---------------------------------------------------------
# The chat answers about the *pair* as well as about one file, so a question like
# "did any pin move?" is not answered from one layout's metadata.
st.markdown("---")
chat.render(documents,
            pair=chat.pair_context(a_doc, b_doc, xor,
                                   _extras_for(uploads[index_a].name,
                                               uploads[index_b].name)),
            xor=xor if xor.get("comparable") else None)
