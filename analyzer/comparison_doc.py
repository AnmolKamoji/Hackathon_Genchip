"""The one deterministic comparison document for a selected A/B pair.

Revision Analysis, What Changed and Integration Impact all read this and compute
nothing of their own. Three sections deriving their own deltas would be three
chances to disagree about the same two files, and the disagreement would surface
as a page that says a pin moved in one panel and did not in the next.

Direction is fixed everywhere: **A is the reference, B is the revision, and the
comparison describes A -> B**.

    difference = B - A          positive means B has more of it
    percentage = ((B - A) / A) x 100        A is always the denominator

Named values are never subtracted - "M1 minus M0" is not a number - so a
categorical pair reports Same or Different instead.

Three distinctions in here are load-bearing and easy to collapse by accident:

* **Unknown is not Different.** A footprint that could not be measured leads to a
  different decision about drop-in replacement than one measured and found to
  differ, so `identical` is a tri-state.
* **A relabelled pin has not changed.** Gaining a second label at a position it
  already occupied does not move a pin or make it harder to reach. Only movement,
  a change in access-shape count, or a gained/lost access layer counts.
* **A swap is one finding.** Two pins trading places is reported once as
  `A1 <-> A2`, never as two unrelated movements.
"""
from __future__ import annotations

from typing import Any

from analyzer.values import difference, is_missing, present

# Two dimensions this close are the same dimension. Floating-point µm values are
# never compared with `==`: the same cell written twice can differ in the last bit.
TOLERANCE_UM = 1e-6


# --- the comparison table catalogue -----------------------------------------
# Fixed and ordered. Every metric resolves through ONE lookup so no two sections
# can read a different field for the same row.
METRICS: list[tuple[str, str, str]] = [
    # (group, label, path in the document)
    ("Whole file", "Cells", "cells.count"),
    ("Whole file", "Top cells", "cells.top_cells"),
    ("Whole file", "Instances", "cells.instances"),
    ("Whole file", "Layers", "layers.count"),
    ("Whole file", "Drawn shapes", "geometry.drawn_shapes"),
    ("Whole file", "Shape records", "geometry.shape_records"),
    ("Whole file", "Labels", "geometry.text_count"),
    ("Whole file", "Vias", "vias.count"),
    ("Whole file", "Pins", "pins.count"),
    ("Whole file", "Nets", "nets.count"),
    ("Whole file", "Transistors", "devices.transistor_count"),
    ("Whole file", "Hierarchy depth", "cells.hierarchy_depth"),
    ("Layout", "Width (um)", "layout.width_um"),
    ("Layout", "Height (um)", "layout.height_um"),
    ("Layout", "Area (um2)", "layout.area_um2"),
    ("Layout", "Aspect ratio", "layout.aspect_ratio"),
    ("Device", "NMOS", "devices.nmos"),
    ("Device", "PMOS", "devices.pmos"),
    ("Device", "Gate length (um)", "devices.gate_length_um"),
    ("Device", "Gate pitch CPP (um)", "_cpp_um"),
]

NON_NUMERIC = [
    ("Top metal", "stack.top_metal"),
    ("Top cell", "file.top_cell"),
]


def lookup(doc: dict[str, Any], path: str):
    """One resolver for every metric. A metric has exactly one home."""
    if path == "_cpp_um":
        cpp_nm = (((doc.get("classification") or {}).get("pitch") or {})
                  .get("gate_pitch") or {}).get("cpp_nm")
        return round(cpp_nm / 1000.0, 6) if cpp_nm else None
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


# --- footprint ---------------------------------------------------------------

