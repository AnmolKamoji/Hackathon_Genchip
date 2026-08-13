"""Every metadata object the analyzer can produce must honour the contract.

The contract's core rule is that an undeterminable fact is None, never 0. These
tests exist so a future change that reintroduces a silent zero fails here rather
than in front of a user.
"""
import json

import pytest

from analyzer.comparison import compare_metadata
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import load_lyp
from analyzer.sidecar_parser import analyze_sidecar
from models.metadata import SchemaError, validate_comparison, validate_metadata

SAMPLES = "data/samples"
LYP = f"{SAMPLES}/Titan_layer_properties.lyp"
STEMS = ["NR2D1_1_RT_4", "NR2D1_2_RT_4", "DCAP0_1_RT_4", "DCAP0_2_RT_4"]


@pytest.fixture(scope="module")
def lyp():
    return load_lyp(LYP)


@pytest.mark.parametrize("stem", STEMS)
def test_gds_mode_honours_the_contract(stem, lyp):
    validate_metadata(analyze_gds(f"{SAMPLES}/{stem}.gds"), source=f"{stem} gds")
    validate_metadata(analyze_gds(f"{SAMPLES}/{stem}.gds", lyp), source=f"{stem} gds+lyp")


@pytest.mark.parametrize("stem", STEMS)
def test_sidecar_mode_honours_the_contract(stem):
    validate_metadata(analyze_sidecar(f"{SAMPLES}/{stem}.json", f"{stem}.gds"),
                      source=f"{stem} sidecar")


@pytest.mark.parametrize("stem", STEMS)
def test_fused_mode_honours_the_contract(stem, lyp):
    validate_metadata(analyze_pair(f"{SAMPLES}/{stem}.gds", f"{SAMPLES}/{stem}.json"),
                      source=f"{stem} fused")
    validate_metadata(analyze_pair(f"{SAMPLES}/{stem}.gds", f"{SAMPLES}/{stem}.json", layermap=lyp),
                      source=f"{stem} fused+lyp")


@pytest.mark.parametrize("pair", [("NR2D1_1_RT_4", "NR2D1_2_RT_4"),
                                 ("DCAP0_1_RT_4", "DCAP0_2_RT_4")])
def test_comparisons_honour_the_contract(pair, lyp):
    a = analyze_pair(f"{SAMPLES}/{pair[0]}.gds", f"{SAMPLES}/{pair[0]}.json", layermap=lyp)
    b = analyze_pair(f"{SAMPLES}/{pair[1]}.gds", f"{SAMPLES}/{pair[1]}.json", layermap=lyp)
    validate_comparison(compare_metadata(a, b))
    # Mixed modes: via deltas must be unavailable, not zero.
    raw = analyze_gds(f"{SAMPLES}/{pair[0]}.gds")
    validate_comparison(compare_metadata(raw, analyze_gds(f"{SAMPLES}/{pair[1]}.gds")))


# --- the validator must actually reject violations -------------------------

def test_validator_rejects_a_silent_zero_via_count():
    m = analyze_gds(f"{SAMPLES}/DCAP0_1_RT_4.gds")
    m["design"]["via_count"] = 0            # the exact bug the project began with
    with pytest.raises(SchemaError, match="confident wrong answer"):
        validate_metadata(m)


def test_validator_rejects_a_zero_via_count_on_a_layer_row():
    m = analyze_gds(f"{SAMPLES}/DCAP0_1_RT_4.gds")
    m["layers"][0]["via_count"] = 0
    with pytest.raises(SchemaError, match="must be None"):
        validate_metadata(m)


def test_validator_rejects_impossible_density():
    m = analyze_gds(f"{SAMPLES}/DCAP0_1_RT_4.gds")
    m["layers"][0]["density_percent"] = 140.0
    with pytest.raises(SchemaError, match="outside 0-100"):
        validate_metadata(m)


def test_validator_rejects_a_union_larger_than_its_parts():
    """A union of overlapping shapes can never exceed the sum of their areas;
    that inversion is what the cross-layer-union bug produced."""
    m = analyze_pair(f"{SAMPLES}/DCAP0_1_RT_4.gds", f"{SAMPLES}/DCAP0_1_RT_4.json")
    g = m["layer_groups"][0]
    g["union_area_um2"] = g["sum_of_datatype_areas_um2"] + 1.0
    with pytest.raises(SchemaError, match="greater than"):
        validate_metadata(m)


def test_validator_rejects_a_fabricated_via_delta():
    a = analyze_gds(f"{SAMPLES}/DCAP0_1_RT_4.gds")
    b = analyze_gds(f"{SAMPLES}/DCAP0_2_RT_4.gds")
    c = compare_metadata(a, b)
    c["layer_changes"][0]["via_delta"] = 0   # unknown minus unknown is not 0
    with pytest.raises(SchemaError, match="must be None"):
        validate_comparison(c)


def test_validator_rejects_missing_fields():
    m = analyze_gds(f"{SAMPLES}/DCAP0_1_RT_4.gds")
    del m["warnings"]
    with pytest.raises(SchemaError, match="missing top-level keys"):
        validate_metadata(m)


def test_written_reports_honour_the_contract(tmp_path):
    """What the CLI writes to disk must validate too, not just in-memory objects."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "analyze.py",
                        f"{SAMPLES}/DCAP0_1_RT_4.gds", f"{SAMPLES}/DCAP0_2_RT_4.gds",
                        "--out", str(tmp_path), "--quiet"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for stem in ("DCAP0_1_RT_4", "DCAP0_2_RT_4"):
        validate_metadata(json.loads((tmp_path / f"{stem}.metadata.json").read_text()), source=stem)
    validate_comparison(json.loads((tmp_path / "comparison.json").read_text()))
