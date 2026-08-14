"""The layout side of resistance and capacitance.

Engineers do judge RC by eye from a layout, and they are not being sloppy: the
geometry really does carry it. What a GDSII cannot carry is the other half.

    R per unit length = ρ / (W · T)          ρ resistivity, T thickness
    C_area            = ε · W / H            ε permittivity, H dielectric height
    C_coupling        = ε · T / S            S spacing to the neighbour
    C_fringe          = f(W, T, H)

Every symbol on the left of those formulas splits cleanly into two groups. **W, S,
L and the via count are in the file** and are measured here exactly. **ρ, T, ε and
H are process constants** and are in an ITF, an ICT or a technology file - not in a
layout, not inferrable from one, and not guessable.

So this module measures the first group and refuses to invent the second. Given a
process file it computes ohms and farads; without one it reports the drivers, which
is enough to answer the question people actually ask - "which of these two is worse
for RC?" - whenever the drivers all move the same way. When they move in opposite
directions it says so instead of picking a winner, because at that point the answer
genuinely depends on the constants nobody has supplied.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# How far apart two shapes on one layer can be and still be treated as coupled for
# the run-length measure. Coupling falls off with distance; the point of a fixed
# window is to compare two layouts on the same basis, not to model the field.
COUPLING_WINDOW_NM = 100.0


def load_process(path: str | Path) -> dict[str, Any]:
    """Read a process file: the constants a layout does not contain.

        {"technology": "...",
         "layers": {"M0": {"sheet_resistance_ohm_sq": 45,
                            "area_cap_aF_per_um2": 40,
                            "fringe_cap_aF_per_um": 20,
                            "coupling_cap_aF_per_um": 60}},
         "vias":   {"VIA0": {"resistance_ohm": 12}}}

    Sheet resistance rather than ρ and T: it is what a technology file actually
    states, and it is ρ/T - the two constants the layout cannot supply, already
    combined.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = data.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise ValueError("the process file has no 'layers' object")
    for name, entry in layers.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: expected an object of constants")
        known = {"sheet_resistance_ohm_sq", "area_cap_aF_per_um2",
                 "fringe_cap_aF_per_um", "coupling_cap_aF_per_um"}
        if not known & set(entry):
            raise ValueError(f"{name}: none of {', '.join(sorted(known))} is given")
    return {"technology": data.get("technology"), "source": str(path),
            "layers": layers, "vias": data.get("vias") or {}}


def _shape_length_width(polygon, dbu: float) -> tuple[float, float]:
    """A wire's length and width, from its own extent.

    The long side is the length and the short side the width, which is exact for the
    rectangles a standard cell is made of and the right reading for anything close to
    one. A polygon that is not wire-shaped is reported by area instead - see
    `wire_geometry`.
    """
    box = polygon.bbox()
    a = box.width() * dbu
    b = box.height() * dbu
    return (max(a, b), min(a, b))


