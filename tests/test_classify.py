"""Tests for standard-cell classification.

Written because the tool answered "frontside or backside" with "the metadata does
not identify frontside vs backside routing" - literally true and completely
useless, since `BM0` carries `VSS` and `VDD` labels and that *is* the answer.

Expected values were established by hand from the sample files before the module
was written, and the negative controls construct layouts that force each branch.
"""
from __future__ import annotations

from pathlib import Path

import klayout.db as db
import pytest

from ai.deterministic import answer
from analyzer.classify import (cell_height, classify, half_dr, metal_solution,
                               min_rt_number, orientation, power_delivery,
                               routing_tracks, technology)
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _outlines(name, lm):
    return shape_outlines(SAMPLES / name, lm)


# --- power delivery ---------------------------------------------------------

@pytest.mark.parametrize("gds", ["DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds",
                                 "NR2D1_2_RT_4.gds", "DCAP0_2_RT_4.gds"])
def test_samples_are_backside_power(lm, gds):
    """BM0 carries VSS and VDD, which is exactly what the check looks for."""
    result = power_delivery(_outlines(gds, lm))
    assert result["power_delivery"] == "backside"
    assert result["backside"] is True
    assert set(result["backside_labels"]) >= {"VDD", "VSS"}
    assert result["backside_qualifies"] is True
    assert result["frontside_qualifies"] is False


