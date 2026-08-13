"""Per-layer geometric measurements: perimeter, vertices, width, spacing, vias.

Everything here is an **observed measurement**, never a verdict. "The narrowest
M0 shape measures 0.02 µm" is derivable from the GDS. "M0 violates minimum width"
is not, because it needs a rule deck, and none is supplied. Every field name says
`observed_` for exactly that reason, and the consumers repeat the distinction.

Two of these were previously answered by accident and wrongly. A question about
polygon *vertices* came back with a polygon *count*, and one about *metal* area
came back with the cell bounding-box area, because the question-routing layer had
no such measurements to offer and fell through to a branch that matched the word
but not the meaning. Computing them properly is the fix; refusing them by name is
the backstop.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analyzer.connectivity import CONDUCTOR_ROLES, CONNECTOR_ROLES, layer_roles
from analyzer.gds_parser import rank_top_cells

# Beyond this, the pairwise width/space checks are skipped rather than left to
# run for minutes on a full-chip layout.
MAX_SHAPES_FOR_CHECKS = 20_000


def _shape_polygons(top, layer_index, db):
    """Every shape on one layer, flattened, with its record type."""
    out = []
    it = top.begin_shapes_rec(layer_index)
    while not it.at_end():
        shape, trans = it.shape(), it.trans()
        if shape.is_box():
            out.append(("box", db.Polygon(shape.box).transformed(trans), None))
        elif shape.is_polygon():
            out.append(("polygon", shape.polygon.transformed(trans), None))
        elif shape.is_path():
            out.append(("path", shape.path.polygon().transformed(trans),
                        shape.path.width))
        elif shape.is_text():
            out.append(("text", None, None))
        it.next()
    return out


def _arrangement(centres: list[tuple[int, int]], dbu: float) -> dict[str, Any]:
    """Describe how shapes are arranged: a row, a column, a grid, or irregular.

    Shapes sharing a y coordinate form a row, and a row whose gaps are all equal is
    an array. Reported as a measurement of the arrangement, with the pitch, rather
    than as a yes/no - "3 shapes on a 0.048 µm pitch" is checkable, "it is an
    array" is not.
    """
    if len(centres) < 2:
        return {"regular": False, "description": "a single shape, so there is no pitch to measure"}

    def pitches(groups: dict[int, list[int]]) -> list[float]:
        out = []
        for coords in groups.values():
            ordered = sorted(coords)
            out.extend(round((b - a) * dbu, 9) for a, b in zip(ordered, ordered[1:]))
        return out

    rows: dict[int, list[int]] = {}
    cols: dict[int, list[int]] = {}
    for x, y in centres:
        rows.setdefault(y, []).append(x)
        cols.setdefault(x, []).append(y)

    x_pitches = sorted(set(pitches(rows)))
    y_pitches = sorted(set(pitches(cols)))
    aligned_rows = sum(1 for v in rows.values() if len(v) > 1)
    aligned_cols = sum(1 for v in cols.values() if len(v) > 1)

    single_x = len(x_pitches) == 1
    single_y = len(y_pitches) == 1
    if aligned_rows and aligned_cols and single_x and single_y:
        desc = (f"a grid on a {x_pitches[0]:g} µm horizontal and {y_pitches[0]:g} µm vertical pitch")
        regular = True
    elif aligned_rows and single_x and not aligned_cols:
        desc = f"{aligned_rows} row(s) on a regular {x_pitches[0]:g} µm horizontal pitch"
        regular = True
    elif aligned_cols and single_y and not aligned_rows:
        desc = f"{aligned_cols} column(s) on a regular {y_pitches[0]:g} µm vertical pitch"
        regular = True
    elif not aligned_rows and not aligned_cols:
        desc = "no two shapes share a row or column, so they form no array"
        regular = False
    else:
        desc = ("aligned but on uneven pitches, so not a regular array: horizontal gaps "
                f"{x_pitches or 'none'}, vertical gaps {y_pitches or 'none'}")
        regular = False
    return {"regular": regular, "description": desc,
            "aligned_rows": aligned_rows, "aligned_columns": aligned_cols,
            "horizontal_pitches_um": x_pitches or None,
            "vertical_pitches_um": y_pitches or None}


def measure_layers(gds_path: str | Path,
                   layermap: dict[str, Any] | None = None,
                   role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Measure every layer in the layout.

    Returns per-layer rows plus aggregates grouped by inferred role, so "total
    metal area" is a real sum over the metal layers rather than a stand-in.
    """
    import klayout.db as db

    roles = layer_roles(layermap, role_overrides)
    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    dbu = float(layout.dbu)

    rows: list[dict[str, Any]] = []
    skipped_checks = 0
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        shapes = _shape_polygons(top, li, db)
        geoms = [(kind, poly, width) for kind, poly, width in shapes if poly is not None]
        if not shapes:
            continue

        kinds = {"box": 0, "polygon": 0, "path": 0, "text": 0}
        for kind, _, _ in shapes:
            kinds[kind] += 1
        path_widths = sorted({round(w * dbu, 9) for _, _, w in geoms if w is not None})

        meta = roles.get(key, {})
        row: dict[str, Any] = {
            "layer": key[0], "datatype": key[1],
            "name": meta.get("name") or f"layer_{key[0]}_{key[1]}",
            "role": meta.get("role", "unknown"),
            "shape_count": len(geoms),
            "shape_types": {k: v for k, v in kinds.items() if v},
            "path_widths_um": path_widths or None,
        }

        if not geoms:
            # A text-only layer. Area, perimeter and vertex count are *determined*
            # and equal to zero - there are no polygons, so nothing is covered.
            # Minimum width and spacing are a different matter: the minimum over an
            # empty set is undefined, not zero, so those stay None. The layers table
            # reported 0.0 here while this reported None, for the same layer.
            rows.append({**row, "area_um2": 0.0, "perimeter_um": 0.0,
                         "merged_perimeter_um": 0.0, "vertex_count": 0,
                         "max_vertices_in_one_polygon": 0, "mean_vertices_per_polygon": 0.0,
                         "non_rectangular_shape_count": 0,
                         "observed_min_width_um": None, "observed_max_width_um": None,
                         "observed_min_space_um": None,
                         "undefined_because": "this layer carries text only, so it has no shape "
                                              "whose width or spacing could be measured"})
            continue

        # Vertices are counted on the shapes as drawn, since that is what
        # "polygon complexity" means; merging would change the answer.
        vertex_counts = [p.num_points() for _, p, _ in geoms]
        region = db.Region()
        for _, poly, _ in geoms:
            region.insert(poly)
        merged = region.merged()

        row.update({
            "area_um2": round(float(merged.area()) * dbu * dbu, 9),
            # Summed per shape, deliberately not Region.perimeter(): a KLayout
            # Region uses merged semantics by default, so asking an unmerged region
            # for its perimeter returns the merged outline. That made the "as drawn"
            # and "merged" fields report the same number on every layer whose shapes
            # abut - 0.87 µm for both, where the shapes really total 0.95 µm.
            "perimeter_um": round(sum(float(p.perimeter()) for _, p, _ in geoms) * dbu, 9),
            "merged_perimeter_um": round(float(merged.perimeter()) * dbu, 9),
            "vertex_count": sum(vertex_counts),
            "max_vertices_in_one_polygon": max(vertex_counts),
            "mean_vertices_per_polygon": round(sum(vertex_counts) / len(vertex_counts), 3),
            # A shape with more than 4 vertices is not a plain rectangle.
            "non_rectangular_shape_count": sum(1 for v in vertex_counts if v > 4),
        })

        if len(geoms) <= MAX_SHAPES_FOR_CHECKS:
            bbox = merged.bbox()
            limit = max(bbox.width(), bbox.height()) or 1
            # width_check reports every place the figure is narrower than `limit`;
            # the smallest distance it returns is the narrowest measured width.
            widths = [ep.distance() for ep in merged.width_check(limit).each()]
            spaces = [ep.distance() for ep in merged.space_check(limit).each()]
            row["observed_min_width_um"] = round(min(widths) * dbu, 9) if widths else None
            row["observed_max_width_um"] = round(max(widths) * dbu, 9) if widths else None
            row["observed_min_space_um"] = round(min(spaces) * dbu, 9) if spaces else None
            row["space_measured_between_shapes"] = len(spaces)
            # KLayout returns these distances as whole database units, so a
            # diagonal gap is reported to the nearest dbu rather than exactly.
            row["distance_resolution_um"] = round(dbu, 9)
        else:
            skipped_checks += 1
            row["observed_min_width_um"] = None
            row["observed_min_space_um"] = None
            row["checks_skipped_because"] = (
                f"{len(geoms)} shapes exceeds the {MAX_SHAPES_FOR_CHECKS} limit for "
                f"pairwise width/spacing measurement")

        # Regular pitch, which is what "are these arranged in an array?" asks. A
        # single repeated gap along a row or column is an array; several different
        # gaps is not. Measured from shape centres, so it works for geometry drawn
        # flat as well as for real GDSII array records.
        centres = sorted((round((b.left + b.right) / 2), round((b.bottom + b.top) / 2))
                         for b in (p.bbox() for _, p, _ in geoms))
        row["arrangement"] = _arrangement(centres, dbu)

        # Per-shape extents, which is what "via size" actually asks for.
        dims = []
        for _, poly, _ in geoms:
            b = poly.bbox()
            dims.append((round(b.width() * dbu, 9), round(b.height() * dbu, 9)))
        row["shape_extents_um"] = {
            "min_width": min(d[0] for d in dims), "max_width": max(d[0] for d in dims),
            "min_height": min(d[1] for d in dims), "max_height": max(d[1] for d in dims),
            "distinct_sizes": sorted({d for d in dims})[:12],
            "uniform": len(set(dims)) == 1,
        }
        rows.append(row)

    rows.sort(key=lambda r: (r["layer"], r["datatype"]))

    # Aggregates by role. "Total metal area" must be a sum over metal layers, and
    # each layer number is counted once so a pin/duplicate copy cannot inflate it.
    aggregates: dict[str, Any] = {}
    for role in set(CONDUCTOR_ROLES) | set(CONNECTOR_ROLES):
        members = [r for r in rows if r["role"] == role and r.get("area_um2")]
        if not members:
            continue
        aggregates[role] = {
            "layer_count": len(members),
            "layers": [r["name"] for r in members],
            "total_area_um2": round(sum(r["area_um2"] for r in members), 9),
            "shape_count": sum(r["shape_count"] for r in members),
            "observed_min_width_um": min(
                (r["observed_min_width_um"] for r in members
                 if r.get("observed_min_width_um") is not None), default=None),
            "observed_min_space_um": min(
                (r["observed_min_space_um"] for r in members
                 if r.get("observed_min_space_um") is not None), default=None),
            "note": ("area is summed across the layers of this role; each layer's own area is a "
                     "merged region, so overlaps within a layer are not double counted"),
        }

    warnings = []
    if skipped_checks:
        warnings.append(
            f"Width and spacing measurement was skipped on {skipped_checks} layer(s) with more than "
            f"{MAX_SHAPES_FOR_CHECKS} shapes. Those fields are null rather than approximated.")
    if not roles:
        warnings.append(
            "No layer map was supplied, so role aggregates (total metal area, total via area) are "
            "unavailable - which layers are metal cannot be known from a .gds alone.")

    return {
        "availability": "GDS-only for per-layer measurement; GDS + LYP for role aggregates",
        "basis": ("observed geometry. Minimum width and spacing are measured values, NOT rule "
                  "compliance - no rule deck was supplied and none is implied"),
        "layers": rows,
        "role_aggregates": aggregates,
        "warnings": warnings,
        "not_derivable": {
            "rule_compliance": ("Whether a measured width or spacing is legal requires a PDK/DRC "
                                "rule deck. Requires PDK/DRC rules."),
        },
    }


