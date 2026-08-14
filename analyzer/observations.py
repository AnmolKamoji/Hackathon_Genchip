"""The change list, the not-compared notes, and the integration risk flags.

All three are derived from the comparison document and from nothing else, so What
Changed and Integration Impact render a list rather than compute one.

Two separations matter here.

**A comparison that could not be made is not a change.** They are kept in a
separate `notes` list prefixed "not compared - ...". Mixing them in meant an
identical pair of files never produced an empty change list, which made the empty
list - the most useful result the section has - unreachable.

**An improvement is INFO, not a warning.** A pin that gained landing options is
easier for the router, not a risk. Filing improvements as risks is how a warning
list becomes something reviewers scroll past.

No speculation appears anywhere in the wording. Why a change was made is design
intent, and design intent is in neither file.
"""
from __future__ import annotations

from typing import Any

from analyzer.values import delta as fmt_delta
from analyzer.values import number

SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


def _point(p: list[float]) -> str:
    return f"({number(p[0])}, {number(p[1])})"


def _points(points: list[list[float]]) -> str:
    return ", ".join(_point(p) for p in points) or "unknown"


def observations(doc: dict[str, Any]) -> list[str]:
    """One bullet per measured change, in the specification's fixed order."""
    out: list[str] = []
    fp = doc["footprint"]
    pins = doc["pins"]
    stack = doc["stack"]
    devices = doc["devices"]
    nets = doc["nets"]
    layers = doc["layers"]

    # 1. footprint
    if fp.get("identical") is False:
        w, h = fp["fields"]["width_um"], fp["fields"]["height_um"]
        out.append(
            f"Footprint changed: {number(w['a'])} x {number(h['a'])} um -> "
            f"{number(w['b'])} x {number(h['b'])} um "
            f"({fmt_delta(w['delta'])} um wide, {fmt_delta(h['delta'])} um high).")

    # 2. device count
    if devices.get("comparable") and devices.get("count_unchanged") is False:
        f = devices["fields"]["transistor_count"]
        out.append(f"Device count changed: {number(f['a'])} -> {number(f['b'])} "
                   f"({fmt_delta(f['delta'])}).")

    # 3-4. pins added and removed
    if pins.get("added"):
        out.append(f"Pins added: {', '.join(pins['added'])}.")
    if pins.get("removed"):
        out.append(f"Pins removed: {', '.join(pins['removed'])}.")

    # 5. swaps, once each
    for one, two in pins.get("swaps") or []:
        out.append(f"Pins {one} <-> {two} exchanged positions.")

    # 6. changed pins not already covered by a swap
    for pin in pins.get("changed_outside_swaps") or []:
        parts = []
        if pin["moved"]:
            parts.append(f"moved from {_points(pin['positions_a'])} to "
                         f"{_points(pin['positions_b'])}")
        if pin["access_shape_delta"]:
            parts.append(f"access shapes {fmt_delta(pin['access_shape_delta'])}")
        if pin["gained_access_layers"]:
            parts.append("gained access on " + ", ".join(pin["gained_access_layers"]))
        if pin["lost_access_layers"]:
            parts.append("lost access on " + ", ".join(pin["lost_access_layers"]))
        if parts:
            out.append(f"Pin {pin['name']} {'; '.join(parts)}.")

    # 7-8. metal levels
    if stack.get("metal_levels_added"):
        out.append("Metal levels added: " + ", ".join(stack["metal_levels_added"]) + ".")
    if stack.get("metal_levels_removed"):
        out.append("Metal levels removed: " + ", ".join(stack["metal_levels_removed"]) + ".")

    # 9-10. via layers
    if stack.get("via_layers_added"):
        out.append("Via layers added: " + ", ".join(stack["via_layers_added"]) + ".")
    if stack.get("via_layers_removed"):
        out.append("Via layers removed: " + ", ".join(stack["via_layers_removed"]) + ".")

    # 11. per-via-layer counts
    for change in stack.get("via_layer_changes") or []:
        out.append(f"Via layer {change['name']}: {number(change['a'])} -> "
                   f"{number(change['b'])} ({fmt_delta(change['delta'])}).")

    # 12-13. nets
    if nets.get("available"):
        for net in nets.get("changed") or []:
            out.append(f"Net {net['net']} changed: "
                       + ", ".join(net["differs_in"]) + " differ.")
        if nets.get("added"):
            out.append("Nets added: " + ", ".join(nets["added"]) + ".")
        if nets.get("removed"):
            out.append("Nets removed: " + ", ".join(nets["removed"]) + ".")

    # 14. the layer tally
    t = layers["tally"]
    out.append(f"{t['added']} layer(s) added, {t['removed']} removed, "
               f"{t['modified']} modified, {t['untouched']} untouched.")

    # 15. the geometric difference, when an XOR was run
    xor = doc.get("xor") or {}
    if xor.get("comparable"):
        s = xor.get("summary") or {}
        if s.get("identical"):
            out.append(f"No geometric difference on any of "
                       f"{s.get('layers_compared')} compared layers.")
        else:
            out.append(f"Geometric difference: {s.get('layers_changed')} of "
                       f"{s.get('layers_compared')} layers differ over "
                       f"{s.get('difference_regions')} region(s), "
                       f"{number(s.get('total_xor_area_um2'), 6)} um2.")
    return out


