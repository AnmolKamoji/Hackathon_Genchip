"""Tests for the visual theme and its application.

These pin the decisions rather than the pixels: one colour per verdict state, every
figure themed, numbers in a tabular face, and the documented availability labels
matching the chips the interface actually shows.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ui.theme import (BG, CHIP_CLASS, CSS, STATE_COLOUR, STATE_ICON, chip, chips,
                      hint, section, style_figure, verdict_html)

ROOT = Path(__file__).resolve().parent.parent


def test_streamlit_config_is_dark():
    """Dark by default: it matches Virtuoso/Innovus/KLayout, and bright layer
    colours separate far better against a dark canvas."""
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert cfg["theme"]["base"] == "dark"
    assert cfg["theme"]["backgroundColor"] == BG


def test_every_verdict_state_has_its_own_colour_and_icon():
    """A colour that means two things means nothing."""
    states = {"identical", "interconnect-only", "base-layers", "blocked", "none"}
    assert states <= set(STATE_COLOUR)
    assert states <= set(STATE_ICON)
    # Distinct colours for the states a reviewer must tell apart at a glance.
    distinct = {STATE_COLOUR[s] for s in
                ("identical", "interconnect-only", "base-layers", "blocked")}
    assert len(distinct) == 4


def test_verdict_carries_its_state_colour_and_escapes_into_one_element():
    html = verdict_html("base-layers", "Base layers changed", "some detail")
    assert STATE_COLOUR["base-layers"] in html
    assert STATE_ICON["base-layers"] in html
    assert "Base layers changed" in html and "some detail" in html
    assert html.count('class="verdict"') == 1


def test_verdict_omits_the_detail_block_when_there_is_none():
    assert "vdetail" not in verdict_html("identical", "Identical")


def test_numbers_are_set_in_a_tabular_face():
    """A column of coordinates that does not align is slower to scan, and this
    screen is mostly columns of figures."""
    assert "tabular-nums" in CSS
    assert '"tnum" 1' in CSS
    assert "stMetricValue" in CSS and "stDataFrame" in CSS


def test_chip_classes_cover_the_documented_availability_labels():
    """The chips and CAPABILITIES.md must not drift apart."""
    doc = (ROOT / "CAPABILITIES.md").read_text()
    for label in ("GDS-only", "GDS + LYP", "GDS + sidecar"):
        assert label in doc, f"{label} should be a documented availability label"
        assert label in CHIP_CLASS
    assert CHIP_CLASS["GDS-only"] == "exact"
    assert CHIP_CLASS["requires PDK"] == "unavailable"


def test_chip_and_helpers_produce_single_elements():
    assert chip("x", "exact").count("<span") == 1
    assert chips(("a", "exact"), ("b", "inferred")).count("<span") == 2
    assert 'class="section"' in section("Heading")
    assert 'class="hint"' in hint("note")


def test_unknown_chip_kind_degrades_to_the_neutral_style():
    assert chip("x", "nonsense").count("chip") == 1


# --- figures are themed -----------------------------------------------------

def test_style_figure_applies_the_dark_template():
    import plotly.graph_objects as go
    fig = style_figure(go.Figure(go.Scatter(x=[0, 1], y=[0, 1])))
    assert fig.layout.template.layout.paper_bgcolor is not None   # a template is set
    assert fig.layout.plot_bgcolor == BG
    assert fig.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert "Mono" in fig.layout.font.family


def test_style_figure_tolerates_none():
    """The figure builders return None when there is nothing to draw."""
    assert style_figure(None) is None


def test_style_figure_does_not_disturb_equal_aspect():
    """The difference map depends on equal aspect; theming must not undo it."""
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.plots import difference_map
    from analyzer.xor_diff import xor_compare
    samples = ROOT / "data" / "samples"
    lm = load_lyp(default_layermap())
    fig = style_figure(difference_map(
        xor_compare(samples / "DCAP0_1_RT_4.gds", samples / "DCAP0_2_RT_4.gds", lm)))
    assert fig.layout.yaxis.scaleanchor == "x"
    assert fig.layout.yaxis.scaleratio == 1


def test_every_figure_builder_survives_theming():
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.plots import (change_hotspot, density_profile, difference_grid,
                                difference_map, similarity_matrix)
    from analyzer.gds_parser import analyze_gds
    from analyzer.present import split_primary
    from analyzer.xor_diff import compare_many, xor_compare
    samples = ROOT / "data" / "samples"
    lm = load_lyp(default_layermap())
    files = [samples / f for f in ("DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds",
                                   "NR2D1_1_RT_4.gds")]
    multi = compare_many(files, lm)
    rows, _ = split_primary(analyze_gds(files[0], layermap=lm)["layers"], lm)
    built = [
        difference_map(xor_compare(files[0], files[1], lm), [0, 0, 0.105, 0.2]),
        difference_grid(multi, files[0].name, [0, 0, 0.105, 0.2]),
        change_hotspot(multi, [0, 0, 0.105, 0.2]),
        similarity_matrix(multi),
        density_profile(rows),
    ]
    for fig in built:
        assert fig is not None
        themed = style_figure(fig)
        assert themed.layout.plot_bgcolor == BG
