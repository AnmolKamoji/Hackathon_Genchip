from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .gds_parser import analyze_gds_with_regions, build_name_groups
from .layermap import annotate_layers
from .sidecar_parser import analyze_sidecar, sidecar_rings


def _key(row: dict[str, Any]) -> tuple[Any, Any]:
    return (row.get("layer"), row.get("datatype"))


def analyze_pair(gds_path: str | Path, sidecar_path: str | Path, gds_name: str | None = None,
                 layermap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fuse measured GDS geometry with sidecar technology semantics.

    Neither input alone answers every review question:

    * A raw GDSII stream gives real merged-region areas, densities and cell
      bounding boxes, but carries no layer names, no via-ness and no
      connectivity.
    * The semantic sidecar gives layer names, ``isVia`` and ``layerMap``, but
      its areas are an unmerged per-polygon sum.

    They join cleanly on ``(layer, datatype)``. This function keeps geometry
    from KLayout and semantics from the sidecar, and cross-checks the two so a
    disagreement is surfaced instead of silently averaged away.
    """
    gds, regions = analyze_gds_with_regions(gds_path)
    side = analyze_sidecar(sidecar_path, gds_name or Path(gds_path).name)

    # Measured geometry, keyed by (layer, datatype).
    geom: dict[tuple[Any, Any], dict[str, Any]] = {_key(r): r for r in gds["layers"]}

    # How many distinct sidecar names share one (layer, datatype)? Where more
    # than one does, the merged area belongs to the group, not to a single row.
    names_per_key: dict[tuple[Any, Any], set[Any]] = defaultdict(set)
    for r in side["layers"]:
        names_per_key[_key(r)].add(r["name"])

    bbox_area = gds["layout"]["bbox_area_um2"]
    layers: list[dict[str, Any]] = []
    for r in side["layers"]:
        k = _key(r)
        measured = geom.get(k)
        row = dict(r)
        if measured:
            # Carry the shape fingerprint for every measured row, including the
            # ambiguous ones: it belongs to the (layer, datatype) and is what
            # lets a comparison notice shapes that moved without any count or
            # area changing. Without it a moved-geometry revision reported
            # "no differences".
            row["geometry_fingerprint"] = measured.get("geometry_fingerprint")
        if measured and len(names_per_key[k]) == 1:
            row["area_um2"] = measured["area_um2"]
            row["density_percent"] = measured["density_percent"]
            row["geometry_source"] = "klayout_merged_region"
        elif measured:
            # Ambiguous: several sidecar layer names map to this (layer,
            # datatype). Keep the name-specific unmerged sum and say so.
            row["geometry_source"] = "sidecar_unmerged_subset"
            row["shares_layer_datatype_with"] = sorted(n for n in names_per_key[k] if n != r["name"])
            row["group_merged_area_um2"] = measured["area_um2"]
        else:
            row["geometry_source"] = "sidecar_unmerged_no_gds_match"
        layers.append(row)

    # Group by sidecar layer NAME so "what is the area of M0?" unions the
    # datatypes that carry M0 rather than adding them up.
    #
    # Only datatypes of the SAME layer number may be unioned. A name that spans
    # several layer numbers (Diffusion_Break sits on 102/1, 103/1 and 121/0 in
    # the samples) describes distinct mask layers that merely overlap in x/y;
    # unioning those would understate the drawn area - by 2.5x for
    # Diffusion_Break - so their per-layer areas are added instead.
    # Attach technology (mask) names from the .lyp. The sidecar's semantic name
    # is kept as `name`; the mask name is added alongside as `technology_name`,
    # because they answer different questions (what the layer is for vs which
    # mask it is). A row the sidecar left as a placeholder adopts the mask name.
    layermap_warnings = annotate_layers(layers, layermap)

    name_keys: dict[str, set[tuple[Any, Any]]] = defaultdict(set)
    for r in layers:
        k = _key(r)
        if k in regions:
            name_keys[str(r["name"])].add(k)

    # A (layer, datatype) shared by more than one name cannot be attributed to
    # one of them, so groups touching a shared key report an upper bound.
    shared_keys = {k for k, names in names_per_key.items() if len(names) > 1}

    # Same helper the raw-GDS path uses: union within a layer number, sum across.
    layer_groups = build_name_groups(
        name_keys, regions, gds["source"]["dbu_um"], gds["layout"]["bbox_area_um2"],
        gds["layers"], shared_keys,
    )
    for g in layer_groups:
        g["area_shared_with_other_layer_names"] = sorted(
            {n for k in map(tuple, g["datatypes"]) if k in shared_keys
             for n in names_per_key[k] if n != g["label"]})

    # --- cross-check the two descriptions of the same layout -----------------
    sd, gd = side["design"], gds["design"]
    mismatches = []
    # Compare records with records. A sidecar lists elements as stored per
    # structure; the GDS `polygon_count` is flattened across instance
    # placements. Comparing those two made every hierarchical design look like a
    # mismatched pairing (a 2-instance design reads 5 flattened vs 3 records),
    # which then discarded the only source of via data in the file.
    for side_field, gds_field, label in (
        ("polygon_record_count", "polygon_record_count", "polygon records"),
        ("text_count", "text_record_count", "text records"),
    ):
        s_val, g_val = sd.get(side_field), gd.get(gds_field)
        if s_val is not None and g_val is not None and s_val != g_val:
            mismatches.append({"field": label, "sidecar": s_val, "gds": g_val})

    # Counts cannot tell two revisions apart when both have the same totals and
    # the same layer set - pairing DCAP0_1.gds with DCAP0_2.json passed every
    # count check. So the sidecar's own coordinates are rebuilt and compared with
    # the measured geometry, per (layer, datatype).
    # Only meaningful for a flat design. A sidecar lists coordinates per
    # structure and does not record placement offsets, so for a hierarchical
    # layout its rings cannot be compared with the flattened GDS geometry - doing
    # so is the same records-vs-placements error that once discarded via data.
    hierarchical = (bool(side["design"].get("placement_count"))
                    or any((c.get("instance_count") or 0) for c in gds["cells"])
                    or gds["design"].get("cell_count", 1) > 1)
    geometry_verified = False
    geometry_mismatches: list[dict[str, Any]] = []
    try:
        import klayout.db as db
        from .gds_parser import geometry_fingerprint
        s_dbu_geo = side["source"].get("dbu_um")
        if s_dbu_geo and not hierarchical:
            geometry_verified = True
            rings = sidecar_rings(sidecar_path)
            for key, polys in rings.items():
                measured = geom.get(key)
                if not measured or not measured.get("geometry_fingerprint"):
                    continue
                region = db.Region()
                for ring in polys:
                    if len(ring) >= 3:
                        region.insert(db.Polygon([db.Point(int(round(x)), int(round(y)))
                                                  for x, y in ring]))
                fp = geometry_fingerprint(region.merged(), s_dbu_geo)
                if fp != measured["geometry_fingerprint"]:
                    geometry_mismatches.append(
                        {"layer": key[0], "datatype": key[1],
                         "name": sorted(names_per_key.get(key, {"?"}))[0]})
    except Exception:
        # Geometry verification is a cross-check, not a hard requirement; if it
        # cannot run, the count checks below still apply.
        geometry_mismatches = []
        geometry_verified = False

    # A wrong UNITS record in the sidecar leaves counts agreeing while every
    # sidecar-derived area is off by the ratio, so check the scale explicitly.
    dbu_mismatch = None
    s_dbu, g_dbu = side["source"].get("dbu_um"), gds["source"].get("dbu_um")
    if s_dbu and g_dbu and abs(s_dbu - g_dbu) > 1e-15 * max(s_dbu, g_dbu):
        if abs(s_dbu - g_dbu) / g_dbu > 1e-6:
            dbu_mismatch = {"sidecar_dbu_um": s_dbu, "gds_dbu_um": g_dbu,
                            "ratio": round(s_dbu / g_dbu, 6)}

    gds_keys = set(geom)
    side_keys = set(names_per_key)
    only_gds = sorted(gds_keys - side_keys, key=str)
    only_side = sorted(side_keys - gds_keys, key=str)

    # If the shape counts disagree, this sidecar describes a different revision
    # of the layout. Layer NAMES are technology-wide and stay useful, but via
    # counts are revision-specific: keeping them would report the other file's
    # via count as fact for this one. Drop them to unavailable instead.
    warnings = (list(gds.get("warnings", [])) + list(side.get("warnings", []))
                + list(layermap_warnings))
    via_count = sd["via_count"]
    # Geometry mismatches get their own sentence; forcing them through the
    # count-mismatch template produced "geometry is different shapes in the
    # sidecar but 4 layer(s) differ ... in the GDS", which is unreadable.
    if mismatches or geometry_mismatches:
        via_count = None
        for row in layers:
            row["via_count"] = None
            row["via_semantics_rejected"] = True
        reasons = [f"{m['field']} is {m['sidecar']} in the sidecar but {m['gds']} in the GDS"
                   for m in mismatches]
        if geometry_mismatches:
            where = ", ".join(f"{m['name']} ({m['layer']}/{m['datatype']})"
                              for m in geometry_mismatches[:6])
            more = "" if len(geometry_mismatches) <= 6 else f" and {len(geometry_mismatches) - 6} more"
            reasons.append(
                f"the shapes differ on {len(geometry_mismatches)} layer(s) - {where}{more}")
        warnings.append(
            f"The sidecar '{Path(sidecar_path).name}' does not describe this GDS: "
            + "; ".join(reasons)
            + ". Layer names are still applied, but via counts have been dropped to unavailable "
              "rather than reporting another revision's numbers. Pair each GDS with its own sidecar."
        )
    if dbu_mismatch:
        warnings.append(
            f"The sidecar's database unit ({dbu_mismatch['sidecar_dbu_um']} um) disagrees with the "
            f"GDS ({dbu_mismatch['gds_dbu_um']} um), a factor of {dbu_mismatch['ratio']}. "
            "Geometry below is measured from the GDS and is unaffected, but any sidecar-derived "
            "area would be wrong by that factor - check the sidecar's UNITS record."
        )

    return {
        "schema_version": "1.3-fused",
        "metadata_source": "fused",
        "warnings": warnings,
        "source": {
            "file": gds_name or Path(gds_path).name,
            "format": "GDSII (geometry) + semantic JSON sidecar (technology)",
            "dbu_um": gds["source"]["dbu_um"],
            "sidecar_file": Path(sidecar_path).name,
        },
        "design": {
            "top_cell": gd["top_cell"],
            "top_cell_index": gd["top_cell_index"],
            "top_cell_count": gd.get("top_cell_count"),
            "top_cells": gd.get("top_cells"),
            # Cell hierarchy is a geometry fact: trust the GDS.
            "cell_count": gd["cell_count"],
            # Layer rows keep sidecar granularity so named layers stay distinct.
            "layer_count": len(layers),
            # Flattened across instance placements: what is physically drawn.
            "polygon_count": gd["polygon_count"],
            # As-stored records, which is what the sidecar cross-check compares.
            "polygon_record_count": gd.get("polygon_record_count"),
            "shape_count": gd["shape_count"],
            "text_count": gd["text_count"],
            # Via-ness only exists in the sidecar, and only counts when the
            # sidecar actually describes this GDS.
            "via_count": via_count,
            # Roll-ups the model would otherwise have to derive by counting rows.
            # It miscounted exactly this ("5 via layers" for 6), so it is stated.
            "via_layer_count": (None if via_count is None
                                else sum(1 for r in layers if r.get("via_count"))),
            "via_layer_names": (None if via_count is None
                                else sorted({str(r["name"]) for r in layers
                                             if r.get("via_count")})),
            "distinct_layer_name_count": len({str(r["name"]) for r in layers}),
        },
        "layout": gds["layout"],
        "cells": gds["cells"],
        "layers": layers,
        "layer_groups": layer_groups,
        "technology": {
            "source": "GDSII geometry fused with user-provided semantic sidecar",
            "semantic_sidecar_used": True,
            "area_method": "klayout_merged_region (sidecar sum where layer names are ambiguous)",
            "layer_map_used": layermap["file"] if layermap else None,
            "raw_version": side["technology"].get("raw_version"),
            "base_layout_name": side["technology"].get("base_layout_name"),
        },
        "consistency": {
            "agrees": (not mismatches and not geometry_mismatches
                       and not only_gds and not only_side and not dbu_mismatch),
            "count_mismatches": mismatches,
            "dbu_mismatch": dbu_mismatch,
            "geometry_mismatches": geometry_mismatches,
            "sidecar_geometry_verified": geometry_verified,
            "sidecar_geometry_not_verified_because": (
                None if geometry_verified else
                ("hierarchical design: the sidecar records no placement offsets, so its "
                 "coordinates cannot be compared with the flattened GDS geometry"
                 if hierarchical else "the sidecar has no usable units record")),
            "compared": "as-stored records on both sides (not flattened instances)",
            "layer_datatype_only_in_gds": [list(k) for k in only_gds],
            "layer_datatype_only_in_sidecar": [list(k) for k in only_side],
            "gds_layer_datatype_count": len(gds_keys),
            "sidecar_named_layer_count": len(layers),
        },
    }
