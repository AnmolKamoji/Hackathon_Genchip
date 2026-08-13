#!/usr/bin/env python3
"""Independent ground truth for the reference layouts.

Deliberately imports **nothing** from `analyzer/`. It re-reads the `.lyp` XML and
the GDSII with raw KLayout and plain Python, so its numbers are an independent
check rather than a restatement of the tool's own output. Comparing the tool
against itself proves nothing; this is what it gets compared against.

    python tools/ground_truth.py            # print the report
    python tools/ground_truth.py --json     # machine-readable, for the diff test
"""
from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import klayout.db as db

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"


# --------------------------------------------------------------- the layer map

def read_lyp(path: Path) -> dict:
    """Parse a KLayout .lyp by hand.

    A <source> reads `layer/datatype@cellview`; the trailing `@1` is the cellview
    index and is not part of the layer identity.
    """
    root = ET.parse(path).getroot()
    entries = {}
    duplicates = []
    for node in root.iter("properties"):
        src = node.findtext("source") or ""
        name = (node.findtext("name") or "").strip()
        m = re.match(r"\s*(\d+)\s*/\s*(\d+)", src)
        if not m or not name:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if key in entries:
            duplicates.append((key, entries[key], name))
        entries[key] = name
    return {"file": path.name, "entries": entries, "duplicate_keys": duplicates}


# Independent role classification, written from the names as a human would read
# them rather than by reusing the analyzer's patterns.
VIA_NAMES = {"VIA0", "VIA1", "DVB", "P-VIAG", "N-VIAG", "P-VIAT", "N-VIAT",
             "BSPDN-PMOS-VIA"}
CONTACT_NAMES = {"NDIFFCON", "PDIFFCON"}
METAL_NAMES = {"M0", "M1", "M2", "BM0"}
POLY_NAMES = {"NPOLY", "PPOLY"}
DIFF_NAMES = {"NDIFF", "PDIFF", "DIFF-INTERCONNECT"}
WELL_NAMES = {"NWELL"}


def role_of(name: str) -> str:
    if name in VIA_NAMES:
        return "via"
    if name in CONTACT_NAMES:
        return "contact"
    if name in METAL_NAMES:
        return "metal"
    if name in POLY_NAMES:
        return "poly"
    if name in DIFF_NAMES:
        return "diffusion"
    if name in WELL_NAMES:
        return "well"
    return "other"


# ------------------------------------------------------------------- the GDSII