def test_frontside_power_is_reported_when_the_labels_are_there(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("FS")
    scale = 1e-3 / layout.dbu
    cell.shapes(layout.layer(200, 0)).insert(db.Box(0, 0, int(80 * scale), int(12 * scale)))
    for text, x in (("VSS", 10), ("VDD", 60)):
        cell.shapes(layout.layer(200, 1)).insert(
            db.Text(text, db.Trans(db.Vector(int(x * scale), int(6 * scale)))))
    path = tmp_path / "frontside.gds"
    layout.write(str(path))
    result = power_delivery(shape_outlines(path, lm))
    assert result["power_delivery"] == "frontside"
    assert result["backside"] is False


def test_backside_wins_when_both_sides_qualify(lm, tmp_path):
    """The specification checks backside first, so both-present reads as backside."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("BOTH")
    scale = 1e-3 / layout.dbu
    for layer in (200, 300):
        cell.shapes(layout.layer(layer, 0)).insert(
            db.Box(0, 0, int(80 * scale), int(12 * scale)))
        for text, x in (("VSS", 10), ("VDD", 60)):
            cell.shapes(layout.layer(layer, 1)).insert(
                db.Text(text, db.Trans(db.Vector(int(x * scale), int(6 * scale)))))
    path = tmp_path / "both.gds"
    layout.write(str(path))
    result = power_delivery(shape_outlines(path, lm))
    assert result["power_delivery"] == "backside"
    assert result["backside_qualifies"] and result["frontside_qualifies"]


def test_power_check_fails_cleanly_without_a_matching_pair(lm, tmp_path):
    """One of the two label kinds is not enough, and must not be guessed at."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("HALF")
    scale = 1e-3 / layout.dbu
    cell.shapes(layout.layer(300, 0)).insert(db.Box(0, 0, int(80 * scale), int(12 * scale)))
    cell.shapes(layout.layer(300, 1)).insert(
        db.Text("VSS", db.Trans(db.Vector(int(10 * scale), int(6 * scale)))))
    path = tmp_path / "half.gds"
    layout.write(str(path))
    result = power_delivery(shape_outlines(path, lm))
    assert result["power_delivery"] is None
    assert result["failed"] is True


def test_alternative_label_vocabularies_are_accepted(lm, tmp_path):
    """VGND/VPWR must work as well as VSS/VDD."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("ALT")
    scale = 1e-3 / layout.dbu
    cell.shapes(layout.layer(300, 0)).insert(db.Box(0, 0, int(80 * scale), int(12 * scale)))
    for text, x in (("VGND", 10), ("VPWR", 60)):
        cell.shapes(layout.layer(300, 1)).insert(
            db.Text(text, db.Trans(db.Vector(int(x * scale), int(6 * scale)))))
    path = tmp_path / "alt.gds"
    layout.write(str(path))
    assert power_delivery(shape_outlines(path, lm))["power_delivery"] == "backside"


# --- technology -------------------------------------------------------------

@pytest.mark.parametrize("gds", ["DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds"])
def test_samples_are_gaa(lm, gds):
    result = technology(_outlines(gds, lm), SAMPLES / gds)
    assert result["technology"] == "GAA"
    assert result["diffusions_touch"] is False
    assert result["nwell_count"] == 0


def _diff_cell(path, *, touching: bool, nwell: bool):
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("T")
    scale = 1e-3 / layout.dbu
    def box(layer, x0, y0, x1, y1):
        cell.shapes(layout.layer(layer, 0)).insert(
            db.Box(int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))
    box(100, 10, 10, 90, 25)                       # ndiff
    box(101, 10, 25 if touching else 60, 90, 40 if touching else 75)   # pdiff
    if nwell:
        box(120, 0, 50, 100, 90)
    layout.write(str(path))
    return path


def test_touching_diffusions_are_cfet(lm, tmp_path):
    path = _diff_cell(tmp_path / "cfet.gds", touching=True, nwell=False)
    result = technology(shape_outlines(path, lm), path)
    assert result["technology"] == "CFET"
    assert result["diffusions_touch"] is True


def test_separated_diffusions_with_a_well_are_finfet(lm, tmp_path):
    path = _diff_cell(tmp_path / "finfet.gds", touching=False, nwell=True)
    result = technology(shape_outlines(path, lm), path)
    assert result["technology"] == "FinFET"
    assert result["nwell_count"] == 1


def test_missing_diffusion_gives_unknown_not_a_guess(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    layout.create_cell("BARE").shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 100, 100))
    path = tmp_path / "bare.gds"
    layout.write(str(path))
    assert technology(shape_outlines(path, lm), path)["technology"] == "Unknown"


# --- metal solution ---------------------------------------------------------

@pytest.mark.parametrize("gds,drawn", [
    ("DCAP0_1_RT_4.gds", ["M0", "M1"]),
    ("NR2D1_1_RT_4.gds", ["M0"]),
    ("NR2D1_2_RT_4.gds", ["M0", "M1"]),
    ("AN2D1_2_RT_4.gds", ["M0", "M1"]),
])
def test_metal_solution_reports_capability_not_usage(lm, gds, drawn):
    """Routing capability comes from the track guides, not from the drawn wires.

    Every one of these cells routes on one or two layers but has an M2 track guide, so
    all are three-metal cells. Counting drawn metal instead reported the same
    technology differently depending on how busy the cell was, which made a standard
    cell's metal solution a function of its logic - and it contradicted the tech file
    for AN2D1_2, which states Three Metal Solution while M2 carries no geometry.
    """
    result = metal_solution(_outlines(gds, lm))
    assert result["metal_solution"] == "ThreeMetalSolution"
    assert result["metals_available"] == ["M0", "M1", "M2"]
    assert result["metals_drawn"] == drawn
    assert result["source"] == "track guide"


def test_metal_solution_falls_back_to_drawn_metal_without_guides(lm, tmp_path):
    """With no track guide there is nothing declaring the capability.

    Reporting three-metal anyway would be a guess dressed as a technology fact, so the
    drawn metal is counted instead and the answer says that is what happened.
    """
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("NOGUIDE")
    for layer in (200, 202):                      # M0 and M1, no track guides
        cell.shapes(layout.layer(layer, 0)).insert(db.Box(0, 0, 1000, 200))
    path = tmp_path / "noguide.gds"
    layout.write(str(path))
    result = metal_solution(shape_outlines(path, lm))
    assert result["metal_solution"] == "TwoMetalSolution"
    assert result["source"] == "drawn geometry"
    assert "no metal track guide" in result["basis"]


def test_three_metal_and_unknown(lm, tmp_path):
    def build(path, layers):
        layout = db.Layout()
        layout.dbu = 5e-05
        cell = layout.create_cell("M")
        for layer in layers:
            cell.shapes(layout.layer(layer, 0)).insert(db.Box(0, 0, 1000, 200))
        layout.write(str(path))
        return path
    assert metal_solution(shape_outlines(build(tmp_path / "m3.gds", (200, 202, 204)), lm)
                          )["metal_solution"] == "ThreeMetalSolution"
    assert metal_solution(shape_outlines(build(tmp_path / "m0none.gds", (202, 204)), lm)
                          )["metal_solution"] == "UNKNOWN"


# --- routing tracks ---------------------------------------------------------

@pytest.mark.parametrize("gds,total,used,empty", [
    ("DCAP0_1_RT_4.gds", 4, 4, 0),
    ("NR2D1_1_RT_4.gds", 4, 2, 2),
    ("NR2D1_2_RT_4.gds", 4, 3, 1),
])
def test_track_occupancy(lm, gds, total, used, empty):
    result = routing_tracks(_outlines(gds, lm))
    assert (result["tracks"], result["tracks_used"], result["tracks_empty"]) == (total, used, empty)
    assert len(result["track_detail"]) == total


def test_tracks_unavailable_without_the_guide_layer(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    layout.create_cell("NG").shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 1000, 200))
    path = tmp_path / "noguide.gds"
    layout.write(str(path))
    assert routing_tracks(shape_outlines(path, lm))["tracks"] is None


# --- height, half-DR, orientation ------------------------------------------

@pytest.mark.parametrize("gds", ["DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds"])
def test_samples_are_single_height(lm, gds):
    result = cell_height(_outlines(gds, lm), "GAA")
    assert result["height"] == "single"
    assert result["base_layer"] == "BM0"          # BM0 takes priority over M0
    assert result["shapes_at_largest_area"] == 2  # fewer than three


def test_three_equal_rails_read_as_multi_height(lm, tmp_path):
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("MH")
    for i in range(3):
        cell.shapes(layout.layer(300, 0)).insert(db.Box(0, i * 4000, 2000, i * 4000 + 240))
    path = tmp_path / "multi.gds"
    layout.write(str(path))
    result = cell_height(shape_outlines(path, lm), "GAA")
    assert result["height"] == "multi"
    assert result["shapes_at_largest_area"] == 3


def test_finfet_height_uses_the_well_count(lm, tmp_path):
    path = _diff_cell(tmp_path / "ff1.gds", touching=False, nwell=True)
    assert cell_height(shape_outlines(path, lm), "FinFET")["height"] == "single"


@pytest.mark.parametrize("gds", ["DCAP0_1_RT_4.gds", "NR2D1_1_RT_4.gds"])
def test_samples_are_half_dr(lm, gds):
    result = half_dr(_outlines(gds, lm))
    assert result["half_dr"] is True
    assert result["target_layer"] == "BM0"
    assert result["rail_centres_y_um"] == result["boundary_y_um"]


def test_orientation_is_r0_when_ground_is_at_the_bottom(lm):
    result = orientation(_outlines("NR2D1_1_RT_4.gds", lm), SAMPLES / "NR2D1_1_RT_4.gds")
    assert result["orientation"] == "R0"
    assert result["confidence"] == "inferred from the power rail order"
    # The honest limit must travel with the answer.
    assert "My cannot be determined" in result["not_derivable"]


def test_orientation_detects_a_flip_about_x(lm, tmp_path):
    """Ground above power is a mirror about the x axis."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("FLIP")
    scale = 1e-3 / layout.dbu
    cell.shapes(layout.layer(300, 0)).insert(db.Box(0, 0, int(80 * scale), int(120 * scale)))
    cell.shapes(layout.layer(300, 1)).insert(
        db.Text("VDD", db.Trans(db.Vector(int(10 * scale), int(5 * scale)))))
    cell.shapes(layout.layer(300, 1)).insert(
        db.Text("VSS", db.Trans(db.Vector(int(10 * scale), int(115 * scale)))))
    path = tmp_path / "flip.gds"
    layout.write(str(path))
    assert orientation(shape_outlines(path, lm), path)["orientation"] == "Mx"


def test_orientation_is_read_from_instance_transforms_when_they_exist(lm, tmp_path):
    """A placement's orientation is recorded in the GDS; that beats any inference."""
    layout = db.Layout()
    layout.dbu = 5e-05
    child = layout.create_cell("LEAF")
    child.shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 1000, 200))
    top = layout.create_cell("TOP")
    top.insert(db.CellInstArray(child.cell_index(), db.Trans(db.Trans.M0, 0, 0)))
    path = tmp_path / "placed.gds"
    layout.write(str(path))
    result = orientation(shape_outlines(path, lm), path)
    assert result["confidence"] == "recorded"
    assert result["orientation"] == "Mx"


