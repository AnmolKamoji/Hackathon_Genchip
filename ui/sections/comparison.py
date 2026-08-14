"""Section 3 - GDS Comparison.

Rendered only when two or more files are uploaded. With one file it is absent -
not empty, not disabled.

The two selectors decide what every section below is about, so they list each
upload with its position: two uploads can share a filename, and a name-keyed
selector could not tell them apart.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from analyzer.compare_engine import compare_cell, shared_cells
from analyzer.present import findings, headline
from analyzer.plots import change_hotspot, difference_grid, similarity_matrix
from analyzer.values import UNAVAILABLE, show
from ui.theme import hint, section, style_figure, verdict_html


def selectors(names: list[str]) -> tuple[int, int]:
    """Compare (A, the reference) and Versus (B, the revision).

    Positions prefix the names because two uploads may share a filename and the
    selection has to identify the upload, not the name.
    """
    labelled = [f"{i + 1}. {name}" for i, name in enumerate(names)]
    cols = st.columns(2)
    a = cols[0].selectbox("Compare", labelled, index=0, key="cmp_a",
                          help="A — the reference. Differences are measured from it.")
    b = cols[1].selectbox("Versus", labelled,
                          index=1 if len(labelled) > 1 else 0, key="cmp_b",
                          help="B — the revision.")
    return labelled.index(a), labelled.index(b)


def render(doc: dict[str, Any], docs: list[dict[str, Any]],
           index_a: int, index_b: int, xor_all=None, viewer=None) -> None:
    a_name = doc["reference"]["file"]
    b_name = doc["revision"]["file"]
    st.caption(f"Every difference is **B − A**: {b_name} measured against "
               f"{a_name}. A is always the percentage denominator.")

    _table(doc)
    _chart(doc)
    _xor(doc, viewer)
    _non_numeric(doc)
    _shared_cell(docs[index_a], docs[index_b])
    if xor_all is not None:
        _reference_comparison(xor_all, docs)


def _table(doc: dict[str, Any]) -> None:
    st.markdown(section("Comparison features"), unsafe_allow_html=True)
    a_name, b_name = doc["reference"]["file"], doc["revision"]["file"]
    rows = []
    for row in doc["metrics"]:
        rows.append({
            "Metric": f"{row['metric']}",
            a_name: show(row["a"]),
            b_name: show(row["b"]),
            "Difference": (show(row["difference"]) if not row["numeric"]
                           else f"{row['difference']:+g}"),
            "% Difference": row["percent"] or "",
            "Note": row["note"],
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key="cmp_table")


def _chart(doc: dict[str, Any]) -> None:
    """A and B side by side, numeric rows only.

    A bar chart cannot draw "Only in B", and the metrics span an area in µm² beside
    a cell count - so the axis is logarithmic and non-positive values are dropped
    rather than silently clipped to the axis floor.
    """
    numeric = [r for r in doc["metrics"]
               if r["numeric"] and isinstance(r["a"], (int, float))
               and isinstance(r["b"], (int, float))]
    plottable = [r for r in numeric if r["a"] > 0 and r["b"] > 0]
    if not plottable:
        return
    import plotly.graph_objects as go
    a_name, b_name = doc["reference"]["file"], doc["revision"]["file"]
    labels = [r["metric"] for r in plottable]
    fig = go.Figure([
        go.Bar(name=a_name, x=labels, y=[r["a"] for r in plottable]),
        go.Bar(name=b_name, x=labels, y=[r["b"] for r in plottable]),
    ])
    fig.update_layout(barmode="group", yaxis_type="log", height=380)
    st.plotly_chart(style_figure(fig), width="stretch", key="cmp_chart")
    dropped = len(numeric) - len(plottable)
    if dropped:
        st.caption(f"{dropped} metric(s) are not drawn: a logarithmic axis cannot "
                   "show a zero or negative value, and clipping them would be a "
                   "silent change to the data.")


def _xor(doc: dict[str, Any], viewer) -> None:
    xor = doc.get("xor") or {}
    if not xor.get("comparable"):
        st.info(f"Geometric comparison {UNAVAILABLE}: {xor.get('reason')}")
        return
    head = headline(xor, (xor.get("summary") or {}).get("cell_area_um2"))
    st.markdown(verdict_html(head["state"], head["headline"], head.get("detail", "")),
                unsafe_allow_html=True)

    summary = xor["summary"]
    if summary.get("identical"):
        if viewer is not None:
            viewer()
        return

    c = st.columns(4)
    c[0].metric("Layers changed",
                f"{show(summary['layers_changed'])}/{show(summary['layers_compared'])}")
    c[1].metric("Regions", show(summary["difference_regions"]))
    c[2].metric("XOR area", show(summary["total_xor_area_um2"], 6) + " µm²",
                help="Total geometry present in one layout and not the other")
    c[3].metric("Removed → Added",
                f"{show(summary['total_area_removed_um2'], 3)} → "
                f"{show(summary['total_area_added_um2'], 3)} µm²")

    if viewer is not None:
        viewer()

    st.markdown(section("Largest differences — biggest first"), unsafe_allow_html=True)
    top = findings(xor, None, limit=8)
    st.dataframe(pd.DataFrame([{
        "layer": f["layer"], "change": f["change"], "size": f["size"],
        "area (µm²)": show(f["area_um2"], 6),
        "at (µm)": f"{show(f['at_um'][0])}, {show(f['at_um'][1])}"} for f in top]),
        width="stretch", hide_index=True, key="cmp_findings")
    st.markdown(hint(xor.get("mask_impact", {}).get("caveat", "")),
                unsafe_allow_html=True)


def _non_numeric(doc: dict[str, Any]) -> None:
    rows = doc.get("non_numeric") or []
    if not rows:
        return
    st.markdown(section("Non-numeric metrics"), unsafe_allow_html=True)
    a_name, b_name = doc["reference"]["file"], doc["revision"]["file"]
    st.dataframe(pd.DataFrame([{
        "Metric": r["metric"], a_name: show(r["a"]), b_name: show(r["b"]),
        "Difference": show(r["difference"])} for r in rows]),
        width="stretch", hide_index=True, key="cmp_nonnumeric")
    st.caption("These are never subtracted: \"M1 minus M0\" is not a number.")


def _shared_cell(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Only cells present in BOTH files, matched exactly.

    No partial matching and no nearest-name substitution: INVD1 never matches
    INVD10, INVD1_X2 or INVD2.
    """
    names = shared_cells(a, b)
    if not names:
        return
    st.markdown(section("Shared cell comparison"), unsafe_allow_html=True)
    chosen = st.selectbox("Cell present in both files", names, key="cmp_cell")
    result = compare_cell(a, b, chosen)
    if not result["found"]:
        st.info(result["reason"])
        return
    a_name, b_name = a["file"]["name"], b["file"]["name"]
    st.dataframe(pd.DataFrame([{
        "Metric": r["metric"], a_name: show(r["a"]), b_name: show(r["b"]),
        "Difference": (f"{r['difference']:+g}" if r["numeric"] else show(r["difference"])),
        "% Difference": r["percent"] or ""} for r in result["rows"]]),
        width="stretch", hide_index=True, key="cmp_cell_table")