def read_gds(path: Path, lyp: dict) -> dict:
    layout = db.Layout()
    layout.read(str(path))
    dbu = float(layout.dbu)

    tops = list(layout.top_cells())
    # Rank by reachable shape count, mirroring what a human would pick as "the"
    # cell, with the name breaking ties.
    def reach(cell):
        return sum(layout.cell(i).shapes(li).size()
                   for i in [cell.cell_index(), *cell.called_cells()]
                   for li in layout.layer_indexes())
    tops.sort(key=lambda c: (-reach(c), c.name))
    top = tops[0]

    per_layer = {}
    total_polys = total_texts = 0
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        polys, texts, kinds = [], 0, defaultdict(int)
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            s, t = it.shape(), it.trans()
            if s.is_text():
                texts += 1
                kinds["text"] += 1
            elif s.is_box():
                polys.append(db.Polygon(s.box).transformed(t)); kinds["box"] += 1
            elif s.is_polygon():
                polys.append(s.polygon.transformed(t)); kinds["polygon"] += 1
            elif s.is_path():
                polys.append(s.path.polygon().transformed(t)); kinds["path"] += 1
            it.next()
        if not polys and not texts:
            continue

        # Area by the shoelace formula on the merged outline, and perimeter by
        # summing edge lengths - neither taken from a KLayout convenience call.
        region = db.Region()
        for p in polys:
            region.insert(p)
        merged = region.merged()
        merged_polys = list(merged.each())

        def shoelace(poly):
            total = 0.0
            for pts in [list(poly.each_point_hull())] + \
                       [list(poly.each_point_hole(h)) for h in range(poly.holes())]:
                acc = 0.0
                for i in range(len(pts)):
                    x1, y1 = pts[i].x, pts[i].y
                    x2, y2 = pts[(i + 1) % len(pts)].x, pts[(i + 1) % len(pts)].y
                    acc += x1 * y2 - x2 * y1
                total += acc / 2.0
            return abs(total)

        area_dbu2 = sum(shoelace(p) for p in merged_polys)
        perim = 0.0
        vtx = 0
        for p in polys:
            pts = list(p.each_point_hull())
            vtx += len(pts)
            for i in range(len(pts)):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                perim += math.hypot(b.x - a.x, b.y - a.y)

        # Connected components by union-find over pairwise interaction.
        parent = list(range(len(polys)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(polys)):
            ri = db.Region(); ri.insert(polys[i])
            for j in range(i + 1, len(polys)):
                rj = db.Region(); rj.insert(polys[j])
                if ri.interacting(rj).count():
                    a, b = find(i), find(j)
                    if a != b:
                        parent[a] = b
        components = len({find(i) for i in range(len(polys))}) if polys else 0

        name = lyp["entries"].get(key, f"layer_{key[0]}_{key[1]}")
        per_layer[key] = {
            "name": name, "role": role_of(name),
            "shapes": len(polys), "texts": texts, "kinds": dict(kinds),
            "area_um2": round(area_dbu2 * dbu * dbu, 9),
            "perimeter_um": round(perim * dbu, 9),
            "vertices": vtx,
            "components": components,
            "merged_polygons": len(merged_polys),
        }
        total_polys += len(polys)
        total_texts += texts

    bbox = top.bbox()
    width = bbox.width() * dbu
    height = bbox.height() * dbu
    by_role = defaultdict(lambda: {"layers": [], "shapes": 0, "area_um2": 0.0})
    for key, row in per_layer.items():
        r = by_role[row["role"]]
        if row["shapes"]:
            r["layers"].append(row["name"])
            r["shapes"] += row["shapes"]
            r["area_um2"] = round(r["area_um2"] + row["area_um2"], 9)

    return {
        "file": path.name,
        "dbu_um": dbu,
        "top_cell": top.name,
        "top_cell_count": len(tops),
        "cell_count_total": layout.cells(),
        "instance_placements": sum(inst.size() for c in layout.each_cell()
                                   for inst in c.each_inst()),
        "hierarchy_depth": top.hierarchy_levels(),
        "layer_entries": len(per_layer),
        "polygons": total_polys,
        "texts": total_texts,
        "bbox_um": [round(width, 9), round(height, 9)],
        "bbox_area_um2": round(width * height, 9),
        "vias": by_role["via"]["shapes"],
        "via_layers": sorted(by_role["via"]["layers"]),
        "contacts": by_role["contact"]["shapes"],
        "contact_layers": sorted(by_role["contact"]["layers"]),
        "metal_area_um2": round(by_role["metal"]["area_um2"], 9),
        "metal_layers": sorted(by_role["metal"]["layers"]),
        "total_components": sum(r["components"] for r in per_layer.values()),
        "total_vertices": sum(r["vertices"] for r in per_layer.values()),
        "layers": {f"{k[0]}/{k[1]}": v for k, v in sorted(per_layer.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lyp", default=str(SAMPLES / "Titan_layer_properties.lyp"))
    ap.add_argument("gds", nargs="*", default=[str(p) for p in sorted(SAMPLES.glob("*.gds"))])
    args = ap.parse_args()

    lyp = read_lyp(Path(args.lyp))
    report = {"layer_map": {"file": lyp["file"], "entries": len(lyp["entries"]),
                            "duplicate_keys": lyp["duplicate_keys"]},
              "designs": {}}
    for g in args.gds:
        report["designs"][Path(g).name] = read_gds(Path(g), lyp)

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0

    print(f"LAYER MAP {lyp['file']}: {len(lyp['entries'])} entries, "
          f"{len(lyp['duplicate_keys'])} duplicate keys")
    roles = defaultdict(list)
    for key, name in sorted(lyp["entries"].items()):
        roles[role_of(name)].append(f"{name} ({key[0]}/{key[1]})")
    for role in ("metal", "via", "contact", "poly", "diffusion", "well", "other"):
        if roles[role]:
            print(f"  {role:<10} {len(roles[role]):>2}: {', '.join(roles[role][:6])}"
                  + (" ..." if len(roles[role]) > 6 else ""))
    for name, d in report["designs"].items():
        print(f"\n=== {name}")
        print(f"  top={d['top_cell']} cells={d['cell_count_total']} depth={d['hierarchy_depth']} "
              f"placements={d['instance_placements']}")
        print(f"  polygons={d['polygons']} texts={d['texts']} layers={d['layer_entries']} "
              f"components={d['total_components']} vertices={d['total_vertices']}")
        print(f"  bbox={d['bbox_um'][0]} x {d['bbox_um'][1]} um ({d['bbox_area_um2']} um2)")
        print(f"  vias={d['vias']} on {d['via_layers']}")
        print(f"  contacts={d['contacts']} on {d['contact_layers']}")
        print(f"  metal area={d['metal_area_um2']} um2 on {d['metal_layers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
