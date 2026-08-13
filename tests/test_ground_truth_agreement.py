"""The tool's output, checked against an independently-written ground truth.

`tools/ground_truth.py` re-reads the `.lyp` XML and the GDSII with raw KLayout and
plain Python, importing nothing from `analyzer/`. Areas come from the shoelace
formula, components from union-find, perimeter from summed edge lengths. Checking
the analyzer against itself proves nothing; this is the check that means something.

The scenario exercised is the one that actually happens: **`.gds` only, with the
bundled `.lyp`** — no JSON sidecar.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.connectivity import analyze_connectivity
from analyzer.gds_parser import analyze_gds
from analyzer.hierarchy import analyze_hierarchy
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import measure_layers, measure_vias
from tools.ground_truth import read_gds, read_lyp

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS_FILES = ["NR2D1_1_RT_4.gds", "NR2D1_2_RT_4.gds",
             "DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"]


@pytest.fixture(scope="module")
def truth():
    lyp = read_lyp(SAMPLES / "Titan_layer_properties.lyp")
    return lyp, {name: read_gds(SAMPLES / name, lyp) for name in GDS_FILES}


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _analysed(gds: Path, lm):
    """Exactly what the app builds for a .gds upload with no sidecar."""
    m = analyze_gds(gds, layermap=lm)
    m["hierarchy"] = analyze_hierarchy(gds)
    m["measurements"] = measure_layers(gds, lm)
    m["measurements"]["vias"] = measure_vias(m["measurements"])
    m["connectivity"] = analyze_connectivity(gds, lm)
    return m


def test_layer_map_parses_to_the_same_entries(truth, lm):
    lyp, _ = truth
    assert lm["entry_count"] == len(lyp["entries"]) == 49
    assert not lyp["duplicate_keys"], "a duplicate (layer, datatype) would make names ambiguous"
    mine = {k: v["technology_name"] for k, v in lm["by_key"].items()}
    assert mine == lyp["entries"]


@pytest.mark.parametrize("name", GDS_FILES)
def test_design_totals_match_ground_truth(truth, lm, name):
    _, designs = truth
    t = designs[name]
    m = _analysed(SAMPLES / name, lm)
    d, layout = m["design"], m["layout"]

    assert d["top_cell"] == t["top_cell"]
    assert d["top_cell_count"] == t["top_cell_count"]
    assert d["total_cell_count_in_file"] == t["cell_count_total"]
    assert d["polygon_count"] == t["polygons"]
    assert d["text_count"] == t["texts"]
    assert d["layer_count"] == t["layer_entries"]
    assert m["source"]["dbu_um"] == pytest.approx(t["dbu_um"])
    assert layout["width_um"] == pytest.approx(t["bbox_um"][0])
    assert layout["height_um"] == pytest.approx(t["bbox_um"][1])
    assert layout["bbox_area_um2"] == pytest.approx(t["bbox_area_um2"])


@pytest.mark.parametrize("name", GDS_FILES)
def test_via_and_contact_counts_match_ground_truth(truth, lm, name):
    """Derived from layer naming on both sides, but by different code."""
    _, designs = truth
    t = designs[name]
    d = _analysed(SAMPLES / name, lm)["design"]
    assert d["via_count"] == t["vias"]
    assert sorted(d["via_layer_names"] or []) == t["via_layers"]
    assert d["contact_count"] == t["contacts"]
    assert sorted(d["contact_layer_names"] or []) == t["contact_layers"]


@pytest.mark.parametrize("name", GDS_FILES)
def test_per_layer_geometry_matches_ground_truth(truth, lm, name):
    _, designs = truth
    t = designs[name]
    m = _analysed(SAMPLES / name, lm)
    rows = {f"{r['layer']}/{r['datatype']}": r for r in m["layers"]}
    meas = {f"{r['layer']}/{r['datatype']}": r for r in m["measurements"]["layers"]}
    assert set(rows) == set(t["layers"])

    for key, exp in t["layers"].items():
        row, mrow = rows[key], meas[key]
        assert row["name"] == exp["name"], key
        # Area: shoelace on the merged outline vs KLayout's merged region.
        assert row["area_um2"] == pytest.approx(exp["area_um2"], abs=1e-6), key
        assert row["polygon_count"] == exp["shapes"], key
        assert row["text_count"] == exp["texts"], key
        # A text-only layer has 0 vertices and 0 area - determined, not unknown.
        assert mrow["vertex_count"] == exp["vertices"], key
        assert mrow["perimeter_um"] == pytest.approx(exp["perimeter_um"], abs=1e-9), key
        if not exp["shapes"]:
            # But the *minimum* over an empty set is undefined, so those stay None.
            assert mrow["observed_min_width_um"] is None, key
            assert mrow["observed_min_space_um"] is None, key


@pytest.mark.parametrize("name", GDS_FILES)
def test_connectivity_components_match_union_find(truth, lm, name):
    _, designs = truth
    t = designs[name]
    conn = _analysed(SAMPLES / name, lm)["connectivity"]
    assert conn["intra_layer"]["total_components"] == t["total_components"]
    assert conn["intra_layer"]["total_shapes"] == t["polygons"]
    by_key = {(r["layer"], r["datatype"]): r for r in conn["intra_layer"]["layers"]}
    for key, exp in t["layers"].items():
        layer, datatype = (int(x) for x in key.split("/"))
        if not exp["shapes"]:
            continue
        assert by_key[(layer, datatype)]["component_count"] == exp["components"], key


@pytest.mark.parametrize("name", GDS_FILES)
def test_role_aggregates_match_ground_truth(truth, lm, name):
    """"Total metal area" summed independently on both sides."""
    _, designs = truth
    t = designs[name]
    agg = _analysed(SAMPLES / name, lm)["measurements"]["role_aggregates"]
    assert agg["metal"]["total_area_um2"] == pytest.approx(t["metal_area_um2"], abs=1e-6)
    assert sorted(agg["metal"]["layers"]) == t["metal_layers"]
    assert agg["via"]["shape_count"] == t["vias"]
    assert agg["contact"]["shape_count"] == t["contacts"]


@pytest.mark.parametrize("name", GDS_FILES)
def test_hierarchy_matches_ground_truth(truth, lm, name):
    _, designs = truth
    t = designs[name]
    h = _analysed(SAMPLES / name, lm)["hierarchy"]
    assert h["top_cell"] == t["top_cell"]
    assert h["cell_count_total"] == t["cell_count_total"]
    assert h["max_depth_below_top"] == t["hierarchy_depth"]
    placed = sum(c["child_instance_placements"] for c in h["cells"])
    assert placed == t["instance_placements"]


def test_the_two_revisions_differ_where_ground_truth_says_they_do(truth, lm):
    """DCAP0's revisions share every count and differ only in metal area."""
    _, designs = truth
    a, b = designs["DCAP0_1_RT_4.gds"], designs["DCAP0_2_RT_4.gds"]
    assert a["polygons"] == b["polygons"] and a["vias"] == b["vias"]
    assert a["metal_area_um2"] != b["metal_area_um2"]

    ma = _analysed(SAMPLES / "DCAP0_1_RT_4.gds", lm)["measurements"]["role_aggregates"]["metal"]
    mb = _analysed(SAMPLES / "DCAP0_2_RT_4.gds", lm)["measurements"]["role_aggregates"]["metal"]
    assert ma["total_area_um2"] == pytest.approx(a["metal_area_um2"], abs=1e-6)
    assert mb["total_area_um2"] == pytest.approx(b["metal_area_um2"], abs=1e-6)
    assert ma["total_area_um2"] != mb["total_area_um2"]
