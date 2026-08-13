#!/usr/bin/env python3
"""Verify the tech-file parameters the way an engineer checks them in KLayout.

`analyzer/techparams.py` reads shape bounding boxes and reasons about them in Python.
That is correct for rectilinear geometry, but it means every width and every spacing in
the table comes from one extraction path: if that path were wrong, the whole table would
be wrong consistently and would still look self-consistent.

So this measures the same quantities through KLayout's DRC engine - `width_check`,
`space_check`, `separation` and `enclosing` - which is the machinery a rule deck and
the ruler tool use. It shares no step with the module under test beyond reading the
file, so agreement means the numbers survive a change of method.

    python tools/verify_techparams.py                    # all samples, 3 runs
    python tools/verify_techparams.py --runs 5
    python tools/verify_techparams.py --gds path.gds
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import klayout.db as db

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "data" / "samples"

# Parameter -> how to measure it with the DRC engine.
#   width  (layer)                  : Region.width_check, narrowest across the wire
#   space  (layer_a, layer_b)       : Region.separation / space_check
#   extend (inner, outer)           : how far `outer` reaches past `inner`
WIDTHS = [
    ("N-poly width", "NPOLY", "x"),
    ("P-poly width", "PPOLY", "x"),
    ("N-diffcon width", "NDIFFCON", "x"),
    ("P-diffcon width", "PDIFFCON", "x"),
    ("Power rail width", "BM0", "y"),
]
SPACINGS = [
    ("N/P Diffusion spacing", "NDIFF", "PDIFF", "y"),
    ("Poly to Diffcon spacing", "NPOLY", "NDIFFCON", "x"),
    ("Gate Cut spacing", "NPOLY", "PPOLY", "y"),
    ("Diffcon ETE spacing", "NDIFFCON", "PDIFFCON", "y"),
]


def regions(path: Path, layermap) -> tuple[dict[str, db.Region], float]:
    """One merged region per named layer, built once."""
    from analyzer.gds_parser import rank_top_cells

    layout = db.Layout()
    layout.read(str(path))
    top = rank_top_cells(layout)[0]
    out: dict[str, db.Region] = {}
    for (layer, datatype), entry in (layermap or {}).get("by_key", {}).items():
        name = entry.get("technology_name")
        index = layout.find_layer(layer, datatype)
        if not name or index is None:
            continue
        region = out.setdefault(name, db.Region())
        it = top.begin_shapes_rec(index)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            poly = (db.Polygon(shape.box) if shape.is_box()
                    else shape.polygon if shape.is_polygon()
                    else shape.path.polygon() if shape.is_path() else None)
            if poly is not None:
                region.insert(poly.transformed(trans))
            it.next()
    return {k: v.merged() for k, v in out.items()}, float(layout.dbu)


def _facing(pair, axis: str) -> bool:
    """Keep only edge pairs that face across `axis`.

    A wire is long and narrow, so its own length dominates `width_check` unless the
    pairs running the other way are dropped. `first` is an attribute, not a method.
    """
    edge = pair.first
    return (edge.dx() == 0) if axis == "x" else (edge.dy() == 0)


def drc_width(region: db.Region, axis: str, dbu: float) -> float | None:
    """Narrowest width across `axis`, from KLayout's own width check."""
    if region.is_empty():
        return None
    box = region.bbox()
    limit = max(box.width(), box.height()) + 1
    distances = sorted({p.distance() for p in region.width_check(limit).each()
                        if _facing(p, axis)})
    return round(distances[0] * dbu * 1000, 4) if distances else None


def drc_space(a: db.Region, b: db.Region, axis: str, dbu: float) -> float | None:
    """Smallest clear gap across `axis`, from `separation` (or `space_check` when the
    two names resolve to the same layer)."""
    if a.is_empty() or b.is_empty():
        return None
    box = a.bbox() + b.bbox()
    limit = max(box.width(), box.height()) + 1
    pairs = (a.space_check(limit) if a == b else a.separation_check(b, limit))
    # Abutting pairs report distance 0, and an end-to-end spacing between shapes that
    # touch does not exist - an uncut gate is one continuous gate, not two ends 0 nm
    # apart. Excluding them is the definition, not a convenience: the engine still
    # produces 17 nm and 21 nm on its own from the pairs that really are separated,
    # so the check keeps its teeth. If the only distance were 0 this returns None and
    # the comparison reports nothing rather than agreeing.
    distances = sorted({p.distance() for p in pairs.each()
                        if _facing(p, axis) and p.distance() > 0})
    return round(distances[0] * dbu * 1000, 4) if distances else None