def compare_footprint(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    la, lb = a.get("layout") or {}, b.get("layout") or {}
    fields = {}
    for key in ("width_um", "height_um", "area_um2"):
        fields[key] = {"a": la.get(key), "b": lb.get(key),
                       "delta": _delta(la.get(key), lb.get(key))}

    missing = [k for k in ("width_um", "height_um")
               if is_missing(la.get(k)) or is_missing(lb.get(k))]
    if missing:
        identical = None                       # UNKNOWN, and not the same as False
        reason = ("the footprint could not be determined: "
                  + ", ".join(k.replace("_um", "") for k in missing)
                  + " is unavailable in one or both files")
    else:
        identical = (abs(lb["width_um"] - la["width_um"]) <= TOLERANCE_UM
                     and abs(lb["height_um"] - la["height_um"]) <= TOLERANCE_UM)
        reason = (f"width and height agree to within {TOLERANCE_UM:g} um"
                  if identical else "width or height differs by more than the tolerance")
    return {"fields": fields, "identical": identical, "reason": reason,
            "tolerance_um": TOLERANCE_UM}


# --- pins --------------------------------------------------------------------

def compare_pins(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    pa, pb = a.get("pins") or {}, b.get("pins") or {}
    if not pa.get("available") or not pb.get("available"):
        return {"available": False,
                "reason": ("pin data is unavailable in one or both files: "
                           + (pa.get("reason") or pb.get("reason")
                              or "the layer map names no pin layer")),
                "added": [], "removed": [], "common": [], "swaps": [],
                "pin_name_set_identical": None, "pin_compatible": None,
                "footprint_pin_compatible": None}

    by_a = {p["name"]: p for p in pa.get("pins") or []}
    by_b = {p["name"]: p for p in pb.get("pins") or []}
    added = sorted(set(by_b) - set(by_a))
    removed = sorted(set(by_a) - set(by_b))

    common = []
    for name in sorted(set(by_a) & set(by_b)):
        one, two = by_a[name], by_b[name]
        moved = not _same_positions(one["positions"], two["positions"])
        gained = sorted(set(two["access_layers"]) - set(one["access_layers"]))
        lost = sorted(set(one["access_layers"]) - set(two["access_layers"]))
        access_delta = two["access_shapes"] - one["access_shapes"]
        common.append({
            "name": name,
            "moved": moved,
            "positions_a": one["positions"], "positions_b": two["positions"],
            "access_shapes_a": one["access_shapes"],
            "access_shapes_b": two["access_shapes"],
            "access_shape_delta": access_delta,
            "access_layers_a": one["access_layers"],
            "access_layers_b": two["access_layers"],
            "gained_access_layers": gained,
            "lost_access_layers": lost,
            # A relabelling is deliberately not a change: the pin has not moved and
            # has not become harder to reach.
            "label_count_a": one.get("label_count"), "label_count_b": two.get("label_count"),
            "changed": bool(moved or access_delta or gained or lost),
        })

    swaps = _swaps(common)
    swapped = {n for pair in swaps for n in pair}
    return {
        "available": True,
        "added": added, "removed": removed, "common": common,
        "swaps": [list(p) for p in swaps],
        "changed": [p for p in common if p["changed"]],
        "moved": [p for p in common if p["moved"]],
        "changed_outside_swaps": [p for p in common
                                  if p["changed"] and p["name"] not in swapped],
        "pin_name_set_identical": not added and not removed,
        "pin_compatible": (not added and not removed
                           and not any(p["changed"] for p in common)),
        "footprint_pin_compatible": (not added and not removed
                                     and not any(p["moved"] for p in common)),
    }


def _same_positions(one: list, two: list) -> bool:
    """Position sets match: same number of coordinates, each within tolerance.

    Both sides were rounded and sorted when they were measured, so this cannot
    depend on the order the labels happened to be stored in.
    """
    if len(one) != len(two):
        return False
    return all(abs(x1 - x2) <= TOLERANCE_UM and abs(y1 - y2) <= TOLERANCE_UM
               for (x1, y1), (x2, y2) in zip(one, two))


def _swaps(common: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Pairs of moved pins that traded places, reported once each."""
    moved = [p for p in common if p["moved"]]
    out: list[tuple[str, str]] = []
    for i, one in enumerate(moved):
        for two in moved[i + 1:]:
            if (_same_positions(one["positions_b"], two["positions_a"])
                    and _same_positions(two["positions_b"], one["positions_a"])
                    # Two pins already on top of each other in A have not traded
                    # anything by ending up on top of each other in B.
                    and not _same_positions(one["positions_a"], two["positions_a"])):
                out.append((one["name"], two["name"]))
    return out


# --- stack -------------------------------------------------------------------

def compare_stack(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    sa, sb = a.get("stack") or {}, b.get("stack") or {}
    levels_a = set(sa.get("metal_levels") or [])
    levels_b = set(sb.get("metal_levels") or [])
    via_a = sa.get("via_layers") or {}
    via_b = sb.get("via_layers") or {}

    # Keyed by (layer, datatype, name): the same layer number with a different
    # datatype is a different layer.
    key_a = {(v["layer"], v["datatype"], v["name"]): v for v in via_a.values()}
    key_b = {(v["layer"], v["datatype"], v["name"]): v for v in via_b.values()}
    changes = []
    for key in sorted(set(key_a) & set(key_b)):
        delta = key_b[key]["count"] - key_a[key]["count"]
        if delta:                              # only non-zero changes are displayed
            changes.append({"name": key[2], "layer": key[0], "datatype": key[1],
                            "a": key_a[key]["count"], "b": key_b[key]["count"],
                            "delta": delta})
    return {
        "metal_levels_a": sorted(levels_a), "metal_levels_b": sorted(levels_b),
        "metal_levels_added": sorted(levels_b - levels_a),
        "metal_levels_removed": sorted(levels_a - levels_b),
        "top_metal_a": sa.get("top_metal"), "top_metal_b": sb.get("top_metal"),
        "top_metal_changed": sa.get("top_metal") != sb.get("top_metal"),
        "via_layers_added": sorted(k[2] for k in set(key_b) - set(key_a)),
        "via_layers_removed": sorted(k[2] for k in set(key_a) - set(key_b)),
        "via_layer_changes": changes,
    }


# --- devices -----------------------------------------------------------------

def compare_devices(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    da, db = a.get("devices") or {}, b.get("devices") or {}
    comparable = bool(da.get("available") and db.get("available"))
    fields = {}
    for key in ("transistor_count", "nmos", "pmos", "gate_length_um"):
        fields[key] = {"a": da.get(key), "b": db.get(key),
                       "delta": _delta(da.get(key), db.get(key))}
    cpp_a, cpp_b = lookup(a, "_cpp_um"), lookup(b, "_cpp_um")
    fields["gate_pitch_um"] = {"a": cpp_a, "b": cpp_b, "delta": _delta(cpp_a, cpp_b)}

    if not comparable:
        unchanged = None                       # UNKNOWN: two failures agreeing
    else:                                      # is not evidence of sameness
        unchanged = da.get("transistor_count") == db.get("transistor_count")
    return {"fields": fields, "comparable": comparable, "count_unchanged": unchanged,
            "reason": (None if comparable else
                       "device extraction did not succeed on both files")}


# --- nets --------------------------------------------------------------------

def compare_nets(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    na, nb = a.get("nets") or {}, b.get("nets") or {}
    if not na.get("available") or not nb.get("available"):
        return {"available": False,
                "reason": ("a net graph was not built for both files"
                           + (f": {na.get('reason') or nb.get('reason')}"
                              if (na.get("reason") or nb.get("reason")) else "")),
                "added": [], "removed": [], "changed": []}

    # Only named nets. The same index in two files is not the same conductor, so
    # matching unnamed nets positionally would invent a correspondence.
    by_a = {n["net"]: n for n in na.get("rows") or [] if _named(n.get("net"))}
    by_b = {n["net"]: n for n in nb.get("rows") or [] if _named(n.get("net"))}
    unnamed_a = len(na.get("rows") or []) - len(by_a)
    unnamed_b = len(nb.get("rows") or []) - len(by_b)

    changed = []
    for name in sorted(set(by_a) & set(by_b)):
        one, two = by_a[name], by_b[name]
        deltas = {
            "shape_count": (one.get("shape_count"), two.get("shape_count")),
            "via_count": (one.get("via_count"), two.get("via_count")),
            "pin_access_shapes": (one.get("pin_access_shapes"),
                                  two.get("pin_access_shapes")),
            "layer_count": (one.get("layer_count"), two.get("layer_count")),
        }
        differing = {k: v for k, v in deltas.items() if v[0] != v[1]}
        if differing:
            changed.append({"net": name, "differs_in": sorted(differing),
                            "a": {k: v[0] for k, v in deltas.items()},
                            "b": {k: v[1] for k, v in deltas.items()}})
    return {
        "available": True,
        "added": sorted(set(by_b) - set(by_a)),
        "removed": sorted(set(by_a) - set(by_b)),
        "common": sorted(set(by_a) & set(by_b)),
        "changed": changed,
        "unnamed_a": unnamed_a, "unnamed_b": unnamed_b,
        "note": ("Only named nets are compared: nothing identifies an unnamed net "
                 "across two files."),
    }


def _named(net: Any) -> bool:
    text = str(net or "").strip()
    return bool(text) and not text.lower().startswith(("net_", "unnamed", "$"))


# --- layers ------------------------------------------------------------------

def compare_layers(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    def index(doc):
        out = {}
        measured = {(r.get("layer"), r.get("datatype")): r
                    for r in (doc.get("layers") or {}).get("measured") or []}
        for row in (doc.get("layers") or {}).get("rows") or []:
            key = (row.get("layer"), row.get("datatype"), row.get("name"))
            m = measured.get((row.get("layer"), row.get("datatype")), {})
            out[key] = {
                "layer": row.get("layer"), "datatype": row.get("datatype"),
                "name": row.get("name"), "role": m.get("role"),
                "polygons": row.get("polygon_count") or 0,
                "vias": row.get("via_count") or 0,
                "area_um2": row.get("area_um2") or 0.0,
            }
        return out

    ia, ib = index(a), index(b)
    added, removed, modified, untouched = [], [], [], []
    for key in sorted(set(ia) | set(ib), key=lambda k: (k[0], k[1], str(k[2]))):
        one, two = ia.get(key), ib.get(key)
        if one is None:
            added.append({**two, "state": "added"})
            continue
        if two is None:
            removed.append({**one, "state": "removed"})
            continue
        d_poly = two["polygons"] - one["polygons"]
        d_via = two["vias"] - one["vias"]
        d_area = round(two["area_um2"] - one["area_um2"], 12)
        row = {**two, "role": two.get("role") or one.get("role"),
               "polygon_delta": d_poly, "via_delta": d_via, "area_delta_um2": d_area}
        if d_poly or d_via or d_area:
            modified.append({**row, "state": "modified"})
        else:
            untouched.append({**row, "state": "untouched"})
    return {"added": added, "removed": removed, "modified": modified,
            "untouched": untouched,
            "tally": {"added": len(added), "removed": len(removed),
                      "modified": len(modified), "untouched": len(untouched)}}


# --- change type, drop-in, topology -----------------------------------------

def classify_change(footprint: dict[str, Any], pins: dict[str, Any],
                    devices: dict[str, Any]) -> str:
    """The ladder, in the order changes invalidate downstream work."""
    fp_identical = footprint.get("identical") is True
    device_unchanged = devices.get("count_unchanged") is True
    device_differs = devices.get("count_unchanged") is False

    if pins.get("pin_compatible") and fp_identical and device_unchanged:
        return "geometry-only change"
    if fp_identical and device_unchanged and pins.get("pin_name_set_identical"):
        return "routing / pin-access change"
    if device_differs and devices.get("comparable"):
        return "functional change"
    if footprint.get("identical") is False:
        return "footprint change"
    return "mixed change"


def drop_in(footprint: dict[str, Any], pins: dict[str, Any]) -> str:
    if footprint.get("identical") is None or not pins.get("available"):
        return "Unknown"
    return "Yes" if (pins.get("pin_compatible") and footprint["identical"]) else "No"


def device_topology(devices: dict[str, Any]) -> str:
    if not devices.get("comparable"):
        return "Unknown"
    return "unchanged" if devices.get("count_unchanged") else "changed"


def _delta(one, two):
    if is_missing(one) or is_missing(two):
        return None
    if isinstance(one, (int, float)) and isinstance(two, (int, float)):
        value = two - one
        return round(value, 12) if isinstance(value, float) else value
    return None
