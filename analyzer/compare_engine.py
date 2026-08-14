"""Assemble the one ComparisonDocument for a selected A/B pair.

This is the join between the two file documents and everything downstream. It is
built once and read by GDS Comparison, Revision Analysis, What Changed and
Integration Impact - none of which recompute a delta of their own.
"""
from __future__ import annotations

from typing import Any

from analyzer import comparison_doc as C
from analyzer import observations as O
from analyzer.values import difference, is_missing, percent

SCHEMA_VERSION = 2


def build_comparison(a: dict[str, Any], b: dict[str, Any],
                     xor: dict[str, Any] | None = None) -> dict[str, Any]:
    """One deterministic comparison of A (reference) against B (revision)."""
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "direction": "A -> B; difference = B - A; percentage uses A as denominator",
        "reference": {"file": (a.get("file") or {}).get("name"),
                      "top_cell": (a.get("file") or {}).get("top_cell")},
        "revision": {"file": (b.get("file") or {}).get("name"),
                     "top_cell": (b.get("file") or {}).get("top_cell")},
        "footprint": C.compare_footprint(a, b),
        "pins": C.compare_pins(a, b),
        "stack": C.compare_stack(a, b),
        "devices": C.compare_devices(a, b),
        "nets": C.compare_nets(a, b),
        "layers": C.compare_layers(a, b),
        "xor": xor or {"comparable": False, "reason": "no XOR was run for this pair"},
    }

    doc["change_type"] = C.classify_change(doc["footprint"], doc["pins"], doc["devices"])
    doc["drop_in_replacement"] = C.drop_in(doc["footprint"], doc["pins"])
    doc["device_topology"] = C.device_topology(doc["devices"])
    doc["deltas"] = _deltas(a, b)
    doc["metrics"] = metric_rows(a, b)
    doc["non_numeric"] = non_numeric_rows(a, b)

    doc["observations"] = O.observations(doc)
    doc["notes"] = O.notes(doc)
    doc["risk_flags"] = O.risk_flags(doc)
    doc["verdict"] = O.verdict(doc)
    return doc


def _deltas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The six header deltas, all B - A.

    "Drawn Shape" is named for what it is: the flattened count. It is not the
    stored shape-record count, and the two are different measurements.
    """
    pairs = {
        "drawn_shapes": "geometry.drawn_shapes",
        "vias": "vias.count",
        "labels": "geometry.text_count",
        "layers": "layers.count",
        "transistors": "devices.transistor_count",
        "width_um": "layout.width_um",
    }
    out = {}
    for key, path in pairs.items():
        one, two = C.lookup(a, path), C.lookup(b, path)
        value = None
        if not is_missing(one) and not is_missing(two):
            value = two - one
            if isinstance(value, float):
                value = round(value, 12)
        out[key] = {"a": one, "b": two, "delta": value}
    return out


def metric_rows(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """The comparison table. A row missing in both files is not rendered at all.

    A dash would imply the metric was checked and found equal, which is a different
    claim from "neither file could express this".
    """
    rows = []
    for group, label, path in C.METRICS:
        one, two = C.lookup(a, path), C.lookup(b, path)
        diff = difference(two, one)
        if diff is None and is_missing(one) and is_missing(two):
            continue                                    # missing in both: omitted
        numeric = isinstance(diff, (int, float)) and not isinstance(diff, bool)
        note = ""
        if numeric and one == 0:
            note = ("the baseline is zero, so a percentage is undefined; the "
                    "absolute difference still applies")
        rows.append({
            "group": group, "metric": label, "path": path,
            "a": one, "b": two,
            "difference": diff,
            "percent": percent(two, one) if numeric else "",
            "numeric": numeric,
            "note": note,
        })
    return rows


def non_numeric_rows(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Named values, reported as Same or Different. Never subtracted."""
    rows = []
    for label, path in C.NON_NUMERIC:
        one, two = C.lookup(a, path), C.lookup(b, path)
        if is_missing(one) and is_missing(two):
            continue
        rows.append({"metric": label, "a": one, "b": two,
                     "difference": difference(two, one)})
    return rows


def shared_cells(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Cell names present in BOTH files, matched exactly.

    No partial matching: INVD1 never matches INVD10, INVD1_X2 or INVD2, and no
    nearest name is ever substituted for one that does not exist.
    """
    names_a = {row.get("name") for row in (a.get("cells") or {}).get("rows") or []}
    names_b = {row.get("name") for row in (b.get("cells") or {}).get("rows") or []}
    return sorted(n for n in names_a & names_b if n)


def compare_cell(a: dict[str, Any], b: dict[str, Any],
                 name: str) -> dict[str, Any]:
    """One named cell in both files. Exact match, or Not found."""
    def find(doc):
        for row in (doc.get("cells") or {}).get("rows") or []:
            if row.get("name") == name:
                return row
        return None

    one, two = find(a), find(b)
    if one is None or two is None:
        return {"found": False,
                "reason": f"'{name}' does not exist in both files, so it is Not found."}

    fields = [
        ("Width (um)", "width_um"), ("Height (um)", "height_um"),
        ("Area (um2)", "area_um2"), ("Drawn shapes", "polygon_count"),
        ("Layers", "layer_count"), ("Vias", "via_count"), ("Pins", "pin_count"),
        ("Transistors", "transistor_count"), ("Labels", "text_count"),
        ("Times placed", "instance_count"),
    ]
    rows = []
    for label, key in fields:
        va, vb = one.get(key), two.get(key)
        diff = difference(vb, va)
        if diff is None and is_missing(va) and is_missing(vb):
            continue
        numeric = isinstance(diff, (int, float)) and not isinstance(diff, bool)
        rows.append({"metric": label, "a": va, "b": vb, "difference": diff,
                     "percent": percent(vb, va) if numeric else "",
                     "numeric": numeric})
    return {"found": True, "cell": name, "rows": rows}
