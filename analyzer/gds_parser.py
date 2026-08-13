from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .layermap import annotate_layers

# Sentinel used in metadata for "this fact is not recoverable from the input".
# It is deliberately distinct from 0 so the Q&A layer can say "unavailable"
# instead of reporting a measured zero.
UNAVAILABLE = None


def _layer_name(info) -> str:
    return info.name or f"layer_{info.layer}"


def geometry_fingerprint(merged_region, dbu_um: float) -> str:
    """A stable digest of a merged region's actual shapes.

    Counts and total area can both be unchanged while every shape has moved, so
    a comparison built only on those reports "no differences" for a layout that
    really did change. Coordinates are normalised to picometres so the digest is
    independent of each file's database unit.
    """
    scale = dbu_um * 1e6  # dbu -> um, then um -> pm below
    parts = []
    for poly in merged_region.each():
        pts = []
        for p in poly.each_point_hull():
            pts.append((round(p.x * scale * 1e6), round(p.y * scale * 1e6)))
        holes = []
        for h in range(poly.holes()):
            holes.append(tuple(sorted(
                (round(p.x * scale * 1e6), round(p.y * scale * 1e6))
                for p in poly.each_point_hole(h))))
        parts.append((tuple(pts), tuple(sorted(holes))))
    parts.sort()
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]


def _vias_from_layermap(layers: list[dict[str, Any]],
                        layermap: dict[str, Any] | None) -> dict[str, Any]:
    """Count via shapes using the layer roles a .lyp implies.

    Returns empty when there is no layer map: a bare `.gds` says nothing about
    which geometry is a via, and a confident `0` there would be a wrong answer.

    Contacts are counted separately rather than folded in. `NDIFFCON` is a contact
    by name, and lumping it into the via total would have disagreed with the
    sidecar's own via count on every reference file.
    """
    if not layermap:
        return {"source": None, "per_layer": {}, "roles": {}, "via_shapes": None,
                "contact_shapes": None, "via_layers": [], "contact_layers": []}

    # Imported here rather than at module scope: connectivity imports this module
    # for rank_top_cells, so a top-level import would be circular.
    from .connectivity import layer_roles

    roles = layer_roles(layermap)
    per_layer: dict[tuple[int, int], int] = {}
    role_of: dict[tuple[int, int], str] = {}
    via_layers: list[str] = []
    contact_layers: list[str] = []
    via_shapes = contact_shapes = 0
    for row in layers:
        key = (row["layer"], row["datatype"])
        role = (roles.get(key) or {}).get("role")
        if role not in ("via", "contact"):
            continue
        # Count shapes, not merged polygons: each drawn via is one via.
        count = row["shape_count"] - (row.get("text_count") or 0)
        per_layer[key] = count
        role_of[key] = role
        # Take the name from the layer map, not from the row: this runs before the
        # rows are annotated, so row["name"] is still the `layer_111` placeholder.
        name = roles[key]["name"]
        if role == "via":
            via_shapes += count
            if count:
                via_layers.append(name)
        else:
            contact_shapes += count
            if count:
                contact_layers.append(name)
    return {
        "source": "layer names in the .lyp (a naming convention, not an explicit flag)",
        "per_layer": per_layer, "roles": role_of,
        "via_shapes": via_shapes, "contact_shapes": contact_shapes,
        "via_layers": via_layers, "contact_layers": contact_layers,
    }


def rank_top_cells(layout) -> list:
    """Top cells ordered best-first: most content, then name.

    Sorting purely by name was reproducible but could hand back a *trivial* top
    cell. A library GDS whose alphabetically-first top cell is an empty
    placeholder was reported as a near-empty design, with every real cell listed
    as unreachable. Content comes first, and the name only breaks ties, so the
    choice stays deterministic.

    Every module that has to pick one top cell uses this, so the parser,
    connectivity and measurements can never disagree about which cell they mean.
    """
    tops = list(layout.top_cells())

    def content(cell) -> int:
        # Own shapes plus those of every cell it reaches. Instance multiplicity is
        # ignored: this only has to rank, not measure.
        reachable = [cell.cell_index(), *cell.called_cells()]
        return sum(layout.cell(i).shapes(li).size()
                   for i in reachable for li in layout.layer_indexes())

    return sorted(tops, key=lambda c: (-content(c), c.name))


