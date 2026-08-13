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
            "xorAreaUm2": summary.get("xor_area_um2"),
            "removedAreaUm2": summary.get("removed_area_um2"),
            "addedAreaUm2": summary.get("added_area_um2"),
            "regionCount": len(regions),
        },
        "names": {"a": a_payload.get("file") or "A", "b": b_payload.get("file") or "B"},
    }


def to_json(payload: dict[str, Any]) -> str:
    """Compact JSON for embedding. Separators matter: the default ones add a space
    per field, which on a few thousand vertices is a measurable slice of the page."""
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)
