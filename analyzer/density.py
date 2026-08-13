"""Density map: how much of each window a layer covers.

Density is the one geometric measure that is about *distribution* rather than about
any single shape, which is why a number for the whole cell is nearly useless. What a
fill or CMP rule cares about is the worst window, so this reports per-window coverage
and names the extremes.

The windows tile the cell from its own bounding box, so the same layout always
produces the same tiling and two runs can be compared. Coverage is computed on merged
regions - overlapping shapes on one layer cover their union, not twice their area,
which is the difference between a density figure and a sum of areas.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def tile_density(region, box, dbu: float, window_nm: float,
                 max_tiles: int = 40_000) -> dict[str, Any]:
    """Coverage per window over the given box.

    Plain tiling rather than KLayout's tiling processor: these cells are small, and a
    loop anyone can check by hand beats a parallel implementation nobody can.
    """
    import klayout.db as db

    step = max(1, int(round(window_nm / 1000.0 / dbu)))
    across = max(1, -(-box.width() // step))
    down = max(1, -(-box.height() // step))
    if across * down > max_tiles:
        return {"available": False,
                "reason": (f"a {window_nm:g} nm window over this layout is "
                           f"{across} × {down} tiles, past the {max_tiles} limit - "
                           "use a larger window")}

    merged = region.dup()
    merged.merge()
    tiles = []
    covered_total = 0.0
    y = box.bottom
    while y < box.top:
        x = box.left
        while x < box.right:
            tile = db.Box(x, y, min(x + step, box.right), min(y + step, box.top))
            clipped = merged & db.Region(tile)
            covered = float(clipped.area())
            area = float(tile.area())
            pct = round(100.0 * covered / area, 4) if area else 0.0
            tiles.append({
                "box": [round(tile.left * dbu, 6), round(tile.bottom * dbu, 6),
                        round(tile.right * dbu, 6), round(tile.top * dbu, 6)],
                "pct": pct,
                "col": int((x - box.left) // step),
                "row": int((y - box.bottom) // step),
            })
            covered_total += covered
            x += step
        y += step

    percentages = [t["pct"] for t in tiles] or [0.0]
    whole = float(box.area())
    ordered = sorted(tiles, key=lambda t: -t["pct"])
    return {
        "available": True,
        "window_nm": window_nm,
        "tiles": tiles,
        "tile_count": len(tiles),
        "columns": max(t["col"] for t in tiles) + 1 if tiles else 0,
        "rows": max(t["row"] for t in tiles) + 1 if tiles else 0,
        "min_pct": round(min(percentages), 4),
        "max_pct": round(max(percentages), 4),
        "mean_pct": round(sum(percentages) / len(percentages), 4),
        "overall_pct": round(100.0 * covered_total / whole, 4) if whole else 0.0,
        "densest": ordered[:5],
        "sparsest": ordered[-5:][::-1],
    }


def density_map(gds_path: str | Path, layermap: dict[str, Any] | None,
                layers: list[str] | None = None, window_nm: float = 100.0,
                combine: bool = False) -> dict[str, Any]:
    """Per-window coverage for each named layer, or for all of them together."""
    import klayout.db as db

    names = {entry["technology_name"]: key
             for key, entry in ((layermap or {}).get("by_key") or {}).items()}
    layout = db.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise ValueError("GDS contains no top-level cell.")
    dbu = float(layout.dbu)
    box = top.bbox()

    wanted = layers or sorted(names)
    maps = {}
    missing = []
    combined = db.Region()
    for name in wanted:
        key = names.get(name)
        index = layout.find_layer(key[0], key[1]) if key else None
        if index is None:
            missing.append(name)
            continue
        region = db.Region(top.begin_shapes_rec(index))
        if region.is_empty():
            missing.append(name)
            continue
        if combine:
            combined.insert(region)
            continue
        maps[name] = tile_density(region, box, dbu, window_nm)

    if combine:
        maps["(combined)"] = tile_density(combined, box, dbu, window_nm)

    return {
        "top_cell": top.name,
        "window_nm": window_nm,
        "bbox_um": [round(box.left * dbu, 6), round(box.bottom * dbu, 6),
                    round(box.right * dbu, 6), round(box.top * dbu, 6)],
        "layers": maps,
        "layers_without_geometry": missing,
        "basis": ("coverage of merged geometry per window, tiled from the cell's own "
                  "bounding box"),
        "not_derivable": {
            "fill_requirements": ("What density a process requires is a rule, not a "
                                  "measurement. Supply a deck with a density rule to "
                                  "check against it."),
        },
    }
