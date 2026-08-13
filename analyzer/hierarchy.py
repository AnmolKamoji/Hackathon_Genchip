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
