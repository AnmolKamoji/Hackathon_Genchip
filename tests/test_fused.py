"""Fusing measured GDS geometry with sidecar technology semantics."""
from pathlib import Path

import pytest

from analyzer.fused import analyze_pair

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


@pytest.fixture(scope="module")
def fused():
    return analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")


def test_keeps_geometry_from_gds_and_semantics_from_sidecar(fused):
    d = fused["design"]
    assert d["polygon_count"] == 60          # measured by KLayout
    assert d["via_count"] == 6               # only the sidecar knows this
    assert d["top_cell"] == "NR2D1"
    assert fused["metadata_source"] == "fused"


def test_gds_and_sidecar_agree_on_the_reference_files(fused):
    """The consistency block is the whole point of fusing: catch disagreement."""
    c = fused["consistency"]
    assert c["agrees"] is True
    assert c["count_mismatches"] == []
    assert c["layer_datatype_only_in_gds"] == []
    assert c["layer_datatype_only_in_sidecar"] == []


def test_named_layers_carry_measured_density(fused):
    named = [x for x in fused["layers"] if x["geometry_source"] == "klayout_merged_region"]
    assert named, "expected at least one layer with merged-region geometry"
    dense = max(named, key=lambda x: x["density_percent"])
    assert dense["name"] == "BSPowerRail"
    assert dense["density_percent"] == pytest.approx(76.5, abs=0.01)


def test_ambiguous_layer_names_are_flagged_not_guessed(fused):
    """(102,1) and (103,1) each carry two sidecar names.

    A merged area cannot be attributed to one of them, so those rows must say so
    instead of silently claiming the group's area.
    """
    ambiguous = [x for x in fused["layers"] if x["geometry_source"] == "sidecar_unmerged_subset"]
    assert {x["name"] for x in ambiguous} == {"Diffusion_Break", "NMOSGate", "PMOSGate"}
    for row in ambiguous:
        assert row["shares_layer_datatype_with"]
        assert "group_merged_area_um2" in row


def test_vias_are_attributed_to_via_layers(fused):
    via_rows = {x["name"]: x["via_count"] for x in fused["layers"] if x["via_count"]}
    assert via_rows == {
        "VIA_M0_PMOSGate": 2,
        "VIA_M0_NMOSInterconnect": 2,
        "VIA_Inteconnect_BSPowerRail": 2,
    }
    assert sum(via_rows.values()) == fused["design"]["via_count"]


def test_cells_come_from_the_gds_hierarchy(fused):
    cell = fused["cells"][0]
    assert cell["name"] == "NR2D1"
    assert cell["area_um2"] == pytest.approx(0.03)
