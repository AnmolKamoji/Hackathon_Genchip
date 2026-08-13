"""Tests for the tech-file parameter measurements.

The expected values are the tech file supplied for `AN2D1_2_RT_4.gds`. That makes this
the strongest check in the suite: the figures were not produced by this code, or by any
code in this repository, so agreement is not the usual risk of a module confirming
itself. Every parameter the layout can express is pinned to the number an engineer
stated independently.

Several of these have a plausible wrong reading that this fixes, and the comments say
which, so a future change that reintroduces one fails here with an explanation rather
than an opaque number mismatch.
"""
from __future__ import annotations

from pathlib import Path

import klayout.db as db
import pytest

from ai.deterministic import answer
from analyzer.classify import classify
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.techparams import (compare_to_reference, find_reference, load_reference,
                                parameter, tech_parameters)

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "AN2D1_2_RT_4.gds"

# The tech file for this cell, as supplied. Scalars in nm.
STATED = {
    "N-poly width": 15, "P-poly width": 15,
    "N-diffcon width": 20, "P-diffcon width": 20,
    "Diffusion width": 15, "Power rail width": 85,
    "N/P Diffusion spacing": 41, "Poly to Diffcon spacing": 5,
    "Gate Cut spacing": 17, "Diffcon ETE spacing": 21,
    "Gate extension": 12, "Diffcon extension": 10,
}
STATED_PROFILES = {
    "Metal0": [15, 12, 9, 12, 19, 12, 9, 12, 15],
    "Metal2": [21.5, 16, 12, 16, 12, 16, 21.5],
}
STATED_CATEGORICAL = {
    "Technology": "gaa", "Power Distribution": True,
    "Routing Capability": "Three Metal Solution", "Orientation": "R0",
    "Number of routing tracks": 4, "Multiheight": 1,
}


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def measured(lm):
    return tech_parameters(GDS, lm)


@pytest.mark.parametrize("name,expected", sorted(STATED.items()))
def test_scalar_parameters_match_the_tech_file(measured, name, expected):
    record = measured["parameters"][name]
    assert record["available"], f"{name}: {record['basis']}"
    assert record["value"] == pytest.approx(expected), record["basis"]
    assert record["unit"] == "nm"
    assert record["drm_rule"]


@pytest.mark.parametrize("name,expected", sorted(STATED_PROFILES.items()))
def test_metal_profiles_match_the_tech_file(measured, name, expected):
    """The profile is a cross-section, so it must also close on the cell."""
    record = measured["parameters"][name]
    assert record["value"] == pytest.approx(expected)
    assert record["closes_on_cell"], "the profile does not sum to the cell dimension"


def test_metal1_profile_expands_the_stated_repeating_unit(measured):
    """The tech file gives Metal1 as [13.5, 18, 12] - margin, width, gap.

    Metal1 has seven uniform tracks, so the compact form carries the whole profile.
    Both are reported: the sequence because it is unambiguous, the compact form
    because that is how the tech file prints it.
    """
    record = measured["parameters"]["Metal1"]
    assert record["compact_nm"] == pytest.approx([13.5, 18, 12])
    assert record["value"] == pytest.approx(
        [13.5, 18, 12, 18, 12, 18, 12, 18, 12, 18, 12, 18, 12, 18, 13.5])
    assert sum(record["value"]) == pytest.approx(225.0)      # the cell width


@pytest.mark.parametrize("name,expected", sorted(STATED_CATEGORICAL.items(), key=str))
def test_categorical_parameters_match_the_tech_file(measured, name, expected):
    record = measured["parameters"][name]
    assert record["available"], record["basis"]
    if isinstance(expected, str):
        assert str(record["value"]).lower() == expected.lower()
    else:
        assert record["value"] == expected


@pytest.mark.parametrize("via,size", [
    ("pviag", [15.0, 12.0]), ("nviag", [15.0, 12.0]),
    ("pviat", [20.0, 12.0]), ("nviat", [20.0, 12.0]),
    ("via0", [18.0, 12.0]),
])
def test_via_offset_enclosure_and_extension_are_zero(measured, via, size):
    """The tech file states [0, 0] for offset, enclosure and via extension.

    Those zeros are real measurements, not blanks. Rule 3.7.2 makes the via height the
    M0 width plus twice the via extension, and every via here is exactly as tall as
    the M0 track it lands on, so the extension is zero. Enclosure is zero because the
    enclosing shape is exactly the via's width, and the offset is zero because the via
    sits on the track centre. The absolute size is not zero and is reported separately.
    """
    record = measured["parameters"][via]
    assert record["available"], record.get("basis")
    assert record["value"]["size"] == pytest.approx(size)
    assert record["value"]["offset"] == 0
    assert record["value"]["enclosure"] == 0
    assert record["value"]["extension"] == 0


