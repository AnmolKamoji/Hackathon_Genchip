from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

# Element kinds that carry geometry or labels. Anything else (sref/aref
# placements, unknown exporter kinds) is counted but never bucketed into a layer.
GEOMETRY_KINDS = ("boundary",)
TEXT_KINDS = ("text",)
PLACEMENT_KINDS = ("sref", "aref")

# Keys an exporter might use to name the structure an sref/aref points at.
_REF_NAME_KEYS = ("sname", "structure", "ref", "name", "refname")


def _dbu_um(raw: dict[str, Any]) -> float | None:
    """Derive the database unit in microns from a GDSII UNITS record.

    A GDSII ``UNITS`` record is ``[dbu_in_user_units, dbu_in_metres]``. The
    physical size only comes from the second entry, so convert metres to
    microns. Falling back to ``units[0]`` is only correct when the user unit
    happens to be 1 µm, which is conventional but not guaranteed.
    """
    units = raw.get("units") or []
    if len(units) > 1 and units[1]:
        return float(units[1]) * 1e6
    if units and units[0]:
        return float(units[0])
    return None


def _parse_points(element: dict[str, Any]) -> tuple[list[tuple[float, float]], bool]:
    """Return (points, trustworthy).

    ``trustworthy`` is False when the ``xy`` list is present but contains
    entries that are not 2-element numeric pairs. Silently dropping those and
    measuring the survivors is what turned one malformed vertex into a
    halved layer area, so the caller must treat the geometry as unknown
    instead of computing from a partial ring.
    """
    xy = element.get("xy")
    if xy is None:
        return [], False
    if not isinstance(xy, (list, tuple)):
        return [], False
    points: list[tuple[float, float]] = []
    clean = True
    for pt in xy:
        if (isinstance(pt, (list, tuple)) and len(pt) == 2
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in pt)):
            points.append((float(pt[0]), float(pt[1])))
        else:
            clean = False
    if not xy:
        # An explicitly empty coordinate list is a known absence, not corruption.
        return [], False
    return points, clean and len(points) >= 3


def _polygon_area_dbu2(pts: list[tuple[float, float]]) -> float:
    """Shoelace area of a closed ring, in dbu²."""
    if len(pts) < 3:
        return 0.0
    ring = pts[:-1] if pts[0] == pts[-1] else pts
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _bbox(pts: list[tuple[float, float]]):
    if not pts:
        return None
    xs, ys = zip(*pts)
    return min(xs), min(ys), max(xs), max(ys)


def _referenced_names(structures: list[dict[str, Any]]) -> set[str]:
    """Names of structures that some other structure places via sref/aref."""
    referenced: set[str] = set()
    for s in structures:
        for e in s.get("elements") or []:
            if e.get("element") in PLACEMENT_KINDS:
                for key in _REF_NAME_KEYS:
                    val = e.get(key)
                    if isinstance(val, str) and val:
                        referenced.add(val)
                        break
    return referenced


def sidecar_rings(json_path: str | Path) -> dict[tuple[Any, Any], list[list[tuple[float, float]]]]:
    """Parsed polygon rings per (layer, datatype), in the sidecar's own dbu.

    Lets a caller rebuild the sidecar's geometry and compare it with the GDS,
    which is the only way to tell two revisions apart when their counts and layer
    sets are identical.
    """
    raw = json.loads(Path(json_path).read_text(encoding="utf-8"))
    out: dict[tuple[Any, Any], list[list[tuple[float, float]]]] = defaultdict(list)
    for s in raw.get("structures") or []:
        for e in s.get("elements") or []:
            if e.get("element") not in GEOMETRY_KINDS:
                continue
            pts, clean = _parse_points(e)
            if clean:
                out[(e.get("layer"), e.get("datatype"))].append(pts)
    return dict(out)


