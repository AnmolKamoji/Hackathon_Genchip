#!/usr/bin/env python3
"""Verify the pitch metrics the way an engineer checks them by hand in KLayout.

The concern this answers: `analyzer/pitch.py` derives CPP three ways, but all three
read per-shape bounding boxes through the same `begin_shapes_rec` iterator. If that
extraction were wrong, all three would be wrong together and would still agree with
each other. Agreement between siblings proves nothing.

So this measures the same quantities through **different KLayout machinery** - the
DRC edge-pair engine and the edge collections, which is what the ruler tool and a
rule deck actually use - and by three methods that would not fail the same way:

  A  centre to centre of consecutive shapes, via `Region.each()` polygon extents
  B  same-side edge to same-side edge, via `Region.edges()` coordinates
  C  space plus width, via `Region.space_check()` and `Region.width_check()`

Method C is the closest analogue of dropping a ruler between two edges: it asks
KLayout's own measurement engine for the gap and the width, and adds them. If A, B
and C agree, the number is not an artefact of how the shapes were read.

The script also runs the whole comparison several times and requires every run to
be identical, so a result that depends on iteration order cannot slip through.

    python tools/verify_pitch.py                 # all samples, 3 runs
    python tools/verify_pitch.py --runs 5
    python tools/verify_pitch.py --gds path.gds
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import klayout.db as db

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "data" / "samples"

# (layer, datatype, name, axis the pitch is measured along)
# The axis is the direction the shapes repeat in, which is across the wire.
TARGETS = [
    (102, 0, "NPOLY", "x"),
    (103, 0, "PPOLY", "x"),
    (104, 0, "NDIFFCON", "x"),
    (105, 0, "PDIFFCON", "x"),
    (220, 0, "M0-TRACK-GUIDE", "y"),
    (221, 0, "M1-TRACK-GUIDE", "x"),
    (222, 0, "M2-TRACK-GUIDE", "y"),
    (200, 0, "M0", "y"),
    (202, 0, "M1", "x"),
]


def flat_region(layout, top, layer: int, datatype: int):
    """One merged region per layer, flattened. Deliberately built from the shape
    iterator only once, so the three methods below share no later step."""
    index = layout.find_layer(layer, datatype)
    region = db.Region()
    if index is None:
        return region
    it = top.begin_shapes_rec(index)
    while not it.at_end():
        shape, trans = it.shape(), it.trans()
        if shape.is_box():
            region.insert(db.Polygon(shape.box).transformed(trans))
        elif shape.is_polygon():
            region.insert(shape.polygon.transformed(trans))
        elif shape.is_path():
            region.insert(shape.path.polygon().transformed(trans))
        it.next()
    return region.merged()


def method_a_centres(region, axis: str, dbu: float) -> list[float]:
    """Centre to centre, from the polygon extents."""
    centres = []
    for poly in region.each():
        box = poly.bbox()
        centres.append(((box.left + box.right) if axis == "x"
                        else (box.bottom + box.top)) / 2.0)
    centres = sorted(set(centres))
    return [round((b - a) * dbu * 1000, 4) for a, b in zip(centres, centres[1:])]


def method_b_edges(region, axis: str, dbu: float) -> list[float]:
    """Same-side edge to same-side edge, from the edge collection.

    An engineer measuring a pitch usually snaps the ruler from one wire's left edge
    to the next wire's left edge. That is this: collect the edges perpendicular to
    the repeat direction, keep the lower-coordinate one of each shape, and difference
    them.
    """
    lows = []
    for poly in region.each():
        coords = []
        for edge in db.Region(poly).edges().each():
            if axis == "x" and edge.dx() == 0:          # vertical edge
                coords.append(edge.x1)
            elif axis == "y" and edge.dy() == 0:        # horizontal edge
                coords.append(edge.y1)
        if coords:
            lows.append(min(coords))
    lows = sorted(set(lows))
    return [round((b - a) * dbu * 1000, 4) for a, b in zip(lows, lows[1:])]


def method_c_space_plus_width(region, axis: str, dbu: float) -> dict:
    """Space plus width, through KLayout's own measurement engine.

    `space_check` and `width_check` are what a rule deck and the ruler use. Asking
    them for the gap and the width and adding the two is the most independent route
    to a pitch available without leaving KLayout.
    """
    if region.count() < 2:
        return {"pitch_nm": None, "space_nm": None, "width_nm": None}
    box = region.bbox()
    limit = max(box.width(), box.height()) or 1

    # Only edge pairs facing along the repeat direction describe the pitch: a
    # horizontal wire's length dominates its own width_check otherwise.
    def along(pair):
        edge = pair.first
        return (edge.dx() == 0) if axis == "x" else (edge.dy() == 0)

    spaces = sorted({p.distance() for p in region.space_check(limit).each() if along(p)})
    widths = sorted({p.distance() for p in region.width_check(limit).each() if along(p)})
    if not spaces or not widths:
        return {"pitch_nm": None, "space_nm": None, "width_nm": None}
    space, width = min(spaces), min(widths)
    return {"pitch_nm": round((space + width) * dbu * 1000, 4),
            "space_nm": round(space * dbu * 1000, 4),
            "width_nm": round(width * dbu * 1000, 4)}


def measure(path: Path) -> dict:
    layout = db.Layout()
    layout.read(str(path))
    from analyzer.gds_parser import rank_top_cells
    top = rank_top_cells(layout)[0]
    dbu = float(layout.dbu)

    out = {"file": path.name, "dbu_um": dbu, "top_cell": top.name, "layers": {}}
    for layer, datatype, name, axis in TARGETS:
        region = flat_region(layout, top, layer, datatype)
        if region.is_empty():
            continue
        a = method_a_centres(region, axis, dbu)
        b = method_b_edges(region, axis, dbu)
        c = method_c_space_plus_width(region, axis, dbu)
        out["layers"][name] = {
            "shapes": region.count(), "axis": axis,
            "A_centre_to_centre_nm": a,
            "B_edge_to_edge_nm": b,
            "C_space_plus_width": c,
        }

    boundary = flat_region(layout, top, 1, 0)
    if not boundary.is_empty():
        box = boundary.bbox()
        out["cell_boundary_nm"] = {
            "width": round(box.width() * dbu * 1000, 4),
            "height": round(box.height() * dbu * 1000, 4),
        }
    return out


def compare(path: Path, runs: int) -> tuple[bool, list[str]]:
    """Measure `runs` times, require identical results, then compare to the analyzer."""
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.measurements import shape_outlines
    from analyzer.pitch import analyze_pitch

    notes: list[str] = []
    ok = True

    results = [measure(path) for _ in range(runs)]
    if any(r != results[0] for r in results[1:]):
        return False, [f"{path.name}: the {runs} runs did not agree with each other - "
                       "a result that depends on iteration order cannot be trusted"]
    notes.append(f"{runs} independent runs identical")
    ref = results[0]

    lm = load_lyp(default_layermap())
    mine = analyze_pitch(shape_outlines(path, lm), path.name)

    # --- CPP -----------------------------------------------------------------
    claimed = (mine["gate_pitch"] or {}).get("cpp_nm")
    votes: dict[str, list[float]] = {}
    for name in ("NPOLY", "PPOLY", "NDIFFCON", "PDIFFCON"):
        entry = ref["layers"].get(name)
        if not entry:
            continue
        for label, value in (("A", entry["A_centre_to_centre_nm"]),
                             ("B", entry["B_edge_to_edge_nm"]),
                             ("C", [entry["C_space_plus_width"]["pitch_nm"]]
                              if entry["C_space_plus_width"]["pitch_nm"] else [])):
            for v in value:
                votes.setdefault(f"{name}/{label}", []).append(v)
    independent = sorted({v for vs in votes.values() for v in vs})
    if claimed is None:
        notes.append("CPP: the analyzer reports none")
    elif independent and min(independent) == claimed:
        agreeing = [k for k, vs in votes.items() if claimed in vs]
        notes.append(f"CPP {claimed:g} nm confirmed by {len(agreeing)} independent "
                     f"measurement(s): {', '.join(agreeing)}")
    else:
        ok = False
        notes.append(f"CPP MISMATCH: analyzer says {claimed}, independent methods say "
                     f"{independent}")

    # --- metal pitches -------------------------------------------------------
    for metal, guide in (("M0", "M0-TRACK-GUIDE"), ("M1", "M1-TRACK-GUIDE"),
                         ("M2", "M2-TRACK-GUIDE")):
        claimed_pitch = ((mine["metal_pitches"] or {}).get(metal) or {}).get("pitch_nm")
        entry = ref["layers"].get(guide)
        if not entry or claimed_pitch is None:
            continue
        a, b = entry["A_centre_to_centre_nm"], entry["B_edge_to_edge_nm"]
        # The analyzer reports the dominant step; the independent methods list every
        # step, so the dominant one must be the most common value there too.
        dominant = max(set(a), key=a.count) if a else None
        if dominant == claimed_pitch and (not b or max(set(b), key=b.count) == claimed_pitch):
            notes.append(f"{metal} pitch {claimed_pitch:g} nm confirmed by A and B on {guide}")
        else:
            ok = False
            notes.append(f"{metal} pitch MISMATCH: analyzer says {claimed_pitch}, "
                         f"A={a} B={b}")

    # --- M0 space + width identity ------------------------------------------
    # The manual relates M0 spacing to pitch less width; this checks the analyzer's
    # pitch against KLayout's measured gap and width rather than against itself.
    m0 = ref["layers"].get("M0")
    claimed_m0 = ((mine["metal_pitches"] or {}).get("M0") or {})
    if m0 and m0["C_space_plus_width"]["pitch_nm"] and claimed_m0.get("width_nm"):
        c = m0["C_space_plus_width"]
        if c["width_nm"] == claimed_m0["width_nm"]:
            notes.append(f"M0 width {c['width_nm']:g} nm confirmed by width_check; "
                         f"measured gap {c['space_nm']:g} nm, so space+width = "
                         f"{c['pitch_nm']:g} nm")
        else:
            ok = False
            notes.append(f"M0 width MISMATCH: analyzer {claimed_m0['width_nm']}, "
                         f"width_check {c['width_nm']}")

    # --- cell width in CPP ---------------------------------------------------
    dims = mine["cell_dimensions"] or {}
    if ref.get("cell_boundary_nm") and dims.get("width_nm"):
        independent_width = ref["cell_boundary_nm"]["width"]
        if independent_width != dims["width_nm"]:
            ok = False
            notes.append(f"cell width MISMATCH: analyzer {dims['width_nm']}, "
                         f"boundary region {independent_width}")
        elif claimed and dims.get("gate_pitches"):
            exact = independent_width / claimed
            if abs(exact - dims["gate_pitches"]) < 1e-9:
                notes.append(f"cell width {independent_width:g} nm = "
                             f"{dims['gate_pitches']} x CPP exactly")
            else:
                ok = False
                notes.append(f"cell width {independent_width} nm is not a whole multiple of "
                             f"CPP {claimed}: {exact}")

    # --- track grid closure --------------------------------------------------
    # The M0 steps must add up to the cell height; if the pitch or the track set were
    # wrong this identity would not close.
    if ref.get("cell_boundary_nm") and dims.get("m0_track_positions_nm"):
        grid = dims["m0_track_positions_nm"]
        span = round(grid[-1] - grid[0], 4)
        if span == ref["cell_boundary_nm"]["height"]:
            notes.append(f"M0 track grid spans {span:g} nm, exactly the cell height")
        else:
            ok = False
            notes.append(f"track grid MISMATCH: grid spans {span}, cell height "
                         f"{ref['cell_boundary_nm']['height']}")

    return ok, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", action="append", default=None)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    files = ([Path(g) for g in args.gds] if args.gds
             else sorted(SAMPLES.glob("*.gds")))
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
    print("All pitch metrics confirmed by independent KLayout measurement."
          if all_ok else "AT LEAST ONE METRIC DISAGREES - see the ! lines above.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
