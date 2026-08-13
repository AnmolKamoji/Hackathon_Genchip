"""Geometry facts measured from the raw GDSII stream.

The ground truth for polygon counts is the semantic sidecar, which reports 60
and 67 `boundary` elements for the two reference files.
"""
from pathlib import Path

import pytest

from analyzer.gds_parser import analyze_gds

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


@pytest.fixture(scope="module")
def meta1():
    return analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds")


def test_polygon_count_includes_boxes(meta1):
    """KLayout stores rectangular BOUNDARY records as Box, not Polygon.

    Counting only `is_polygon()` shapes reported 0 polygons for these files.
    """
    assert meta1["design"]["polygon_count"] == 60
    assert meta1["design"]["shape_count"] == 70
    assert meta1["design"]["text_count"] == 10
    assert meta1["design"]["polygon_count"] + meta1["design"]["text_count"] == meta1["design"]["shape_count"]


def test_second_file_polygon_count():
    m = analyze_gds(SAMPLES / "NR2D1_2_RT_4.gds")
    assert m["design"]["polygon_count"] == 67
    assert m["design"]["text_count"] == 14


def test_via_count_is_unavailable_not_zero(meta1):
    """Raw GDSII carries no via semantics, so the count must be unknown.

    Reporting 0 would answer "how many vias?" with a confident wrong number.
    """
    assert meta1["design"]["via_count"] is None
    assert all(layer["via_count"] is None for layer in meta1["layers"])


def test_layer_polygon_counts_sum_to_design_total(meta1):
    assert sum(x["polygon_count"] for x in meta1["layers"]) == meta1["design"]["polygon_count"]


def test_bounding_box_and_density(meta1):
    assert meta1["layout"]["width_um"] == pytest.approx(0.15)
    assert meta1["layout"]["height_um"] == pytest.approx(0.20)
    assert meta1["layout"]["bbox_area_um2"] == pytest.approx(0.03)
    assert meta1["source"]["dbu_um"] == pytest.approx(5e-05)
    # Density is coverage of the bounding box, so it cannot exceed 100%.
    for layer in meta1["layers"]:
        assert 0.0 <= layer["density_percent"] <= 100.0
        assert layer["area_um2"] >= 0.0


def test_cell_geometry_is_measured(meta1):
    cell = meta1["cells"][0]
    assert cell["name"] == "NR2D1"
    assert cell["area_um2"] == pytest.approx(0.03)


def test_metadata_source_tag(meta1):
    assert meta1["metadata_source"] == "gds"