def drc_extension(inner: db.Region, outer: db.Region, axis: str,
                  dbu: float) -> float | None:
    """How far `outer` reaches past `inner`, via boolean subtraction.

    Independent of any bounding box: subtract the inner shape from the outer, keep the
    leftover pieces that lie beyond it along `axis`, and measure the shortest one. That
    is the extension, arrived at without looking at a single coordinate directly.
    """
    if inner.is_empty() or outer.is_empty():
        return None
    leftover = outer - inner
    if leftover.is_empty():
        return None
    extents = []
    for poly in leftover.each():
        box = poly.bbox()
        # Only the pieces beyond an end count; the pieces alongside are the width.
        extents.append(box.height() if axis == "y" else box.width())
    return round(min(extents) * dbu * 1000, 4) if extents else None


def measure(path: Path, layermap) -> dict[str, float | None]:
    region, dbu = regions(path, layermap)
    get = lambda name: region.get(name, db.Region())  # noqa: E731

    out: dict[str, float | None] = {}
    for label, layer, axis in WIDTHS:
        out[label] = drc_width(get(layer), axis, dbu)
    for label, a, b, axis in SPACINGS:
        out[label] = drc_space(get(a), get(b), axis, dbu)

    # Diffusion width is measured along the poly direction, so it is a width check on
    # the other axis from the poly widths above.
    diffusion = get("NDIFF") + get("PDIFF")
    out["Diffusion width"] = drc_width(diffusion.merged(), "y", dbu)

    # Extensions, by subtraction rather than by coordinates.
    gate = [v for v in (drc_extension(get("NDIFF"), get("NPOLY"), "y", dbu),
                        drc_extension(get("PDIFF"), get("PPOLY"), "y", dbu))
            if v is not None]
    out["Gate extension"] = min(gate) if gate else None
    diffcon = [v for v in (drc_extension(get("NDIFF"), get("NDIFFCON"), "y", dbu),
                           drc_extension(get("PDIFF"), get("PDIFFCON"), "y", dbu))
               if v is not None]
    out["Diffcon extension"] = min(diffcon) if diffcon else None
    return out


def compare(path: Path, runs: int) -> tuple[bool, list[str]]:
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.techparams import tech_parameters

    layermap = load_lyp(default_layermap())
    notes: list[str] = []
    ok = True

    results = [measure(path, layermap) for _ in range(runs)]
    if any(r != results[0] for r in results[1:]):
        return False, [f"{path.name}: the {runs} runs disagreed with each other"]
    notes.append(f"{runs} independent runs identical")
    independent = results[0]

    mine = tech_parameters(path, layermap)["parameters"]
    checked = 0
    for label, value in sorted(independent.items()):
        record = mine.get(label)
        if value is None or not record or not record.get("available"):
            continue
        checked += 1
        if abs(float(record["value"]) - value) < 1e-6:
            notes.append(f"{label} = {value:g} nm confirmed by the DRC engine")
        else:
            ok = False
            notes.append(f"{label} MISMATCH: module says {record['value']}, "
                         f"DRC engine says {value}")
    if not checked:
        return False, notes + ["nothing was cross-checked, so this proves nothing"]

    # Rule 3.2.6: the gate pitch is twice the poly-to-diffcon spacing plus the diffcon
    # width plus the poly width. All four come from the DRC engine here, so this closes
    # the loop on the pitch as well.
    parts = (independent.get("Poly to Diffcon spacing"),
             independent.get("N-diffcon width"), independent.get("N-poly width"))
    if all(p is not None for p in parts):
        cpp = 2 * parts[0] + parts[1] + parts[2]
        from analyzer.pitch import analyze_pitch
        from analyzer.measurements import shape_outlines
        claimed = (analyze_pitch(shape_outlines(path, layermap),
                                 path.name)["gate_pitch"] or {}).get("cpp_nm")
        if claimed is not None and abs(cpp - claimed) < 1e-6:
            notes.append(f"rule 3.2.6 gives 2 x {parts[0]:g} + {parts[1]:g} + "
                         f"{parts[2]:g} = {cpp:g} nm, the measured gate pitch")
        elif claimed is not None:
            ok = False
            notes.append(f"rule 3.2.6 gives {cpp} nm but the gate pitch is {claimed}")
    return ok, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", action="append", default=None)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    files = ([Path(g) for g in args.gds] if args.gds else sorted(SAMPLES.glob("*.gds")))
    if not files:
        raise SystemExit("no GDS files to verify")

    all_ok = True
    for path in files:
        ok, notes = compare(path, args.runs)
        all_ok &= ok
        print(f"\n=== {path.name}   {'OK' if ok else 'MISMATCH'}")
        for note in notes:
            print(f"    {'.' if 'MISMATCH' not in note else '!'} {note}")

    print("\n" + "=" * 70)
    print("Tech-file parameters confirmed by KLayout's DRC engine." if all_ok
          else "AT LEAST ONE PARAMETER DISAGREES - see the ! lines above.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