# --- RT number --------------------------------------------------------------

def test_min_rt_number_from_filenames():
    result = min_rt_number(["design_RT_10.gds", "design_RT_5.gds", "design_RT_20.gds"])
    assert result["min_rt"] == 5
    assert min_rt_number(["nothing.gds"])["min_rt"] is None
    assert min_rt_number(["a_RT_4.gds"])["min_rt"] == 4


# --- the headline and the answers ------------------------------------------

def test_headline_summarises_the_cell(lm):
    cls = classify(_outlines("NR2D1_1_RT_4.gds", lm), SAMPLES / "NR2D1_1_RT_4.gds")
    assert cls["headline"] == ("single-height GAA, backside power, three-metal routing, "
                              "4 M0 tracks (2 used)")


def _meta(gds, lm):
    m = analyze_gds(SAMPLES / gds, layermap=lm)
    m["classification"] = classify(_outlines(gds, lm), SAMPLES / gds, [gds])
    return m


def test_the_question_that_used_to_fail_now_answers(lm):
    """"frontside or backside" previously got "the metadata does not identify it"."""
    reply = answer(_meta("NR2D1_1_RT_4.gds", lm), "frontside or backside")
    assert "Backside power" in reply
    assert "VDD" in reply and "VSS" in reply
    assert "does not identify" not in reply


