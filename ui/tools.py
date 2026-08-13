"""The tool bench: everything KLayout puts under its Tools menu.

One rule runs through all of it. A tool that needs an input the layout cannot supply
says so, names the input, and shows the format - it does not fall back on a guess.
LVS without a schematic, a rule deck that was never loaded, elevations for a 2.5D
view: each of those is a missing input, and the honest answer is to ask for it.

Rendering only. Every number comes from `analyzer/`; nothing is computed here.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import pandas as pd
import streamlit as st

from ui.theme import hint, section

# What each tool needs before it can run, in the words of the person who has to
# supply it. Shown in place of the tool, not as an error after a failure.
NEEDS = {
    "lvs": ("a schematic netlist", "SPICE or CDL (`.cir`, `.sp`, `.spice`, `.cdl`)",
            "LVS compares a layout against a schematic. There is no schematic in a "
            "GDSII, so without one there is nothing to compare against - and a "
            "verdict produced without it would be invented."),
    "deck": ("a design rule deck", "JSON (see the format below)",
             "The bundled check covers the rules transcribed from one manual. Any "
             "other technology needs its own deck; a limit cannot be guessed from "
             "the geometry it is supposed to judge."),
    "stack3d": ("a layer stack", "JSON with an elevation and a thickness per layer",
                "GDSII stores no Z. Heights have to come from the process, and a "
                "made-up elevation produces a picture that looks authoritative and "
                "is wrong about the only thing it exists to show."),
}

DECK_EXAMPLE = {
    "technology": "my process",
    "rules": [
        {"id": "M1.W.1", "type": "width", "layer": "M1", "min_nm": 30,
         "text": "M1 minimum width"},
        {"id": "M1.S.1", "type": "space", "layer": "M1", "min_nm": 30},
        {"id": "V0.A.1", "type": "area", "layer": "VIA0", "min_nm2": 400},
        {"id": "M1.E.1", "type": "enclosure", "layer": "M1", "of": "VIA0", "min_nm": 5},
        {"id": "V0.I.1", "type": "inside", "layer": "VIA0", "of": "M1"},
        {"id": "M1.D.1", "type": "density", "layer": "M1", "min_pct": 20,
         "max_pct": 80, "window_nm": 1000},
        {"id": "M1.G.1", "type": "grid", "layer": "M1", "grid_nm": 1},
    ],
}

STACK3D_EXAMPLE = {
    "technology": "my process",
    "layers": {
        "NDIFF": {"elevation_nm": 0, "thickness_nm": 20},
        "NPOLY": {"elevation_nm": 20, "thickness_nm": 40},
        "M0": {"elevation_nm": 70, "thickness_nm": 25},
        "VIA0": {"elevation_nm": 95, "thickness_nm": 20},
        "M1": {"elevation_nm": 115, "thickness_nm": 30},
    },
}


def missing(kind: str, uploader: Callable[[], Any] | None = None) -> None:
    """Say what is missing, why, and in what format - then offer the uploader."""
    what, form, why = NEEDS[kind]
    st.info(f"**This needs {what}.** {why}")
    st.caption(f"Format: {form}")
    if uploader:
        uploader()


def _status_colour(status: str) -> str:
    return {"violation": "#f85149", "error": "#f85149", "unusable": "#d29922",
            "not applicable": "#7d8b9c", "pass": "#3fb950"}.get(status, "#7d8b9c")


# --- DRC decks --------------------------------------------------------------

def deck_panel(result: dict[str, Any] | None, deck: dict[str, Any] | None,
               uploader: Callable[[], Any]) -> None:
    """Results of a user-supplied rule deck."""
    if not deck:
        missing("deck", uploader)
        with st.expander("Deck format"):
            st.code(json.dumps(DECK_EXAMPLE, indent=2), language="json")
            st.caption("Rule types: width, space, notch, area, enclosure, overlap, "
                       "separation, inside, not_overlapping, density, grid.")
        return

    uploader()
    if not result:
        return
    summary = result["summary"]
    columns = st.columns(5)
    columns[0].metric("Rules", summary["rules"])
    columns[1].metric("Violations", summary["violation"])
    columns[2].metric("Passed", summary["pass"])
    columns[3].metric("Not applicable", summary["not applicable"])
    columns[4].metric("Unusable", summary["unusable"] + summary["error"])
    if summary["violation"]:
        st.error(f"{summary['violation']} rule(s) failed, "
                 f"{summary['violations_found']} place(s) in total.")
    else:
        st.success("Every usable rule in this deck passed.")

    st.dataframe(pd.DataFrame([{
        "rule": row["id"],
        "type": row["type"],
        "status": row["status"],
        "layers": ", ".join(row["layers"]),
        "found": row["count"],
        "detail": row["detail"],
    } for row in result["results"]]), width="stretch", hide_index=True,
        key="deck_results")
    st.caption(result["not_derivable"]["rules_not_in_the_deck"])


# --- LVS --------------------------------------------------------------------

def lvs_panel(result: dict[str, Any] | None, uploader: Callable[[], Any]) -> None:
    if result is None:
        missing("lvs", uploader)
        return
    uploader()
    if not result.get("available"):
        st.warning(result.get("reason", "LVS could not run."))
        return

    if result["matched"]:
        st.success(f"**{result['headline']}.** Every device, net and pin paired up.")
    else:
        st.error(f"**{result['headline']}.** {result['problem_count']} difference(s).")

    totals = result["totals"]
    columns = st.columns(3)
    for column, kind in zip(columns, ("devices", "nets", "pins")):
        matched = totals[kind]["match"]
        column.metric(kind.capitalize(), f"{matched}/{matched + totals[kind]['other']}")

    if result["problems"]:
        st.markdown("**Differences**")
        for line in result["problems"][:40]:
            st.markdown(f"- {line}")
        if result["problem_count"] > 40:
            st.caption(f"…and {result['problem_count'] - 40} more.")

    with st.expander("Cross-reference"):
        for circuit in result["circuits"]:
            st.markdown(f"**{circuit['layout']} / {circuit['schematic']}** — "
                        f"{circuit['status']}")
            for kind in ("devices", "nets", "pins"):
                rows = circuit[kind]
                if not rows:
                    continue
                st.dataframe(pd.DataFrame([{
                    "layout": row["layout"], "schematic": row["schematic"],
                    "status": row["status"]} for row in rows]),
                    width="stretch", hide_index=True, key=f"lvs_{circuit['layout']}_{kind}")

    with st.expander("How this comparison was set up"):
        st.markdown("**Schematic** — " + result["schematic"]["file"])
        st.json(result["schematic"]["circuits"])
        st.markdown("**Device parameters**")
        for line in result["parameter_comparison"]:
            st.markdown(f"- {line}")
        st.markdown("**Connections used**")
        st.caption(", ".join(result["connections_used"]))
    for key, text in result["not_derivable"].items():
        st.caption(f"*{key.replace('_', ' ')}* — {text}")


# --- netlist ----------------------------------------------------------------

def netlist_panel(result: dict[str, Any] | None, filename: str = "netlist") -> None:
    if not result:
        st.info("The netlist is built when a connection stack is available.")
        return
    if not result.get("available"):
        st.warning(result.get("reason", "No netlist could be extracted."))
        if result.get("recipe"):
            st.json(result["recipe"])
        return

    summary = result["summary"]
    columns = st.columns(4)
    columns[0].metric("Devices", summary["device_count"])
    columns[1].metric("Nets", summary["net_count"])
    columns[2].metric("Named nets", summary["named_net_count"])
    columns[3].metric("Circuits", summary["circuit_count"])
    st.caption("Device classes: " + ", ".join(f"{k} × {v}" for k, v in
                                              summary["device_classes"].items()))

    floating = result["diagnostics"]["floating_terminals"]
    if floating:
        st.warning(f"{len(floating)} device terminal(s) reach nothing else in the "
                   f"layout: " + ", ".join(f"{f['class']} {f['device']}.{f['terminal']}"
                                           for f in floating[:8]))
        st.caption(result["diagnostics"]["note"])

    for circuit in result["circuits"]:
        st.markdown(f"**{circuit['name']}** — {len(circuit['devices'])} device(s), "
                    f"{len(circuit['nets'])} net(s)")
        if circuit["devices"]:
            st.dataframe(pd.DataFrame([{
                "device": device["name"],
                "class": device["class"],
                **{f"{terminal}": net or "—"
                   for terminal, net in device["terminals"].items()},
                "L (µm)": device["parameters"].get("L"),
                "W (µm)": device["parameters"].get("W"),
            } for device in circuit["devices"]]), width="stretch", hide_index=True,
                key=f"devices_{circuit['name']}")
        st.dataframe(pd.DataFrame([{
            "net": net["name"], "named": net["named"],
            "terminals": net["terminals"], "pins": net["pins"],
        } for net in circuit["nets"]]), width="stretch", hide_index=True,
            key=f"nets_{circuit['name']}")

    if result.get("spice"):
        st.download_button("Download the extracted netlist (SPICE)", result["spice"],
                           file_name=f"{filename}.cir", mime="text/plain",
                           key="netlist_spice")
    with st.expander("Recipe and connections"):
        st.json(result["recipe"])
        st.caption(", ".join(result["connections_used"]))
    for key, text in result["not_derivable"].items():
        st.caption(f"*{key.replace('_', ' ')}* — {text}")


# --- density ----------------------------------------------------------------

def density_panel(result: dict[str, Any] | None, figure=None) -> None:
    if not result:
        return
    if figure is not None:
        st.plotly_chart(figure, width="stretch", key="density_map")
    rows = []
    for name, entry in result["layers"].items():
        if not entry.get("available", True):
            rows.append({"layer": name, "windows": "—", "min %": "—", "mean %": "—",
                         "max %": "—", "overall %": "—", "note": entry["reason"]})
            continue
        rows.append({"layer": name, "windows": entry["tile_count"],
                     "min %": entry["min_pct"], "mean %": entry["mean_pct"],
                     "max %": entry["max_pct"], "overall %": entry["overall_pct"],
                     "note": ""})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key="density_rows")
    st.caption(result["not_derivable"]["fill_requirements"])


# --- structural diff --------------------------------------------------------

def diff_panel(result: dict[str, Any] | None) -> None:
    if not result:
        return
    if result["identical"]:
        st.success(f"**{result['headline']}** — same cells, same shapes, same "
                   "instances, same texts.")
    else:
        st.warning(f"**{result['headline']}**")
    totals = result["totals"]
    columns = st.columns(3)
    columns[0].metric("Shapes only in A → B",
                      f"{totals['shapes_only_in_a']} → {totals['shapes_only_in_b']}")
    columns[1].metric("Texts only in A → B",
                      f"{totals['texts_only_in_a']} → {totals['texts_only_in_b']}")
    columns[2].metric("Instances only in A → B",
                      f"{totals['instances_only_in_a']} → {totals['instances_only_in_b']}")

    if result["cells_only_in_a"] or result["cells_only_in_b"]:
        st.markdown(f"**Cells only in {result['a']}**: "
                    f"{', '.join(result['cells_only_in_a']) or '—'}")
        st.markdown(f"**Cells only in {result['b']}**: "
                    f"{', '.join(result['cells_only_in_b']) or '—'}")

    for cell in result["cells_that_differ"]:
        st.markdown(f"**{cell['cell']}**")
        if cell["layers"]:
            st.dataframe(pd.DataFrame([{
                "layer": row["name"],
                f"shapes only in {result['a']}": row["shapes_only_in_a"],
                f"shapes only in {result['b']}": row["shapes_only_in_b"],
                f"texts only in {result['a']}": row["texts_only_in_a"],
                f"texts only in {result['b']}": row["texts_only_in_b"],
            } for row in cell["layers"]]), width="stretch", hide_index=True,
                key=f"diff_{cell['cell']}")
        for side in ("a", "b"):
            rows = cell[f"instances_only_in_{side}"]
            if rows:
                st.caption(f"Instances only in {result[side]}: " +
                           ", ".join(f"{r['cell']} at {r['trans']}" for r in rows[:10]))
    st.caption(result["difference_from_xor"])


# --- 2.5D -------------------------------------------------------------------

def stack3d_panel(slabs: dict[str, Any] | None, figure,
                  uploader: Callable[[], Any]) -> None:
    if not slabs:
        missing("stack3d", uploader)
        with st.expander("Stack format"):
            st.code(json.dumps(STACK3D_EXAMPLE, indent=2), language="json")
        return
    uploader()
    if not slabs.get("available"):
        st.warning("Nothing to draw: none of the layers in the stack file have "
                   "geometry in this layout.")
        st.caption("Layers in the stack without geometry here: " +
                   (", ".join(slabs["layers_without_geometry"]) or "—"))
        return
    if figure is not None:
        st.plotly_chart(figure, width="stretch", key="stack3d")
    columns = st.columns(3)
    columns[0].metric("Slabs", slabs["slab_count"])
    columns[1].metric("Layers drawn", len(slabs["layers_drawn"]))
    columns[2].metric("Stack height", f"{slabs['height_nm']:g} nm")
    st.dataframe(pd.DataFrame(slabs["layers_drawn"]), width="stretch",
                 hide_index=True, key="stack3d_layers")
    if slabs["layers_not_in_the_stack"]:
        st.caption("In the layout but not in the stack file, so not drawn: " +
                   ", ".join(slabs["layers_not_in_the_stack"]))
    for key, text in slabs["not_derivable"].items():
        st.caption(f"*{key}* — {text}")


# --- browsers ---------------------------------------------------------------

def browse_shapes(outlines: dict[str, Any]) -> None:
    """Every shape, with its measurements. KLayout calls this Browse Shapes."""
    rows = []
    for layer in outlines.get("layers") or []:
        for index, shape in enumerate(layer.get("shapes") or []):
            rows.append({
                "layer": layer["name"],
                "layer/datatype": f"{layer['layer']}/{layer['datatype']}",
                "#": index,
                "width (nm)": round(shape["width_um"] * 1000, 4),
                "height (nm)": round(shape["height_um"] * 1000, 4),
                "area (nm²)": round(shape["area_um2"] * 1e6, 4),
                "x (nm)": round(shape["left_um"] * 1000, 4),
                "y (nm)": round(shape["bottom_um"] * 1000, 4),
                "vertices": shape.get("vertices"),
            })
    if not rows:
        st.info("This layout has no drawable geometry.")
        return
    frame = pd.DataFrame(rows)
    names = sorted(frame["layer"].unique())
    chosen = st.multiselect("Layers", names, default=names[:6], key="browse_layers")
    if chosen:
        frame = frame[frame["layer"].isin(chosen)]
    st.caption(f"{len(frame)} shape(s). Every column is measured, not estimated — "
               "sort by width to find the narrowest.")
    st.dataframe(frame, width="stretch", hide_index=True, key="browse_shapes")


def browse_instances(tree: dict[str, Any] | None) -> None:
    """Every cell placement. KLayout calls this Browse Instances."""
    if not tree:
        st.info("No cell tree was read for this layout.")
        return
    if tree.get("flat"):
        st.info(f"**{tree['top']} is flat** — it contains no instances, so there is "
                "nothing to browse. Its shapes are on the Shapes tab.")
        return
    rows = [{
        "cell": placement["cell"],
        "in": placement["parent"],
        "level": placement["depth"],
        "orientation": placement["orient"],
        "x (nm)": round(placement["bbox"][0] * 1000, 4) if placement["bbox"] else None,
        "y (nm)": round(placement["bbox"][1] * 1000, 4) if placement["bbox"] else None,
        "shapes in cell": placement["shapes"],
        "path": placement["path"],
    } for placement in tree.get("placements") or []]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 key="browse_instances")
    if tree.get("truncated"):
        st.caption("The list is truncated: this layout has more placements than the "
                   "walk limit.")


def technology_panel(state: dict[str, Any]) -> None:
    """What is loaded, and what each thing unlocks. KLayout's Manage Technologies."""
    rows = []
    for key, label, unlocks in (
            ("layermap", "Layer map (.lyp)", "layer names, roles, colours"),
            ("stack", "Connection stack (.json)", "nets, netlist, LVS"),
            ("recipe", "Device recipe (.json)", "which layers make a transistor"),
            ("deck", "Design rule deck (.json)", "your own DRC"),
            ("schematic", "Schematic netlist (SPICE)", "LVS"),
            ("stack3d", "Layer stack (.json)", "the 2.5D view"),
            ("drm", "Bundled rule catalogue", "the built-in design rule check"),
    ):
        entry = state.get(key) or {}
        rows.append({
            "input": label,
            "loaded": "yes" if entry.get("loaded") else "no",
            "source": entry.get("source") or "—",
            "unlocks": unlocks,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key="tech_state")
    st.markdown(hint(
        "Everything a <b>.gds</b> and a <b>.lyp</b> can answer is answered without any "
        "of these. The rest each need one file, and each tool says which."),
        unsafe_allow_html=True)
