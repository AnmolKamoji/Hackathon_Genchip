#!/usr/bin/env python3
"""Render a layout to PNG through KLayout's own view engine.

Useful for eyeballing a cell - is the power rail where you expect, does the M1 stripe
run where the net list says - and for putting a picture next to a report. It uses
`klayout.lay`, the same LayoutView the GUI draws with, so the colours and fill patterns
are the ones in the .lyp rather than an approximation.

An image is for sanity, not for measurement. Nothing in the analysis reads a rendered
image, and it should not: a pixel is about 0.15 nm at these zoom levels, so anything
measured off a picture is worse than the numbers already computed from the geometry.

    python tools/render.py                                  # every sample
    python tools/render.py --gds path.gds --out /tmp
    python tools/render.py --layers M0,M1,BM0               # only these
    python tools/render.py --hide-guides                    # drop the track guides
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import klayout.lay as lay

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "data" / "samples"
DEFAULT_LYP = SAMPLES / "Titan_layer_properties.lyp"

# Layers that are scaffolding rather than the design. Track guides tile the whole cell,
# so leaving them on hides everything underneath.
SCAFFOLD = ("TRACK-GUIDE", "DUPLICATE", "EXTENDED", "PATTERN-CUT")


def render(gds: Path, out: Path, lyp: Path = DEFAULT_LYP, width: int = 1600,
           height: int = 900, only: list[str] | None = None,
           hide_guides: bool = False) -> Path:
    view = lay.LayoutView()
    view.load_layout(str(gds), 0)
    if lyp.exists():
        view.load_layer_props(str(lyp))
    view.max_hier()

    if only or hide_guides:
        for layer in view.each_layer():
            name = layer.name or layer.source or ""
            if only:
                layer.visible = any(want.lower() in name.lower() for want in only)
            elif any(tag in name.upper() for tag in SCAFFOLD):
                layer.visible = False

    view.zoom_fit()
    out.parent.mkdir(parents=True, exist_ok=True)
    view.save_image(str(out), width, height)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", action="append", default=None)
    parser.add_argument("--lyp", default=str(DEFAULT_LYP))
    parser.add_argument("--out", default=None, help="directory for the PNGs")
    parser.add_argument("--layers", default=None,
                        help="comma-separated layer names to show, others hidden")
    parser.add_argument("--hide-guides", action="store_true",
                        help="hide track guides, duplicates and pattern-cut layers")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()

    files = ([Path(g) for g in args.gds] if args.gds else sorted(SAMPLES.glob("*.gds")))
    out_dir = Path(args.out) if args.out else ROOT / "build" / "renders"
    only = [s.strip() for s in args.layers.split(",")] if args.layers else None

    for path in files:
        suffix = ("-" + "-".join(only) if only else "-clean" if args.hide_guides else "")
        target = out_dir / f"{path.stem}{suffix}.png"
        render(path, target, Path(args.lyp), args.width, args.height, only,
               args.hide_guides)
        print(f"  {target}  ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
