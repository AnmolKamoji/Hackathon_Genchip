"""Structural defects, and the two counts the file reports about itself.

Everything here is detectable without a single number from the design rule manual,
which is what makes it worth separating from rule checking: these findings hold
whatever technology the layout is in.

**Order matters between the two polygon defects.** A bow-tie - an outline that
crosses itself - encloses equal positive and negative area, so its signed area
reads zero while it plainly covers silicon. Testing for zero area first would
misfile every bow-tie as a degenerate shape and the self-intersection count would
read zero on a layout full of them. So self-intersection is tested first.

**Off-grid is unavailable, not zero.** The measured grid computed here is the
greatest common divisor of every vertex coordinate, so by construction every shape
sits on it. Reporting "0 shapes off grid" against a grid derived from the shapes
themselves is a hollow result. A real off-grid count needs the technology's stated
manufacturing grid, and this manual states none.

**Drawn shapes and shape records are different measurements.** One walks the
hierarchy and expands every instance placement - what is physically drawn. The
other counts the records as stored in the file, instances unexpanded. A cell placed
four times contributes four drawn shapes per record and one record.
"""
from __future__ import annotations

from math import gcd
from pathlib import Path
from typing import Any


def _polygon_of(shape, db):
    if shape.is_box():
        return db.Polygon(shape.box)
    if shape.is_polygon():
        return shape.polygon
    if shape.is_path():
        return shape.path.polygon()
    return None


def shape_records(gds_path: str | Path) -> dict[str, Any]:
    """Shapes as stored in the file, with instances NOT expanded.

    Counted over every cell in the top cell's hierarchy, because that is the scope
    every other count uses; a cell outside it is excluded from the file's totals.
    """
    import klayout.db as db
    from analyzer.gds_parser import rank_top_cells

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]

    in_scope = {top.cell_index()} | set(top.called_cells())
    records = texts = instance_records = 0
    for index in in_scope:
        cell = layout.cell(index)
        for li in layout.layer_indexes():
            for shape in cell.shapes(li).each():
                if shape.is_text():
                    texts += 1
                else:
                    records += 1
        instance_records += cell.child_instances()
    return {"shape_records": records, "text_records": texts,
            "instance_records": instance_records,
            "cells_in_scope": len(in_scope),
            "basis": ("records as stored in the file, instance placements not "
                      "expanded")}


def geometry_integrity(gds_path: str | Path) -> dict[str, Any]:
    """Zero-area polygons, self-intersecting polygons and the measured grid."""
    import klayout.db as db
    from analyzer.gds_parser import rank_top_cells

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    dbu = float(layout.dbu)

    examined = 0
    zero_area: list[dict[str, Any]] = []
    self_intersecting: list[dict[str, Any]] = []
    grid = 0                                   # gcd accumulator, in dbu

    for li in layout.layer_indexes():
        info = layout.get_info(li)
        name = f"{info.layer}/{info.datatype}"
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            if shape.is_text():
                it.next()
                continue
            poly = _polygon_of(shape, db)
            if poly is None:
                it.next()
                continue
            poly = poly.transformed(trans)
            examined += 1

            for point in poly.each_point_hull():
                grid = gcd(grid, abs(int(point.x)))
                grid = gcd(grid, abs(int(point.y)))

            own = float(poly.area())
            # Merging one polygon with itself yields the area it actually covers.
            covered = float(db.Region(poly).merged().area())

            # Self-intersection FIRST: a bow-tie has own area 0 and covered area
            # greater than 0, and testing zero area first would claim it degenerate.
            if abs(covered - abs(own)) > 0.5:
                self_intersecting.append(
                    {"layer": name, "own_area_dbu": own, "covered_area_dbu": covered,
                     "at_dbu": [poly.bbox().left, poly.bbox().bottom]})
            elif covered == 0:
                zero_area.append(
                    {"layer": name, "at_dbu": [poly.bbox().left, poly.bbox().bottom]})
            it.next()

    return {
        "shapes_examined": examined,
        "zero_area_count": len(zero_area),
        "zero_area": zero_area[:50],
        "self_intersecting_count": len(self_intersecting),
        "self_intersecting": self_intersecting[:50],
        "measured_grid_um": round(grid * dbu, 9) if grid else None,
        "measured_grid_nm": round(grid * dbu * 1000, 6) if grid else None,
        "measured_grid_basis": ("the greatest common divisor of every vertex "
                                "coordinate: the coarsest grid all geometry sits on"),
        # Deliberately not a count. See the module docstring.
        "off_grid": {
            "available": False,
            "count": None,
            "reason": ("Off-grid detection compares against the technology's stated "
                       "manufacturing grid, and the design rule manual states none. "
                       "Every shape sits on the measured grid by construction, so a "
                       "zero here would be a hollow result."),
        },
        "notches_and_slivers": {
            "available": False,
            "reason": ("A notch is a defect only below a stated depth and a sliver "
                       "only below a stated width; the manual states neither. The "
                       "measured minimum width and spacing per layer are reported "
                       "instead."),
        },
    }