def shape_outlines(gds_path: str | Path, layermap: dict[str, Any] | None = None,
                   max_shapes: int = 8000,
                   role_overrides: dict[str, str] | None = None,
                   include_identity: bool = False) -> dict[str, Any]:
    """Every shape's outline and its own dimensions, per layer.

    This is what a layout *view* needs, as opposed to the per-layer summary above:
    the polygons themselves, each carrying its width, height, centre and area. The
    dimensions travel with the shape so they can be read off directly rather than
    measured with a ruler, which is the slow part of inspecting a layout.

    `include_identity` adds what an *editor* needs and a viewer does not: which cell
    the shape actually lives in, its outline in that cell's own coordinates as exact
    database units, and its rank among identical siblings. The polygons here are
    flattened through the hierarchy, so the shape you see may belong to a child cell
    and be shared by every placement of it - editing it by screen position would
    silently change the wrong thing. It is off by default because it roughly doubles
    the payload and nothing but the editor can use it.
    """
    import klayout.db as db

    roles = layer_roles(layermap, role_overrides)
    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    dbu = float(layout.dbu)
    bbox = top.bbox()

    layers: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        meta = roles.get(key, {})
        shapes: list[dict[str, Any]] = []
        labels: list[dict[str, Any]] = []
        # Identical polygons can repeat inside one cell on one layer, so the local
        # outline alone does not identify a shape. The rank among identical siblings
        # completes it, and both are exact integers - no rounding to disagree over.
        #
        # The counter is keyed by the *placement* - the cell and the transform that
        # got us there - not by the cell alone. A cell placed twice is visited twice
        # and its shapes repeat, so counting per cell would give the second placement
        # rank 1 for a shape that is the only one of its kind inside the definition,
        # and the edit would look for a sibling that does not exist.
        seen_local: dict[tuple[str, str, tuple], int] = {}
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            if shape.is_text():
                text = shape.text.transformed(trans)
                labels.append({"text": text.string,
                               "at_um": [round(text.x * dbu, 6), round(text.y * dbu, 6)]})
                it.next()
                continue
            if total >= max_shapes:
                truncated = True
                it.next()
                continue
            if shape.is_box():
                local = db.Polygon(shape.box)
            elif shape.is_polygon():
                local = shape.polygon
            elif shape.is_path():
                local = shape.path.polygon()
            else:
                it.next()
                continue
            poly = local.transformed(trans)
            box = poly.bbox()
            identity = None
            if include_identity:
                owner = it.cell().name
                where = trans.to_s()
                points = tuple((pt.x, pt.y) for pt in local.each_point_hull())
                rank = seen_local.get((owner, where, points), 0)
                seen_local[(owner, where, points)] = rank + 1
                identity = {"cell": owner, "local_dbu": [list(p) for p in points],
                            "dup": rank, "in_top": owner == top.name,
                            # The transform from this shape's cell to the top cell,
                            # exactly as KLayout writes it. An edit arrives in the
                            # coordinates the user saw - the top cell's - and this is
                            # what maps it back to where the shape actually lives.
                            "trans": where}
            shapes.append({
                **({"id": identity} if identity else {}),
                "outline_um": [[round(pt.x * dbu, 6), round(pt.y * dbu, 6)]
                               for pt in poly.each_point_hull()],
                "width_um": round(box.width() * dbu, 6),
                "height_um": round(box.height() * dbu, 6),
                "centre_um": [round((box.left + box.right) / 2 * dbu, 6),
                              round((box.bottom + box.top) / 2 * dbu, 6)],
                "left_um": round(box.left * dbu, 6),
                "bottom_um": round(box.bottom * dbu, 6),
                "area_um2": round(float(poly.area()) * dbu * dbu, 9),
                "vertices": poly.num_points(),
            })
            total += 1
            it.next()
        if not shapes and not labels:
            continue
        extent = None
        if shapes:
            left = min(s["left_um"] for s in shapes)
            bottom = min(s["bottom_um"] for s in shapes)
            right = max(s["left_um"] + s["width_um"] for s in shapes)
            upper = max(s["bottom_um"] + s["height_um"] for s in shapes)
            extent = {"width_um": round(right - left, 6),
                      "height_um": round(upper - bottom, 6),
                      "bbox_um": [left, bottom, right, upper]}
        layers.append({
            "layer": key[0], "datatype": key[1],
            "name": meta.get("name") or f"layer_{key[0]}_{key[1]}",
            "role": meta.get("role", "unknown"),
            "colour": ((layermap or {}).get("by_key", {}).get(key) or {}).get("fill_color"),
            "shapes": shapes, "labels": labels,
            "shape_count": len(shapes), "label_count": len(labels),
            "extent": extent,
        })
    layers.sort(key=lambda r: (r["layer"], r["datatype"]))

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"Only the first {max_shapes} shapes are drawn. Every count and measurement "
            "elsewhere covers the whole layout; this limit applies to the drawing only.")
    return {
        "top_cell": top.name,
        "dbu_um": dbu,
        "cell_bbox_um": [round(bbox.left * dbu, 6), round(bbox.bottom * dbu, 6),
                         round(bbox.right * dbu, 6), round(bbox.top * dbu, 6)],
        "cell_width_um": round(bbox.width() * dbu, 6),
        "cell_height_um": round(bbox.height() * dbu, 6),
        "layers": layers,
        "shape_total": total,
        "truncated": truncated,
        "warnings": warnings,
    }


def measure_vias(measurements: dict[str, Any]) -> dict[str, Any]:
    """Via and contact geometry, pulled out of the per-layer measurements."""
    vias = [r for r in measurements["layers"] if r["role"] in CONNECTOR_ROLES
            and r.get("shape_count")]
    out = []
    for row in vias:
        ext = row.get("shape_extents_um") or {}
        out.append({
            "name": row["name"], "role": row["role"], "layer": row["layer"],
            "datatype": row["datatype"], "count": row["shape_count"],
            "uniform_size": ext.get("uniform"),
            "size_um": (f"{ext.get('min_width')} x {ext.get('min_height')}"
                        if ext.get("uniform") else None),
            "distinct_sizes_um": ext.get("distinct_sizes"),
            "observed_min_spacing_um": row.get("observed_min_space_um"),
            "total_area_um2": row.get("area_um2"),
        })
    out.sort(key=lambda r: (-r["count"], r["name"]))
    return {
        "availability": "GDS + LYP (the LYP identifies which layers are vias/contacts)",
        "via_layers": out,
        "total_via_shape_count": sum(r["count"] for r in out),
        "note": ("counts are flattened shape counts on layers the LYP names as vias or contacts; "
                 "sizes and spacings are measured, not checked against any rule"),
    }