def test_gate_extension_is_the_minimum_not_the_first_pair(measured):
    """An uncut gate measures 20.5 nm because the poly runs on to meet its opposite.

    Only the cut column shows the real 12 nm, so the minimum over every poly and
    diffusion pair is what the manual's "minimum extension" means. Measuring one pair,
    or averaging, gives a wrong answer that still looks like a plausible number.
    """
    assert measured["parameters"]["Gate extension"]["value"] == 12.0
    assert "smallest" in measured["parameters"]["Gate extension"]["basis"]


def test_gate_cut_spacing_ignores_the_uncut_gates(measured):
    """Three of the four gates run straight through, so they have no end-to-end gap.

    Counting them as zero-spacing pairs would report a gate cut spacing of 0 nm.
    """
    assert measured["parameters"]["Gate Cut spacing"]["value"] == 17.0


def test_diffcon_profile_is_not_collapsed_by_the_bridging_column(measured):
    """One column runs diffcon from the n row to the p row without a break.

    Merging the layers the way the metal profile does turns the two 35 nm rows into a
    single 91 nm block - true of that column, false of the cell. The row each layer
    occupies in most of its columns is the answer, and the exception is reported.
    """
    record = measured["parameters"]["diffcon"]
    assert record["value"] == pytest.approx([12, 35, 21, 35, 12])
    assert sum(record["value"]) == pytest.approx(115.0)      # the cell height
    assert record["exceptions"], "the bridging column should be reported"


def test_poly_direction_is_derived_not_assumed(measured):
    """Widths are measured across the gates and extensions along them, so getting
    this wrong silently transposes every width and spacing in the table."""
    assert measured["poly_direction"] == "y"
    assert measured["orthogonal_direction"] == "x"


# --- what the layout cannot express -----------------------------------------

def test_cfet_only_parameter_is_not_measured_in_a_gaa_cell(measured):
    """Rule 3.13.5 defines this for CFET, and the Diff Interconnect layer is empty.

    Six unrelated pairs in this cell happen to measure exactly 15 nm - the diffusion
    break, and poly and diffcon to BM0 - because rule 3.1.6 ties the diffusion break
    to the poly width. Any of them could be dressed up as the answer, which is why
    this must stay unavailable rather than pick one.
    """
    record = measured["parameters"]["Diffusion to Diff interconnect spacing"]
    assert record["available"] is False
    assert record["value"] is None
    assert "CFET" in record["basis"]


@pytest.mark.parametrize("name", ["via1", "Diff Interconnect"])
def test_absent_layers_report_the_reason_not_zero(measured, name):
    record = measured["parameters"][name]
    assert record["available"] is False
    assert record["value"] is None
    assert "no geometry" in record["basis"]


# --- comparison against the stated tech file --------------------------------

def test_every_comparable_parameter_agrees_with_the_stated_tech_file(measured):
    reference = load_reference(find_reference(GDS))
    result = compare_to_reference(measured, reference)
    assert result["disagree"] == [], result["disagree"]
    assert result["agree_count"] == 26, result["headline"]
    # The three that cannot be measured must be carried through as stated-only, not
    # silently dropped and not counted as agreement.
    assert {row["parameter"] for row in result["stated_only"]} == {
        "Diffusion to Diff interconnect spacing", "via1", "Diff Interconnect"}


def test_the_comparison_can_fail():
    """Negative control. A comparison that cannot disagree proves nothing."""
    measured = {"parameters": {
        "Gate extension": {"parameter": "Gate extension", "value": 12.0,
                           "unit": "nm", "available": True, "basis": "x"}}}
    reference = {"file": "r.json", "stated": {"Gate extension":
                                              {"value": 13, "unit": "nm"}}}
    result = compare_to_reference(measured, reference)
    assert result["disagree_count"] == 1
    assert result["disagree"][0]["measured"] == 12.0
    assert result["disagree"][0]["stated"] == 13


def test_a_stated_value_never_becomes_a_measurement(measured):
    """The CFET parameter has a stated 15 nm. It must not appear as measured."""
    reference = load_reference(find_reference(GDS))
    result = compare_to_reference(measured, reference)
    row = next(r for r in result["stated_only"]
               if r["parameter"] == "Diffusion to Diff interconnect spacing")
    assert row["measured"] is None
    assert row["stated"] == 15
    assert "CFET" in row["reason"]


