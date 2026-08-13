"""Tests for the per-layout view: geometry, technology colours and dimensions.

The point of this view is that a dimension is *read*, not measured. Reaching for a
ruler to answer "how wide is that wire?" is the slow part of inspecting a layout,
and the answer is already in the file — so the tests check that every shape carries
its own size and that the cell extent is annotated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.plots import layout_view

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
DCAP = SAMPLES / "DCAP0_1_RT_4.gds"
NR2D1 = SAMPLES / "NR2D1_1_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def dcap(lm):
    return shape_outlines(DCAP, lm)


# --- extraction -------------------------------------------------------------

def test_every_shape_in_the_layout_is_extracted(dcap):
    """56 shapes is the independently-established count for this cell."""
    assert dcap["shape_total"] == 56
    assert dcap["top_cell"] == "DCAP0"
    assert dcap["truncated"] is False
    assert dcap["warnings"] == []


def test_cell_extent_is_measured_not_estimated(dcap):
    assert dcap["cell_width_um"] == 0.105
    assert dcap["cell_height_um"] == 0.2
    left, bottom, right, top = dcap["cell_bbox_um"]
    assert right - left == pytest.approx(0.105)
    assert top - bottom == pytest.approx(0.2)


def test_each_shape_carries_its_own_dimensions(dcap):
    """This is the feature: no ruler needed, the size travels with the shape."""
    for row in dcap["layers"]:
        for shape in row["shapes"]:
            assert shape["width_um"] > 0 and shape["height_um"] > 0
            assert len(shape["centre_um"]) == 2
            assert shape["area_um2"] > 0
            assert shape["vertices"] >= 4
            # The centre must follow from the origin and the size.
            assert shape["centre_um"][0] == pytest.approx(
                shape["left_um"] + shape["width_um"] / 2, abs=1e-9)
            assert shape["centre_um"][1] == pytest.approx(
                shape["bottom_um"] + shape["height_um"] / 2, abs=1e-9)


def test_per_layer_extent_bounds_its_own_shapes(dcap):
    for row in dcap["layers"]:
        if not row["shapes"]:
            continue
        left, bottom, right, top = row["extent"]["bbox_um"]
        for shape in row["shapes"]:
            assert shape["left_um"] >= left - 1e-9
            assert shape["bottom_um"] >= bottom - 1e-9
            assert shape["left_um"] + shape["width_um"] <= right + 1e-9
            assert shape["bottom_um"] + shape["height_um"] <= top + 1e-9


def test_shapes_agree_with_the_measurement_module(lm, dcap):
    """Two separate readers of the same file must not disagree on counts."""
    from analyzer.measurements import measure_layers
    measured = {(r["layer"], r["datatype"]): r for r in measure_layers(DCAP, lm)["layers"]}
    for row in dcap["layers"]:
        other = measured[(row["layer"], row["datatype"])]
        assert row["shape_count"] == other["shape_count"], row["name"]
        assert row["label_count"] == (other["shape_types"] or {}).get("text", 0), row["name"]


def test_labels_are_extracted_with_their_positions(dcap):
    labelled = {r["name"]: r for r in dcap["layers"] if r["label_count"]}
    assert "M0-LABEL" in labelled
    texts = {lab["text"] for lab in labelled["M0-LABEL"]["labels"]}
    assert {"NET03", "NET05"} <= texts
    for lab in labelled["M0-LABEL"]["labels"]:
        assert len(lab["at_um"]) == 2


def test_technology_colours_come_through(dcap):
    by_name = {r["name"]: r for r in dcap["layers"]}
    assert by_name["M0"]["colour"] == "#f3ff80"
    assert by_name["DVB"]["colour"] == "#ff0000"


def test_extraction_works_without_a_layer_map():
    """Geometry is GDS-only; the map only supplies names and colours."""
    bare = shape_outlines(DCAP, None)
    assert bare["shape_total"] == 56
    assert all(r["colour"] is None for r in bare["layers"])
    assert all(r["name"].startswith("layer_") for r in bare["layers"])


def test_shape_limit_truncates_the_drawing_and_says_so(lm):
    limited = shape_outlines(DCAP, lm, max_shapes=10)
    assert limited["shape_total"] == 10
    assert limited["truncated"] is True
    assert any("drawing only" in w for w in limited["warnings"])


# --- the figure -------------------------------------------------------------

def test_layout_view_draws_every_shape_and_labels_the_extent(dcap):
    fig = layout_view(dcap)
    assert fig is not None
    # The cell size is on the title and annotated on the drawing.
    assert "105 × 200 nm" in fig.layout.title.text
    labels = {a.text for a in fig.layout.annotations}
    assert "<b>105 nm</b>" in labels and "<b>200 nm</b>" in labels
    # Equal aspect, or the drawing misrepresents the layout.
    assert fig.layout.yaxis.scaleanchor == "x"
    assert fig.layout.yaxis.scaleratio == 1


def test_hover_gives_the_dimensions_so_no_ruler_is_needed(dcap):
    fig = layout_view(dcap)
    geometry = [t for t in fig.data if t.fill == "toself"]
    assert geometry
    for trace in geometry[:8]:
        assert "nm" in trace.hovertemplate
        assert "area" in trace.hovertemplate
        assert "centre" in trace.hovertemplate


def test_one_legend_entry_per_layer(dcap):
    fig = layout_view(dcap)
    shown = [t.name for t in fig.data if t.showlegend]
    assert len(shown) == len(set(shown)), "a layer must not appear twice in the legend"
    drawn = {r["name"] for r in dcap["layers"] if r["shape_count"]}
    assert set(shown) == drawn


def test_layers_can_be_filtered(dcap):
    fig = layout_view(dcap, only_layers={"M0", "M1", "VIA0"})
    assert {t.name for t in fig.data if t.showlegend} == {"M0", "M1", "VIA0"}
    assert layout_view(dcap, only_layers=set()) is None


def test_labels_and_dimensions_can_be_turned_off(dcap):
    with_labels = layout_view(dcap, show_labels=True)
    without = layout_view(dcap, show_labels=False)
    assert len(without.data) < len(with_labels.data)
    assert layout_view(dcap, show_dimensions=False).layout.annotations == ()


def test_shapes_are_drawn_in_technology_colours(dcap):
    fig = layout_view(dcap)
    colours = {t.name: t.fillcolor for t in fig.data if t.showlegend}
    assert colours["M0"] == "#f3ff80"
    assert colours["DVB"] == "#ff0000"


def test_fallback_colours_are_used_when_the_file_has_none():
    """With no layer map the shapes still have to be distinguishable."""
    bare = shape_outlines(DCAP, None)
    name = next(r["name"] for r in bare["layers"] if r["shape_count"])
    fig = layout_view(bare, fallback_colours={name: "#123456"})
    assert "#123456" in {t.fillcolor for t in fig.data}


def test_bigger_layers_are_drawn_first_so_small_shapes_stay_visible(dcap):
    """A cell-spanning rail drawn last would hide every via underneath it."""
    fig = layout_view(dcap)
    order = [t.name for t in fig.data if t.showlegend]
    extents = {r["name"]: (r["extent"]["width_um"] * r["extent"]["height_um"])
               for r in dcap["layers"] if r["extent"]}
    areas = [extents[n] for n in order if n in extents]
    assert areas == sorted(areas, reverse=True)


@pytest.mark.parametrize("gds,width,height", [
    ("DCAP0_1_RT_4.gds", 0.105, 0.2),
    ("NR2D1_1_RT_4.gds", 0.15, 0.2),
])
def test_every_sample_renders_with_its_own_measured_size(lm, gds, width, height):
    outlines = shape_outlines(SAMPLES / gds, lm)
    assert outlines["cell_width_um"] == width
    assert outlines["cell_height_um"] == height
    fig = layout_view(outlines)
    assert f"{width * 1000:.0f} × {height * 1000:.0f} nm" in fig.layout.title.text
