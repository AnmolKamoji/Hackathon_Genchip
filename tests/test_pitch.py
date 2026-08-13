"""Tests for the standard-cell pitch metrics.

These are the questions a layout engineer asks first - gate pitch, metal pitch, how
many gate pitches wide - and the tool used to answer them by describing how the
shapes happened to be arranged, which is a different quantity entirely.

Expected values were measured by hand from the sample files before this module was
written, and CPP is cross-checked three ways against each other.
"""
from __future__ import annotations

from pathlib import Path

import klayout.db as db
import pytest

from ai.deterministic import answer
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.pitch import analyze_pitch, cell_dimensions, gate_pitch, metal_pitches

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _out(name, lm):
    return shape_outlines(SAMPLES / name, lm)


# --- gate pitch (CPP) -------------------------------------------------------

@pytest.mark.parametrize("gds", ["DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds",
                                 "NR2D1_2_RT_4.gds", "DCAP0_2_RT_4.gds"])
def test_gate_pitch_is_45nm_on_every_sample(lm, gds):
    result = gate_pitch(_out(gds, lm))
    assert result["cpp_nm"] == 45.0
    assert result["cpp_um"] == 0.045
    assert result["sources_agree"] is True


def test_cpp_is_confirmed_by_three_independent_derivations(lm):
    """Poly spacing, diffcon spacing, and the manual's decomposition.

    Rule 3.3.8 requires the diffcon pitch to equal the poly pitch, so agreement
    between them is a real cross-check rather than a restatement.
    """
    result = gate_pitch(_out("NR2D1_1_RT_4.gds", lm))
    ev = result["evidence"]
    assert ev["from_poly_spacing_nm"] == [45.0]
    assert ev["from_diffcon_spacing_nm"] == [45.0]
    # CPP = 2 x spacing + diffcon width + poly width  ->  45 = 2x5 + 20 + 15
    assert ev["poly_width_nm"] == 15.0
    assert ev["diffcon_width_nm"] == 20.0
    assert ev["implied_poly_to_diffcon_spacing_nm"] == 5.0
    assert ev["decomposition"] == "45 = 2 x 5 + 20 + 15"


def test_cpp_survives_a_single_poly_shape(lm):
    """DCAP0 has poly at one x position, so the poly spacing alone yields nothing.

    The diffcon spacing carries it, which is why more than one source is measured.
    """
    result = gate_pitch(_out("DCAP0_1_RT_4.gds", lm))
    assert "from_poly_spacing_nm" not in result["evidence"]
    assert result["evidence"]["from_diffcon_spacing_nm"] == [45.0]
    assert result["cpp_nm"] == 45.0


def test_all_four_names_are_recorded_as_one_number(lm):
    assert set(gate_pitch(_out("NR2D1_1_RT_4.gds", lm))["aliases"]) == {
        "CPP", "CGP", "gate pitch", "poly pitch"}


def test_gate_pitch_unavailable_is_not_a_guess(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    layout.create_cell("BARE").shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 1000, 200))
    path = tmp_path / "bare.gds"
    layout.write(str(path))
    result = gate_pitch(shape_outlines(path, lm))
    assert result["cpp_nm"] is None
    assert "cannot be determined" in result["basis"]


# --- metal pitches ----------------------------------------------------------

def test_metal_pitches_come_from_the_track_guides(lm):
    """The grid exists whether or not a wire uses it, so the guide is the source."""
    result = metal_pitches(_out("NR2D1_1_RT_4.gds", lm))
    assert result["M1"]["pitch_nm"] == 30.0
    assert result["M1"]["uniform"] is True
    assert result["M1"]["routing_direction"] == "vertical"
    assert result["M1"]["pitch_axis"] == "x"
    assert "M1-TRACK-GUIDE" in result["M1"]["source"]
    assert result["M2"]["pitch_nm"] == 28.0
    assert result["M2"]["routing_direction"] == "horizontal"


def test_m0_pitch_reports_the_dominant_step_and_names_the_exception(lm):
    """M0 tracks sit at 21, 42, 73, 94 nm - two 21 nm steps and one 31 nm step
    across the cell centre, where the n/p boundary is. Averaging would invent a
    pitch that no track uses."""
    m0 = metal_pitches(_out("NR2D1_1_RT_4.gds", lm))["M0"]
    assert m0["pitch_nm"] == 21.0
    assert m0["uniform"] is False
    assert m0["steps_nm"] == [21.0, 31.0]
    assert "exception" in m0["note"] and "31" in m0["note"]
    assert m0["positions_nm"] == [21.0, 42.0, 73.0, 94.0]


def test_metal_width_and_implied_space(lm):
    m0 = metal_pitches(_out("NR2D1_1_RT_4.gds", lm))["M0"]
    assert m0["width_nm"] == 12.0
    assert m0["implied_space_nm"] == 9.0        # 21 - 12


