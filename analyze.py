from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analyzer.comparison import compare_metadata
from analyzer.connectivity import (analyze_connectivity, default_stack, load_stack,
                                   stack_from_sidecar)
from analyzer.hierarchy import analyze_hierarchy
from analyzer.measurements import measure_layers, measure_vias
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds, save_metadata
from analyzer.layermap import default_layermap, find_layermap, load_lyp
from analyzer.sidecar_parser import analyze_sidecar


def find_sidecar(gds: Path, sidecar_dir: str | None) -> Path | None:
    """Locate the semantic JSON sidecar for a GDS file.

    Looks in --sidecar-dir first, then next to the GDS itself, so the documented
    `python analyze.py data/samples/A.gds data/samples/B.gds` picks up the
    sidecars that ship beside the samples without extra flags.
    """
    candidates = []
    if sidecar_dir:
        candidates.append(Path(sidecar_dir) / (gds.stem + ".json"))
    candidates.append(gds.with_suffix(".json"))
    for c in candidates:
        if c.exists():
            return c
    return None


def analyze_one(gds: Path, sidecar_dir: str | None, mode: str,
                layermap: dict | None = None) -> dict:
    """Analyze one GDS, fusing its sidecar when one is available."""
    sidecar = None if mode == "gds" else find_sidecar(gds, sidecar_dir)

    if mode == "sidecar":
        if not sidecar:
            raise SystemExit(f"--mode sidecar requires a JSON sidecar for {gds.name}, none found.")
        return analyze_sidecar(sidecar, gds.name)
    if sidecar and gds.exists():
        return analyze_pair(gds, sidecar, layermap=layermap)
    if sidecar:
        return analyze_sidecar(sidecar, gds.name)
    return analyze_gds(gds, layermap=layermap)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Deterministically analyze GDSII layouts and compare two of them.",
    )
    p.add_argument("gds_files", nargs="+")
    p.add_argument("--sidecar-dir", default=None,
                   help="Directory holding <stem>.json sidecars. Defaults to alongside each GDS file.")
    p.add_argument("--out", default="reports")
    p.add_argument("--mode", choices=["auto", "gds", "sidecar"], default="auto",
                   help="auto: fuse GDS geometry with the sidecar when both exist (default). "
                        "gds: ignore sidecars. sidecar: require sidecars and ignore GDS geometry.")
    p.add_argument("--layermap", default=None,
                   help="KLayout .lyp layer-properties file giving technology names for each "
                        "(layer, datatype). Auto-detected next to the GDS when not given; "
                        "pass --layermap none to disable.")
    p.add_argument("--stack", default=None,
                   help="JSON connection stack mapping each via/contact layer to the two conductor "
                        "layers it joins. GDSII stores no layer elevations, so this is the one piece "
                        "of information neither the .gds nor the .lyp can provide; without it the "
                        "net graph is not built. See data/samples/Titan_stack.json.")
    p.add_argument("--no-connectivity", action="store_true",
                   help="Skip physical connectivity analysis.")
    p.add_argument("--quiet", action="store_true", help="Only print the output paths.")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # A .lyp turns numeric layers into technology names even with no sidecar.
    layermap = None
    if (args.layermap or "").lower() != "none":
        lyp_path = Path(args.layermap) if args.layermap else find_layermap(Path(args.gds_files[0]))
        if lyp_path and lyp_path.exists():
            try:
                layermap = load_lyp(lyp_path)
                origin = ("bundled default" if lyp_path == default_layermap() else str(lyp_path))
                print(f"Layer map: {lyp_path.name} ({layermap['entry_count']} entries, {origin})")
            except ValueError as exc:
                print(f"WARNING: ignoring layer map: {exc}", file=sys.stderr)
        elif args.layermap:
            raise SystemExit(f"Layer map not found: {args.layermap}")

    stack = None
    if args.stack:
        if not layermap:
            raise SystemExit("--stack needs a layer map as well, so its layer names can be resolved "
                             "to layer numbers. Supply --layermap or place a .lyp beside the GDS.")
        stack = load_stack(Path(args.stack), layermap)
        print(f"Connection stack: {Path(args.stack).name} "
              f"({stack['usable_count']} via/contact rules, {len(stack['same_level'])} same-level)")
        for problem in stack["problems"]:
            print(f"WARNING: connection stack: {problem}", file=sys.stderr)

    results = []
    for g in args.gds_files:
        gds = Path(g)
        if not gds.exists() and args.mode != "sidecar":
            raise SystemExit(f"GDS file not found: {gds}")
        meta = analyze_one(gds, args.sidecar_dir, args.mode, layermap)
        if not args.no_connectivity and gds.exists():
            use = stack
            if use is None and meta.get("metadata_source") in ("fused", "sidecar"):
                # Sidecar via layers are named after their endpoints (VIA_M0_M1),
                # which states the stack rather than leaving it to be guessed.
                derived = stack_from_sidecar(meta, layermap)
                if derived["usable_count"]:
                    use = derived
                    print(f"Connection stack derived from sidecar via names "
                          f"({derived['usable_count']} rules)")
            if use is None:
                use = default_stack(layermap)
                if use:
                    print(f"Connection stack: bundled technology default "
                          f"({use['usable_count']} rules, not PDK-verified)")
            meta["connectivity"] = analyze_connectivity(gds, layermap, stack=use)
            meta["hierarchy"] = analyze_hierarchy(gds)
            overrides = (use or {}).get("role_overrides") or None
            meta["measurements"] = measure_layers(gds, layermap, overrides)
            meta["measurements"]["vias"] = measure_vias(meta["measurements"])
            for w in meta["connectivity"]["warnings"]:
                print(f"WARNING: connectivity: {w}", file=sys.stderr)
        dest = out / (gds.stem + ".metadata.json")
        save_metadata(meta, dest)
        results.append(meta)
        if args.quiet:
            print(f"Wrote {dest}")
        else:
            print(f"=== {meta['source']['file']}  [{meta['metadata_source']}]")
            print(json.dumps(meta["design"], indent=2))
            conn = meta.get("connectivity")
            if conn:
                t1 = conn["intra_layer"]
                print(f"connectivity: {t1['total_shapes']} conducting shapes -> "
                      f"{t1['total_components']} within-layer conductors (exact, GDS-only)")
                nets = conn.get("nets")
                if nets and nets.get("available"):
                    s = nets["summary"]
                    print(f"              {s['net_count']} net(s) under the "
                          f"{conn['stack_source']} stack, {s['floating_net_count']} floating")
                else:
                    print("              net graph not built (no connection stack supplied)")
            if meta.get("consistency") and not meta["consistency"]["agrees"]:
                print("WARNING: GDS and sidecar disagree:", json.dumps(meta["consistency"]), file=sys.stderr)

    if len(results) == 2:
        comparison = compare_metadata(results[0], results[1])
        (out / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        print(f"Comparison written to {out / 'comparison.json'}")
        for w in comparison["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        if not args.quiet:
            print(json.dumps(comparison["summary"], indent=2))
    elif len(results) > 2:
        print(f"Analyzed {len(results)} files. Comparison is only written for exactly two inputs.")


if __name__ == "__main__":
    main()
