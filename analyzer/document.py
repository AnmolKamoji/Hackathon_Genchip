"""The one authoritative analysis document per GDS file.

Every section of the review page reads this and nothing else. That is the whole
point of it: a transistor count lives in exactly one place, so GDS Summary,
Inspect File and the comparison cannot disagree about it. Two sections computing
the same metric their own way is not a hypothetical failure - it is the one that
produced a file described as having both 4 and 5 transistors on the same screen.

The document is built once per file and cached on the file's bytes. Nothing below
re-reads a `.gds` that has already been read: the expensive passes (flattened
geometry, per-layer measurement, connectivity) each run once and everything else
is derived from their output.

Structure, fixed:

    file          name, dbu, top cell, warnings
    layout        bounding box, width, height, area
    geometry      drawn shapes, shape records, integrity
    layers        per (layer, datatype) rows, role aggregates
    pins          pin list with positions and access shapes
    devices       NMOS, PMOS, transistor count, gate length
    connectivity  tiers 1-3, stack source
    density       per metal level, per layer, tiles
    rules         the relational rule check and the numeric checks
    classification power, technology, routing, height, tracks, pitch
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from analyzer.classify import classify
from analyzer.connectivity import analyze_connectivity, default_stack
from analyzer.density_levels import analyze_density
from analyzer.devices import extract_devices, gate_length_um
from analyzer.drc import check_layout, load_rules, rules_available
from analyzer.gds_parser import analyze_gds
from analyzer.hierarchy import analyze_hierarchy
from analyzer.integrity import geometry_integrity, shape_records
from analyzer.measurements import measure_layers, measure_vias, shape_outlines
from analyzer.pins import analyze_pins
from analyzer.pitch import analyze_pitch
from analyzer.techparams import (compare_to_reference, find_reference,
                                 load_reference, tech_parameters)

SCHEMA_VERSION = 2


def build_document(gds_path: str | Path, layermap: dict[str, Any] | None,
                   stack: dict[str, Any] | None = None,
                   all_filenames: list[str] | None = None) -> dict[str, Any]:
    """Analyze one GDS completely, once.

    `layermap` is the technology layer map, loaded automatically rather than
    uploaded. Without it the semantic half of the analysis is unavailable and says
    so; the geometric half still runs, because a bounding box needs no layer names.
    """
    path = Path(gds_path)
    stack = stack if stack is not None else default_stack(layermap)
    overrides = (stack or {}).get("role_overrides") or None

    metadata = analyze_gds(path, layermap=layermap)
    outlines = shape_outlines(path, layermap, role_overrides=overrides)
    measurements = measure_layers(path, layermap, overrides)
    measurements["vias"] = measure_vias(measurements)

    design, layout = metadata["design"], metadata["layout"]

    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": {
            "name": path.name,
            "top_cell": design["top_cell"],
            "dbu_um": metadata["source"].get("dbu_um"),
            "warnings": list(metadata.get("warnings") or []),
        },
        "layout": {
            "width_um": layout.get("width_um"),
            "height_um": layout.get("height_um"),
            "area_um2": layout.get("bbox_area_um2"),
            "bbox_dbu": layout.get("bbox_dbu"),
            "aspect_ratio": _aspect(layout),
        },
        "layers": {
            "count": design.get("layer_count"),
            "rows": metadata.get("layers") or [],
            "measured": measurements.get("layers") or [],
            "role_aggregates": measurements.get("role_aggregates") or {},
        },
        # Kept whole as well as split out: the deterministic answerer reads the
        # measurement block as one object, and rebuilding it from the pieces above
        # would be a second version of the same numbers.
        "measurements": measurements,
        "cells": {
            "count": design.get("cell_count"),
            "rows": metadata.get("cells") or [],
        },
    }

    # --- geometry: the two shape counts, kept apart on purpose ----------------
    records = _safe(lambda: shape_records(path), {})
    doc["geometry"] = {
        "drawn_shapes": design.get("polygon_count"),
        "drawn_shapes_basis": ("flattened: every instance placement expanded, text "
                               "excluded"),
        "shape_records": records.get("shape_records"),
        "shape_records_basis": records.get("basis"),
        "text_count": design.get("text_count"),
        "instance_records": records.get("instance_records"),
        "path_count": _path_count(metadata),
        "integrity": _safe(lambda: geometry_integrity(path), {}),
    }

    # --- hierarchy -----------------------------------------------------------
    hierarchy = _safe(lambda: analyze_hierarchy(path), {})
    doc["hierarchy"] = hierarchy
    doc["cells"]["hierarchy_depth"] = hierarchy.get("max_depth_below_top")
    doc["cells"]["instances"] = _instances(metadata)
    doc["cells"]["top_cells"] = len(hierarchy.get("top_cells") or []) or 1

    # --- semantics: everything that needs the layer map ----------------------
    doc["pins"] = analyze_pins(outlines, layermap, overrides)
    doc["devices"] = _safe(lambda: extract_devices(path, layermap),
                           {"available": False, "transistor_count": None,
                            "nmos": None, "pmos": None,
                            "reason": "device extraction failed"})
    doc["devices"]["gate_length_um"] = gate_length_um(measurements)
    doc["vias"] = {
        "count": design.get("via_count"),
        "source": design.get("via_count_source"),
        "layer_names": design.get("via_layer_names") or [],
        "contact_count": design.get("contact_count"),
    }

    # --- connectivity --------------------------------------------------------
    doc["connectivity"] = _safe(
        lambda: analyze_connectivity(path, layermap, stack=stack),
        {"error": "connectivity analysis failed"})
    nets = (doc["connectivity"] or {}).get("nets") or {}
    doc["nets"] = {
        "available": bool(nets.get("available")),
        "count": (nets.get("summary") or {}).get("net_count") if nets.get("available") else None,
        "reason": nets.get("reason"),
        "stack_source": (doc["connectivity"] or {}).get("stack_source"),
        "rows": nets.get("nets") or [],
        "summary": nets.get("summary") or {},
    }

    # --- density, per metal level --------------------------------------------
    doc["density"] = _safe(
        lambda: analyze_density(path, layermap, overrides),
        {"available": False, "levels": [], "rows": [],
         "reason": "density analysis failed"})

    # --- classification, pitch, tech parameters ------------------------------
    cls = _safe(lambda: classify(outlines, path, list(all_filenames or [path.name])),
                None)
    if cls is not None:
        cls["pitch"] = _safe(lambda: analyze_pitch(outlines, path.name), None)
        params = _safe(lambda: tech_parameters(path, layermap), None)
        if params:
            reference = find_reference(path)
            if reference:
                stated = load_reference(reference)
                params["reference"] = stated
                params["comparison"] = compare_to_reference(params, stated)
            cls["tech_parameters"] = params
    doc["classification"] = cls

    # --- rules ---------------------------------------------------------------
    doc["rules"] = _rules(outlines)

    # --- the metal stack, for the comparison ---------------------------------
    doc["stack"] = _metal_stack(measurements, doc["vias"])

    # The enriched metadata is kept whole: the viewer and the XOR both read it.
    doc["metadata"] = metadata
    doc["outlines_available"] = bool(outlines.get("layers"))
    return doc


# --- derived pieces ---------------------------------------------------------

def _aspect(layout: dict[str, Any]) -> float | None:
    w, h = layout.get("width_um"), layout.get("height_um")
    if not w or not h:
        return None
    return round(w / h, 6)


def _path_count(metadata: dict[str, Any]) -> int | None:
    rows = metadata.get("layers") or []
    if not rows:
        return None
    counts = [r.get("path_count") for r in rows if r.get("path_count") is not None]
    return sum(counts) if counts else None


def _instances(metadata: dict[str, Any]) -> int | None:
    """Total placements over the cells in scope. A 2x2 array is four placements."""
    rows = metadata.get("cells") or []
    if not rows:
        return None
    counts = [r.get("instance_count") for r in rows if r.get("instance_count") is not None]
    return sum(counts) if counts else 0


def _metal_stack(measurements: dict[str, Any], vias: dict[str, Any]) -> dict[str, Any]:
    """Which metal levels are drawn, and how many shapes each via layer carries.

    Metal levels are read from the layers that actually carry geometry, so a
    technology that defines M2 but a cell that never draws on it reports two
    levels. Derived copies are excluded: an `M1-PIN` is not a metal level.
    """
    levels: list[str] = []
    via_layers: dict[str, dict[str, Any]] = {}
    for row in measurements.get("layers") or []:
        name, role = row.get("name") or "", row.get("role")
        if row.get("derived") or name.upper().endswith(("-PIN", "-LABEL", "-DUPLICATE",
                                                        "-TEXT", "-TRACK-GUIDE")):
            continue
        if role == "metal" and row.get("shape_count"):
            levels.append(name)
        if role in ("via", "contact") and row.get("shape_count"):
            via_layers[name] = {"layer": row.get("layer"), "datatype": row.get("datatype"),
                                "name": name, "role": role,
                                "count": row.get("shape_count")}
    levels = sorted(set(levels))
    return {
        "metal_levels": levels,
        "top_metal": _top_metal(levels),
        "via_layers": via_layers,
        "via_count": vias.get("count"),
    }


def _top_metal(levels: list[str]) -> str | None:
    """The highest numbered drawn metal level. `M2` beats `M1`; `BM0` is backside."""
    best, best_n = None, -1
    for name in levels:
        digits = "".join(ch for ch in name if ch.isdigit())
        if not digits:
            continue
        n = int(digits)
        # A backside level is not "higher" than a frontside one of the same number.
        if name.upper().startswith("B"):
            n -= 100
        if n > best_n:
            best, best_n = name, n
    return best or (levels[-1] if levels else None)


def _rules(outlines: dict[str, Any]) -> dict[str, Any]:
    """The relational rule check, or an explicit statement that none ran."""
    if not rules_available():
        return {
            "available": False,
            "checked": 0,
            "reason": ("The design rule catalogue is not present, so no rule was "
                       "checked. This is not a clean rule result."),
            "results": [], "violations": [], "summary": {},
        }
    result = check_layout(outlines, load_rules())
    if result.get("available") is False:
        return {"available": False, "checked": 0, "reason": result.get("reason"),
                "results": [], "violations": [], "summary": {}}
    summary = result.get("summary") or {}
    return {
        "available": True,
        "checked": summary.get("rules_checked"),
        "in_manual": summary.get("rules_in_manual"),
        "summary": summary,
        "results": result.get("results") or [],
        "violations": result.get("violations") or [],
        "not_checked": result.get("rules_not_checked") or [],
        "technology": result.get("technology"),
        "source": result.get("source"),
        "caveat": result.get("caveat"),
        "not_derivable": result.get("not_derivable") or {},
    }


def _safe(fn, fallback):
    """Run one analysis; a failure must not take the whole document with it."""
    try:
        return fn()
    except Exception:
        return fallback
