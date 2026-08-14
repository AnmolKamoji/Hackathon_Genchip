"""Section 4 - Revision Analysis.

Answers "what kind of change is this?" and reads the one ComparisonDocument to do
it. Nothing here recomputes a delta.

The three header values each carry a tri-state that must not be flattened. A
footprint that could not be measured is Unknown, not Different; a device topology
where either extraction failed is Unknown, because two failed extractions agreeing
that they know nothing is not evidence that the topology is the same.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from analyzer.values import UNAVAILABLE, delta, show
from ui.theme import section


def render(doc: dict[str, Any]) -> None:
    st.markdown(section("Revision Analysis"), unsafe_allow_html=True)

    c = st.columns(3)
    c[0].metric("Change type", doc["change_type"])
    c[1].metric("Drop-in replacement", doc["drop_in_replacement"])
    c[2].metric("Device topology", doc["device_topology"])

    d = doc["deltas"]
    m = st.columns(6)
    m[0].metric("Drawn Shape Δ", delta(d["drawn_shapes"]["delta"]),
                help="The flattened drawn count. Not the stored shape-record count — "
                     "they are different measurements.")
    m[1].metric("Via Δ", delta(d["vias"]["delta"]))
    m[2].metric("Label Δ", delta(d["labels"]["delta"]))
    m[3].metric("Layer Δ", delta(d["layers"]["delta"]))
    m[4].metric("Transistor Δ", delta(d["transistors"]["delta"]))
    m[5].metric("Width Δ µm", delta(d["width_um"]["delta"]))

    _footprint(doc)
    _pins(doc)
    _stack(doc)
    _devices(doc)
    _nets(doc)
    _layers(doc)
    _tabs(doc)


def _footprint(doc: dict[str, Any]) -> None:
    fp = doc["footprint"]
    st.markdown(section("Footprint"), unsafe_allow_html=True)
    state = {True: "identical", False: "different", None: "Unknown"}[fp["identical"]]
    st.caption(f"Identical: **{state}** — {fp['reason']}.")
    rows = [{"Metric": label, "A": show(f["a"]), "B": show(f["b"]),
             "Δ": delta(f["delta"])}
            for label, key in (("Width (µm)", "width_um"), ("Height (µm)", "height_um"),
                               ("Area (µm²)", "area_um2"))
            for f in [fp["fields"][key]]]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key="rev_fp")


def _pins(doc: dict[str, Any]) -> None:
    pins = doc["pins"]
    st.markdown(section("Pins"), unsafe_allow_html=True)
    if not pins.get("available"):
        st.info(f"{UNAVAILABLE}: {pins.get('reason')}")
        return
    c = st.columns(4)
    c[0].metric("Added", len(pins["added"]))
    c[1].metric("Removed", len(pins["removed"]))
    c[2].metric("Changed", len(pins["changed"]))
    c[3].metric("Swaps", len(pins["swaps"]))
    flags = st.columns(3)
    flags[0].metric("Pin name set identical", show(pins["pin_name_set_identical"]))
    flags[1].metric("Pin compatible", show(pins["pin_compatible"]))
    flags[2].metric("Footprint pin compatible", show(pins["footprint_pin_compatible"]))


def _stack(doc: dict[str, Any]) -> None:
    stack = doc["stack"]
    st.markdown(section("Stack"), unsafe_allow_html=True)
    c = st.columns(3)
    # A set difference that came back empty is a measurement - the comparison ran
    # and found nothing - so it reads "none", never Unavailable.
    c[0].metric("Metal levels added", ", ".join(stack["metal_levels_added"]) or "none")
    c[1].metric("Metal levels removed", ", ".join(stack["metal_levels_removed"]) or "none")
    c[2].metric("Top metal",
                f"{show(stack['top_metal_a'])} → {show(stack['top_metal_b'])}")
    if stack["via_layer_changes"]:
        st.dataframe(pd.DataFrame([{
            "via layer": r["name"], "layer/datatype": f"{r['layer']}/{r['datatype']}",
            "A": r["a"], "B": r["b"], "Δ": delta(r["delta"])}
            for r in stack["via_layer_changes"]]),
            width="stretch", hide_index=True, key="rev_via")


def _devices(doc: dict[str, Any]) -> None:
    devices = doc["devices"]
    st.markdown(section("Devices"), unsafe_allow_html=True)
    if not devices["comparable"]:
        st.info(f"{UNAVAILABLE}: {devices.get('reason')}")
    rows = [{"Metric": label, "A": show(f["a"]), "B": show(f["b"]),
             "Δ": delta(f["delta"])}
            for label, key in (("Transistor count", "transistor_count"),
                               ("NMOS", "nmos"), ("PMOS", "pmos"),
                               ("Gate length (µm)", "gate_length_um"),
                               ("Gate pitch (µm)", "gate_pitch_um"))
            for f in [devices["fields"][key]]]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key="rev_dev")


def _nets(doc: dict[str, Any]) -> None:
    nets = doc["nets"]
    st.markdown(section("Nets"), unsafe_allow_html=True)
    if not nets.get("available"):
        st.info(f"{UNAVAILABLE}: {nets.get('reason')}")
        return
    c = st.columns(3)
    c[0].metric("Added", len(nets["added"]))
    c[1].metric("Removed", len(nets["removed"]))
    c[2].metric("Changed", len(nets["changed"]))
    st.caption(nets.get("note", ""))


def _layers(doc: dict[str, Any]) -> None:
    tally = doc["layers"]["tally"]
    st.markdown(section("Layers"), unsafe_allow_html=True)
    c = st.columns(4)
    c[0].metric("Added", tally["added"])
    c[1].metric("Removed", tally["removed"])
    c[2].metric("Modified", tally["modified"])
    c[3].metric("Untouched", tally["untouched"])


def _tabs(doc: dict[str, Any]) -> None:
    pin_tab, layer_tab = st.tabs(["Pin differences", "Layer differences"])

    with pin_tab:
        pins = doc["pins"]
        if not pins.get("available"):
            st.info(f"{UNAVAILABLE}: {pins.get('reason')}")
        else:
            # One row per COMMON pin, not only the changed ones: "this pin did not
            # move" is a result a reviewer needs to see stated.
            st.dataframe(pd.DataFrame([{
                "Pin": p["name"], "Changed": show(p["changed"]), "Moved": show(p["moved"]),
                "access shapes in A": p["access_shapes_a"],
                "access shapes in B": p["access_shapes_b"],
                "access layers in A": ", ".join(p["access_layers_a"]) or "—",
                "access layers in B": ", ".join(p["access_layers_b"]) or "—",
            } for p in pins["common"]]), width="stretch", hide_index=True,
                key="rev_pins")
            for one, two in pins["swaps"]:
                st.warning(f"Pin swap: **{one} ↔ {two}** — reported once, not as two "
                           "unrelated movements.")
            if pins["added"]:
                st.info("Only in B: " + ", ".join(pins["added"]))
            if pins["removed"]:
                st.info("Only in A: " + ", ".join(pins["removed"]))

    with layer_tab:
        layers = doc["layers"]
        rows = []
        for state in ("added", "removed", "modified", "untouched"):
            for row in layers[state]:
                rows.append({
                    "state": state, "layer": row.get("name"),
                    "layer/datatype": f"{row.get('layer')}/{row.get('datatype')}",
                    "role": row.get("role") or "",
                    "polygon Δ": delta(row.get("polygon_delta")) if "polygon_delta" in row else "—",
                    "via Δ": delta(row.get("via_delta")) if "via_delta" in row else "—",
                    "area Δ (µm²)": (delta(row.get("area_delta_um2"), 6)
                                     if "area_delta_um2" in row else "—"),
                })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     key="rev_layers")