def notes(doc: dict[str, Any]) -> list[str]:
    """Comparisons that could not be made. Never mixed into the change list."""
    out: list[str] = []
    if doc["footprint"].get("identical") is None:
        out.append("not compared - footprint: " + doc["footprint"]["reason"] + ".")
    if not doc["pins"].get("available"):
        out.append("not compared - pins: " + (doc["pins"].get("reason") or "") + ".")
    if not doc["devices"].get("comparable"):
        out.append("not compared - devices: "
                   + (doc["devices"].get("reason") or "") + ".")
    if not doc["nets"].get("available"):
        out.append("not compared - nets: " + (doc["nets"].get("reason") or "") + ".")
    if doc["nets"].get("available") and (doc["nets"].get("unnamed_a")
                                         or doc["nets"].get("unnamed_b")):
        out.append(f"not compared - {doc['nets']['unnamed_a']} unnamed net(s) in A and "
                   f"{doc['nets']['unnamed_b']} in B: nothing identifies an unnamed "
                   "net across two files.")
    if not (doc.get("xor") or {}).get("comparable"):
        reason = (doc.get("xor") or {}).get("reason") or "the XOR did not run"
        out.append(f"not compared - geometry: {reason}.")
    return out


def risk_flags(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Integration consequences of the measured changes, in the fixed mapping.

    None of these is a DRC or an LVS result. Each is what a measured geometric
    change means for the downstream flow.
    """
    flags: list[dict[str, Any]] = []
    fp, pins, stack, devices = (doc["footprint"], doc["pins"], doc["stack"],
                                doc["devices"])

    # RULE 1 - footprint
    if fp.get("identical") is False:
        flags.append({
            "severity": "high", "area": "placement",
            "impact": "Not a drop-in replacement: placement and legalisation must be redone.",
            "detail": fp["reason"]})
    elif fp.get("identical") is None:
        flags.append({
            "severity": "medium", "area": "placement",
            "impact": "The footprint could not be determined, so compatibility is not claimed either way.",
            "detail": fp["reason"]})

    # RULE 2 - pin set
    if pins.get("added") or pins.get("removed"):
        detail = []
        if pins.get("added"):
            detail.append("added " + ", ".join(pins["added"]))
        if pins.get("removed"):
            detail.append("removed " + ", ".join(pins["removed"]))
        flags.append({
            "severity": "high", "area": "netlist",
            "impact": "Instance connections and the cell's abstract/LEF view must be updated.",
            "detail": "; ".join(detail)})

    # RULE 3 - pins moved
    moved = [p["name"] for p in pins.get("moved") or []]
    if moved:
        flags.append({
            "severity": "medium", "area": "routing",
            "impact": "Existing routes to these pins are invalid; affected nets need re-routing.",
            "detail": "moved: " + ", ".join(moved)})

    # RULE 4 - swap
    for one, two in pins.get("swaps") or []:
        flags.append({
            "severity": "medium", "area": "routing",
            "impact": ("The two pins trade places physically. Whether this is "
                       "functionally harmless depends on whether the inputs are "
                       "logically interchangeable, which cannot be determined from "
                       "geometry alone."),
            "detail": f"{one} <-> {two}"})

    # RULE 5 - device topology
    if devices.get("comparable") and devices.get("count_unchanged") is False:
        f = devices["fields"]["transistor_count"]
        flags.append({
            "severity": "high", "area": "function",
            "impact": "The device topology differs; this is a functional change and needs LVS.",
            "detail": f"transistor count {number(f['a'])} -> {number(f['b'])}"})

    # RULE 6 - metal level added
    for level in stack.get("metal_levels_added") or []:
        flags.append({
            "severity": "medium", "area": "routing resource",
            "impact": ("The cell consumes routing resource on a higher level, which "
                       "can block over-cell routing; the abstract view must be "
                       "regenerated."),
            "detail": f"metal level {level} added"})

    # RULE 7 - improved pin access. INFO, never a warning.
    for pin in pins.get("common") or []:
        if pin["access_shape_delta"] > 0 or pin["gained_access_layers"]:
            bits = []
            if pin["access_shape_delta"] > 0:
                bits.append(f"access shapes {fmt_delta(pin['access_shape_delta'])}")
            if pin["gained_access_layers"]:
                bits.append("gained " + ", ".join(pin["gained_access_layers"]))
            flags.append({
                "severity": "info", "area": "pin access",
                "impact": "More landing options for the router, which usually eases pin access.",
                "detail": f"{pin['name']}: " + "; ".join(bits)})

    flags.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return flags


def verdict(doc: dict[str, Any]) -> dict[str, Any]:
    """What to say when nothing was flagged - which is not simply "no risks".

    Four things are checked. Any of them may have been unavailable rather than
    passed, and a clean-looking page that quietly skipped two of them is the exact
    failure this wording exists to prevent.
    """
    checks = {
        "footprint": doc["footprint"].get("identical") is not None,
        "pin set": bool(doc["pins"].get("available")),
        "metal stack": bool((doc["stack"].get("metal_levels_a")
                             or doc["stack"].get("metal_levels_b"))),
        "device topology": bool(doc["devices"].get("comparable")),
    }
    unavailable = sorted(name for name, ok in checks.items() if not ok)
    flags = doc.get("risk_flags") or []
    actionable = [f for f in flags if f["severity"] != "info"]

    if actionable:
        return {"clean": False, "unavailable": unavailable,
                "message": (f"{len(actionable)} integration risk(s) were flagged "
                            "between these two revisions.")}
    if unavailable:
        return {"clean": False, "unavailable": unavailable,
                "message": ("No integration risks were flagged, but this is not a "
                            "clean result: " + ", ".join(unavailable)
                            + " could not be checked, so nothing is claimed about "
                              "them.")}
    return {"clean": True, "unavailable": [],
            "message": ("No integration risks were detected between these two "
                        "revisions: footprint, pin set, metal stack and device "
                        "topology are all compatible.")}
