#!/usr/bin/env python3
"""Contextual fact-checker for LLM answers about a layout.

A bag-of-numbers check is not enough. Layout metadata contains hundreds of
numeric values (coordinates, indices, datatypes), so almost any small integer
appears somewhere by coincidence and "73 polygons" passes a naive membership
test. This extracts *claims* - a number together with the thing it is asserted
about - and checks each against the specific metadata field that governs it.

    python tools/claimcheck.py                      # audit local + model answers
    python tools/claimcheck.py --deterministic-only  # no API calls
    python tools/claimcheck.py --self-test           # negative control only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from ai.deterministic import answer, answer_comparison            # noqa: E402
from ai.llm import ask_llm, generate_comparison, generate_review, looks_like_failure, provider_status  # noqa: E402
from analyzer.comparison import compare_metadata                  # noqa: E402
from analyzer.fused import analyze_pair                           # noqa: E402
from analyzer.layermap import find_layermap, load_lyp             # noqa: E402

SAMPLES = ROOT / "data/samples"
NUM = r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)"


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _close(a: float, b: float) -> bool:
    """True when `a` is `b`, or a correctly-rounded rendering of it.

    An answer that prints 72.857143% as "72.86%" is faithful, not fabricated, so
    a rounding at the precision actually written must not be flagged.
    """
    if abs(a - b) <= max(1e-9, abs(b) * 1e-6):
        return True
    for digits in range(0, 7):
        if abs(a - round(b, digits)) <= 5e-10:
            return True
    return False


class Checker:
    """Builds the allowed value sets for each kind of claim, then audits prose."""

    def __init__(self, meta: dict):
        # Accepts either a metadata object or a comparison object; a comparison
        # has no design/layout blocks, only summary and per-layer deltas.
        self.meta = meta
        d = meta.get("design") or {}
        lay = meta.get("layout") or {}
        rows = meta.get("layers", [])
        groups = meta.get("layer_groups", [])

        # A "N polygons" claim is legitimate as the design total, any layer's
        # count, or any group's record/unique count - but nothing else.
        self.polygon_counts = ({d.get("polygon_count"), d.get("polygon_record_count")}
                               | {r.get("polygon_count") for r in rows}
                               | {r.get("merged_polygon_count") for r in rows}
                               | {g.get("polygon_records") for g in groups}
                               | {g.get("unique_polygons") for g in groups})
        self.via_counts = ({d.get("via_count")} | {r.get("via_count") for r in rows})
        # "N via layers" is a different claim from "N vias".
        self.via_layer_counts = {d.get("via_layer_count")}
        self.text_counts = ({d.get("text_count"), d.get("text_record_count")}
                            | {r.get("text_count") for r in rows})
        self.cell_counts = {d.get("cell_count"), d.get("total_cell_count_in_file")}
        self.layer_counts = {d.get("layer_count"), d.get("distinct_layer_name_count"),
                             d.get("via_layer_count")}
        # "M1 is present ... across 1 layer/datatype entry" is a count of the rows
        # carrying one layer name, and is a legitimate layer-count claim. The two
        # naming vocabularies are counted separately: tallying both on the same
        # row double-counts it, so a single-row layer came out as 2, not 1.
        for key in ("name", "technology_name"):
            per_name: dict[str, int] = {}
            for r in rows:
                if r.get(key):
                    nm = str(r[key]).lower()
                    per_name[nm] = per_name.get(nm, 0) + 1
            self.layer_counts |= set(per_name.values())
        self.layer_counts |= {g.get("datatype_count") for g in groups}
        self.areas = ({lay.get("bbox_area_um2")}
                      | {r.get("area_um2") for r in rows}
                      | {r.get("group_merged_area_um2") for r in rows}
                      | {g.get("union_area_um2") for g in groups}
                      | {g.get("sum_of_datatype_areas_um2") for g in groups}
                      | {c.get("area_um2") for c in meta.get("cells", [])})
        self.lengths = ({lay.get("width_um"), lay.get("height_um"),
                         (meta.get("source") or {}).get("dbu_um")}
                        | {c.get("width_um") for c in meta.get("cells", [])}
                        | {c.get("height_um") for c in meta.get("cells", [])})
        # A comparison object has no layout block; its length claims are deltas.
        s0 = meta.get("summary") or {}
        self.lengths |= {s0.get("width_delta_um"), s0.get("height_delta_um")}
        self.densities = ({r.get("density_percent") for r in rows}
                          | {g.get("union_density_percent") for g in groups})

        # Connectivity figures. Without these, a correct net or conductor count is
        # reported as unsupported, which would train us to ignore the checker.
        conn = meta.get("connectivity") or {}
        t1 = conn.get("intra_layer") or {}
        nets_block = conn.get("nets") or {}
        net_summary = nets_block.get("summary") or {}
        land = conn.get("landings") or {}
        self.shape_counts = ({t1.get("total_shapes")}
                             | {r.get("shape_count") for r in t1.get("layers") or []}
                             | {c.get("shape_count") for c in land.get("connectors") or []}
                             | {n.get("shape_count") for n in nets_block.get("nets") or []}
                             | {net_summary.get("conducting_shape_count"),
                                net_summary.get("largest_net_shape_count")})
        self.component_counts = ({t1.get("total_components"),
                                  t1.get("layers_with_abutting_shapes")}
                                 | {r.get("component_count") for r in t1.get("layers") or []})
        self.net_counts = {net_summary.get(k) for k in
                           ("net_count", "multi_layer_net_count", "single_layer_net_count",
                            "floating_net_count")}
        self.areas |= {n.get("area_um2") for n in nets_block.get("nets") or []}
        # "a single net spanning 15 layers" is a per-net layer count, which is a
        # legitimate layer-count claim.
        self.layer_counts |= {n.get("layer_count") for n in nets_block.get("nets") or []}
        self.areas |= {r.get(k) for r in t1.get("layers") or []
                       for k in ("largest_component_area_um2", "smallest_component_area_um2")}

        # Geometric measurements: perimeter, vertices, widths, spacings, extents.
        meas = meta.get("measurements") or {}
        mrows = meas.get("layers") or []
        magg = meas.get("role_aggregates") or {}
        self.vertex_counts = ({r.get("vertex_count") for r in mrows}
                             | {r.get("max_vertices_in_one_polygon") for r in mrows}
                             | {r.get("mean_vertices_per_polygon") for r in mrows}
                             | {r.get("non_rectangular_shape_count") for r in mrows}
                             | {sum(r["vertex_count"] for r in mrows if r.get("vertex_count"))})
        self.lengths |= {r.get(k) for r in mrows for k in
                         ("perimeter_um", "merged_perimeter_um", "observed_min_width_um",
                          "observed_max_width_um", "observed_min_space_um",
                          "distance_resolution_um")}
        for r in mrows:
            ext = r.get("shape_extents_um") or {}
            self.lengths |= {ext.get(k) for k in ("min_width", "max_width",
                                                  "min_height", "max_height")}
            for pair in ext.get("distinct_sizes") or []:
                self.lengths |= set(pair)
            self.lengths |= set(r.get("path_widths_um") or [])
            arr = r.get("arrangement") or {}
            self.lengths |= set(arr.get("horizontal_pitches_um") or [])
            self.lengths |= set(arr.get("vertical_pitches_um") or [])
            self.cell_counts |= {arr.get("aligned_rows"), arr.get("aligned_columns")}
        self.areas |= {r.get("area_um2") for r in mrows}
        self.areas |= {a.get("total_area_um2") for a in magg.values()}
        self.lengths |= {a.get(k) for a in magg.values()
                         for k in ("observed_min_width_um", "observed_min_space_um")}
        self.shape_counts |= {r.get("shape_count") for r in mrows}
        self.shape_counts |= {a.get("shape_count") for a in magg.values()}
        self.layer_counts |= {a.get("layer_count") for a in magg.values()}
        self.polygon_counts |= {sum(r["shape_count"] for r in mrows if r.get("shape_count"))}

        # Landing-analysis layer counts: how many connector and conductor layers
        # took part in the measurement.
        land = conn.get("landings") or {}
        self.layer_counts |= {len(land.get("connectors") or []),
                              len(land.get("conductor_layers") or [])}
        self.via_layer_counts |= {len(land.get("connectors") or [])}
        self.via_layer_counts |= {a.get("layer_count") for role, a in magg.items()
                                  if role in ("via", "contact")}

        # Hierarchy figures.
        hier = meta.get("hierarchy") or {}
        self.cell_counts |= {hier.get("cell_count_total"), hier.get("cell_count_in_scope"),
                             hier.get("top_cell_count"),
                             len(hier.get("empty_cells") or []),
                             len(hier.get("orphan_cells") or []),
                             len(hier.get("recursive_cells") or []),
                             len(hier.get("unresolved_reference_cells") or []),
                             hier.get("max_depth_below_top")}
        self.layer_counts |= {hier.get("max_depth_below_top")}
        for c in hier.get("cells") or []:
            self.cell_counts |= {c.get("child_instance_placements"),
                                 c.get("child_instance_records"), c.get("levels_below")}
            self.shape_counts |= {c.get("shape_count")}

        for name in ("polygon_counts", "via_counts", "via_layer_counts", "text_counts",
                     "cell_counts", "layer_counts", "areas", "lengths", "densities",
                     "shape_counts", "component_counts", "net_counts", "vertex_counts"):
            setattr(self, name, {v for v in getattr(self, name) if v is not None})

        # Per-name values, for attribution checking. A number can be a real
        # measurement and still be attached to the wrong layer name, which a
        # global membership test cannot see.
        self.by_name: dict[str, dict[str, set[float]]] = {}
        for g in groups:
            slot = self.by_name.setdefault(str(g.get("label", "")).lower(),
                                           {"areas": set(), "densities": set(), "counts": set()})
            for k in ("union_area_um2", "sum_of_datatype_areas_um2"):
                if isinstance(g.get(k), (int, float)):
                    slot["areas"].add(float(g[k]))
            if isinstance(g.get("union_density_percent"), (int, float)):
                slot["densities"].add(float(g["union_density_percent"]))
            for k in ("polygon_records", "unique_polygons"):
                if isinstance(g.get(k), (int, float)):
                    slot["counts"].add(float(g[k]))
        for r in rows:
            for key in ("name", "technology_name"):
                nm = r.get(key)
                if not nm:
                    continue
                slot = self.by_name.setdefault(str(nm).lower(),
                                               {"areas": set(), "densities": set(), "counts": set()})
                for k in ("area_um2", "group_merged_area_um2"):
                    if isinstance(r.get(k), (int, float)):
                        slot["areas"].add(float(r[k]))
                if isinstance(r.get("density_percent"), (int, float)):
                    slot["densities"].add(float(r["density_percent"]))
                for k in ("polygon_count", "merged_polygon_count", "via_count", "text_count"):
                    if isinstance(r.get(k), (int, float)):
                        slot["counts"].add(float(r[k]))

        # Layout-versus-layout XOR figures. Both the totals and every per-layer and
        # per-location value, and they are also registered per layer name so that
        # attribution checking works on them - "DVB changed by 0.0025 um2" must fail
        # if 0.0025 is actually M0's XOR area.
        xor = meta.get("xor") or {}
        xs = xor.get("summary") or {}
        self.areas |= {xs.get(k) for k in ("total_xor_area_um2", "largest_single_difference_um2",
                                          "total_area_removed_um2", "total_area_added_um2")}
        self.layer_counts |= {xs.get("layers_compared"), xs.get("layers_changed")}
        self.component_counts |= {xs.get("difference_regions")}
        self.shape_counts |= {xs.get("difference_regions")}
        for row in xor.get("layers", []):
            name = str(row.get("name", "")).lower()
            slot = self.by_name.setdefault(name, {"areas": set(), "densities": set(),
                                                  "counts": set()})
            for block in ("xor", "removed", "added", "above_tolerance"):
                part = row.get(block) or {}
                for key in ("area_um2", "largest_area_um2"):
                    if isinstance(part.get(key), (int, float)):
                        self.areas.add(part[key])
                        slot["areas"].add(float(part[key]))
                if isinstance(part.get("count"), (int, float)):
                    self.component_counts.add(part["count"])
                    self.shape_counts.add(part["count"])
                    slot["counts"].add(float(part["count"]))
                for loc in part.get("locations") or []:
                    self.areas.add(loc["area_um2"])
                    slot["areas"].add(float(loc["area_um2"]))
                    self.lengths |= {loc["width_um"], loc["height_um"]}
                    self.lengths |= set(loc["centre_um"])
                for edge in part.get("bbox_um") or []:
                    self.lengths.add(edge)
            for key in ("area_a_um2", "area_b_um2", "area_delta_um2"):
                if isinstance(row.get(key), (int, float)):
                    self.areas.add(row[key])
                    slot["areas"].add(float(row[key]))
            for key in ("shapes_a", "shapes_b", "texts_a", "texts_b",
                        "at_or_below_tolerance_count"):
                if isinstance(row.get(key), (int, float)):
                    self.shape_counts.add(row[key])
                    slot["counts"].add(float(row[key]))

        # Pairwise matrix totals, when auditing a multi-file comparison.
        for pair in xor.get("pairs", []):
            if isinstance(pair.get("total_xor_area_um2"), (int, float)):
                self.areas.add(pair["total_xor_area_um2"])
            if isinstance(pair.get("layers_changed"), (int, float)):
                self.layer_counts.add(pair["layers_changed"])
            if isinstance(pair.get("difference_regions"), (int, float)):
                self.component_counts.add(pair["difference_regions"])


        # Design-level figures are legitimate in any sentence, whatever layer it
        # names ("M0 covers X, which is Y% of the 0.021 um2 cell").
        self.design_level = {float(v) for v in (
            lay.get("bbox_area_um2"), lay.get("width_um"), lay.get("height_um"),
            d.get("polygon_count"), d.get("polygon_record_count"), d.get("via_count"),
            d.get("text_count"), d.get("cell_count"), d.get("layer_count"),
            d.get("via_layer_count"), d.get("distinct_layer_name_count"),
        ) if isinstance(v, (int, float))}

        # Final sweep. The blocks above run after the first None filter, so any
        # missing field they read back as None must be stripped again here.
        for _name in ("polygon_counts", "via_counts", "via_layer_counts", "text_counts",
                      "cell_counts", "layer_counts", "areas", "lengths", "densities",
                      "shape_counts", "component_counts", "net_counts", "vertex_counts"):
            setattr(self, _name, {v for v in getattr(self, _name) if v is not None})
        for _slot in self.by_name.values():
            for _kind in _slot:
                _slot[_kind] = {v for v in _slot[_kind] if v is not None}

        # Deltas, when auditing a comparison object.
        s = meta.get("summary") or {}
        self.deltas = {v for v in s.values() if isinstance(v, (int, float))}
        # `layers_added`, `layers_modified`, `layers_with_geometry_change` and the
        # rest are counts of layers, so a correct sentence like "4 layers had
        # geometry changes" must be checkable against them. Without this they only
        # reached `deltas`, and the "N layers" rule reported the true figure as
        # unsupported - a false alarm that trains you to ignore the audit.
        self.layer_counts |= {v for k, v in s.items()
                              if k.startswith("layers_") and isinstance(v, int)}
        for row in meta.get("layer_changes", []):
            for k in ("polygon_delta", "via_delta", "text_delta", "area_delta_um2",
                      "polygon_count_a", "polygon_count_b", "area_um2_a", "area_um2_b"):
                if isinstance(row.get(k), (int, float)):
                    self.deltas.add(row[k])

    # Each rule: (regex, description, allowed-set attribute, is_float)
    RULES = [
        # Before the polygon rule: "240 polygon vertices" would otherwise be checked
        # as a claim of 240 polygons.
        (rf"{NUM}\s+(?:polygon\s+)?vert(?:ex|ices)\b", "vertex count", "vertex_counts", False),
        (rf"{NUM}\s+(?:distinct\s+|unique\s+|total\s+)?polygons?\b(?!\s*vert)", "polygon count", "polygon_counts", False),
        (rf"{NUM}\s+polygon\s+records?\b", "polygon records", "polygon_counts", False),
        # Ordered before the generic via rule; the generic one excludes "via layer".
        (rf"{NUM}\s+via\s+(?:layer|type)", "via layer count", "via_layer_counts", False),
        (rf"{NUM}\s+vias?\b(?!\s*(?:layer|type)|\s*/)", "via count", "via_counts", False),
        # Connectivity claims, before the generic layer/polygon rules so that
        # "3 physical nets" is checked against net counts and not layer counts.
        (rf"{NUM}\s+(?:physical\s+)?nets?\b", "net count", "net_counts", False),
        (rf"{NUM}\s+(?:separate\s+|physical\s+|distinct\s+)*conductors?\b(?!\s+layer)",
         "within-layer conductor count", "component_counts", False),
        # "8 via/contact layer(s)" and "9 conductor layer(s)" are counts of layers.
        (rf"{NUM}\s+(?:via/contact|via|contact|conductor|connector)\s+layers?\b",
         "layer count", "layer_counts", False),
        (rf"{NUM}\s+(?:connected\s+)?components?\b", "component count", "component_counts", False),
        (rf"{NUM}\s+(?:conducting\s+|connector\s+)shapes?\b", "shape count", "shape_counts", False),
        (rf"{NUM}\s+(?:text\s+)?labels?\b", "text count", "text_counts", False),
        (rf"{NUM}\s+text\b", "text count", "text_counts", False),
        (rf"{NUM}\s+cells?\b", "cell count", "cell_counts", False),
        (rf"{NUM}\s+layers?\b", "layer count", "layer_counts", False),
        (rf"{NUM}\s*(?:µm²|um2|um\^2|µm\^2|square microns?)", "area", "areas", True),
        (rf"{NUM}\s*(?:µm|um|microns?)\b(?!\s*²)", "length", "lengths", True),
        (rf"{NUM}\s*%", "density/percentage", "densities", True),
    ]

    # Split only on punctuation followed by whitespace. Splitting on a bare "."
    # tore "0.00246" in half, so the layer name and its figure landed in
    # different fragments and no attribution was ever checked.
    # Note: no colon. A colon introduces the value for the label before it
    # ("- **BM0**: 0.0153 um2"), which is the commonest shape in the model's
    # markdown, so splitting there separated every label from its figure.
    _SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+|\s+[-*•]\s+")

    def audit_attribution(self, prose: str) -> list[str]:
        """Flag a figure that belongs to a different layer than the one named.

        Only fires when a sentence names exactly one known layer and the number
        matches some *other* layer's value while matching none of this layer's
        own values and no design-level figure. That keeps it quiet on legitimate
        cross-references and loud on a genuine mix-up.
        """
        problems: list[str] = []
        kinds = (
            (rf"{NUM}\s*(?:µm²|um2|um\^2|µm\^2)", "areas", "area"),
            (rf"{NUM}\s*%", "densities", "density"),
        )
        for sentence in self._SPLIT.split(prose):
            if not sentence or not sentence.strip():
                continue
            low = sentence.lower()
            named = [nm for nm in self.by_name
                     if nm and re.search(rf"(?<![a-z0-9_]){re.escape(nm)}(?![a-z0-9_])", low)]
            # Longest match wins, so "M0" inside "M0-PIN" is not double counted.
            named = [nm for nm in named
                     if not any(other != nm and nm in other for other in named)]
            if len(named) != 1:
                continue
            owner = named[0]
            mine = self.by_name[owner]
            for pattern, slot, what in kinds:
                for m in re.finditer(pattern, sentence, re.IGNORECASE):
                    try:
                        val = _f(m.group(1))
                    except ValueError:
                        continue
                    if any(_close(val, v) for v in mine[slot]):
                        continue
                    if any(_close(val, v) for v in self.design_level):
                        continue
                    others = sorted({nm for nm, vals in self.by_name.items()
                                     if nm != owner and any(_close(val, v) for v in vals[slot])})
                    if others:
                        problems.append(
                            f"{what} {m.group(1)} is attributed to '{owner}' but is "
                            f"{'/'.join(others[:3])}'s value  …{sentence.strip()[:110]}…")
        return problems

    def audit(self, prose: str) -> tuple[int, list[str]]:
        problems: list[str] = []
        checked = 0
        for pattern, what, attr, is_float in self.RULES:
            allowed = getattr(self, attr)
            for m in re.finditer(pattern, prose, re.IGNORECASE):
                raw = m.group(1)
                try:
                    val = _f(raw)
                except ValueError:
                    continue
                checked += 1
                if attr == "densities":
                    allowed_now = allowed | {100.0}       # "% of" phrasing
                elif self.deltas and attr in ("polygon_counts", "via_counts",
                                              "text_counts", "areas"):
                    allowed_now = allowed | self.deltas
                else:
                    allowed_now = allowed
                # A prose direction word carries the sign ("decreased by 0.0003"
                # is a faithful rendering of a -0.0003 delta), so compare
                # magnitudes as well when deltas are in play.
                candidates = set(allowed_now)
                if self.deltas:
                    candidates |= {abs(float(x)) for x in allowed_now
                                   if isinstance(x, (int, float))}
                if not any(_close(val, float(a)) for a in candidates):
                    ctx = prose[max(0, m.start() - 45):m.end() + 25].replace("\n", " ")
                    problems.append(f"{what}={raw} not in metadata  …{ctx}…")
        # A figure can be present in the metadata and still be pinned to the
        # wrong layer, which the rules above cannot see.
        problems.extend(self.audit_attribution(prose))
        return checked, problems


# --------------------------------------------------------------------- self-test

def negative_control(meta: dict) -> list[tuple[str, str, bool]]:
    """Build the control set from this design's own numbers.

    Hardcoding one design's figures made the "truthful" cases false for every
    other design, so the self-test failed and the tool aborted on valid input.
    """
    d = meta["design"]
    lay = meta["layout"]
    rows = [r for r in meta.get("layers", []) if r.get("area_um2")]
    area = rows[0]["area_um2"] if rows else None
    name = rows[0]["name"] if rows else "SOMELAYER"

    allowed = Checker(meta)

    def unused_int(base):
        """An integer near `base` that is not a legitimate value anywhere."""
        for delta in range(1, 60):
            for cand in (base + delta, base - delta):
                if cand > 0 and not any(
                    _close(float(cand), float(v))
                    for v in (allowed.polygon_counts | allowed.via_counts
                              | allowed.text_counts | allowed.cell_counts
                              | allowed.layer_counts | allowed.via_layer_counts
                              | allowed.net_counts | allowed.component_counts
                              | allowed.shape_counts)):
                    return cand
        return base + 997

    cases = [
        ("truthful polygons", f"The design contains {d['polygon_count']} polygons.", False),
        ("fabricated polygons", f"The design contains {unused_int(d['polygon_count'])} polygons.", True),
        ("truthful bbox", f"The bounding box is {lay['width_um']} um by {lay['height_um']} um.", False),
        ("wrong bbox length", f"The bounding box is {lay['width_um']} um by {lay['height_um'] + 0.0137} um.", True),
    ]
    if d.get("via_count") is not None:
        cases += [
            ("truthful vias", f"There are {d['via_count']} vias.", False),
            ("fabricated vias", f"There are {unused_int(d['via_count'])} vias.", True),
        ]
    if d.get("via_layer_count"):
        cases += [
            ("truthful via layers", f"{d['via_count']} vias across {d['via_layer_count']} via layers.", False),
            ("wrong via layers", f"{d['via_count']} vias across {unused_int(d['via_layer_count'])} via layers.", True),
        ]
    if area:
        cases += [
            ("truthful area", f"{name} covers {area} um2.", False),
            ("fabricated area", f"{name} covers {round(area * 1.371 + 1e-5, 9)} um2.", True),
        ]

    # Connectivity, which is the newest and therefore least-proven vocabulary.
    conn = meta.get("connectivity") or {}
    t1 = conn.get("intra_layer") or {}
    if t1.get("total_components"):
        cases += [
            ("truthful conductors",
             f"There are {t1['total_components']} separate physical conductors.", False),
            ("fabricated conductors",
             f"There are {unused_int(t1['total_components'])} separate physical conductors.", True),
        ]
    net_summary = ((conn.get("nets") or {}).get("summary") or {})
    if net_summary.get("net_count"):
        cases += [
            ("truthful nets", f"The layout resolves to {net_summary['net_count']} physical nets.", False),
            ("fabricated nets",
             f"The layout resolves to {unused_int(net_summary['net_count'])} physical nets.", True),
        ]
    return cases


def attribution_control(meta: dict) -> list[tuple[str, str, bool]]:
    """Cases where a real figure is pinned to the wrong layer name."""
    groups = {g["label"]: g for g in meta.get("layer_groups", [])
              if isinstance(g.get("union_area_um2"), (int, float))}
    if len(groups) < 2:
        return []
    (n1, g1), (n2, g2) = sorted(groups.items(), key=lambda kv: -kv[1]["union_area_um2"])[:2]
    a1, a2 = g1["union_area_um2"], g2["union_area_um2"]
    if a1 == a2:
        return []
    return [
        ("attribution correct", f"{n1} covers {a1} um2.", False),
        ("attribution swapped", f"{n1} covers {a2} um2.", True),
        ("attribution in a bullet", f"- **{n2}**: {a1} um2 coverage", True),
    ]


def self_test(checker: Checker, meta: dict) -> int:
    print("=== NEGATIVE CONTROL: the checker must catch every fabrication ===")
    failures = 0
    for label, prose, should_catch in negative_control(meta) + attribution_control(meta):
        _, problems = checker.audit(prose)
        caught = bool(problems)
        ok = caught == should_catch
        if not ok:
            failures += 1
        state = "caught" if caught else "passed"
        want = "should catch" if should_catch else "should pass"
        print(f"  {'OK ' if ok else 'BAD'} {label:<22} {state:<7} ({want})"
              + (f"  -> {problems[0][:70]}" if problems and not should_catch else ""))
    print(f"\n  self-test failures: {failures}")
    return failures


# ------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deterministic-only", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--stem", default="DCAP0_1_RT_4")
    ap.add_argument("--stem-b", default="DCAP0_2_RT_4")
    ap.add_argument("--show", action="store_true", help="print each answer in full")
    args = ap.parse_args()

    lyp = find_layermap(SAMPLES / f"{args.stem}.gds")
    lm = load_lyp(lyp) if lyp else None
    a = analyze_pair(SAMPLES / f"{args.stem}.gds", SAMPLES / f"{args.stem}.json", layermap=lm)
    b = analyze_pair(SAMPLES / f"{args.stem_b}.gds", SAMPLES / f"{args.stem_b}.json", layermap=lm)
    comparison = compare_metadata(a, b)
    ca, cc = Checker(a), Checker(comparison)

    if args.self_test:
        return 1 if self_test(ca, a) else 0

    print(f"files    : {args.stem} / {args.stem_b}")
    print(f"layer map: {lyp.name if lyp else 'none'}")
    print(f"backend  : {provider_status().get('primary')}")

    if self_test(ca, a):
        print("  ABORT: the checker itself is unsound.")
        return 1

    total = bad_total = 0

    print(f"\n{'='*76}\nDETERMINISTIC ANSWERS (no API calls)\n{'='*76}")
    for q in ["How many polygons are there?", "How many vias are present?",
              "What is the layout size?", "Which layer has the highest density?",
              "What is the area of M0?", "What is the largest cell?",
              "Give me a summary of this GDS."]:
        r = answer(a, q)
        if not r:
            print(f"  (deferred to model) {q}")
            continue
        n, probs = ca.audit(r)
        total += n
        bad_total += len(probs)
        print(f"  {'OK ' if not probs else 'BAD'} {q:<42} [{n} claims]")
        for p in probs:
            print(f"        ! {p}")
        if args.show:
            print("        " + r[:300].replace("\n", "\n        "))

    r = answer_comparison(comparison, "what changed?")
    n, probs = cc.audit(r)
    total += n
    bad_total += len(probs)
    print(f"  {'OK ' if not probs else 'BAD'} {'what changed?':<42} [{n} claims]")
    for p in probs:
        print(f"        ! {p}")

    if not args.deterministic_only:
        print(f"\n{'='*76}\nMODEL ANSWERS ({provider_status().get('primary')})\n{'='*76}")
        jobs = [
            ("Explain this layout to a non-expert.", lambda: ask_llm(a, "Explain this layout to a non-expert."), ca),
            ("Describe the via structure of this cell.", lambda: ask_llm(a, "Describe the via structure of this cell."), ca),
            ("AI design review", lambda: generate_review(a), ca),
            ("Comparison narrative", lambda: generate_comparison(comparison), cc),
        ]
        for label, fn, checker in jobs:
            r = fn()
            if looks_like_failure(r):
                print(f"  SKIP {label} (backend unavailable)")
                continue
            n, probs = checker.audit(r)
            total += n
            bad_total += len(probs)
            print(f"  {'OK ' if not probs else 'BAD'} {label:<42} [{n} claims]")
            for p in probs:
                print(f"        ! {p}")
            if args.show:
                print("        " + r[:1200].replace("\n", "\n        "))

    print(f"\n{'='*76}")
    print(f"claims checked: {total}   unsupported: {bad_total}")
    return 1 if bad_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
