from __future__ import annotations

from typing import Any


def _delta(a: Any, b: Any) -> Any:
    """Subtract only when both operands are real measurements.

    A missing fact (``None``) must not be coerced to 0, or an unavailable via
    count in one file turns into a fabricated "+9 vias added".
    """
    if a is None or b is None:
        return None
    return b - a


def _geometry_changed(x: dict[str, Any], y: dict[str, Any]) -> bool | None:
    """True when the measured shapes differ, independent of counts and areas."""
    fa, fb = x.get("geometry_fingerprint"), y.get("geometry_fingerprint")
    if fa is None or fb is None:
        return None
    return fa != fb


def compare_metadata(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a_layers = {(x.get("layer"), x.get("datatype"), x.get("name")): x for x in a.get("layers", [])}
    b_layers = {(x.get("layer"), x.get("datatype"), x.get("name")): x for x in b.get("layers", [])}
    keys = sorted(set(a_layers) | set(b_layers), key=str)
    changes = []
    for key in keys:
        x, y = a_layers.get(key, {}), b_layers.get(key, {})
        changes.append({
            "layer": key[0], "datatype": key[1], "name": key[2],
            "present_in_a": key in a_layers, "present_in_b": key in b_layers,
            "polygon_count_a": x.get("polygon_count", 0), "polygon_count_b": y.get("polygon_count", 0),
            "polygon_delta": y.get("polygon_count", 0) - x.get("polygon_count", 0),
            "via_count_a": x.get("via_count"), "via_count_b": y.get("via_count"),
            "via_delta": _delta(x.get("via_count"), y.get("via_count")),
            "text_count_a": x.get("text_count", 0), "text_count_b": y.get("text_count", 0),
            "text_delta": y.get("text_count", 0) - x.get("text_count", 0),
            "area_um2_a": x.get("area_um2"), "area_um2_b": y.get("area_um2"),
            "area_delta_um2": _delta(x.get("area_um2"), y.get("area_um2")),
            # Shapes can move without changing any count or total area, so the
            # fingerprints are compared too. None where geometry was not measured.
            "geometry_changed": _geometry_changed(x, y),
        })

    da, dbb = a["design"], b["design"]
    la, lb = a["layout"], b["layout"]

    # Two metadata objects are only comparable when they were produced the same
    # way. A raw-GDS run has no layer names or via semantics, so diffing it
    # against a sidecar run reports every layer as added and every via as new.
    src_a = a.get("metadata_source", "unknown")
    src_b = b.get("metadata_source", "unknown")
    comparable = src_a == src_b
    warnings: list[str] = []
    if not comparable:
        warnings.append(
            f"'{a['source']['file']}' was analyzed as '{src_a}' but '{b['source']['file']}' as '{src_b}'. "
            "Layer identity and via semantics are not comparable across these two modes, so the layer "
            "deltas below are not meaningful. Supply the semantic JSON sidecar for both files, or neither."
        )
    if da.get("via_count") is None or dbb.get("via_count") is None:
        warnings.append("Via counts are unavailable for at least one file (no semantic sidecar), so via deltas are reported as unavailable rather than zero.")

    added = [k for k in keys if k not in a_layers]
    removed = [k for k in keys if k not in b_layers]
    modified = [c for c in changes
                if c["present_in_a"] and c["present_in_b"]
                and (c["polygon_delta"] or c["text_delta"] or c["via_delta"]
                     or c["geometry_changed"])]
    moved = [c for c in changes
             if c["geometry_changed"] and not (c["polygon_delta"] or c["text_delta"]
                                               or c["via_delta"] or c["area_delta_um2"])]

    return {
        "file_a": a["source"]["file"], "file_b": b["source"]["file"],
        "comparable": comparable,
        "metadata_source_a": src_a, "metadata_source_b": src_b,
        "warnings": warnings,
        "summary": {
            "polygon_delta": _delta(da.get("polygon_count"), dbb.get("polygon_count")),
            "shape_delta": _delta(da.get("shape_count"), dbb.get("shape_count")),
            "text_delta": _delta(da.get("text_count"), dbb.get("text_count")),
            "via_delta": _delta(da.get("via_count"), dbb.get("via_count")),
            "cell_delta": _delta(da.get("cell_count"), dbb.get("cell_count")),
            "layer_delta": _delta(da.get("layer_count"), dbb.get("layer_count")),
            "width_delta_um": _delta(la.get("width_um"), lb.get("width_um")),
            "height_delta_um": _delta(la.get("height_um"), lb.get("height_um")),
            "bbox_area_delta_um2": _delta(la.get("bbox_area_um2"), lb.get("bbox_area_um2")),
            "layers_added": len(added),
            "layers_removed": len(removed),
            "layers_modified": len(modified),
            # Layers whose shapes moved with no change to any count or area.
            "layers_geometry_moved": len(moved),
            "layers_with_geometry_change": sum(
                1 for c in changes if c["geometry_changed"]),
        },
        "layers_added": [{"layer": k[0], "datatype": k[1], "name": k[2], "polygon_count": b_layers[k].get("polygon_count")} for k in added],
        "layers_removed": [{"layer": k[0], "datatype": k[1], "name": k[2], "polygon_count": a_layers[k].get("polygon_count")} for k in removed],
        "layers_modified": modified,
        "layers_geometry_moved": moved,
        "layer_changes": changes,
    }
