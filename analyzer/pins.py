"""Pins: which labels name a port, where they sit, and what a router can land on.

A GDSII pin is two unrelated things that the format never joins up. There is a
*label* - a text record carrying the port's name - and there are *shapes* on a pin
layer that a router may land on. Nothing in the file says which shape belongs to
which label. They are joined here geometrically: a shape is an access shape for a
pin when it contains one of that pin's label points.

That is why the pin count is a count of distinct label *strings* rather than of
shapes. A port drawn as four separate landing rectangles is one pin, and counting
shapes would report four. The fallback exists for the opposite case: pin layers
carrying geometry but no labels at all, where the shape count is the only pin
evidence in the file and the basis says so.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analyzer.connectivity import layer_roles

# The layer map's own suffix convention for a pin layer. `-PIN-LABEL` is listed
# before `-PIN` only for readability; both are matched by suffix.
PIN_SUFFIXES = ("-PIN-LABEL", "-PIN", "_PIN")


def pin_layer_names(layermap: dict[str, Any] | None,
                    role_overrides: dict[str, str] | None = None) -> list[str]:
    """The technology's pin layers, by name.

    A layer qualifies on its map role or on its name suffix. Without a layer map
    there is no answer at all - not an empty list, which would read as "this
    technology has no pins".
    """
    if not layermap:
        return []
    names = []
    roles = layer_roles(layermap, role_overrides)
    for key, entry in (layermap.get("by_key") or {}).items():
        name = entry.get("technology_name") or ""
        meta = roles.get(key) or {}
        if meta.get("lyp_role") == "pin" or name.upper().endswith(PIN_SUFFIXES):
            names.append(name)
    return sorted(set(names))


def analyze_pins(outlines: dict[str, Any], layermap: dict[str, Any] | None,
                 role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Every pin in the layout: its labels, its positions and its access shapes.

    `outlines` is the flattened geometry the viewer already reads, so nothing is
    parsed twice. Coordinates are rounded to 6 decimal places and sorted, which is
    what lets two revisions be compared without the answer depending on the order
    the labels happened to be stored in.
    """
    layers = pin_layer_names(layermap, role_overrides)
    if not layers:
        # No layer map, or a map that names no pin layer: pin-ness is undeterminable.
        return {"available": False,
                "reason": ("The technology layer map names no pin layer, so which "
                           "labels are ports cannot be determined."),
                "count": None, "basis": None, "pins": [], "layers": []}

    wanted = set(layers)
    rows = [r for r in outlines.get("layers") or [] if r.get("name") in wanted]

    # Every label on a pin layer, with its transformed position.
    labels: dict[str, list[tuple[float, float]]] = {}
    label_layers: dict[str, set[str]] = {}
    for row in rows:
        for lab in row.get("labels") or []:
            text = str(lab.get("text") or "").strip()
            if not text:
                continue
            at = lab.get("at_um") or [0.0, 0.0]
            labels.setdefault(text, []).append((round(float(at[0]), 6),
                                                round(float(at[1]), 6)))
            label_layers.setdefault(text, set()).add(row["name"])

    pin_shapes = sum(len(r.get("shapes") or []) for r in rows)

    if not labels:
        if pin_shapes:
            return {"available": True, "count": pin_shapes,
                    "basis": ("pin-layer shape count: the pin layers carry geometry "
                              "but no labels, so no port name is recoverable"),
                    "pins": [], "layers": layers, "label_count": 0,
                    "shape_count": pin_shapes}
        # Pin layers exist in the technology and this layout uses none of them.
        # That is a measured zero, not a missing value.
        return {"available": True, "count": 0,
                "basis": "distinct label strings on the technology's pin layers",
                "pins": [], "layers": layers, "label_count": 0, "shape_count": 0}

    pins = []
    for name in sorted(labels):
        points = sorted(set(labels[name]))
        access = _access(outlines, rows, points)
        pins.append({
            "name": name,
            "positions": [list(p) for p in points],
            "label_count": len(labels[name]),
            "label_layers": sorted(label_layers[name]),
            "access_shapes": access["count"],
            "access_layers": access["layers"],
        })

    return {"available": True, "count": len(pins),
            "basis": "distinct label strings on the technology's pin layers",
            "pins": pins, "layers": layers,
            "label_count": sum(len(v) for v in labels.values()),
            "shape_count": pin_shapes}


def _access(outlines: dict[str, Any], pin_rows: list[dict[str, Any]],
            points: list[tuple[float, float]]) -> dict[str, Any]:
    """Which pin-layer shapes a router could land on for this pin.

    A shape counts when it contains one of the pin's label points. This is the only
    join available: the shape carries no name, the label carries no extent.
    """
    count = 0
    layers: set[str] = set()
    for row in pin_rows:
        for shape in row.get("shapes") or []:
            outline = shape.get("outline_um") or []
            if not outline:
                continue
            if any(_inside(px, py, outline) for px, py in points):
                count += 1
                layers.add(row["name"])
    return {"count": count, "layers": sorted(layers)}


def _inside(px: float, py: float, outline: list[list[float]]) -> bool:
    """Ray casting, with the boundary counting as inside.

    A label sits on the edge of its landing rectangle often enough that excluding
    the boundary loses real access shapes.
    """
    inside = False
    n = len(outline)
    for i in range(n):
        x1, y1 = outline[i]
        x2, y2 = outline[i - 1]
        # On the edge: accept immediately rather than let the parity test decide.
        if (abs((x2 - x1) * (py - y1) - (px - x1) * (y2 - y1)) < 1e-12
                and min(x1, x2) - 1e-12 <= px <= max(x1, x2) + 1e-12
                and min(y1, y2) - 1e-12 <= py <= max(y1, y2) + 1e-12):
            return True
        if (y1 > py) != (y2 > py):
            x_at = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            if px < x_at:
                inside = not inside
    return inside
