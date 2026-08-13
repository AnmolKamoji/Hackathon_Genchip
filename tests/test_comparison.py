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


def test_area_deltas_carry_no_float_subtraction_noise():
    """A delta must not claim more precision than the database unit allows.

    0.00246 - 0.00216 evaluates to 0.0002999999999999999 in binary floating point.
    Reported verbatim, that tells the engineer the area changed by a figure known to
    sixteen significant digits, in a layout whose grid is one nanometre - and the
    model quotes the metadata verbatim by design, so the noise reaches the answer.
    """
    import json
    import re

    a = analyze_pair(SAMPLES / "DCAP0_1_RT_4.gds", SAMPLES / "DCAP0_1_RT_4.json")
    b = analyze_pair(SAMPLES / "DCAP0_2_RT_4.gds", SAMPLES / "DCAP0_2_RT_4.json")
    c = compare_metadata(a, b)

    noisy = sorted(set(re.findall(r"-?\d+\.\d{10,}", json.dumps(c))))
    assert not noisy, f"float noise in the comparison digest: {noisy}"

    m0 = next(r for r in c["layer_changes"] if r["name"] == "M0")
    assert m0["area_delta_um2"] == -0.0003


def test_delta_rounding_does_not_alter_real_measurements():
    """The rounding must be finer than anything the files can express.

    One nanometre is 0.001 um, so the smallest area step is 1e-6 um2. If the rounding
    were ever coarse enough to swallow a real change, this would catch it.
    """
    from analyzer.comparison import _delta

    assert _delta(1.0, 1.000001) == 1e-6            # the smallest real area step
    assert _delta(0.00246, 0.00216) == -0.0003      # the noise case
    assert _delta(None, 5) is None                  # still no fabricated zero
    assert _delta(5, None) is None
    assert _delta(3, 10) == 7                       # ints stay ints
    assert isinstance(_delta(3, 10), int)
