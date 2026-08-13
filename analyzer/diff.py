"""Structural diff of two layouts: cells, shapes, instances and texts.

This is not the XOR. The XOR answers "what geometry differs?" by merging every layer
and subtracting - which is the right question for mask impact and the wrong one for
"what did the last edit change?". Two files can XOR to nothing and still differ:
a shape split into two, a cell renamed, an instance replaced by flattened geometry, a
label moved by nothing. This finds those.

It compares cell by cell in each cell's own coordinates, the way KLayout's Diff Tool
does, so a change inside a cell is reported once rather than once per placement.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_EXAMPLES = 50


def _shape_key(shape, dbu: float):
    """A shape as a comparable value: kind and exact integer geometry."""
    import klayout.db as db

    if shape.is_text():
        text = shape.text
        return ("text", text.string, text.x, text.y)
    if shape.is_box():
        polygon = db.Polygon(shape.box)
    elif shape.is_polygon():
        polygon = shape.polygon
    elif shape.is_path():
        polygon = shape.path.polygon()
    else:
        return None
    return ("poly", tuple((p.x, p.y) for p in polygon.each_point_hull()))


def _describe(key, dbu: float) -> dict[str, Any]:
    if key[0] == "text":
        return {"kind": "text", "text": key[1],
                "at_um": [round(key[2] * dbu, 6), round(key[3] * dbu, 6)]}
    points = key[1]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {"kind": "polygon", "vertices": len(points),
            "bbox_um": [round(min(xs) * dbu, 6), round(min(ys) * dbu, 6),
                        round(max(xs) * dbu, 6), round(max(ys) * dbu, 6)]}


def _instance_key(inst, dbu: float, layout):
    child = layout.cell(inst.cell_index)
    return (child.name if child else str(inst.cell_index), inst.trans.to_s(), inst.size())


def _counts(bag_a: dict, bag_b: dict):
    """Multiset difference: what is in A and not B, and the other way round."""
    only_a, only_b = {}, {}
    for key, count in bag_a.items():
        extra = count - bag_b.get(key, 0)
        if extra > 0:
            only_a[key] = extra
    for key, count in bag_b.items():
        extra = count - bag_a.get(key, 0)
        if extra > 0:
            only_b[key] = extra
    return only_a, only_b


def diff(path_a: str | Path, path_b: str | Path,
         layermap: dict[str, Any] | None = None,
         max_examples: int = MAX_EXAMPLES) -> dict[str, Any]:
    """Compare two layouts structurally."""
    import klayout.db as db

    names = {key: entry["technology_name"]
             for key, entry in ((layermap or {}).get("by_key") or {}).items()}

    layouts = []
    for path in (path_a, path_b):
        layout = db.Layout()
        layout.read(str(path))
        layouts.append(layout)
    a, b = layouts
    dbu = float(a.dbu)
    dbu_differs = abs(float(a.dbu) - float(b.dbu)) > 1e-15

    cells_a = {cell.name for cell in a.each_cell()}
    cells_b = {cell.name for cell in b.each_cell()}
    shared = sorted(cells_a & cells_b)

    cell_reports = []
    totals = {"shapes_only_in_a": 0, "shapes_only_in_b": 0,
              "texts_only_in_a": 0, "texts_only_in_b": 0,
              "instances_only_in_a": 0, "instances_only_in_b": 0}

    for name in shared:
        cell_a, cell_b = a.cell(name), b.cell(name)
        layer_rows = []

        keys = {(a.get_info(i).layer, a.get_info(i).datatype) for i in a.layer_indexes()}
        keys |= {(b.get_info(i).layer, b.get_info(i).datatype) for i in b.layer_indexes()}
        for key in sorted(keys):
            index_a = a.find_layer(key[0], key[1])
            index_b = b.find_layer(key[0], key[1])
            bag_a: dict[Any, int] = {}
            bag_b: dict[Any, int] = {}
            if index_a is not None:
                for shape in cell_a.shapes(index_a).each():
                    entry = _shape_key(shape, dbu)
                    if entry:
                        bag_a[entry] = bag_a.get(entry, 0) + 1
            if index_b is not None:
                for shape in cell_b.shapes(index_b).each():
                    entry = _shape_key(shape, dbu)
                    if entry:
                        bag_b[entry] = bag_b.get(entry, 0) + 1
            only_a, only_b = _counts(bag_a, bag_b)
            if not only_a and not only_b:
                continue
            shapes_a = sum(v for k, v in only_a.items() if k[0] == "poly")
            shapes_b = sum(v for k, v in only_b.items() if k[0] == "poly")
            texts_a = sum(v for k, v in only_a.items() if k[0] == "text")
            texts_b = sum(v for k, v in only_b.items() if k[0] == "text")
            totals["shapes_only_in_a"] += shapes_a
            totals["shapes_only_in_b"] += shapes_b
            totals["texts_only_in_a"] += texts_a
            totals["texts_only_in_b"] += texts_b
            layer_rows.append({
                "layer": key[0], "datatype": key[1],
                "name": names.get(key, f"layer_{key[0]}_{key[1]}"),
                "shapes_only_in_a": shapes_a, "shapes_only_in_b": shapes_b,
                "texts_only_in_a": texts_a, "texts_only_in_b": texts_b,
                "examples_only_in_a": [_describe(k, dbu) for k in list(only_a)[:max_examples]],
                "examples_only_in_b": [_describe(k, dbu) for k in list(only_b)[:max_examples]],
            })

        inst_a: dict[Any, int] = {}
        inst_b: dict[Any, int] = {}
        for inst in cell_a.each_inst():
            key = _instance_key(inst, dbu, a)
            inst_a[key] = inst_a.get(key, 0) + 1
        for inst in cell_b.each_inst():
            key = _instance_key(inst, dbu, b)
            inst_b[key] = inst_b.get(key, 0) + 1
        only_ia, only_ib = _counts(inst_a, inst_b)
        totals["instances_only_in_a"] += sum(only_ia.values())
        totals["instances_only_in_b"] += sum(only_ib.values())

        if layer_rows or only_ia or only_ib:
            cell_reports.append({
                "cell": name,
                "layers": layer_rows,
                "instances_only_in_a": [{"cell": k[0], "trans": k[1], "copies": k[2]}
                                        for k in list(only_ia)[:max_examples]],
                "instances_only_in_b": [{"cell": k[0], "trans": k[1], "copies": k[2]}
                                        for k in list(only_ib)[:max_examples]],
            })

    identical = (not cell_reports and cells_a == cells_b and not dbu_differs)
    return {
        "available": True,
        "a": Path(path_a).name,
        "b": Path(path_b).name,
        "identical": identical,
        "headline": ("the two files are structurally identical" if identical else
                     "the two files differ"),
        "dbu_um": {"a": float(a.dbu), "b": float(b.dbu), "differs": dbu_differs},
        "cells_only_in_a": sorted(cells_a - cells_b),
        "cells_only_in_b": sorted(cells_b - cells_a),
        "cells_compared": len(shared),
        "cells_that_differ": cell_reports,
        "totals": totals,
        "basis": ("shape-for-shape comparison in each cell's own coordinates, on exact "
                  "database units"),
        "difference_from_xor": (
            "A structural diff, not an XOR. Two files can XOR to nothing and still "
            "differ here - a shape split in two, a cell renamed, geometry flattened "
            "out of an instance - and that is usually what a reviewer wants to know "
            "after an edit."),
    }
