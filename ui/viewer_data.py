"""Build the geometry payload the interactive viewer draws.

The viewer runs in the browser and needs every polygon, so this is the one place
that decides what crosses that boundary. Two constraints shape it:

* **Exactness.** Coordinates are sent as measured, in micrometres, and every
  derived figure a user can read off the screen (width, height, area, pitch) is
  computed here in Python rather than in JavaScript. A number the analyzer already
  measured must not be re-derived from screen pixels - that is how a viewer starts
  disagreeing with the report beside it.
* **Size.** A standard cell is small, but the payload is embedded in the page, so
  redundant precision is dropped: coordinates are rounded to the database grid
  rather than shipped as full floats.
"""
from __future__ import annotations

import json
from typing import Any

# Nanometre grid. These layouts have a 0.05 nm database unit, so six decimal
# places in micrometres is finer than anything the file can express - past that
# the digits are float noise, and they cost payload size on every vertex.
_PLACES = 6


def _round(value: float) -> float:
    return round(float(value), _PLACES)


def _outline(points: list[list[float]]) -> list[list[float]]:
    return [[_round(x), _round(y)] for x, y in points]


def layer_payload(row: dict[str, Any], fallback: str | None = None) -> dict[str, Any]:
    """One layer, with its shapes and everything the viewer shows about them."""
    shapes = []
    for shape in row.get("shapes") or []:
        shapes.append({
            "o": _outline(shape["outline_um"]),
            "w": _round(shape["width_um"]),
            "h": _round(shape["height_um"]),
            "a": float(f"{shape['area_um2']:.9g}"),
            "cx": _round(shape["centre_um"][0]),
            "cy": _round(shape["centre_um"][1]),
            "x": _round(shape["left_um"]),
            "y": _round(shape["bottom_um"]),
            "v": shape.get("vertices"),
            # Present only when the layout was read for editing: which cell the
            # shape lives in and its outline in that cell's own database units.
            # An edit names this, never a screen position.
            **({"id": shape["id"]} if shape.get("id") else {}),
        })
    labels = [{"t": lab["text"], "x": _round(lab["at_um"][0]), "y": _round(lab["at_um"][1])}
              for lab in row.get("labels") or []]
    extent = row.get("extent") or {}
    return {
        "name": row["name"],
        "layer": row["layer"],
        "datatype": row["datatype"],
        "role": row.get("role") or "drawing",
        "colour": row.get("colour") or fallback or "#8aa0b6",
        "shapes": shapes,
        "labels": labels,
        "count": row.get("shape_count") or 0,
        "labelCount": row.get("label_count") or 0,
        "extent": ({"w": _round(extent["width_um"]), "h": _round(extent["height_um"])}
                   if extent else None),
    }


def build(outlines: dict[str, Any], fallback_colours: dict[str, str] | None = None,
          title: str = "") -> dict[str, Any]:
    """The full payload for one layout.

    `default_on` decides what the viewer shows before the user touches anything. A
    standard cell carries more pin, label, duplicate and track-guide copies than
    real layers, and switching all of them on at once produces a solid block of
    colour - so the derived copies start hidden, exactly as the old panel did.
    """
    rows = [r for r in outlines.get("layers") or []
            if r.get("shape_count") or r.get("label_count")]
    fallback_colours = fallback_colours or {}

    layers = [layer_payload(r, fallback_colours.get(r["name"])) for r in rows]
    primary = [layer["name"] for layer in layers if layer["role"] != "derived"]

    left, bottom, right, top = outlines.get("cell_bbox_um") or [0, 0, 0, 0]
    return {
        "title": title or outlines.get("top_cell") or "",
        "file": outlines.get("file") or "",
        "topCell": outlines.get("top_cell") or "",
        "dbu": outlines.get("dbu_um"),
        "bbox": [_round(left), _round(bottom), _round(right), _round(top)],
        "width": _round(outlines.get("cell_width_um") or (right - left)),
        "height": _round(outlines.get("cell_height_um") or (top - bottom)),
        "layers": layers,
        "defaultOn": primary or [layer["name"] for layer in layers],
        "warnings": outlines.get("warnings") or [],
    }