@pytest.mark.parametrize("question,expected", [
    ("Is this backside power?", "Backside power"),
    ("What is the metal solution?", "ThreeMetalSolution"),
    ("Is it single or multi height?", "Single-Height GDS — GAA"),
    ("What is the orientation?", "R0"),
    ("How many routing tracks?", "4 M0 routing tracks"),
    ("How many empty tracks are there?", "2 are empty"),
    ("What technology is this?", "GAA"),
    ("Is it half-DR?", "Half-DR: True"),
    ("What kind of cell is this?", "single-height GAA"),
    ("What is the minimum RT number?", "minimum RT number is **4**"),
])
def test_each_classification_question_is_answered(lm, question, expected):
    reply = answer(_meta("NR2D1_1_RT_4.gds", lm), question)
    assert reply is not None, question
    assert expected in reply, f"{question} -> {reply}"


def test_classification_questions_say_so_when_unavailable(lm):
    """Without the classification block the answer must not be invented."""
    reply = answer(analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds", layermap=lm),
                   "frontside or backside")
    assert "No cell classification is available" in reply
    assert "layer map" in reply


def test_layer_names_survive_the_prose(lm):
    """str.capitalize() lowercases the remainder, which turned VDD/VSS into vdd/vss
    and "an M0 polygon" into "an m0 polygon"."""
    meta = _meta("NR2D1_1_RT_4.gds", lm)
    assert "vdd" not in answer(meta, "frontside or backside")
    assert "m0 polygon" not in answer(meta, "How many routing tracks?")
    assert "M0 polygon" in answer(meta, "How many routing tracks?")


def test_metal_solution_answer_separates_capability_from_usage(lm):
    """The answer must not read as self-contradictory.

    Reporting capability while describing usage in the old wording produced
    "a three-metal cell. M0 carries geometry; M2 is absent", which states and denies
    the same thing in one sentence.
    """
    reply = answer(_meta("NR2D1_1_RT_4.gds", lm), "What is the metal solution?")
    assert "ThreeMetalSolution" in reply
    assert "M0, M1, M2 have track guides" in reply
    assert "available but unused" in reply
    assert "is absent" not in reply