def _reference_comparison(xor_all: dict[str, Any], docs: list[dict[str, Any]]) -> None:
    """Three or more files is a revision family, reviewed against one reference."""
    names = xor_all["files"]
    if len(names) < 3:
        return
    st.markdown(section(f"All {len(names)} layouts — reference comparison"),
                unsafe_allow_html=True)
    reference = st.selectbox("Reference layout — everything is compared back to this",
                             names, key="xor_reference",
                             help="A revision family is reviewed against one golden "
                                  "database.")
    pairs = [p for p in xor_all["pairs"]
             if reference in (p["a"], p["b"]) and p.get("comparable")]
    same = [p for p in pairs if p["identical"]]
    worst = max(pairs, key=lambda p: p["total_xor_area_um2"], default=None)

    c = st.columns(3)
    c[0].metric("Match the reference", f"{len(same)} of {len(pairs)}")
    if worst:
        other = worst["b"] if worst["a"] == reference else worst["a"]
        c[1].metric("Furthest from reference", other)
        c[2].metric("Its XOR area", show(worst["total_xor_area_um2"], 6) + " µm²")
    if same:
        st.success("Identical to the reference: "
                   + ", ".join(p["b"] if p["a"] == reference else p["a"] for p in same))

    layout = docs[0]["layout"]
    bbox = layout.get("bbox_dbu") or {}
    dbu = docs[0]["file"].get("dbu_um") or 1.0
    cell_bbox = ([bbox["left"] * dbu, bbox["bottom"] * dbu,
                  bbox["right"] * dbu, bbox["top"] * dbu] if bbox else None)

    grid = style_figure(difference_grid(xor_all, reference, cell_bbox))
    if grid is not None:
        st.plotly_chart(grid, width="stretch", key="diffgrid")
        st.caption(f"Each panel is one layout against **{reference}**, on shared axes "
                   "and to scale. Red = missing relative to the reference, green = extra.")

    hot = style_figure(change_hotspot(xor_all, cell_bbox, reference=reference))
    if hot is not None:
        st.plotly_chart(hot, width="stretch", key="hotspot")
        st.caption("Difference area accumulated over a grid of the cell, so a "
                   "repeatedly-edited region stands out however the revisions are paired.")

    with st.expander("Pairwise matrix — every combination"):
        heat = style_figure(similarity_matrix(xor_all))
        if heat is not None:
            st.plotly_chart(heat, width="stretch", key="simmatrix")