def editable_payload(outlines: dict[str, Any], layermap: dict[str, Any] | None,
                     tech: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the editor needs beyond the drawing: where a new shape may go.

    A layer the technology does not define has no layer number, so drawing on it
    would produce a file whose layers mean nothing. The editor therefore offers the
    technology's own layers and no others, and the grid choices come from the
    database unit rather than from a list of round numbers.
    """
    dbu_nm = (outlines.get("dbu_um") or 0.001) * 1000
    catalogue = []
    for key, entry in sorted(((layermap or {}).get("by_key") or {}).items()):
        catalogue.append({
            "name": entry.get("technology_name") or f"layer_{key[0]}_{key[1]}",
            "layer": key[0], "datatype": key[1],
            "role": entry.get("role") or "unknown",
            "colour": entry.get("fill_color") or "#8aa0b6",
        })
    drawn = {row["name"] for row in outlines.get("layers") or []}
    # The grid the file can actually express, then the ones a designer works on.
    steps = sorted({round(dbu_nm, 6), 0.5, 1.0, 5.0, 10.0} - {0.0})
    # Measured figures, shown while drawing so the constraint is on screen rather
    # than in a manual on another monitor. These are what this layout *is*, not what
    # a rule deck says it must be - the wording in the editor has to keep that
    # distinction, because the two are only the same on a layout that already passes.
    rules = {}
    for name, entry in ((tech or {}).get("parameters") or {}).items():
        if (isinstance(entry, dict) and entry.get("available")
                and isinstance(entry.get("value"), (int, float))
                and entry.get("unit") == "nm"):
            rules[name] = {"nm": entry["value"], "rule": entry.get("drm_rule")}
    return {
        "topCell": outlines.get("top_cell"),
        "dbuNm": round(dbu_nm, 6),
        "gridStepsNm": steps,
        "layers": catalogue,
        "drawnLayers": sorted(drawn),
        "rulesNm": rules,
    }


def build_comparison(xor: dict[str, Any], a_payload: dict[str, Any],
                     b_payload: dict[str, Any]) -> dict[str, Any]:
    """The payload for a two-layout comparison.

    Both layouts travel whole, so the viewer can show A, B, an overlay, a wipe or
    a blink without another round trip - switching between those is a view
    decision, and making it a server call would put a spinner in the middle of an
    inspection. The XOR regions ride alongside as their own layer.
    """
    regions = []
    for row in xor.get("layers") or []:
        for side, key in (("a", "removed"), ("b", "added")):
            block = row.get(key) or {}
            for item in block.get("locations") or []:
                outline = item.get("outline_um")
                if not outline:
                    continue
                regions.append({
                    "layer": row["name"],
                    "side": side,
                    "o": _outline(outline),
                    "a": float(f"{item.get('area_um2', 0):.9g}"),
                })
    summary = xor.get("summary") or {}
    return {
        "a": a_payload,
        "b": b_payload,
        "regions": regions,
        "changedLayers": sorted({r["layer"] for r in regions}),
        "summary": {
            "layersChanged": summary.get("layers_changed"),
            "layersCompared": summary.get("layers_compared"),
            # These are the analyzer's own key names. Guessing shorter ones made
            # every area read None, so the difference browser quietly showed no
            # areas at all - a wrong number would have been noticed; a missing row
            # was not.
            "xorAreaUm2": summary.get("total_xor_area_um2"),
            "removedAreaUm2": summary.get("total_area_removed_um2"),
            "addedAreaUm2": summary.get("total_area_added_um2"),
            "regionCount": len(regions),
        },
        "names": {"a": a_payload.get("file") or "A", "b": b_payload.get("file") or "B"},
    }


def to_json(payload: dict[str, Any]) -> str:
    """Compact JSON for embedding. Separators matter: the default ones add a space
    per field, which on a few thousand vertices is a measurable slice of the page."""
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)

def markers_payload(drc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Rule results as reviewable markers.

    This is the marker browser KLayout calls RVE: a result that cannot be clicked
    back to the geometry is a sentence with nowhere to go, and the reviewer ends up
    typing coordinates by hand. `layers` comes from the check itself - the layers it
    actually read - so the link is recorded rather than guessed from the wording.
    """
    if not drc or drc.get("available") is False:
        return []
    order = {"violation": 0, "not checked": 1, "not applicable": 2, "pass": 3}
    markers = []
    for row in drc.get("results") or []:
        markers.append({
            "id": row["id"],
            "section": row.get("section"),
            "rule": row.get("rule"),
            "status": row.get("status"),
            "detail": row.get("detail"),
            "layers": row.get("layers") or [],
            "observed": row.get("observed") or {},
        })
    markers.sort(key=lambda m: (order.get(m["status"], 9), m["id"]))
    return markers


def nets_payload(connectivity: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Physical nets with their polygons, for click-to-trace highlighting.

    Only present when a connection stack was available - without one there is no
    net graph, and the viewer says so rather than showing an empty list as though
    the layout had no nets.
    """
    if not connectivity:
        return []
    block = (connectivity.get("nets") or {})
    out = []
    for net in block.get("nets") or []:
        shapes = [{"layer": s["layer"], "o": _outline(s["outline_um"])}
                  for s in net.get("shapes") or []]
        if not shapes:
            continue
        out.append({
            "net": net.get("net"),
            "layers": net.get("layers") or [],
            "shapeCount": net.get("shape_count"),
            "area": float(f"{net.get('area_um2', 0):.9g}"),
            "spans": bool(net.get("spans_multiple_layers")),
            "provisional": bool(block.get("provisional")),
            "shapes": shapes,
        })
    return out


def tracks_payload(pitch: dict[str, Any] | None) -> dict[str, Any]:
    """The routing grid, so it can be drawn over the layout.

    A track overlay answers "is this wire on grid?" by looking, which is otherwise
    a ruler measurement repeated once per wire.
    """
    if not pitch:
        return {}
    out = {}
    for metal, entry in (pitch.get("metal_pitches") or {}).items():
        if not entry or not entry.get("positions_nm"):
            continue
        out[metal] = {
            "axis": entry.get("pitch_axis") or ("y" if entry.get("routing_direction") == "horizontal" else "x"),
            "pitchNm": entry.get("pitch_nm"),
            "positionsNm": entry.get("positions_nm"),
            "widthNm": entry.get("width_nm"),
            "uniform": bool(entry.get("uniform")),
            "note": entry.get("note"),
        }
    gate = pitch.get("gate_pitch") or {}
    if gate.get("cpp_nm"):
        out["_cpp"] = {"cppNm": gate["cpp_nm"],
                       "columnsNm": (gate.get("detail") or {}).get("centres_nm")
                       or gate.get("centres_nm") or []}
    return out


def cells_payload(hierarchy: dict[str, Any] | None,
                  tree: dict[str, Any] | None) -> dict[str, Any]:
    """The cell tree, for navigation and instance boundaries.

    `tree` carries the placements with their transformed boxes; `hierarchy` carries
    the structural counts already reported on the Hierarchy tab. Both are passed so
    the panel can be built from one payload, and either may be missing - a viewer
    handed only geometry still has to open.
    """
    out: dict[str, Any] = {}
    if tree:
        out = {
            "top": tree.get("top"),
            "topBbox": tree.get("topBbox"),
            "maxDepth": tree.get("maxDepth") or 0,
            "flat": bool(tree.get("flat")),
            "note": tree.get("note"),
            "truncated": bool(tree.get("truncated")),
            "cells": [{"name": c["name"], "shapes": c.get("shapes"),
                       "bbox": c.get("bbox"), "levels": c.get("levels"),
                       "placements": c.get("placements"), "isTop": bool(c.get("isTop"))}
                      for c in tree.get("cells") or []],
            "placements": [{"id": p["id"], "cell": p["cell"], "parent": p.get("parent"),
                            "path": p.get("path"), "depth": p.get("depth"),
                            "bbox": p.get("bbox"), "orient": p.get("orient"),
                            "shapes": p.get("shapes")}
                           for p in tree.get("placements") or []],
        }
    if hierarchy:
        out.setdefault("top", hierarchy.get("top_cell"))
        out["structure"] = {
            "cellCount": hierarchy.get("cell_count_total"),
            "topCells": hierarchy.get("top_cells") or [],
            "depth": hierarchy.get("max_depth_below_top"),
            "description": hierarchy.get("depth_description"),
            "emptyCells": hierarchy.get("empty_cells") or [],
            "orphanCells": hierarchy.get("orphan_cells") or [],
        }
    return out


def with_analysis(payload: dict[str, Any], drc: dict[str, Any] | None = None,
                  connectivity: dict[str, Any] | None = None,
                  pitch: dict[str, Any] | None = None,
                  hierarchy: dict[str, Any] | None = None,
                  tree: dict[str, Any] | None = None,
                  editable: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach the analysis the viewer can act on.

    Everything here was already computed for the report. Sending it to the viewer
    is what turns a picture into a review surface: rule results become clickable
    markers, nets become traceable, and the routing grid becomes visible.
    """
    payload["markers"] = markers_payload(drc)
    payload["nets"] = nets_payload(connectivity)
    payload["tracks"] = tracks_payload(pitch)
    payload["tree"] = cells_payload(hierarchy, tree)
    payload["netsAvailable"] = bool(payload["nets"])
    # Present only when the page can actually write a file back. A viewer that
    # offers a drawing tool it cannot save is worse than one that offers none.
    if editable:
        payload["editable"] = editable
    return payload
