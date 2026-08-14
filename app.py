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
from analyzer.measurements import shape_outlines
from analyzer.netlist import extract as extract_netlist
from analyzer.parasitics import wire_geometry
from analyzer.xor_diff import compare_many, xor_compare
from ui.sections import (changes, chat, comparison, impact, inspect, revision,
                         summary)
from ui.theme import CSS
from ui.viewer_data import build as build_payload
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


@st.cache_data(show_spinner=False)
def outlines_of(gds_bytes: bytes, filename: str):
    """Flattened geometry for the viewer. Cached separately: the polygons are large
    and belong nowhere near the analysis document the sections read."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        try:
            return shape_outlines(path, layermap)
        except Exception:
            return None


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


names = tuple(u.name for u in uploads)
documents = []
for upload in uploads:
    try:
        documents.append(analyse(upload.getvalue(), upload.name, names))
    except Exception as exc:
        st.error(f"Failed to analyze {upload.name}: {exc}")
if not documents:
    st.stop()

_colours = {e["technology_name"]: e.get("fill_color")
            for e in layermap.get("by_key", {}).values() if e.get("fill_color")}


def _layout_viewer(doc, index):
    data = outlines_of(uploads[index].getvalue(), uploads[index].name)
    if data:
        layout_panel(data, key=f"lv{index}", colours=_colours,
                     title=doc["file"]["name"], expandable=False)


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
xor = pair_xor(uploads[index_a].getvalue(), uploads[index_a].name,
               uploads[index_b].getvalue(), uploads[index_b].name)


@st.cache_data(show_spinner=False)
def comparison_document(a_name: str, b_name: str, _a, _b, _xor):
    """The one ComparisonDocument. Sections 3-6 all read this object."""
    return build_comparison(_a, _b, _xor)


doc = comparison_document(uploads[index_a].name, uploads[index_b].name,
                          a_doc, b_doc, xor)


def _comparison_viewer():
    oa = outlines_of(uploads[index_a].getvalue(), uploads[index_a].name)
    ob = outlines_of(uploads[index_b].getvalue(), uploads[index_b].name)
    if oa and ob:
        compare_panel(xor if xor.get("comparable") else {}, oa, ob, _colours,
                      uploads[index_a].name, uploads[index_b].name, key="cmp",
                      expandable=False)


family = family_xor(names, tuple(u.getvalue() for u in uploads)) if len(uploads) > 2 else None

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
@st.cache_data(show_spinner=False)
def chat_extras(gds_bytes: bytes, filename: str):
    """The three analyses the chat can be asked about that the document omits.

    They are here rather than in the document because nothing on the page displays
    them - only a question can ask for them, and then only sometimes.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        path.write_bytes(gds_bytes)
        stack = default_stack(layermap)
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
            out["parasitics"] = wire_geometry(path, layermap)
        except Exception:
            out["parasitics"] = None
        return out


st.markdown("---")
_extras = {}
for _i in (index_a, index_b):
    _name = uploads[_i].name
    _extras[_name] = dict(chat_extras(uploads[_i].getvalue(), _name))
    # Already cached from the viewer, so this costs nothing to reuse.
    _extras[_name]["outlines"] = outlines_of(uploads[_i].getvalue(), _name)
chat.render(documents,
            pair=chat.pair_context(a_doc, b_doc, xor, _extras),
            xor=xor if xor.get("comparable") else None)
