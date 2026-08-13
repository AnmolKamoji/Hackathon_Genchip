from pathlib import Path

from analyzer.comparison import compare_metadata
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds
from analyzer.sidecar_parser import analyze_sidecar

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


def test_reference_comparison():
    a = analyze_sidecar(SAMPLES / "NR2D1_1_RT_4.json")
    b = analyze_sidecar(SAMPLES / "NR2D1_2_RT_4.json")
    c = compare_metadata(a, b)
    assert c["summary"]["polygon_delta"] == 7
    assert c["summary"]["via_delta"] == 3


def test_fused_comparison_matches_sidecar_deltas():
    a = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")
    b = analyze_pair(SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    c = compare_metadata(a, b)
    assert c["comparable"] is True
    assert c["warnings"] == []
    assert c["summary"]["polygon_delta"] == 7
    assert c["summary"]["via_delta"] == 3
    assert c["summary"]["text_delta"] == 4
    assert c["summary"]["width_delta_um"] == 0.0


def test_added_layers_are_identified():
    a = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")
    b = analyze_pair(SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    c = compare_metadata(a, b)
    added = {x["name"] for x in c["layers_added"]}
    # Revision 2 introduces an M1 metal layer and the vias that reach it.
    assert {"M1", "VIA_M0_M1", "VIA_M0_PMOSInterconnect"} <= added
    assert c["layers_removed"] == []
    assert c["summary"]["layers_added"] == len(c["layers_added"])


def test_mismatched_analysis_modes_are_refused():
    """Diffing a raw-GDS run against a sidecar run used to fabricate '+9 vias'."""
    a = analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds")
    b = analyze_sidecar(SAMPLES / "NR2D1_2_RT_4.json", "NR2D1_2_RT_4.gds")
    c = compare_metadata(a, b)
    assert c["comparable"] is False
    assert c["warnings"]
    # An unknown minus a known is unknown, never a number.
    assert c["summary"]["via_delta"] is None


def test_unavailable_via_counts_do_not_become_zero():
    a = analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds")
    b = analyze_gds(SAMPLES / "NR2D1_2_RT_4.gds")
    c = compare_metadata(a, b)
    assert c["comparable"] is True
    assert c["summary"]["via_delta"] is None
    assert c["summary"]["polygon_delta"] == 7
    assert all(row["via_delta"] is None for row in c["layer_changes"])


def test_identical_inputs_produce_no_deltas():
    a = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")
    c = compare_metadata(a, a)
    assert c["summary"]["polygon_delta"] == 0
    assert c["summary"]["via_delta"] == 0
    assert c["summary"]["layers_added"] == 0
    assert c["summary"]["layers_removed"] == 0
    assert c["summary"]["layers_modified"] == 0