def wire_geometry(gds_path: str | Path, layermap: dict[str, Any] | None,
                  coupling_window_nm: float = COUPLING_WINDOW_NM,
                  role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Per-layer wire length, width, area, perimeter, spacing and coupling run.

    Everything here is measured off the polygons. Nothing is scaled by a process
    constant, so two layouts measured this way can be compared directly.
    """
    import klayout.db as db

    # The electrical role, not the .lyp's purpose field - that one says "drawing"
    # for every conductor in the technology, which classifies nothing.
    from .connectivity import layer_roles

    classified = layer_roles(layermap, role_overrides)
    names = {meta.get("name"): key for key, meta in classified.items() if meta.get("name")}
    roles = {meta.get("name"): (meta.get("role") or "") for meta in classified.values()}
    layout = db.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise ValueError("GDS contains no top-level cell.")
    dbu = float(layout.dbu)
    window = int(round(coupling_window_nm / 1000.0 / dbu))

    conductors: dict[str, Any] = {}
    vias: dict[str, int] = {}
    for name, key in sorted(names.items()):
        index = layout.find_layer(key[0], key[1])
        if index is None:
            continue
        region = db.Region(top.begin_shapes_rec(index))
        if region.is_empty():
            continue
        role = roles.get(name, "")
        if role in ("via", "contact"):
            vias[name] = region.count()
            continue
        if role not in ("metal", "interconnect", "routing", "poly", "gate"):
            continue

        merged = region.dup()
        merged.merge()
        total_length = 0.0
        widths = []
        for polygon in merged.each():
            length, width = _shape_length_width(polygon, dbu)
            total_length += length
            widths.append(width)
        area = float(merged.area()) * dbu * dbu
        perimeter = float(merged.perimeter()) * dbu

        # Coupling run: the edge length that faces another shape on the same layer
        # within the window. This is the S in C = ε·T/S turned into something two
        # layouts can be compared on.
        pairs = merged.space_check(window)
        coupling_length = 0.0
        closest = None
        for pair in pairs.each():
            edge = pair.first
            coupling_length += edge.length() * dbu
            distance = pair.distance() * dbu * 1000
            if closest is None or distance < closest:
                closest = distance

        conductors[name] = {
            "shapes": merged.count(),
            "total_length_um": round(total_length, 6),
            "min_width_um": round(min(widths), 6) if widths else None,
            "max_width_um": round(max(widths), 6) if widths else None,
            "area_um2": round(area, 9),
            "perimeter_um": round(perimeter, 6),
            "coupling_run_um": round(coupling_length, 6),
            "closest_spacing_nm": None if closest is None else round(closest, 4),
        }

    return {
        "file": Path(gds_path).name,
        "top_cell": top.name,
        "coupling_window_nm": coupling_window_nm,
        "conductors": conductors,
        "vias": vias,
        "via_total": sum(vias.values()),
        "totals": {
            "wire_length_um": round(sum(c["total_length_um"] for c in conductors.values()), 6),
            "metal_area_um2": round(sum(c["area_um2"] for c in conductors.values()), 9),
            "coupling_run_um": round(sum(c["coupling_run_um"] for c in conductors.values()), 6),
        },
        "basis": ("lengths, widths, areas, perimeters and facing-edge runs measured "
                  "from the polygons; no process constant is applied"),
        "not_derivable": {
            "resistance": ("R = ρ·L/(W·T) needs resistivity and metal thickness. "
                           "Neither is in a GDSII."),
            "capacitance": ("C needs the permittivity and the dielectric height as "
                            "well as the geometry. Neither is in a GDSII."),
            "coupling": ("The coupling run is measured within a fixed window, not a "
                         "field solution. It compares two layouts; it is not a farad."),
        },
    }


def estimate_rc(geometry: dict[str, Any], process: dict[str, Any]) -> dict[str, Any]:
    """Ohms and farads, from the geometry and the supplied constants.

    Each figure names the constant it used. A layer the process file does not mention
    is listed as unpriced rather than defaulted - a default here would be a number
    with no provenance, which is worse than a gap.
    """
    layers = process.get("layers") or {}
    via_constants = process.get("vias") or {}

    rows = []
    unpriced = []
    total_r = total_c = 0.0
    for name, measured in (geometry.get("conductors") or {}).items():
        constants = layers.get(name)
        if not constants:
            unpriced.append(name)
            continue
        length = measured["total_length_um"]
        width = measured["min_width_um"] or 0.0
        row: dict[str, Any] = {"layer": name, "length_um": length, "width_um": width}

        sheet = constants.get("sheet_resistance_ohm_sq")
        if sheet is not None and width:
            # Squares along the wire: length over width. Sheet resistance already
            # carries ρ/T, which is exactly the pair the layout cannot supply.
            squares = length / width
            row["squares"] = round(squares, 4)
            row["resistance_ohm"] = round(squares * float(sheet), 4)
            total_r += row["resistance_ohm"]

        capacitance = 0.0
        if constants.get("area_cap_aF_per_um2") is not None:
            capacitance += measured["area_um2"] * float(constants["area_cap_aF_per_um2"])
        if constants.get("fringe_cap_aF_per_um") is not None:
            capacitance += measured["perimeter_um"] * float(constants["fringe_cap_aF_per_um"])
        if constants.get("coupling_cap_aF_per_um") is not None:
            capacitance += (measured["coupling_run_um"]
                            * float(constants["coupling_cap_aF_per_um"]))
        if capacitance:
            row["capacitance_aF"] = round(capacitance, 4)
            total_c += capacitance
        rows.append(row)

    via_rows = []
    via_r = 0.0
    for name, count in (geometry.get("vias") or {}).items():
        constants = via_constants.get(name)
        if not constants or constants.get("resistance_ohm") is None:
            unpriced.append(name)
            continue
        resistance = count * float(constants["resistance_ohm"])
        via_rows.append({"via": name, "count": count,
                         "resistance_ohm": round(resistance, 4)})
        via_r += resistance

    return {
        "available": bool(rows or via_rows),
        "file": geometry.get("file"),
        "layers": rows,
        "vias": via_rows,
        "totals": {
            "wire_resistance_ohm": round(total_r, 4),
            "via_resistance_ohm": round(via_r, 4),
            "resistance_ohm": round(total_r + via_r, 4),
            "capacitance_aF": round(total_c, 4),
            "capacitance_fF": round(total_c / 1000.0, 6),
        },
        "unpriced_layers": sorted(set(unpriced)),
        "process": process.get("technology") or process.get("source"),
        "basis": ("the measured geometry multiplied by the constants in the supplied "
                  "process file"),
        "not_derivable": {
            "distribution": ("These are lumped totals per layer, not a distributed RC "
                             "network. A delay needs the network, the drivers and the "
                             "loads - an extractor and a timer, not a layout."),
            "coupling_partner": ("Coupling here is measured against any neighbour on "
                                 "the same layer. Which *net* it couples to needs the "
                                 "net graph, and what that costs needs the switching "
                                 "activity."),
        },
    }


def compare_geometry(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The two layouts on the drivers of R and C, and what that does or does not settle.

    The honest part is the verdict rule. More length and more vias both raise
    resistance, so when every driver moves the same way the direction is settled
    without any process constant. When they move in opposite directions - shorter but
    narrower, say - the answer depends on ρ, T, ε and H, and this says so rather than
    picking the winner it happens to be able to compute.
    """
    def total(side, key):
        return (side.get("totals") or {}).get(key)

    rows = []
    for name in sorted(set(a.get("conductors") or {}) | set(b.get("conductors") or {})):
        row_a = (a.get("conductors") or {}).get(name)
        row_b = (b.get("conductors") or {}).get(name)
        if not row_a or not row_b:
            rows.append({"layer": name, "only_in": "a" if row_a else "b"})
            continue
        rows.append({
            "layer": name,
            "length_um": [row_a["total_length_um"], row_b["total_length_um"]],
            "min_width_um": [row_a["min_width_um"], row_b["min_width_um"]],
            "area_um2": [row_a["area_um2"], row_b["area_um2"]],
            "coupling_run_um": [row_a["coupling_run_um"], row_b["coupling_run_um"]],
            "closest_spacing_nm": [row_a["closest_spacing_nm"], row_b["closest_spacing_nm"]],
        })

    drivers = {
        "wire_length_um": [total(a, "wire_length_um"), total(b, "wire_length_um")],
        "metal_area_um2": [total(a, "metal_area_um2"), total(b, "metal_area_um2")],
        "coupling_run_um": [total(a, "coupling_run_um"), total(b, "coupling_run_um")],
        "via_count": [a.get("via_total"), b.get("via_total")],
    }

    # Which way each driver pushes resistance and capacitance. Length and vias raise
    # R; length, area and coupling run raise C. Width raises C and lowers R, which is
    # why a width change alone cannot settle anything.
    def direction(pair):
        first, second = pair
        if first is None or second is None or first == second:
            return 0
        return 1 if second > first else -1

    resistance_drivers = [direction(drivers["wire_length_um"]),
                          direction(drivers["via_count"])]
    capacitance_drivers = [direction(drivers["wire_length_um"]),
                           direction(drivers["metal_area_um2"]),
                           direction(drivers["coupling_run_um"])]

    name_a = a.get("file") or "A"
    name_b = b.get("file") or "B"

    def verdict(directions, quantity):
        moving = [d for d in directions if d]
        if not moving:
            return (f"{quantity.capitalize()}: every driver measured here is identical, "
                    "so nothing separates them on this measure.")
        higher = name_b if all(d > 0 for d in moving) else name_a
        if all(d > 0 for d in moving) or all(d < 0 for d in moving):
            return (f"{quantity.capitalize()}: every driver that moved is larger in "
                    f"`{higher}`, so `{higher}` is the higher-{quantity} layout on "
                    "these measures whatever the process constants turn out to be.")
        return (f"{quantity.capitalize()}: the drivers move in opposite directions, so "
                "which is higher depends on the process constants - and those are not "
                "in a layout. Nothing here settles it.")

    return {
        "a": a.get("file"), "b": b.get("file"),
        "drivers": drivers,
        "layers": rows,
        "resistance": verdict(resistance_drivers, "resistance"),
        "capacitance": verdict(capacitance_drivers, "capacitance"),
        "basis": ("measured geometry only. R rises with length and via count; C rises "
                  "with length, plate area and coupling run. Width pulls the two "
                  "opposite ways, which is why a width change alone settles nothing."),
    }
