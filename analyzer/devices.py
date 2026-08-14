"""Geometric transistor extraction: gate over diffusion, counted.

A transistor is where a gate crosses a diffusion. That crossing is visible in the
geometry, so it can be counted from a `.gds` and a `.lyp` alone:

    NMOS = disjoint regions of (NPOLY AND NDIFF), after merging
    PMOS = disjoint regions of (PPOLY AND PDIFF), after merging

Two things about this count are easy to get wrong and both matter.

**The layer names are exact, never prefixes.** `NPOLY-EXTENDED` carries the dummy
gates as well as the real ones, and matching on the prefix `NPOLY` pulls them in
and inflates the count. The same applies to `-PIN` and `-DUPLICATE` copies.

**This is not LVS device extraction.** Each finger of a multi-finger device is its
own gate-over-diffusion crossing and is counted separately. Merging fingers into
one logical device requires knowing which fingers share a source and drain, which
is a netlist question. A four-finger inverter reads as four here, and that is the
correct answer to the question this function asks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# The exact technology layer names. A pair is measurable only when both of its
# layers exist in the map and carry geometry.
NMOS_LAYERS = ("NPOLY", "NDIFF")
PMOS_LAYERS = ("PPOLY", "PDIFF")


def _key_of(layermap: dict[str, Any] | None, name: str) -> tuple[int, int] | None:
    """The (layer, datatype) for an exact technology name. No prefix matching."""
    for key, entry in ((layermap or {}).get("by_key") or {}).items():
        if entry.get("technology_name") == name:
            return key
    return None


def _region(layout, db, key: tuple[int, int] | None, top):
    """The merged region of one layer, flattened from the top cell."""
    if key is None:
        return None
    index = layout.find_layer(key[0], key[1])
    if index is None:
        return None
    region = db.Region(top.begin_shapes_rec(index))
    region.merge()
    return region


def _crossings(layout, db, top, layermap, gate_name: str,
               diff_name: str) -> dict[str, Any]:
    """Disjoint regions of (gate AND diffusion) for one device flavour."""
    gate_key, diff_key = _key_of(layermap, gate_name), _key_of(layermap, diff_name)
    if gate_key is None or diff_key is None:
        missing = [n for n, k in ((gate_name, gate_key), (diff_name, diff_key))
                   if k is None]
        return {"available": False, "count": None,
                "reason": f"the layer map does not name {' and '.join(missing)}",
                "layers": [gate_name, diff_name]}
    gate, diff = _region(layout, db, gate_key, top), _region(layout, db, diff_key, top)
    if gate is None or diff is None or gate.is_empty() or diff.is_empty():
        return {"available": True, "count": 0,
                "reason": (f"{gate_name} and {diff_name} are named by the layer map; "
                           "one of them carries no geometry in this layout"),
                "layers": [gate_name, diff_name]}
    crossing = gate & diff
    crossing.merge()
    return {"available": True, "count": crossing.count(),
            "reason": f"disjoint regions of ({gate_name} AND {diff_name}), merged",
            "layers": [gate_name, diff_name]}


def extract_devices(gds_path: str | Path,
                    layermap: dict[str, Any] | None) -> dict[str, Any]:
    """NMOS, PMOS and their total, measured from gate-over-diffusion crossings."""
    if not layermap:
        return {"available": False, "transistor_count": None, "nmos": None,
                "pmos": None,
                "reason": ("Device extraction needs the technology layer map to "
                           "identify the gate and diffusion layers."),
                "basis": "geometric device extraction, not LVS device extraction"}

    import klayout.db as db
    from analyzer.gds_parser import rank_top_cells

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]

    n = _crossings(layout, db, top, layermap, *NMOS_LAYERS)
    p = _crossings(layout, db, top, layermap, *PMOS_LAYERS)

    # Neither pair measurable means the count is unavailable, not zero. One pair
    # measurable and empty is a real zero for that flavour.
    if not n["available"] and not p["available"]:
        return {"available": False, "transistor_count": None,
                "nmos": None, "pmos": None, "nmos_detail": n, "pmos_detail": p,
                "reason": ("Neither NPOLY/NDIFF nor PPOLY/PDIFF is named by the "
                           "layer map, so no gate-over-diffusion crossing can be "
                           "identified."),
                "basis": "geometric device extraction, not LVS device extraction"}

    nmos = n["count"] if n["available"] else None
    pmos = p["count"] if p["available"] else None
    total = (nmos or 0) + (pmos or 0) if (nmos is not None or pmos is not None) else None
    return {
        "available": True,
        "transistor_count": total,
        "nmos": nmos,
        "pmos": pmos,
        "nmos_detail": n,
        "pmos_detail": p,
        "basis": ("Each gate-over-diffusion crossing is counted separately, so every "
                  "finger of a multi-finger device is its own count. This is "
                  "geometric device extraction, not LVS device extraction."),
    }


def gate_length_um(measurements: dict[str, Any] | None) -> float | None:
    """The drawn gate length: the smallest observed width on NPOLY or PPOLY.

    Read from the per-layer measurements already taken, so it cannot disagree with
    the width reported in the layer table.
    """
    if not measurements:
        return None
    widths = [row.get("observed_min_width_um")
              for row in measurements.get("layers") or []
              if row.get("name") in ("NPOLY", "PPOLY")
              and row.get("observed_min_width_um") is not None]
    return min(widths) if widths else None
