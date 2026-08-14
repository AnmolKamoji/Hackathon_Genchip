"""Section 2 - Inspect File.

One collapsible panel per file, expanded automatically when only one was uploaded.
Everything here is read from that file's FileAnalysisDocument; this module measures
nothing.

The tabs answer different questions and are deliberately not merged: a reviewer
opening "Via / contact" is asking about vias, and making them scroll past the
density grid to get there is how a review surface becomes a wall.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from analyzer.limits import compare as compare_limit
from analyzer.values import UNAVAILABLE, is_missing, number, show
from ui.theme import hint, section, verdict_html

ROUTING_LABEL = {"SingleMetalSolution": "1 metal", "TwoMetalSolution": "2 metal",
                 "ThreeMetalSolution": "3 metal"}


def render(documents: list[dict[str, Any]], viewer=None) -> None:
    st.markdown(section("Inspect File"), unsafe_allow_html=True)
    for index, doc in enumerate(documents):
        with st.expander(_title(doc), expanded=len(documents) == 1):
            _panel(doc, index, viewer, multi=len(documents) > 1)


def _title(doc: dict[str, Any]) -> str:
    layout, geom = doc["layout"], doc["geometry"]
    title = (f"**{doc['file']['name']}** — {doc['file']['top_cell']}, "
             f"{show(geom['drawn_shapes'])} polygons, "
             f"{show(layout['width_um'])} × {show(layout['height_um'])} µm")
    if doc["nets"].get("count") is not None:
        title += f", {doc['nets']['count']} nets"
    return title


def _panel(doc: dict[str, Any], index: int, viewer, multi: bool) -> None:
    st.caption("Geometry is read from the GDSII; layers are named by the technology "
               "layer map, which is loaded automatically.")
    for warning in doc["file"].get("warnings") or []:
        st.warning(warning)

    _classification(doc)
    _headline(doc, index)

    names = ["GDS layout", "Layer analysis", "Geometry & DRC", "Via / contact",
             "Physical connectivity", "Density"]
    # The Cells tab is absent with a single file, not empty and not disabled.
    if multi:
        names.append("Cells")
    tabs = st.tabs(names)

    with tabs[0]:
        _layout_tab(doc, index, viewer)
    with tabs[1]:
        _layer_analysis(doc, index)
    with tabs[2]:
        _geometry_drc(doc, index)
    with tabs[3]:
        _via_contact(doc, index)
    with tabs[4]:
        _connectivity(doc, index)
    with tabs[5]:
        _density(doc, index)
    if multi:
        with tabs[6]:
            _cells(doc, index)


# --- classification, pitch, tech parameters ---------------------------------

def _classification(doc: dict[str, Any]) -> None:
    cls = doc.get("classification")
    if not cls:
        st.info("Cell classification is unavailable for this layout.")
        return
    st.markdown(verdict_html("interconnect-only", cls.get("headline", ""),
                             cls["technology"]["basis"]
                             + "; power delivery from the labels on the power layers."),
                unsafe_allow_html=True)
    c = st.columns(5)
    c[0].metric("Power", (cls["power_delivery"]["power_delivery"] or UNAVAILABLE).capitalize())
    c[1].metric("Technology", cls["technology"]["technology"])
    c[2].metric("Routing", ROUTING_LABEL.get(cls["metal_solution"]["metal_solution"],
                                             UNAVAILABLE))
    c[3].metric("Height", (cls["cell_height"].get("height") or UNAVAILABLE).capitalize())
    c[4].metric("M0 tracks", f"{show(cls['routing_tracks'].get('tracks_used'))}"
                             f"/{show(cls['routing_tracks'].get('tracks'))}")

    _pitch(cls)
    _tech_parameters(cls)


def _pitch(cls: dict[str, Any]) -> None:
    pitch = cls.get("pitch") or {}
    gate = (pitch.get("gate_pitch") or {}).get("cpp_nm")
    if not gate:
        return                                  # shown only when it was measurable
    dims = pitch.get("cell_dimensions") or {}
    metals = pitch.get("metal_pitches") or {}
    st.markdown(section("Pitch metrics"), unsafe_allow_html=True)
    p = st.columns(6)
    p[0].metric("Gate pitch (CPP)", f"{number(gate)} nm",
                help="Also called CGP or the poly pitch — the poly-to-poly spacing.")
    p[1].metric("Cell width", f"{show(dims.get('gate_pitches'))} CPP",
                help=dims.get("width_basis", ""))
    for slot, metal in zip(p[2:5], ("M0", "M1", "M2")):
        entry = metals.get(metal) or {}
        slot.metric(f"{metal} pitch",
                    f"{number(entry['pitch_nm'])} nm" if entry.get("pitch_nm") else UNAVAILABLE,
                    help=entry.get("note") or entry.get("source") or "")
    gear = (pitch.get("gear_ratio") or {}).get("gear_ratio")
    p[5].metric("Gear ratio", number(gear) if gear else UNAVAILABLE,
                help="CPP / M1 pitch — the device grid against the routing grid.")
    if dims.get("rt_in_filename") is not None:
        agrees = dims.get("rt_matches_measured_tracks")
        st.markdown(hint(
            f"The filename declares RT {dims['rt_in_filename']} and "
            f"{show(dims.get('signal_tracks'))} M0 signal tracks were measured — "
            + ("they agree." if agrees else "<b>they disagree.</b>")),
            unsafe_allow_html=True)


def _tech_parameters(cls: dict[str, Any]) -> None:
    params = (cls.get("tech_parameters") or {}).get("parameters")
    if not params:
        return
    comparison = (cls.get("tech_parameters") or {}).get("comparison") or {}
    st.markdown(section("Tech file parameters"), unsafe_allow_html=True)
    stated = {row["parameter"]: row for row in
              (comparison.get("agree") or []) + (comparison.get("disagree") or [])
              + (comparison.get("stated_only") or [])}
    disagreeing = {r["parameter"] for r in comparison.get("disagree") or []}
    rows = []
    for name, record in params.items():
        against = (stated.get(name) or {}).get("stated")
        # Four situations that must not read alike.
        if against is None:
            verdict = ""
        elif not record.get("available"):
            verdict = "tech file only"
        elif name in disagreeing:
            verdict = "DISAGREES"
        else:
            verdict = "matches"
        rows.append({"Parameter": name, "Measured": show(record.get("value")),
                     "Unit": record.get("unit") or "",
                     "Rule": record.get("drm_rule") or "",
                     "vs tech file": verdict})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _headline(doc: dict[str, Any], index: int) -> None:
    c = st.columns(5)
    c[0].metric("Cells", show(doc["cells"]["count"]))
    c[1].metric("Polygons", show(doc["geometry"]["drawn_shapes"]))
    c[2].metric("Vias", show(doc["vias"]["count"]))
    c[3].metric("Cell area", show(doc["layout"]["area_um2"], 6) + " µm²")
    c[4].metric("Nets", show(doc["nets"]["count"]))
    vias = doc["vias"]
    if vias.get("count") is not None and vias.get("layer_names"):
        st.caption(f"{vias['count']} vias on {', '.join(vias['layer_names'])}"
                   + (f"; {vias['contact_count']} contacts counted separately"
                      if vias.get("contact_count") else "")
                   + (f". Derived from {vias['source']}." if vias.get("source") else "."))

    agg = doc["layers"].get("role_aggregates") or {}
    if agg:
        st.markdown(section("By layer role"), unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "role": role, "layers": ", ".join(a["layers"]),
            "shapes": a["shape_count"],
            "area (µm²)": show(a["total_area_um2"], 6),
            "min width (µm)": show(a.get("observed_min_width_um")),
            "min space (µm)": show(a.get("observed_min_space_um")),
        } for role, a in sorted(agg.items())]),
            width="stretch", hide_index=True, key=f"roles_{index}")


# --- tabs -------------------------------------------------------------------

def _layout_tab(doc: dict[str, Any], index: int, viewer) -> None:
    layout, geom, hier = doc["layout"], doc["geometry"], doc.get("hierarchy") or {}
    box = layout.get("bbox_dbu") or {}
    rows = [
        ("Top cell", doc["file"]["top_cell"], "GDSII"),
        ("Cell count", show(doc["cells"]["count"]), "GDSII hierarchy"),
        ("Hierarchy depth", show(doc["cells"]["hierarchy_depth"]),
         "GDSII — 0 means flat, which is a measurement"),
        ("Layout dimensions",
         f"{show(layout['width_um'])} × {show(layout['height_um'])} µm", "bounding box × dbu"),
        ("Bounding box",
         f"({show(box.get('left'))}, {show(box.get('bottom'))}) → "
         f"({show(box.get('right'))}, {show(box.get('top'))}) dbu", "GDSII"),
        ("Total layout area", show(layout["area_um2"], 6) + " µm²", "width × height"),
        ("Polygon count", show(geom["drawn_shapes"]), "flattened"),
        ("Path count", show(geom.get("path_count")), "GDSII path records"),
        ("Via count", show(doc["vias"]["count"]), "layer map via layer names"),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["property", "value", "source"]),
                 width="stretch", hide_index=True, key=f"overview_{index}")
    if viewer is not None:
        viewer(doc, index)


def _layer_analysis(doc: dict[str, Any], index: int) -> None:
    measured = {(r.get("layer"), r.get("datatype")): r
                for r in doc["layers"].get("measured") or []}
    show_all = st.checkbox("Include the pin / label / duplicate copies",
                           key=f"showall_{index}",
                           help="These repeat another layer's geometry, so they "
                                "double every total.")
    rows = []
    for row in doc["layers"].get("rows") or []:
        m = measured.get((row.get("layer"), row.get("datatype")), {})
        if not show_all and m.get("derived"):
            continue
        rows.append({
            "layer": row.get("name"),
            "layer/datatype": f"{row.get('layer')}/{row.get('datatype')}",
            "role": m.get("role", ""),
            "polygons": show(row.get("polygon_count")),
            "vias": show(row.get("via_count")),
            "labels": show(row.get("text_count")),
            "area (µm²)": show(row.get("area_um2"), 6),
            "% of cell": (f"{row['density_percent']:.2f}%"
                          if row.get("density_percent") is not None else UNAVAILABLE),
            "min width (µm)": show(m.get("observed_min_width_um")),
            "min space (µm)": show(m.get("observed_min_space_um")),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 key=f"layers_{index}")


def _geometry_drc(doc: dict[str, Any], index: int) -> None:
    rules = doc["rules"]
    if not rules.get("available"):
        st.info(f"**No rule was checked.** {rules.get('reason')}")
    else:
        s = rules["summary"]
        state = "identical" if not s.get("violation") else "base-layers"
        head = (f"No violations of the {s.get('rules_checked')} rules checked"
                if not s.get("violation")
                else f"{s['violation']} violation(s) of the checked rules")
        tech = rules.get("technology") or {}
        st.markdown(verdict_html(state, head,
                                 f"Checked against {rules.get('source')}. Technology "
                                 f"read as <b>{tech.get('used')}</b> — {tech.get('basis')}."),
                    unsafe_allow_html=True)
        c = st.columns(4)
        c[0].metric("Pass", show(s.get("pass")))
        c[1].metric("Violations", show(s.get("violation")))
        c[2].metric("Not evaluable", show(s.get("not checked")))
        c[3].metric("Rules checked",
                    f"{show(s.get('rules_checked'))}/{show(s.get('rules_in_manual'))}")

        if rules.get("violations"):
            st.markdown(section("Violations"), unsafe_allow_html=True)
            for v in rules["violations"]:
                st.markdown(f"**{v['id']}** — {v['detail']}<br>"
                            f'<span class="hint">Manual {v["section"]}: "{v["rule"]}"</span>',
                            unsafe_allow_html=True)

        st.markdown(section("All checked rules"), unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "rule": r["id"], "status": r["status"], "finding": r["detail"],
            "manual wording": r["rule"]} for r in rules.get("results") or []]),
            width="stretch", hide_index=True, key=f"drc_{index}")

    _numeric_checks(doc, index)
    _integrity(doc, index)


def _numeric_checks(doc: dict[str, Any], index: int) -> None:
    """Measured width and spacing against the manual - which prescribes no limit.

    The flow is fixed: geometry measured from the GDS -> the layer identified by
    the layer map -> the required value read from the manual -> the two compared.
    For this manual the third step yields nothing, so every row reads unavailable
    and carries only the measurement. That is the correct result, not a gap.
    """
    st.markdown(section("Minimum width, spacing and area"), unsafe_allow_html=True)
    results = {r["id"]: r for r in (doc["rules"].get("results") or [])}
    rows, evaluated, violations = [], 0, 0
    for layer in doc["layers"].get("measured") or []:
        if layer.get("derived") or not layer.get("shape_count"):
            continue
        for kind, key in (("min width", "observed_min_width_um"),
                          ("min spacing", "observed_min_space_um"),
                          ("area", "total_area_um2")):
            measured = layer.get(key)
            rule = next((r for r in results.values()
                         if layer.get("name", "") in (r.get("rule") or "")), None)
            verdict = compare_limit(measured, (rule or {}).get("rule", ""))
            if verdict["status"] != "unavailable":
                evaluated += 1
                violations += verdict["status"] == "violation"
            rows.append({
                "layer": layer.get("name"), "role": layer.get("role"), "check": kind,
                "measured (µm)": show(measured),
                "required (µm)": show(verdict["required_um"]),
                "difference (µm)": show(verdict["difference_um"]),
                "status": verdict["status"],
                "rule": (rule or {}).get("id", ""),
                "why": verdict["why"],
            })
    c = st.columns(4)
    c[0].metric("Total checks", len(rows))
    c[1].metric("Evaluated against a limit", evaluated)
    c[2].metric("Violations", violations)
    c[3].metric("No limit in the manual", len(rows) - evaluated)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 key=f"numeric_{index}")


def _integrity(doc: dict[str, Any], index: int) -> None:
    integ = doc["geometry"].get("integrity") or {}
    if not integ:
        return
    st.markdown(section("Geometry integrity"), unsafe_allow_html=True)
    c = st.columns(4)
    c[0].metric("Shapes examined", show(integ.get("shapes_examined")))
    c[1].metric("Zero-area polygons", show(integ.get("zero_area_count")))
    c[2].metric("Self-intersecting", show(integ.get("self_intersecting_count")))
    c[3].metric("Measured grid", f"{show(integ.get('measured_grid_nm'))} nm")
    st.markdown(hint(integ.get("measured_grid_basis", "")), unsafe_allow_html=True)
    st.markdown(hint("<b>Off-grid shapes:</b> " + UNAVAILABLE + " — "
                     + (integ.get("off_grid") or {}).get("reason", "")),
                unsafe_allow_html=True)
    st.markdown(hint("<b>Notches and slivers:</b> " + UNAVAILABLE + " — "
                     + (integ.get("notches_and_slivers") or {}).get("reason", "")),
                unsafe_allow_html=True)


def _via_contact(doc: dict[str, Any], index: int) -> None:
    conn = doc.get("connectivity") or {}
    land = conn.get("landings") or {}
    area = doc["layout"].get("area_um2")
    vias = doc["vias"].get("count")
    st.caption("Vias are identified semantically: (layer, datatype) → layer map → "
               "role via. Never inferred from two metals overlapping.")
    c = st.columns(4)
    c[0].metric("Vias and contacts",
                show((vias or 0) + (doc["vias"].get("contact_count") or 0)
                     if vias is not None else None))
    c[1].metric("Via layers", show(len(doc["vias"].get("layer_names") or []) or None))
    c[2].metric("Via density (per µm²)",
                show(round(vias / area, 3) if vias is not None and area else None))
    orphans = _orphan_vias(land)
    c[3].metric("Overlapping no conductor", show(orphans))

    rows = []
    for layer in doc["layers"].get("measured") or []:
        if layer.get("role") not in ("via", "contact") or not layer.get("shape_count"):
            continue
        ext = layer.get("shape_extents") or {}
        rows.append({
            "via layer": layer.get("name"), "role": layer.get("role"),
            "layer/datatype": f"{layer.get('layer')}/{layer.get('datatype')}",
            "count": layer.get("shape_count"),
            "size (µm)": f"{show(ext.get('min_width_um'))} × {show(ext.get('min_height_um'))}",
            "same size": show(ext.get("uniform")),
            "min spacing (µm)": show(layer.get("observed_min_space_um")),
            "arrangement": (layer.get("arrangement") or {}).get("description", UNAVAILABLE),
        })
    if rows:
        st.markdown(section("Via size, spacing and arrangement"), unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     key=f"viasize_{index}")

    if land.get("available"):
        st.markdown(section("Via enclosure — measured plan-view overlap"),
                    unsafe_allow_html=True)
        st.caption("**Overlap is not connection.** GDSII stores no layer elevations, "
                   "so which levels a via bridges comes from the connection stack. "
                   "The required enclosure is " + UNAVAILABLE
                   + ": the manual names via extension without giving its value.")
        st.dataframe(pd.DataFrame([{
            "via layer": cn["name"], "conductor": o["name"],
            "shapes interacting": o["shapes_interacting"],
            "shapes enclosed": o["shapes_enclosed"],
            "interaction ratio": show(o["interaction_ratio"]),
            "enclosure ratio": show(o["enclosure_ratio"]),
        } for cn in land["connectors"] for o in cn["overlaps"]]),
            width="stretch", hide_index=True, key=f"encl_{index}")


def _orphan_vias(land: dict[str, Any]) -> int | None:
    if not land.get("available"):
        return None
    return sum(1 for cn in land.get("connectors") or []
               if not any(o["shapes_interacting"] for o in cn.get("overlaps") or []))


def _connectivity(doc: dict[str, Any], index: int) -> None:
    conn = doc.get("connectivity") or {}
    if conn.get("error"):
        st.error(f"Connectivity analysis failed: {conn['error']}")
        return
    for w in conn.get("warnings") or []:
        st.warning(w)

    t1 = conn.get("intra_layer") or {}
    st.markdown("#### Tier 1 · intra-layer connectivity — GDS-only, exact")
    st.caption(t1.get("basis", ""))
    c = st.columns(3)
    c[0].metric("Conducting shapes", show(t1.get("total_shapes")))
    c[1].metric("Physical conductors", show(t1.get("total_components")))
    c[2].metric("Layers with abutting shapes", show(t1.get("layers_with_abutting_shapes")))
    if t1.get("layers"):
        st.dataframe(pd.DataFrame([{
            "layer": r["name"], "role": r.get("role"),
            "shapes": r["shape_count"], "components": r["component_count"],
            "largest (µm²)": show(r.get("largest_component_area_um2"), 6),
            "smallest (µm²)": show(r.get("smallest_component_area_um2"), 6),
        } for r in t1["layers"]]), width="stretch", hide_index=True,
            key=f"tier1_{index}")

    st.markdown("#### Tier 2 · via and contact landings — GDS + layer map, measured")
    land = conn.get("landings") or {}
    if not land.get("available"):
        st.info(f"{UNAVAILABLE}: {land.get('reason')}")
    else:
        st.caption("Reported as **overlap**, never as connection.")
        st.dataframe(pd.DataFrame([{
            "connector": cn["name"], "conductor": o["name"],
            "shapes interacting": o["shapes_interacting"],
            "shapes enclosed": o["shapes_enclosed"],
        } for cn in land["connectors"] for o in cn["overlaps"]]),
            width="stretch", hide_index=True, key=f"tier2_{index}")

    st.markdown("#### Tier 3 · net graph — requires a connection stack")
    nets = doc["nets"]
    if not nets.get("available"):
        st.info(f"{UNAVAILABLE}: {nets.get('reason')}")
    else:
        st.caption(f"Stack source: {nets.get('stack_source')}. The stack is a named "
                   "assumption transcribed from via layer names, not PDK-verified.")
        s = nets.get("summary") or {}
        c = st.columns(4)
        c[0].metric("Nets", show(s.get("net_count")))
        c[1].metric("Multi-layer nets", show(s.get("multi_layer_net_count")))
        c[2].metric("Single-layer nets", show(s.get("single_layer_net_count")))
        c[3].metric("Floating nets", show(s.get("floating_net_count")))
    st.caption("Shorts and opens are never reported: both are defined against an "
               "intended netlist, which neither file contains.")


def _density(doc: dict[str, Any], index: int) -> None:
    density = doc.get("density") or {}
    levels = density.get("levels") or []
    c = st.columns(3)
    c[0].metric("Cell area", show(doc["layout"]["area_um2"], 6) + " µm²")
    c[1].metric("Densest metal level",
                show(density.get("densest_percent")) + "%" if levels else UNAVAILABLE)
    c[2].metric("Mean metal level",
                show(density.get("mean_percent")) + "%" if levels else UNAVAILABLE)
    st.caption("Metal density is measured **per level** and the levels are "
               "deliberately not summed: they stack, so four levels at 30% each are "
               "not 120% dense.")
    if density.get("rows"):
        st.dataframe(pd.DataFrame(density["rows"]), width="stretch", hide_index=True,
                     key=f"density_{index}")
    st.markdown(hint("No density verdict is available: the manual states no density "
                     "limit, so the measurement is reported without a pass or fail."),
                unsafe_allow_html=True)


def _cells(doc: dict[str, Any], index: int) -> None:
    hier = doc.get("hierarchy") or {}
    if hier and not hier.get("error"):
        st.caption(f"{hier.get('depth_description', '')}. "
                   f"{show(hier.get('cell_count_total'))} cell(s) in the file, "
                   f"{show(hier.get('cell_count_in_scope'))} reachable from "
                   f"`{hier.get('top_cell')}`.")
    rows = [{
        "name": r.get("name"), "index": r.get("index"),
        "width (µm)": show(r.get("width_um")), "height (µm)": show(r.get("height_um")),
        "area (µm²)": show(r.get("area_um2"), 6),
        "instance count": show(r.get("instance_count")),
        "instance records": show(r.get("instance_record_count")),
    } for r in doc["cells"].get("rows") or []]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 key=f"cells_{index}")
    st.caption("Instance count means placements: a 2×2 array is one instance record "
               "and four placements.")