def test_reference_lookup_finds_the_bundled_tech_file(tmp_path):
    """An uploaded layout is written to a temporary directory, so the bundled
    lookup by stem is what lets a sample's stated tech file still be found."""
    copy = tmp_path / "AN2D1_2_RT_4.gds"
    copy.write_bytes(GDS.read_bytes())
    found = find_reference(copy)
    assert found is not None and found.name == "AN2D1_2_RT_4.techparams.json"
    assert find_reference(tmp_path / "NoSuchCell.gds") is None


# --- graceful degradation ----------------------------------------------------

def test_a_layout_with_no_poly_reports_why_nothing_could_be_measured(lm, tmp_path):
    """Without poly there is no poly direction, and every width and extension in the
    table is defined relative to it. Saying so beats returning a table of nulls."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("NOPOLY")
    cell.shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 1000, 240))
    path = tmp_path / "nopoly.gds"
    layout.write(str(path))
    result = tech_parameters(path, lm)
    assert result["poly_direction"] is None
    assert result["measured_count"] == 0
    assert "poly direction is undefined" in result["parameters"]["Poly direction"]["basis"]


def test_parameters_are_measured_for_every_sample(lm):
    """The measurements must not be specific to the cell they were developed on."""
    for gds in sorted(SAMPLES.glob("*.gds")):
        result = tech_parameters(gds, lm)
        assert result["poly_direction"] == "y", gds.name
        # CPP decomposes as 2 x poly-to-diffcon + diffcon width + poly width (3.2.6),
        # which is a cross-check that these three agree with the 45 nm gate pitch.
        params = result["parameters"]
        if all(params[k]["available"] for k in
               ("Poly to Diffcon spacing", "N-diffcon width", "N-poly width")):
            cpp = (2 * params["Poly to Diffcon spacing"]["value"]
                   + params["N-diffcon width"]["value"] + params["N-poly width"]["value"])
            assert cpp == pytest.approx(45.0), f"{gds.name}: rule 3.2.6 gives {cpp}"


# --- answering ---------------------------------------------------------------

@pytest.fixture(scope="module")
def metadata(lm):
    meta = analyze_gds(GDS, layermap=lm)
    outlines = shape_outlines(GDS, lm)
    cls = classify(outlines, GDS, [GDS.name])
    params = tech_parameters(GDS, lm)
    reference = load_reference(find_reference(GDS))
    params["reference"] = reference
    params["comparison"] = compare_to_reference(params, reference)
    cls["tech_parameters"] = params
    meta["classification"] = cls
    return meta


@pytest.mark.parametrize("question,expected", [
    ("calculate Gate extension in AN2D1_2_RT_4.gds", "Gate extension: 12 nm"),
    ("What is the Diffcon extension?", "Diffcon extension: 10 nm"),
    ("N-poly width?", "N-poly width: 15 nm"),
    ("What is the P-diffcon width", "P-diffcon width: 20 nm"),
    ("What is the Diffusion width", "Diffusion width: 15 nm"),
    ("What is the Power rail width", "Power rail width: 85 nm"),
    ("N/P Diffusion spacing?", "N/P Diffusion spacing: 41 nm"),
    ("Poly to Diffcon spacing", "Poly to Diffcon spacing: 5 nm"),
    ("Gate Cut spacing", "Gate Cut spacing: 17 nm"),
    ("Diffcon ETE spacing", "Diffcon ETE spacing: 21 nm"),
    ("What is Metal0", "15, 12, 9, 12, 19, 12, 9, 12, 15 nm"),
    ("What is Metal2", "21.5, 16, 12, 16, 12, 16, 21.5 nm"),
    ("what is via0", "size 18, 12"),
    ("what is pviag", "size 15, 12"),
    ("what is nviat", "size 20, 12"),
])
def test_each_parameter_question_is_answered_with_the_measured_figure(
        metadata, question, expected):
    reply = answer(metadata, question)
    assert reply is not None, question
    assert expected in reply, f"{question} -> {reply}"


def test_the_users_example_question_answers_exactly_twelve_nanometres(metadata):
    """The worked example: "calculate Gate extension" must give 12 nm."""
    reply = answer(metadata, "calculate Gate extension in AN2D1_2_RT_4.gds")
    assert "**Gate extension: 12 nm**" in reply
    assert "matches the 12 nm stated in" in reply


def test_power_rail_width_is_not_answered_with_the_power_scheme(metadata):
    """"power rail width" is a dimension.

    The classifier's "power rail" pattern used to claim the question first and answer
    it with backside-versus-frontside, which is a different question entirely.
    """
    reply = answer(metadata, "What is the Power rail width")
    assert "85 nm" in reply
    assert "Backside power" not in reply


def test_the_cfet_parameter_answer_attributes_the_stated_figure(metadata):
    reply = answer(metadata, "Diffusion to Diff interconnect spacing?")
    assert "cannot be measured" in reply
    assert "CFET" in reply
    assert "tech file states 15 nm" in reply
    assert "not a measurement of this cell" in reply


def test_an_unknown_parameter_is_refused_by_name(metadata):
    """A tech-file question naming nothing measured must be refused, not guessed at.

    "fin pitch" is deliberately not used here: it contains "pitch", so the pitch
    branch claims it first and answers about pitch metrics, which is correct.
    """
    reply = answer(metadata, "what is the tech file value for the cobalt liner")
    assert reply is not None
    assert "not one of the" in reply
    assert "measured ones include" in reply


def test_metal_pitch_still_reaches_the_pitch_answer(metadata, lm):
    """"Metal0" is a profile and "Metal0 pitch" is a pitch; both triggers match the
    phrase, so the order they are tested in decides which question gets answered."""
    from analyzer.pitch import analyze_pitch
    metadata = dict(metadata)
    metadata["pitch"] = analyze_pitch(shape_outlines(GDS, lm), GDS.name)
    reply = answer(metadata, "What is the Metal0 pitch")
    assert "21 nm pitch" in reply


# --- independent confirmation by KLayout's DRC engine ------------------------

def _samples():
    return sorted(SAMPLES.glob("*.gds"))


@pytest.mark.parametrize("gds", _samples(), ids=lambda p: p.name)
def test_drc_engine_confirms_the_measurements(gds):
    """Cross-check through `width_check`, `separation_check` and boolean subtraction.

    This module reads bounding boxes and reasons about them in Python. That is right
    for rectilinear geometry, but it is one extraction path: a fault in it would make
    the whole table wrong consistently and still self-consistent. KLayout's DRC engine
    shares no step with it beyond opening the file, so agreement means the numbers
    survive a change of method.
    """
    from tools.verify_techparams import compare

    ok, notes = compare(gds, runs=3)
    assert ok, f"{gds.name}:\n" + "\n".join(f"  {n}" for n in notes)
    joined = "\n".join(notes)
    assert "3 independent runs identical" in joined
    assert "rule 3.2.6" in joined                 # the pitch identity closed
    assert joined.count("confirmed by the DRC engine") >= 11


@pytest.mark.parametrize("gds", _samples(), ids=lambda p: p.name)
def test_the_drc_measurement_is_deterministic(gds):
    from analyzer.layermap import default_layermap as dlm
    from tools.verify_techparams import measure

    layermap = load_lyp(dlm())
    assert measure(gds, layermap) == measure(gds, layermap)


def test_the_drc_cross_check_can_fail():
    """Negative control. Perturb one measurement and require it to be caught."""
    import analyzer.techparams as module
    from tools.verify_techparams import compare

    real = module.tech_parameters

    def wrong(gds, layermap):
        result = real(gds, layermap)
        result["parameters"]["Gate extension"]["value"] = 11.0   # one nanometre off
        return result

    module.tech_parameters = wrong
    try:
        ok, notes = compare(_samples()[0], runs=1)
    finally:
        module.tech_parameters = real

    assert not ok, "an 11 nm gate extension was accepted"
    assert any("Gate extension MISMATCH" in n for n in notes), notes


def test_abutting_shapes_are_excluded_from_a_spacing_not_reported_as_zero():
    """An uncut gate is one continuous gate, not two ends 0 nm apart.

    KLayout reports distance 0 for the abutting pairs, and taking the minimum over all
    of them gives a gate cut spacing of 0 nm. The engine's own output still contains
    the real 17 nm, so excluding the abutting pairs recovers it rather than inventing
    it - which is why this can be a definitional exclusion and not a fudge.
    """
    from analyzer.layermap import default_layermap as dlm
    from tools.verify_techparams import _facing, regions

    layermap = load_lyp(dlm())
    region, dbu = regions(GDS, layermap)
    npoly, ppoly = region["NPOLY"], region["PPOLY"]
    box = npoly.bbox() + ppoly.bbox()
    limit = max(box.width(), box.height()) + 1
    distances = sorted({p.distance() for p in npoly.separation_check(ppoly, limit).each()
                        if _facing(p, "y")})
    nanometres = [round(d * dbu * 1000, 3) for d in distances]
    assert 0.0 in nanometres, "the abutting pairs should be present in the raw output"
    assert 17.0 in nanometres, "the real spacing must come from the engine itself"
    assert min(d for d in nanometres if d > 0) == 17.0
