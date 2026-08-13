"""The 2.5D view: the layout given a third dimension it does not have.

GDSII stores no elevations. Every other module in this repository says so as a
limitation; this one turns it into an input. Supply a stack file - an elevation and a
thickness per layer - and each polygon becomes a slab, which is what KLayout's 2.5D
view draws.

Nothing is inferred. A layer the stack does not mention is not drawn and is listed as
such, because a made-up elevation produces a picture that looks authoritative and is
wrong about the one thing the picture exists to show.

Stack file (JSON):

    {"technology": "...",
     "layers": {"M0": {"elevation_nm": 100, "thickness_nm": 30},
                "VIA0": {"elevation_nm": 130, "thickness_nm": 20}}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_SLABS = 4000


def load_stack3d(path: str | Path) -> dict[str, Any]:
    """Read a 2.5D stack file, with the errors named rather than raised as KeyError."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = data.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise ValueError("the stack file has no 'layers' object")
    clean = {}
    for name, entry in layers.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: expected an object with elevation_nm and thickness_nm")
        if "elevation_nm" not in entry or "thickness_nm" not in entry:
            raise ValueError(f"{name}: needs both elevation_nm and thickness_nm")
        thickness = float(entry["thickness_nm"])
        if thickness <= 0:
            raise ValueError(f"{name}: thickness_nm must be positive")
        clean[name] = {"elevation_nm": float(entry["elevation_nm"]),
                       "thickness_nm": thickness,
                       "colour": entry.get("colour")}
    return {"technology": data.get("technology"),
            "source": str(path),
            "layers": clean,
            "layer_count": len(clean)}


def build_slabs(gds_path: str | Path, layermap: dict[str, Any] | None,
                stack: dict[str, Any], layers: list[str] | None = None,
                merge: bool = True, max_slabs: int = MAX_SLABS) -> dict[str, Any]:
    """Every polygon as a slab: its outline, its bottom and its top.

    Merged per layer by default. A standard cell has hundreds of small rectangles and
    a browser drawing each one separately spends its time on geometry nobody can see;
    merging first is also what makes the picture readable.
    """
    import klayout.db as db

    names = {entry["technology_name"]: key
             for key, entry in ((layermap or {}).get("by_key") or {}).items()}
    colours = {entry["technology_name"]: entry.get("fill_color")
               for entry in ((layermap or {}).get("by_key") or {}).values()}
    layout = db.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise ValueError("GDS contains no top-level cell.")
    dbu = float(layout.dbu)
    box = top.bbox()

    stack_layers = stack.get("layers") or {}
    wanted = layers or sorted(stack_layers)
    slabs = []
    drawn, skipped, unstacked = [], [], []
    truncated = False

    for name in wanted:
        entry = stack_layers.get(name)
        if not entry:
            unstacked.append(name)
            continue
        key = names.get(name)
        index = layout.find_layer(key[0], key[1]) if key else None
        if index is None:
            skipped.append(name)
            continue
        region = db.Region(top.begin_shapes_rec(index))
        if region.is_empty():
            skipped.append(name)
            continue
        if merge:
            region.merge()
        bottom = entry["elevation_nm"]
        height = entry["thickness_nm"]
        count = 0
        for polygon in region.each():
            if len(slabs) >= max_slabs:
                truncated = True
                break
            slabs.append({
                "layer": name,
                "colour": entry.get("colour") or colours.get(name) or "#8aa0b6",
                "outline_um": [[round(p.x * dbu, 6), round(p.y * dbu, 6)]
                               for p in polygon.each_point_hull()],
                "bottom_nm": bottom,
                "top_nm": bottom + height,
            })
            count += 1
        if count:
            drawn.append({"layer": name, "slabs": count,
                          "elevation_nm": bottom, "thickness_nm": height})

    # Layers the file has but the stack does not mention: named, never guessed.
    in_file = {name for name in names
               if layout.find_layer(*names[name]) is not None}
    missing_from_stack = sorted(in_file - set(stack_layers))

    return {
        "available": bool(slabs),
        "top_cell": top.name,
        "bbox_um": [round(box.left * dbu, 6), round(box.bottom * dbu, 6),
                    round(box.right * dbu, 6), round(box.top * dbu, 6)],
        "slabs": slabs,
        "slab_count": len(slabs),
        "truncated": truncated,
        "layers_drawn": drawn,
        "layers_without_geometry": skipped,
        "layers_not_in_the_stack": missing_from_stack,
        "layers_requested_without_a_stack_entry": unstacked,
        "height_nm": max((s["top_nm"] for s in slabs), default=0),
        "stack_source": stack.get("source"),
        "basis": ("outlines measured from the layout; elevation and thickness from the "
                  "supplied stack file"),
        "not_derivable": {
            "elevations": ("GDSII stores no Z. Every height here comes from the stack "
                           "file - change the file and the picture changes, because "
                           "the layout says nothing about it."),
            "shape": ("Slabs are drawn with vertical walls. Real deposition has "
                      "sloped sidewalls and dishing; neither is in a GDSII."),
        },
    }


def mesh(slabs: dict[str, Any]) -> list[dict[str, Any]]:
    """Triangles for each slab, ready for a 3D plot.

    Every outline is cut into convex pieces first. A fan triangulation of a concave
    polygon produces triangles outside it, which in a 3D view looks like metal where
    there is none - and the whole point of this view is showing where metal is.
    """
    import klayout.db as db

    out = []
    for slab in slabs.get("slabs") or []:
        # Decomposition is an integer-polygon operation, so the outline goes back to
        # database units for the cut and returns to micrometres for the mesh.
        scale = 1000.0                      # nanometre grid is fine enough to mesh on
        polygon = db.Polygon([db.Point(round(x * scale), round(y * scale))
                              for x, y in slab["outline_um"]])
        pieces = polygon.decompose_convex() or [polygon]
        vertices: list[list[float]] = []
        triangles: list[list[int]] = []
        bottom, top = slab["bottom_nm"] / 1000.0, slab["top_nm"] / 1000.0
        for piece in pieces:
            # decompose_convex returns SimplePolygons, which have each_point rather
            # than each_point_hull - there is no hole to distinguish.
            walk = (piece.each_point_hull if hasattr(piece, "each_point_hull")
                    else piece.each_point)
            points = [(p.x / scale, p.y / scale) for p in walk()]
            if len(points) < 3:
                continue
            base = len(vertices)
            for x, y in points:
                vertices.append([x, y, bottom])
            for x, y in points:
                vertices.append([x, y, top])
            count = len(points)
            # bottom and top faces, fan from the first vertex of this convex piece
            for i in range(1, count - 1):
                triangles.append([base, base + i, base + i + 1])
                triangles.append([base + count, base + count + i, base + count + i + 1])
            # side walls
            for i in range(count):
                j = (i + 1) % count
                triangles.append([base + i, base + j, base + count + i])
                triangles.append([base + j, base + count + j, base + count + i])
        if triangles:
            out.append({"layer": slab["layer"], "colour": slab["colour"],
                        "vertices": vertices, "triangles": triangles,
                        "bottom_nm": slab["bottom_nm"], "top_nm": slab["top_nm"]})
    return out
