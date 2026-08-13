"""Cell hierarchy analysis: depth, empty cells, orphans, recursion, references.

All of this is GDS-only and exact - the structure is recorded in the file, so
nothing here is inferred. A `.lyp` adds nothing.

One definition worth stating, because "orphan" is used loosely. A cell with no
callers *is* a top cell as far as GDSII is concerned. What makes it an orphan is
that a second such cell exists alongside the one being analysed, so its geometry
is never reached from the design being reviewed. That is reported as an orphan; a
sole top cell never is.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .gds_parser import rank_top_cells


def analyze_hierarchy(gds_path: str | Path) -> dict[str, Any]:
    """Describe the cell hierarchy of a layout."""
    import klayout.db as db

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    primary = tops[0]

    cells: list[dict[str, Any]] = []
    for cell in layout.each_cell():
        shape_count = sum(cell.shapes(li).size() for li in layout.layer_indexes())
        child_placements = sum(inst.size() for inst in cell.each_inst())
        callers = list(cell.caller_cells())
        called = list(cell.called_cells())
        cells.append({
            "name": cell.name,
            "index": cell.cell_index(),
            "shape_count": shape_count,
            "child_instance_placements": child_placements,
            "child_instance_records": len(list(cell.each_inst())),
            "distinct_child_cells": len(called),
            "caller_count": len(callers),
            # Levels of hierarchy below this cell; 0 means it contains no instances.
            "levels_below": cell.hierarchy_levels(),
            "is_top": cell.cell_index() in {t.cell_index() for t in tops},
            "is_empty": shape_count == 0 and child_placements == 0,
            "is_proxy": cell.is_proxy(),
        })
    cells.sort(key=lambda c: c["name"])

    by_index = {c["index"]: c for c in cells}
    in_scope = {primary.cell_index()} | set(primary.called_cells())

    empty = [c["name"] for c in cells if c["is_empty"]]
    # Unreachable from the cell being analysed.
    orphans = [c["name"] for c in cells if c["index"] not in in_scope]

    # GDSII cannot legally express a recursive reference and KLayout rejects one
    # on read, so finding a cycle here means something very unusual. Checked
    # anyway rather than assumed.
    cycles = []
    for cell in layout.each_cell():
        if cell.cell_index() in set(cell.called_cells()):
            cycles.append(cell.name)

    # A reference to a cell that does not exist cannot survive a KLayout read
    # either - it would become a proxy or fail. Proxies are surfaced so a library
    # reference that failed to resolve is visible rather than silent.
    unresolved = [c["name"] for c in cells if c["is_proxy"]]

    depth = primary.hierarchy_levels()
    return {
        "availability": "GDS-only",
        "basis": "the cell structure recorded in the GDSII file; nothing is inferred",
        "top_cell": primary.name,
        "top_cell_count": len(tops),
        "top_cells": [t.name for t in tops],
        "cell_count_total": len(cells),
        "cell_count_in_scope": len(in_scope),
        "max_depth_below_top": depth,
        "depth_description": ("flat - the top cell contains no instances" if depth == 0 else
                              f"{depth} level(s) of instantiation below the top cell"),
        "cells": cells,
        "empty_cells": empty,
        "orphan_cells": orphans,
        "recursive_cells": cycles,
        "unresolved_reference_cells": unresolved,
        "warnings": _warnings(tops, empty, orphans, cycles, unresolved),
        "not_derivable": {
            "cell_purpose": ("What a cell is *for* - standard cell, macro, filler - is not recorded "
                             "in GDSII. Naming may hint at it. Requires a library or Liberty file."),
        },
    }


def instance_tree(gds_path: str | Path, max_placements: int = 3000) -> dict[str, Any]:
    """Every cell placement, with the box it occupies in top-cell coordinates.

    This is what a cell-tree navigator needs and `analyze_hierarchy` does not give:
    the counts there describe the *structure*, but a reviewer clicking a cell name
    wants to be taken to where that copy sits on the screen. Transforms are
    accumulated down the tree, so a box is where the instance really lands - not
    where its cell was defined.

    The geometry itself is not repeated per cell: the viewer draws a flattened
    layout, so an instance contributes a boundary and a name, not another copy of
    its polygons. `max_placements` caps the walk because an array-heavy block can
    hold millions of placements and none of them would be legible.
    """
    import klayout.db as db

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    primary = tops[0]
    dbu = layout.dbu

    def box_um(box) -> list[float] | None:
        if box.empty():
            return None
        return [round(box.left * dbu, 6), round(box.bottom * dbu, 6),
                round(box.right * dbu, 6), round(box.top * dbu, 6)]

    cells = []
    for cell in layout.each_cell():
        cells.append({
            "name": cell.name,
            "shapes": sum(cell.shapes(li).size() for li in layout.layer_indexes()),
            "bbox": box_um(cell.bbox()),
            "levels": cell.hierarchy_levels(),
            "placements": sum(inst.size() for inst in cell.each_inst()),
            "isTop": cell.cell_index() == primary.cell_index(),
        })
    cells.sort(key=lambda c: (not c["isTop"], c["name"]))

    placements: list[dict[str, Any]] = []
    truncated = False

    def walk(cell, trans, depth: int, path: str) -> None:
        nonlocal truncated
        for inst in cell.each_inst():
            child = layout.cell(inst.cell_index)
            for local in inst.cell_inst.each_cplx_trans():
                if len(placements) >= max_placements:
                    truncated = True
                    return
                world = trans * local
                here = f"{path}/{child.name}"
                placements.append({
                    "id": len(placements),
                    "cell": child.name,
                    "parent": cell.name,
                    "path": here,
                    "depth": depth,
                    "bbox": box_um(child.bbox().transformed(world)),
                    # Orientation of this copy, not its position: the box already
                    # says where it is, and "R90 mirrored" is what a reviewer reads
                    # off a placement. Magnification is only mentioned when it is
                    # not 1, because a magnified instance is unusual enough to notice.
                    "orient": (f"R{local.angle:g}"
                               + (" mirrored" if local.is_mirror() else "")
                               + (f" ×{local.mag:g}" if local.mag != 1 else "")),
                    "shapes": sum(child.shapes(li).size() for li in layout.layer_indexes()),
                })
                walk(child, world, depth + 1, here)

    walk(primary, db.ICplxTrans(), 1, primary.name)

    depth = primary.hierarchy_levels()
    return {
        "top": primary.name,
        "topBbox": box_um(primary.bbox()),
        "maxDepth": depth,
        "flat": depth == 0,
        "cells": cells,
        "placements": placements,
        "truncated": truncated,
        "note": ("flat - the top cell contains no instances" if depth == 0 else
                 f"{len(placements)} placement(s) across {depth} level(s)"
                 + (" (list truncated)" if truncated else "")),
    }


def _warnings(tops, empty, orphans, cycles, unresolved) -> list[str]:
    out = []
    if len(tops) > 1:
        out.append(
            f"{len(tops)} top-level cells exist ({', '.join(t.name for t in tops)}). "
            f"'{sorted(tops, key=lambda c: c.name)[0].name}' was analysed; the geometry of the "
            "others is not included in any figure reported here.")
    if empty:
        out.append(f"{len(empty)} cell(s) contain no shapes and no instances: {', '.join(empty[:8])}.")
    if orphans:
        out.append(
            f"{len(orphans)} cell(s) are not reachable from the analysed top cell, so their "
            f"geometry is excluded: {', '.join(orphans[:8])}.")
    if cycles:
        out.append(f"Recursive cell reference detected in: {', '.join(cycles)}. "
                   "This is not legal GDSII and the hierarchy figures cannot be trusted.")
    if unresolved:
        out.append(
            f"{len(unresolved)} cell(s) are proxies, meaning a reference could not be resolved to a "
            f"real cell definition: {', '.join(unresolved[:8])}.")
    return out