def _box_dims(box, dbu_um: float) -> tuple[float, float]:
    """Width and height in microns, or (0, 0) for an empty box.

    KLayout represents an empty bounding box with sentinel extremes, so a cell
    holding no shapes otherwise reports a width of 2**32 dbu (4294967.294 um at
    dbu 0.001) and a nonsensical area.
    """
    if box.empty():
        return 0.0, 0.0
    return box.width() * dbu_um, box.height() * dbu_um


def analyze_gds(gds_path: str | Path, layermap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministically extract GDSII metadata using KLayout. See _analyze()."""
    return _analyze(gds_path, layermap)[0]


def analyze_gds_with_regions(gds_path: str | Path, layermap: dict[str, Any] | None = None):
    """Return (metadata, {(layer, datatype): klayout Region}).

    The regions let a caller union geometry across datatypes, which is required
    to get true coverage when a technology duplicates shapes onto several
    datatypes of the same layer.
    """
    return _analyze(gds_path, layermap)


def _analyze(gds_path: str | Path,
             layermap: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[tuple[int, int], Any]]:
    """Deterministically extract GDSII metadata using KLayout.

    The LLM is never used for numeric calculations. Geometry facts are computed
    here and serialized as JSON for downstream Q&A/review.

    Note on counting: KLayout normalizes a rectangular GDS ``BOUNDARY`` record
    into a ``Box`` rather than a ``Polygon``, so ``Shape.is_polygon()`` is false
    for axis-aligned rectangles. ``polygon_count`` therefore counts every
    non-text geometric shape (polygon, simple polygon, box, path), which is what
    a layout engineer means by "polygon" and what the semantic sidecars report
    as ``element: "boundary"``.
    """
    gds_path = Path(gds_path)
    try:
        import klayout.db as db
    except ImportError as exc:
        raise RuntimeError(
            "KLayout Python package is required for raw GDS parsing. Install requirements.txt first."
        ) from exc
    layout = db.Layout()
    layout.read(str(gds_path))

    top_cells = list(layout.top_cells())
    if not top_cells:
        raise ValueError("GDS does not contain a top-level cell.")

    warnings: list[str] = []
    top_cells = rank_top_cells(layout)
    top = top_cells[0]
    if len(top_cells) > 1:
        others = ", ".join(c.name for c in top_cells[1:])
        warnings.append(
            f"This GDS has {len(top_cells)} top-level cells. Only `{top.name}` was analyzed; "
            f"geometry under {others} is NOT included in any count, area or density below."
        )

    bbox = top.bbox()
    dbu_um = float(layout.dbu)
    width_um, height_um = _box_dims(bbox, dbu_um)
    bbox_area_um2 = width_um * height_um
    if bbox.empty():
        warnings.append(f"Top cell `{top.name}` contains no geometry; its bounding box is empty.")

    # Cells belonging to the analyzed top cell's hierarchy. Counting every cell
    # in the file would contradict the multi-top warning, which says geometry
    # under the other tops is excluded.
    in_scope = {top.cell_index()} | set(top.called_cells())

    # Records = shapes as stored, summed over the in-scope cells without
    # expanding instances. This is the count directly comparable with a semantic
    # sidecar, which lists elements per structure. `polygon_count` below stays
    # flattened (what is physically drawn), so both questions can be answered.
    record_polygons = record_texts = 0
    for ci in in_scope:
        cell = layout.cell(ci)
        for layer_index in layout.layer_indices():
            for shape in cell.shapes(layer_index).each():
                if shape.is_text():
                    record_texts += 1
                elif shape.is_polygon() or shape.is_box() or shape.is_path():
                    record_polygons += 1

    layers: list[dict[str, Any]] = []
    regions: dict[tuple[int, int], Any] = {}
    total_polygons = total_shapes = total_texts = 0

    for layer_index in layout.layer_indices():
        info = layout.get_info(layer_index)
        name = _layer_name(info)
        polygon_count = shape_count = text_count = 0
        region = db.Region()

        # KLayout returns a RecursiveShapeIterator here, not individual Shape
        # objects. Advance the iterator explicitly and fetch the current shape
        # with iterator.shape().
        iterator = top.begin_shapes_rec(layer_index)
        while not iterator.at_end():
            shape = iterator.shape()
            shape_count += 1
            # Shapes are reported in the coordinate system of the cell that
            # holds them, so apply the iterator transform before measuring.
            trans = iterator.trans()
            if shape.is_text():
                text_count += 1
            elif shape.is_polygon():
                polygon_count += 1
                region.insert(shape.polygon.transformed(trans))
            elif shape.is_box():
                polygon_count += 1
                region.insert(db.Polygon(shape.box).transformed(trans))
            elif shape.is_path():
                polygon_count += 1
                region.insert(shape.path.polygon().transformed(trans))
            iterator.next()

        if shape_count == 0:
            continue

        # Region.area() uses merged semantics, so overlapping shapes on the same
        # layer are counted once. Density is therefore true coverage, not a sum.
        merged = region.merged()
        area_um2 = float(merged.area()) * dbu_um**2
        # With no bounding box there is no denominator, so coverage is unknown -
        # not 0%. layer_groups already reported None here; match it.
        density = (area_um2 / bbox_area_um2 * 100.0) if bbox_area_um2 else None
        key = (int(info.layer), int(info.datatype))
        regions[key] = merged
        layers.append({
            "layer": key[0],
            "datatype": key[1],
            "name": name,
            "polygon_count": polygon_count,
            # Polygons remaining after merging touching/overlapping shapes.
            "merged_polygon_count": int(merged.count()),
            "shape_count": shape_count,
            # Filled in below when a .lyp identifies which layers are vias. Raw
            # GDSII labels nothing, so with no layer map this stays unavailable.
            "via_count": UNAVAILABLE,
            "text_count": text_count,
            "area_um2": round(area_um2, 6),
            "density_percent": round(density, 4) if density is not None else None,
            # Detects "same count, same area, different shapes".
            "geometry_fingerprint": geometry_fingerprint(merged, dbu_um),
        })
        total_polygons += polygon_count
        total_shapes += shape_count
        total_texts += text_count

    # --- via counts from the layer map -------------------------------------
    # A raw GDSII stream labels nothing, so with no .lyp the via count stays
    # unavailable. A .lyp *does* name the via layers, which makes the count
    # derivable - reported here with its source stated, because it rests on a
    # naming convention rather than an explicit flag. Cross-checked against the
    # sidecar's isVia on every reference file: 6, 9, 10 and 10, all exact.
    via_info = _vias_from_layermap(layers, layermap)
    if via_info["source"]:
        # Every layer gets a real number here, not just the via layers. Once the
        # map has told us which layers are vias, "how many vias are on M0?" has the
        # determinate answer 0 - leaving it None would claim we could not tell,
        # which is false. Without a map they all stay None, because then we really
        # cannot tell.
        for row in layers:
            key = (row["layer"], row["datatype"])
            row["via_count"] = via_info["per_layer"].get(key, 0)
            if key in via_info["roles"]:
                row["via_role"] = via_info["roles"][key]

    cells = []
    for cell in layout.each_cell():
        if cell.cell_index() not in in_scope:
            continue  # belongs to another top cell's hierarchy
        cw, ch = _box_dims(cell.bbox(), dbu_um)
        cells.append({
            "name": cell.name,
            "index": int(cell.cell_index()),
            "width_um": round(cw, 6),
            "height_um": round(ch, 6),
            "area_um2": round(cw * ch, 6),
            # inst.size() is the number of placements; a 2x2 CellInstArray is one
            # instance record but four placements, and counting records made
            # instance_count disagree with the polygon count in the same object.
            "instance_count": sum(inst.size() for inst in cell.each_inst()),
            "instance_record_count": sum(1 for _ in cell.each_inst()),
        })

    # Apply the technology layer map before grouping, so groups are keyed by the
    # real names (BM0) rather than placeholders (layer_300).
    warnings += annotate_layers(layers, layermap)

    # Group by the same name the layer rows carry, so a question about a named
    # layer finds its group. Keying these by layer number while the rows were
    # named `layer_300` meant the lookup missed and the answer fell back to
    # summing duplicated datatypes (0.0459 instead of the 0.02295 union).
    name_to_keys: dict[str, set[tuple[int, int]]] = {}
    for row in layers:
        key = (row["layer"], row["datatype"])
        if key in regions:
            name_to_keys.setdefault(row["name"], set()).add(key)
    groups = build_name_groups(name_to_keys, regions, dbu_um, bbox_area_um2, layers)

    metadata = {
        "schema_version": "1.3",
        "metadata_source": "gds",
        "source": {"file": gds_path.name, "format": "GDSII", "dbu_um": dbu_um},
        "warnings": warnings,
        "design": {
            "top_cell": top.name,
            "top_cell_index": int(top.cell_index()),
            "top_cell_count": len(top_cells),
            "top_cells": [c.name for c in top_cells],
            "cell_count": len(cells),
            "total_cell_count_in_file": layout.cells(),
            "layer_count": len(layers),
            "distinct_layer_name_count": len({r["name"] for r in layers}),
            # Flattened: every instance placement counted, i.e. what is drawn.
            "polygon_count": total_polygons,
            # As-stored records, comparable with a sidecar's element list.
            "polygon_record_count": record_polygons,
            "text_record_count": record_texts,
            "shape_count": total_shapes,
            "text_count": total_texts,
            # Via-ness is a technology semantic that a raw GDSII stream does not
            # carry, so with no layer map this stays unavailable rather than a
            # misleading 0. A .lyp names the via layers, which makes it derivable.
            "via_count": via_info["via_shapes"],
            "via_count_source": via_info["source"],
            "via_layer_count": len(via_info["via_layers"]) or None,
            "via_layer_names": via_info["via_layers"] or None,
            # Contacts are reported separately. Folding them in would have
            # disagreed with the sidecar's own via count on every sample file.
            "contact_count": via_info["contact_shapes"],
            "contact_layer_names": via_info["contact_layers"] or None,
        },
        "layout": {
            "bbox_dbu": {"left": int(bbox.left), "bottom": int(bbox.bottom), "right": int(bbox.right), "top": int(bbox.top)},
            "width_um": round(width_um, 6),
            "height_um": round(height_um, 6),
            "bbox_area_um2": round(bbox_area_um2, 6),
        },
        "cells": cells,
        "layers": sorted(layers, key=lambda x: (x["layer"], x["datatype"], x["name"])),
        "layer_groups": groups,
        "technology": {
            "source": "GDSII only" if not layermap else f"GDSII + layer map ({layermap['file']})",
            "semantic_sidecar_used": False,
            "layer_map_used": layermap["file"] if layermap else None,
            "area_method": "klayout_merged_region",
            # A .lyp supplies mask names, which also makes via-ness derivable from
            # the via layer names. What stays out of reach without a connection
            # stack is which levels each via joins.
            "unavailable_facts": (["connectivity_stack"] if layermap
                                  else ["via_count", "layer_names", "connectivity_stack"]),
        },
    }
    return metadata, regions


def build_name_groups(
    name_to_keys: dict[str, set[tuple[Any, Any]]],
    regions: dict[tuple[Any, Any], Any],
    dbu_um: float,
    bbox_area_um2: float,
    layer_rows: list[dict[str, Any]],
    shared_keys: set[tuple[Any, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-layer-name coverage, computed the one correct way.

    Two rules, and they pull in opposite directions:

    * Datatypes of the SAME layer number may hold the same geometry (a
      drawing/pin duplication), so their areas must be **unioned** or coverage
      doubles.
    * A name spanning SEVERAL layer numbers describes distinct mask layers that
      merely overlap in x/y, so those must be **added** or the drawn area is
      understated - by 2.5x for `Diffusion_Break` in the reference files.

    So: union within each layer number, then sum across layer numbers. Both the
    raw-GDS and fused paths call this, so they cannot drift apart.

    `shared_keys` marks (layer, datatype) pairs carrying more than one name; a
    group touching one of those is reporting an upper bound, not an attribution.
    """
    shared_keys = shared_keys or set()
    out: list[dict[str, Any]] = []
    for name, keys in sorted(name_to_keys.items(), key=lambda kv: str(kv[0])):
        keys = {k for k in keys if k in regions}
        if not keys:
            continue
        by_number: dict[Any, list[tuple[Any, Any]]] = {}
        for k in sorted(keys, key=str):
            by_number.setdefault(k[0], []).append(k)
        subgroups = build_layer_groups(
            {str(num): ks for num, ks in sorted(by_number.items(), key=lambda kv: str(kv[0]))},
            regions, dbu_um, bbox_area_um2, layer_rows,
        )
        union_area = round(sum(g["union_area_um2"] for g in subgroups), 6)
        out.append({
            "label": name,
            "datatypes": [list(k) for k in sorted(keys, key=str)],
            "layer_numbers": sorted(by_number, key=str),
            "polygon_records": sum(g["polygon_records"] for g in subgroups),
            "unique_polygons": sum(g["unique_polygons"] for g in subgroups),
            "union_area_um2": union_area,
            "sum_of_datatype_areas_um2": round(
                sum(g["sum_of_datatype_areas_um2"] for g in subgroups), 6),
            "union_density_percent": (round(union_area / bbox_area_um2 * 100.0, 4)
                                      if bbox_area_um2 else None),
            "geometry_duplicated_across_datatypes": any(
                g["geometry_duplicated_across_datatypes"] for g in subgroups),
            "per_layer_number": subgroups,
            "area_is_exclusive_to_this_name": not (keys & shared_keys),
            "area_shared_with_other_layer_names": [],
        })
    return out


def build_layer_groups(
    grouping: dict[str, list[tuple[int, int]]],
    regions: dict[tuple[int, int], Any],
    dbu_um: float,
    bbox_area_um2: float,
    layer_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute true coverage for each group of (layer, datatype) pairs.

    Technologies routinely place identical geometry on several datatypes of one
    layer - in the reference files, datatypes 0 and 2 of layer 300 hold the same
    two rectangles, with the labels on 1 and 3. Adding the per-datatype areas
    would then double the reported coverage of `BSPowerRail`.

    Unioning the regions first gives the physical answer, and comparing the union
    against the sum reveals the duplication instead of hiding it.
    """
    import klayout.db as db

    out: list[dict[str, Any]] = []
    by_key = {(r["layer"], r["datatype"]): r for r in layer_rows}
    for label, keys in grouping.items():
        keys = [k for k in keys if k in regions]
        if not keys:
            continue
        union = db.Region()
        for k in keys:
            union.insert(regions[k])
        union = union.merged()
        union_area = float(union.area()) * dbu_um**2
        sum_area = sum(float(regions[k].area()) for k in keys) * dbu_um**2
        records = sum(by_key[k]["polygon_count"] for k in keys if k in by_key)
        out.append({
            "label": label,
            "datatypes": [list(k) for k in sorted(keys)],
            "polygon_records": records,
            "unique_polygons": int(union.count()),
            "union_area_um2": round(union_area, 6),
            "sum_of_datatype_areas_um2": round(sum_area, 6),
            "union_density_percent": round(union_area / bbox_area_um2 * 100.0, 4) if bbox_area_um2 else None,
            # True when the datatypes overlap, i.e. the sum overstates coverage.
            "geometry_duplicated_across_datatypes": bool(len(keys) > 1 and sum_area - union_area > 1e-12),
        })
    return out


def save_metadata(metadata: dict[str, Any], output_path: str | Path) -> None:
    Path(output_path).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