def test_missing_guide_falls_back_and_says_so(lm, tmp_path):
    """Without a guide the metal positions are all there is, and that must be stated
    - it is where the wires are, not where the tracks are."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("NOGUIDE")
    for i in range(3):
        cell.shapes(layout.layer(200, 0)).insert(
            db.Box(0, i * 400, 2000, i * 400 + 240))
    path = tmp_path / "noguide.gds"
    layout.write(str(path))
    m0 = metal_pitches(shape_outlines(path, lm))["M0"]
    assert m0["pitch_nm"] == 20.0
    assert "happens to sit" in m0["source"]


# --- cell dimensions --------------------------------------------------------

@pytest.mark.parametrize("gds,width,cpp_count", [
    ("DCAP0_1_RT_4.gds", 90.0, 2),
    ("NR2D1_1_RT_4.gds", 135.0, 3),
    ("NR2D1_2_RT_4.gds", 135.0, 3),
])
def test_cell_width_in_gate_pitches(lm, gds, width, cpp_count):
    dims = cell_dimensions(_out(gds, lm), 45.0, metal_pitches(_out(gds, lm)), gds)
    assert dims["width_nm"] == width
    assert dims["gate_pitches"] == cpp_count
    assert dims["width_is_whole_cpp"] is True


def test_cell_dimensions_use_the_boundary_not_the_bounding_box(lm):
    """Track guides extend past the cell, so the layout bbox is wider than the cell."""
    outlines = _out("NR2D1_1_RT_4.gds", lm)
    dims = cell_dimensions(outlines, 45.0, metal_pitches(outlines), None)
    assert dims["width_nm"] == 135.0
    assert outlines["cell_width_um"] * 1000 == 150.0     # the bbox is wider
    assert dims["height_nm"] == 115.0


def test_track_count_matches_the_rt_number_in_the_filename(lm):
    """RT in the filename is the routing-track count, and it agrees with the
    measured M0 signal tracks - an independent confirmation of both."""
    for gds in ("DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds", "NR2D1_2_RT_4.gds"):
        outlines = _out(gds, lm)
        dims = cell_dimensions(outlines, 45.0, metal_pitches(outlines), gds)
        assert dims["rt_in_filename"] == 4
        assert dims["signal_tracks"] == 4
        assert dims["rt_matches_measured_tracks"] is True


def test_track_grid_spans_the_cell_height_exactly(lm):
    """4 guide tracks plus the two rail positions on the cell edges, and the steps
    between them must add up to the cell height."""
    outlines = _out("NR2D1_1_RT_4.gds", lm)
    dims = cell_dimensions(outlines, 45.0, metal_pitches(outlines), None)
    grid = dims["m0_track_positions_nm"]
    assert grid == [0.0, 21.0, 42.0, 73.0, 94.0, 115.0]
    assert dims["track_positions_including_rails"] == 6
    assert round(grid[-1] - grid[0], 4) == dims["height_nm"] == 115.0


def test_boundary_absent_refuses_to_substitute_the_bounding_box(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    layout.create_cell("NB").shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 1000, 200))
    path = tmp_path / "noboundary.gds"
    layout.write(str(path))
    dims = cell_dimensions(shape_outlines(path, lm), 45.0, None, None)
    assert dims["width_nm"] is None
    assert "not a substitute" in dims["basis"]


# --- gear ratio and the headline -------------------------------------------

def test_gear_ratio_is_cpp_over_m1_pitch(lm):
    result = analyze_pitch(_out("NR2D1_1_RT_4.gds", lm), "NR2D1_1_RT_4.gds")
    assert result["gear_ratio"]["gear_ratio"] == 1.5      # 45 / 30
    assert "45" in result["gear_ratio"]["basis"]


def test_headline_carries_the_numbers_an_engineer_quotes(lm):
    result = analyze_pitch(_out("NR2D1_1_RT_4.gds", lm), "NR2D1_1_RT_4.gds")
    for fragment in ("45 nm gate pitch", "3 CPP wide", "4 M0 signal tracks",
                     "M0 21 nm", "M1 30 nm", "M2 28 nm"):
        assert fragment in result["headline"], result["headline"]


# --- the three questions that prompted this --------------------------------

def _meta(gds, lm):
    m = analyze_gds(SAMPLES / gds, layermap=lm)
    m["pitch"] = analyze_pitch(_out(gds, lm), gds)
    return m


def test_metal_pitch_question(lm):
    reply = answer(_meta("NR2D1_2_RT_4.gds", lm),
                   "What is the Metal0, Metal1, Metal2 pitch?")
    assert "21 nm pitch" in reply and "30 nm pitch" in reply and "28 nm pitch" in reply
    # The non-uniform M0 grid must not be presented as a single clean pitch.
    assert "31 nm" in reply
    # And it must not fall back to describing the shape arrangement.
    assert "place their shapes" not in reply


def test_how_many_gate_pitches_question(lm):
    reply = answer(_meta("NR2D1_2_RT_4.gds", lm), "How many gate pitches are in the layout?")
    assert "3 gate pitches" in reply
    assert "135 nm / 45 nm" in reply


def test_how_many_poly_pitch_question(lm):
    """Poly pitch and gate pitch are the same thing, so this is the same count."""
    reply = answer(_meta("NR2D1_2_RT_4.gds", lm), "How many poly pitch?")
    assert "3 gate pitches" in reply


@pytest.mark.parametrize("question,expected", [
    ("What is the CPP?", "45 nm"),
    ("What is the gate pitch?", "45 nm"),
    ("What is the poly pitch?", "45 nm"),
    ("What is the M1 pitch?", "30 nm pitch"),
    ("What is the gear ratio?", "Gear ratio 1.5"),
    ("How wide is the cell?", "3 gate pitches"),
])
def test_each_pitch_question_is_answered(lm, question, expected):
    reply = answer(_meta("NR2D1_1_RT_4.gds", lm), question)
    assert reply is not None, question
    assert expected in reply, f"{question} -> {reply}"


def test_cpp_answer_cites_the_manual_decomposition(lm):
    reply = answer(_meta("NR2D1_1_RT_4.gds", lm), "What is the CPP?")
    assert "45 = 2 x 5 + 20 + 15" in reply
    assert "3.2.6" in reply


def test_pitch_questions_say_so_when_unavailable(lm):
    reply = answer(analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds", layermap=lm),
                   "What is the CPP?")
    assert "No pitch metrics are available" in reply
