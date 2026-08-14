"""Metal density, measured per level and never summed.

Levels stack. Four metal levels at 30% each are not "120% dense" - that figure
describes nothing physical, and a single summed number is the easiest way to
produce it by accident. So density is reported per level, and the two headline
figures are the densest level and the mean across levels.

Two denominators are involved and only one of them is the cell.

* **Global density** is the layer's merged area over the bounding-box area.
* **Local density** is measured on an 8x8 grid, and each tile uses *its own*
  integer box area. Using one nominal tile area for every tile made a fully
  covered tile read 100.19% - the grid does not divide the cell evenly, so the
  edge tiles are smaller than the middle ones.

Pin, label and duplicate copies are excluded from the metal totals. They repeat
another layer's geometry, so counting them counts the same metal twice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analyzer.connectivity import layer_roles

GRID = 8


def _percent(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 4) if whole else None


def analyze_density(gds_path: str | Path, layermap: dict[str, Any] | None,
                    role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Per-layer density with its tile distribution, and the per-level summary."""
    if not layermap:
        return {"available": False, "levels": [], "rows": [],
                "reason": ("Metal density needs the technology layer map to know "
                           "which layers are metal.")}

    import klayout.db as db
    from analyzer.gds_parser import rank_top_cells

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    dbu = float(layout.dbu)
    box = top.bbox()
    cell_area_dbu = float(box.width()) * float(box.height())
    if cell_area_dbu <= 0:
        return {"available": False, "levels": [], "rows": [],
                "reason": "the top cell has no area, so density has no denominator"}

    roles = layer_roles(layermap, role_overrides)
    rows: list[dict[str, Any]] = []
    levels: list[dict[str, Any]] = []

    for li in layout.layer_indexes():
        info = layout.get_info(li)
        meta = roles.get((info.layer, info.datatype)) or {}
        name = meta.get("name") or f"layer_{info.layer}_{info.datatype}"
        region = db.Region(top.begin_shapes_rec(li))
        region.merge()
        if region.is_empty():
            continue
        area_dbu = float(region.area())
        tiles = _tiles(db, region, box)
        row = {
            "layer": name,
            "role": meta.get("role", "unknown"),
            "copy of another layer": "yes" if meta.get("derived") else "no",
            "area (µm²)": round(area_dbu * dbu * dbu, 9),
            "global density": _percent(area_dbu, cell_area_dbu),
            "densest tile": tiles["densest"],
            "emptiest tile": tiles["emptiest"],
            "imbalance (pp)": (round(tiles["densest"] - tiles["emptiest"], 4)
                               if tiles["densest"] is not None else None),
            "hotspot tile": tiles["hotspot"],
        }
        rows.append(row)
        # Metal levels only, and never a derived copy of one.
        if meta.get("role") == "metal" and not meta.get("derived"):
            levels.append({"layer": name, "density": row["global density"]})

    densities = [l["density"] for l in levels if l["density"] is not None]
    return {
        "available": True,
        "rows": rows,
        "levels": levels,
        "densest_percent": round(max(densities), 4) if densities else None,
        "densest_level": (max(levels, key=lambda l: l["density"] or -1)["layer"]
                          if densities else None),
        "mean_percent": round(sum(densities) / len(densities), 4) if densities else None,
        "cell_area_um2": round(cell_area_dbu * dbu * dbu, 9),
        "basis": ("global density is merged layer area over the bounding-box area; "
                  f"local density is measured on a {GRID}x{GRID} grid, each tile "
                  "against its own area"),
        "verdict": {
            "available": False,
            "reason": ("The manual states no density limit, so the measurement is "
                       "reported without a pass or fail."),
        },
    }


def _tiles(db, region, box) -> dict[str, Any]:
    """The layer's density in each tile of the grid, each against its own area."""
    left, bottom = box.left, box.bottom
    width, height = box.width(), box.height()
    if width <= 0 or height <= 0:
        return {"densest": None, "emptiest": None, "hotspot": None}

    best, worst, hotspot = None, None, None
    for row in range(GRID):
        for col in range(GRID):
            # Integer edges, so the tiles tile the box exactly: the last column and
            # row absorb the remainder rather than being dropped.
            x0 = left + width * col // GRID
            x1 = left + width * (col + 1) // GRID
            y0 = bottom + height * row // GRID
            y1 = bottom + height * (row + 1) // GRID
            tile = db.Box(x0, y0, x1, y1)
            tile_area = float(tile.width()) * float(tile.height())
            if tile_area <= 0:
                continue
            covered = float((region & db.Region(tile)).merged().area())
            value = _percent(covered, tile_area)
            if value is None:
                continue
            if best is None or value > best:
                best, hotspot = value, [col, row]
            if worst is None or value < worst:
                worst = value
    return {"densest": best, "emptiest": worst, "hotspot": hotspot}
