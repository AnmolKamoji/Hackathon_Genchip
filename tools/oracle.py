#!/usr/bin/env python3
"""An independent fact sheet for a GDS file, for judging answers against.

This exists to be a *different implementation*. It imports nothing from `analyzer/`
and does not use KLayout at all: geometry comes from `gdstk`, a separate GDS parser
with its own C++ reader, and the layer names come from parsing the .lyp as plain XML
here. So when an answer agrees with this, the agreement is between two codebases that
share no code, not between a module and itself.

That independence is the whole point. A judge built on the analyzer under test would
pass every answer the analyzer produces, including the wrong ones.

    python tools/oracle.py                          # fact sheet for every sample
    python tools/oracle.py --gds path.gds --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import gdstk

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
DEFAULT_LYP = SAMPLES / "Titan_layer_properties.lyp"


def layer_names(lyp: Path) -> dict[tuple[int, int], str]:
    """Parse a KLayout .lyp with a plain XML reader.

    Deliberately not `analyzer.layermap`: the names decide which measurement is which,
    so reusing that parser would put the code under test inside its own answer key.
    The source is of the form "102/0@1".
    """
    names: dict[tuple[int, int], str] = {}
    for element in ET.parse(lyp).getroot().iter("properties"):
        source = (element.findtext("source") or "").split("@")[0]
        name = element.findtext("name") or ""
        if "/" not in source or not name:
            continue
        try:
            layer, datatype = (int(part) for part in source.split("/")[:2])
        except ValueError:
            continue
        names[(layer, datatype)] = name.strip()
    return names


class Rect:
    """A shape reduced to its extent, in nanometres.

    Every shape in these cells is rectilinear, so the extent is the shape. Integers
    keep the comparisons exact - the coordinates arrive as floats scaled by the unit,
    and 0.0575 * 1000 is not reliably 57.5 in binary floating point.
    """

    __slots__ = ("x1", "y1", "x2", "y2")

    def __init__(self, x1: float, y1: float, x2: float, y2: float):
        self.x1, self.y1 = round(x1, 4), round(y1, 4)
        self.x2, self.y2 = round(x2, 4), round(y2, 4)

    def lo(self, axis: str) -> float:
        return self.x1 if axis == "x" else self.y1

    def hi(self, axis: str) -> float:
        return self.x2 if axis == "x" else self.y2

    def extent(self, axis: str) -> float:
        return round(self.hi(axis) - self.lo(axis), 4)

    def centre(self, axis: str) -> float:
        return round((self.lo(axis) + self.hi(axis)) / 2, 4)

    def meets(self, other: "Rect", axis: str) -> bool:
        return self.lo(axis) < other.hi(axis) and other.lo(axis) < self.hi(axis)


def read(gds: Path, lyp: Path) -> tuple[dict[str, list[Rect]], dict[str, Any]]:
    """Shapes by layer name, in nanometres, plus the file-level facts."""
    library = gdstk.read_gds(str(gds))
    top = library.top_level()[0]
    names = layer_names(lyp)
    # gdstk reports coordinates in microns via `unit`; the database unit is
    # unit * precision, which for these files is 5e-05 um.
    scale = 1000.0

    shapes: dict[str, list[Rect]] = {}
    for polygon in top.polygons:
        key = (polygon.layer, polygon.datatype)
        name = names.get(key) or f"layer_{polygon.layer}_{polygon.datatype}"
        (x1, y1), (x2, y2) = polygon.bounding_box()
        shapes.setdefault(name, []).append(
            Rect(x1 * scale, y1 * scale, x2 * scale, y2 * scale))

    labels = [{"text": label.text, "layer": label.layer, "texttype": label.texttype,
               "x_nm": round(label.origin[0] * scale, 4),
               "y_nm": round(label.origin[1] * scale, 4),
               "layer_name": names.get((label.layer, label.texttype), "")}
              for label in top.labels]

    areas: dict[str, float] = {}
    for polygon in top.polygons:
        name = (names.get((polygon.layer, polygon.datatype))
                or f"layer_{polygon.layer}_{polygon.datatype}")
        areas[name] = round(areas.get(name, 0.0) + polygon.area(), 9)

    facts = {
        "file": gds.name,
        "top_cell": top.name,
        "cell_count": len(library.cells),
        "dbu_um": library.unit * library.precision / 1e-6 * 1e-6,
        "polygon_count": len(top.polygons),
        "label_count": len(labels),
        "labels": labels,
        "layer_count": len(shapes),
        "layer_names": sorted(shapes),
        "shape_count_by_layer": {k: len(v) for k, v in sorted(shapes.items())},
        "area_um2_by_layer": {k: areas[k] for k in sorted(areas)},
    }
    return shapes, facts


# --- measurements, formulated differently from the module under test ----------

def min_extent(rects: list[Rect], axis: str) -> float | None:
    return min((r.extent(axis) for r in rects), default=None)


def min_gap(a: list[Rect], b: list[Rect], axis: str) -> float | None:
    """Smallest positive gap between facing shapes, by interval scanning.

    Formulated as a scan over sorted intervals rather than as a pairwise distance
    loop, so a fault in one shape of the reasoning is unlikely to appear in the other.
    """
    other = "y" if axis == "x" else "x"
    best = None
    for first in a:
        # Only the shapes that share coordinates on the other axis face this one.
        facing = sorted((s for s in b if first.meets(s, other)),
                        key=lambda s: s.lo(axis))
        for second in facing:
            gap = (second.lo(axis) - first.hi(axis) if second.lo(axis) >= first.hi(axis)
                   else first.lo(axis) - second.hi(axis))
            gap = round(gap, 4)
            if gap > 0 and (best is None or gap < best):
                best = gap
    return best


def min_extension(inner: list[Rect], outer: list[Rect], axis: str) -> float | None:
    other = "y" if axis == "x" else "x"
    best = None
    for i in inner:
        for o in outer:
            if not i.meets(o, other):
                continue
            for value in (round(i.lo(axis) - o.lo(axis), 4),
                          round(o.hi(axis) - i.hi(axis), 4)):
                if value > 0 and (best is None or value < best):
                    best = value
    return best


def centres_pitch(rects: list[Rect], axis: str) -> dict[str, Any]:
    """Distinct centre positions and the steps between them."""
    centres = sorted({r.centre(axis) for r in rects})
    steps = [round(b - a, 4) for a, b in zip(centres, centres[1:])]
    dominant = max(set(steps), key=steps.count) if steps else None
    return {"centres_nm": centres, "steps_nm": steps, "dominant_step_nm": dominant}


def profile(rects: list[Rect], axis: str, lo: float, hi: float) -> list[float]:
    """Cell cross-section: margin, width, gap, ... margin, by merging then walking."""
    intervals = sorted((r.lo(axis), r.hi(axis)) for r in rects)
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    sequence, position = [], lo
    for start, end in merged:
        sequence.append(round(start - position, 4))
        sequence.append(round(end - start, 4))
        position = end
    sequence.append(round(hi - position, 4))
    return sequence


def fact_sheet(gds: Path, lyp: Path = DEFAULT_LYP) -> dict[str, Any]:
    """Everything an answer about this file could be judged against."""
    shapes, facts = read(gds, lyp)
    get = lambda name: shapes.get(name, [])  # noqa: E731

    boundary = get("CELL-BOUNDARY")
    cell = boundary[0] if boundary else None
    npoly, ppoly = get("NPOLY"), get("PPOLY")
    ndiff, pdiff = get("NDIFF"), get("PDIFF")
    ndiffcon, pdiffcon = get("NDIFFCON"), get("PDIFFCON")

    # The gates are taller than they are wide, so they run along y; widths are
    # measured across that and extensions along it.
    along = "y" if npoly and npoly[0].extent("y") > npoly[0].extent("x") else "x"
    across = "x" if along == "y" else "y"

    facts["cell_boundary_nm"] = ({"width": cell.extent("x"), "height": cell.extent("y")}
                                 if cell else None)
    facts["poly_direction"] = along

    # Poly first. A decap cell draws no gates at all, and rule 3.3.8 gives the
    # diffcon columns the same pitch, so they carry the answer when poly is absent.
    gate = centres_pitch(npoly + ppoly, across)
    gate_source = "poly columns"
    if gate["dominant_step_nm"] is None:
        gate = centres_pitch(ndiffcon + pdiffcon, across)
        gate_source = "diffcon columns (rule 3.3.8; this cell draws no poly)"
    facts["gate_pitch_nm"] = gate["dominant_step_nm"]
    facts["gate_pitch_source"] = gate_source
    facts["gate_pitch_detail"] = gate
    if cell and gate["dominant_step_nm"]:
        facts["gate_pitches_across_cell"] = round(
            cell.extent(across) / gate["dominant_step_nm"], 6)

    facts["m0_track_count"] = len(get("M0-TRACK-GUIDE"))
    for metal in ("M0", "M1", "M2"):
        guides = get(f"{metal}-TRACK-GUIDE")
        axis = across if metal == "M1" else along
        facts[f"{metal.lower()}_pitch_nm"] = centres_pitch(guides, axis)["dominant_step_nm"]
        if cell:
            facts[f"{metal.lower()}_profile_nm"] = profile(
                guides, axis, cell.lo(axis), cell.hi(axis))
    facts["metals_with_track_guides"] = [m for m in ("M0", "M1", "M2")
                                         if get(f"{m}-TRACK-GUIDE")]
    facts["metals_with_geometry"] = [m for m in ("M0", "M1", "M2") if get(m)]

    facts["tech_parameters_nm"] = {
        "N-poly width": min_extent(npoly, across),
        "P-poly width": min_extent(ppoly, across),
        "N-diffcon width": min_extent(ndiffcon, across),
        "P-diffcon width": min_extent(pdiffcon, across),
        "Diffusion width": min_extent(ndiff + pdiff, along),
        "Power rail width": min_extent(get("BM0"), along),
        "N/P Diffusion spacing": min_gap(ndiff, pdiff, along),
        "Poly to Diffcon spacing": min_gap(npoly + ppoly, ndiffcon + pdiffcon, across),
        "Gate Cut spacing": min_gap(npoly, ppoly, along),
        "Diffcon ETE spacing": min_gap(ndiffcon, pdiffcon, along),
        "Gate extension": min((v for v in (min_extension(ndiff, npoly, along),
                                           min_extension(pdiff, ppoly, along))
                               if v is not None), default=None),
        "Diffcon extension": min((v for v in (min_extension(ndiff, ndiffcon, along),
                                              min_extension(pdiff, pdiffcon, along))
                                  if v is not None), default=None),
    }

    # Power delivery and orientation, from the labels rather than from any classifier.
    ground = {"VSS", "GND", "VGND"}
    power = {"VDD", "VPWR", "VCC"}
    backside = [lbl for lbl in facts["labels"] if lbl["layer_name"].startswith("BM0")]
    facts["backside_supply_labels"] = sorted({lbl["text"] for lbl in backside
                                              if lbl["text"] in ground | power})
    facts["power_delivery"] = ("backside" if {t for t in facts["backside_supply_labels"]}
                              & ground and {t for t in facts["backside_supply_labels"]}
                              & power else None)
    ground_y = [lbl["y_nm"] for lbl in backside if lbl["text"] in ground]
    power_y = [lbl["y_nm"] for lbl in backside if lbl["text"] in power]
    facts["orientation"] = ("R0" if ground_y and power_y and min(ground_y) < min(power_y)
                            else "Mx" if ground_y and power_y else None)

    # Technology: GAA is separated diffusions with no nwell, CFET is touching.
    touching = any(a.meets(b, "x") and a.meets(b, "y") for a in ndiff for b in pdiff)
    facts["diffusions_touch"] = touching
    facts["nwell_shape_count"] = len(get("NWELL"))
    facts["technology"] = ("CFET" if touching
                           else "FinFET" if get("NWELL") else "GAA")

    facts["via_layers"] = sorted(n for n in shapes
                                 if "VIA" in n.upper() or n.upper() == "DVB")
    facts["via_shape_total"] = sum(len(shapes[n]) for n in facts["via_layers"])
    return facts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", action="append", default=None)
    parser.add_argument("--lyp", default=str(DEFAULT_LYP))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    files = ([Path(g) for g in args.gds] if args.gds else sorted(SAMPLES.glob("*.gds")))
    sheets = [fact_sheet(path, Path(args.lyp)) for path in files]

    if args.json:
        print(json.dumps(sheets, indent=1))
        return 0

    for sheet in sheets:
        print(f"\n=== {sheet['file']}  (top cell {sheet['top_cell']}, "
              f"{sheet['polygon_count']} polygons, {sheet['layer_count']} layers)")
        box = sheet["cell_boundary_nm"]
        print(f"    cell boundary   {box['width']:g} x {box['height']:g} nm"
              if box else "    cell boundary   absent")
        pitch = sheet["gate_pitch_nm"]
        print(f"    gate pitch      {pitch:g} nm  "
              f"({sheet.get('gate_pitches_across_cell')} across the cell, from "
              f"{sheet['gate_pitch_source']})" if pitch is not None
              else "    gate pitch      not derivable")
        print(f"    metal pitches   M0 {sheet['m0_pitch_nm']}  M1 {sheet['m1_pitch_nm']}"
              f"  M2 {sheet['m2_pitch_nm']} nm")
        print(f"    M0 tracks       {sheet['m0_track_count']}")
        print(f"    technology      {sheet['technology']}, {sheet['power_delivery']} "
              f"power, {sheet['orientation']}")
        print(f"    metals          guides {sheet['metals_with_track_guides']}, "
              f"drawn {sheet['metals_with_geometry']}")
        print("    tech parameters " + ", ".join(
            f"{k} {v:g}" for k, v in sheet["tech_parameters_nm"].items()
            if v is not None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