def analyze_sidecar(json_path: str | Path, gds_name: str | None = None) -> dict[str, Any]:
    """Convert the user's rich GDS-derived JSON sidecar into reviewer metadata.

    This preserves technology-specific fields present in the supplied sidecar,
    including layer_name, isVia, layerMap and connectivity references.

    Areas here are a sum over individual polygons and are NOT merged, so
    overlapping shapes on one layer are double counted. ``technology
    .area_method`` records this. Fuse with the real GDS (see
    ``analyzer.fused.analyze_pair``) for merged-region coverage.
    """
    p = Path(json_path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    structures = raw.get("structures", [])
    if not structures:
        raise ValueError("Sidecar JSON contains no structures.")

    all_elements = [e for s in structures for e in (s.get("elements") or [])]
    boundaries = [e for e in all_elements if e.get("element") in GEOMETRY_KINDS]
    texts = [e for e in all_elements if e.get("element") in TEXT_KINDS]
    placements = [e for e in all_elements if e.get("element") in PLACEMENT_KINDS]
    vias = [e for e in boundaries if e.get("isVia") is True]

    warnings: list[str] = []

    # A sidecar from a different exporter could use other key names or types.
    # Silently reporting "0 polygons" or "0 vias" would be the worst outcome.
    if all_elements and not boundaries and not texts:
        kinds = sorted({str(e.get("element")) for e in all_elements})[:5]
        warnings.append(
            f"None of the {len(all_elements)} sidecar elements are tagged "
            f"element='boundary' or element='text' (saw: {', '.join(kinds)}). "
            "Polygon and text counts are 0 because nothing could be classified, not because "
            "the layout is empty."
        )
    odd_via = [e for e in boundaries if "isVia" in e and not isinstance(e.get("isVia"), bool)]
    if odd_via:
        warnings.append(
            f"{len(odd_via)} element(s) carry a non-boolean isVia value "
            f"(e.g. {odd_via[0].get('isVia')!r}). Only isVia == true counts as a via, so the via "
            "count may be understated."
        )

    dbu_um = _dbu_um(raw)
    if dbu_um is None:
        warnings.append(
            "The sidecar has no usable units record, so every physical dimension "
            "(areas, densities, bounding box) is unavailable."
        )
    scale = dbu_um**2 if dbu_um else None

    # --- geometry trustworthiness -------------------------------------------
    # Parse once; remember which elements produced a complete ring.
    parsed: dict[int, tuple[list[tuple[float, float]], bool]] = {
        id(e): _parse_points(e) for e in boundaries
    }
    malformed = [e for e in boundaries if not parsed[id(e)][1]]
    if malformed and len(malformed) < len(boundaries):
        warnings.append(
            f"{len(malformed)} of {len(boundaries)} boundary elements have missing or malformed "
            "'xy' coordinates. Areas and densities for the affected layers are reported as "
            "unavailable rather than measured from the remaining vertices."
        )
    elif malformed and boundaries:
        warnings.append(
            "No boundary element has usable 'xy' coordinates, so all areas, densities and the "
            "bounding box are unavailable. Polygon counts are still reported."
        )

    # --- overall bounding box ------------------------------------------------
    # Coordinates are per-structure and sref/aref placement offsets are not
    # modelled here, so unioning raw coordinates across structures produces a
    # meaningless extent (observed: 1000 um for a 0.1 um design).
    single_flat_structure = len(structures) == 1 and not placements
    usable_pts = [pt for e in boundaries if parsed[id(e)][1] for pt in parsed[id(e)][0]]
    box = _bbox(usable_pts) if (single_flat_structure and usable_pts) else None

    if box and dbu_um:
        left, bottom, right, top = box
        width_um = (right - left) * dbu_um
        height_um = (top - bottom) * dbu_um
        bbox_area_um2 = width_um * height_um
    else:
        left = right = bottom = top = None
        width_um = height_um = bbox_area_um2 = None
        if not single_flat_structure:
            warnings.append(
                f"This sidecar describes {len(structures)} structure(s) and {len(placements)} "
                "placement(s). Placement offsets are not recorded per element, so the overall "
                "bounding box, densities and cell extents cannot be derived from the sidecar "
                "alone - analyze the .gds file for those."
            )

    # --- per-layer rows ------------------------------------------------------
    # Only geometry and text elements define a layer. Bucketing placements here
    # produced a phantom row with layer=None that inflated layer_count.
    layer_buckets: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for e in boundaries + texts:
        layer_buckets[(e.get("layer"), e.get("datatype"), e.get("layer_name"))].append(e)

    layers = []
    for key in sorted(layer_buckets, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        layer, datatype, name = key
        elems = layer_buckets[key]
        b = [e for e in elems if e.get("element") in GEOMETRY_KINDS]
        t = [e for e in elems if e.get("element") in TEXT_KINDS]
        v = [e for e in b if e.get("isVia") is True]

        # One untrustworthy element makes the whole row's area unknown.
        all_clean = all(parsed[id(e)][1] for e in b)
        if b and all_clean and scale:
            area_dbu2 = sum(_polygon_area_dbu2(parsed[id(e)][0]) for e in b)
            area_um2 = round(area_dbu2 * scale, 6)
        else:
            area_um2 = None
        density = (round(area_um2 / bbox_area_um2 * 100.0, 4)
                   if area_um2 is not None and bbox_area_um2 else None)

        layers.append({
            "layer": layer,
            "datatype": datatype,
            "name": name or f"layer_{layer}",
            "polygon_count": len(b),
            "shape_count": len(elems),
            "via_count": len(v),
            "text_count": len(t),
            "area_um2": area_um2,
            "density_percent": density,
            "semantic": {
                "via": bool(v),
                "connected_polygon_refs": sorted(
                    {ref for e in b for ref in (e.get("layerMap") or [])
                     if isinstance(ref, (str, int))}),
            },
        })

    # --- cells ---------------------------------------------------------------
    # GDSII writes children before parents, so structures[0] is normally a leaf.
    # The top structure is the one nothing else places.
    referenced = _referenced_names(structures)
    names = [s.get("name") for s in structures]
    unreferenced = [n for n in names if n and n not in referenced]
    if len(structures) == 1:
        top_name, top_index = names[0], 0
    elif len(unreferenced) == 1:
        top_name = unreferenced[0]
        top_index = names.index(top_name)
    else:
        top_name, top_index = names[0], 0
        if len(structures) > 1:
            warnings.append(
                f"Could not identify a single top structure among {len(structures)} "
                f"(unreferenced: {unreferenced or 'none'}). Reporting '{top_name}' as the top "
                "cell; analyze the .gds file for the authoritative hierarchy."
            )

    cells = []
    for idx, s in enumerate(structures):
        s_boundaries = [e for e in (s.get("elements") or []) if e.get("element") in GEOMETRY_KINDS]
        s_places = [e for e in (s.get("elements") or []) if e.get("element") in PLACEMENT_KINDS]
        s_clean = s_boundaries and all(parsed[id(e)][1] for e in s_boundaries)
        s_box = _bbox([pt for e in s_boundaries for pt in parsed[id(e)][0]]) if s_clean else None
        # A structure that places children has geometry beyond its own elements,
        # which the sidecar does not locate, so its extent is not derivable.
        if s_box and dbu_um and not s_places:
            cl, cb, cr, ct = s_box
            cw, ch = (cr - cl) * dbu_um, (ct - cb) * dbu_um
            cells.append({"name": s.get("name", f"structure_{idx}"), "index": idx,
                          "width_um": round(cw, 6), "height_um": round(ch, 6),
                          "area_um2": round(cw * ch, 6),
                          "instance_count": len(s_places) or None})
        else:
            cells.append({"name": s.get("name", f"structure_{idx}"), "index": idx,
                          "width_um": None, "height_um": None, "area_um2": None,
                          "instance_count": len(s_places) if s_places else None})

    return {
        "schema_version": "1.3-sidecar",
        "metadata_source": "sidecar",
        "warnings": warnings,
        "source": {"file": gds_name or p.stem + ".gds",
                   "format": "GDSII + semantic JSON sidecar", "dbu_um": dbu_um},
        "design": {
            "top_cell": top_name,
            "top_cell_index": top_index,
            "cell_count": len(structures),
            "layer_count": len(layers),
            "polygon_count": len(boundaries),
            # Sidecar elements are records within their structure; placements are
            # not expanded. Recorded explicitly so the fused cross-check compares
            # like with like instead of records against flattened instances.
            "polygon_record_count": len(boundaries),
            "shape_count": len(all_elements),
            "text_count": len(texts),
            "via_count": len(vias),
            "placement_count": len(placements),
        },
        "layout": {
            "bbox_dbu": {"left": left, "bottom": bottom, "right": right, "top": top},
            "width_um": width_um,
            "height_um": height_um,
            "bbox_area_um2": bbox_area_um2,
        },
        "cells": cells,
        "layers": layers,
        "technology": {
            "source": "user-provided semantic sidecar",
            "semantic_sidecar_used": True,
            "area_method": "sum_of_polygons_unmerged",
            "raw_version": raw.get("version"),
            "base_layout_name": raw.get("base_layout_name"),
        },
    }
