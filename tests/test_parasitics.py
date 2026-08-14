"""Resistance and capacitance: what a layout settles, and what it does not.

    R = ρ·L/(W·T)      C_area = ε·W/H      C_coupling = ε·T/S

L, W and S are in the file and are measured here against a second parser. ρ, T, ε and
H are process constants and are refused rather than defaulted - a made-up sheet
resistance produces an ohm figure with no provenance, which is worse than no figure.

The test that matters most is the last one: when the drivers move in opposite
directions the answer genuinely depends on those constants, and the tool has to say
so instead of picking the side it happens to be able to compute.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.compare import answer_pair
from analyzer.connectivity import default_stack
from analyzer.layermap import default_layermap, load_lyp
from analyzer.parasitics import (compare_geometry, estimate_rc, load_process,
                                 wire_geometry)

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
A = SAMPLES / "DCAP0_1_RT_4.gds"
B = SAMPLES / "DCAP0_2_RT_4.gds"
PROCESS = SAMPLES / "example_process.json"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def overrides(lm):
    return default_stack(lm).get("role_overrides")


@pytest.fixture(scope="module")
def geometry_a(lm, overrides):
    return wire_geometry(A, lm, role_overrides=overrides)


@pytest.fixture(scope="module")
def geometry_b(lm, overrides):
    return wire_geometry(B, lm, role_overrides=overrides)


# --- the measurements -------------------------------------------------------

def test_wire_geometry_matches_a_second_parser(geometry_a, lm, overrides):
    """Areas and lengths, checked against gdstk rather than against ourselves."""
    gdstk = pytest.importorskip("gdstk")
    from analyzer.connectivity import layer_roles

    roles = layer_roles(lm, overrides)
    by_name = {meta["name"]: key for key, meta in roles.items()}
    cell = gdstk.read_gds(str(A)).top_level()[0]

    for name, row in geometry_a["conductors"].items():
        key = by_name[name]
        polygons = [p for p in cell.polygons if (p.layer, p.datatype) == key]
        area = sum(abs(p.area()) for p in polygons)
        length = sum(max(max(pt[0] for pt in p.points) - min(pt[0] for pt in p.points),
                         max(pt[1] for pt in p.points) - min(pt[1] for pt in p.points))
                     for p in polygons)
        assert row["area_um2"] == pytest.approx(area, abs=1e-9), name
        assert row["total_length_um"] == pytest.approx(length, abs=1e-9), name


def test_vias_are_counted_separately_from_wires(geometry_a):
    assert geometry_a["via_total"] == sum(geometry_a["vias"].values())
    assert set(geometry_a["vias"]) & {"VIA0", "DVB"}
    # A via is not a wire: it must not appear in the conductor lengths.
    assert not set(geometry_a["vias"]) & set(geometry_a["conductors"])


def test_coupling_is_measured_within_a_stated_window(geometry_a):
    assert geometry_a["coupling_window_nm"] == 100.0
    m0 = geometry_a["conductors"]["M0"]
    assert m0["coupling_run_um"] > 0
    assert m0["closest_spacing_nm"] is not None
    # The closest spacing has to be inside the window it was measured in.
    assert m0["closest_spacing_nm"] <= geometry_a["coupling_window_nm"]


def test_a_layer_with_nothing_near_it_has_no_coupling_run(geometry_a):
    poly = geometry_a["conductors"]["NPOLY"]
    assert poly["coupling_run_um"] == 0
    assert poly["closest_spacing_nm"] is None


def test_geometry_states_what_it_cannot_give(geometry_a):
    assert "resistivity" in geometry_a["not_derivable"]["resistance"]
    assert "permittivity" in geometry_a["not_derivable"]["capacitance"]


# --- the estimate -----------------------------------------------------------

def test_the_process_file_is_validated(tmp_path):
    with pytest.raises(ValueError, match="no 'layers' object"):
        path = tmp_path / "a.json"
        path.write_text(json.dumps({"technology": "x"}))
        load_process(path)
    with pytest.raises(ValueError, match="none of"):
        path = tmp_path / "b.json"
        path.write_text(json.dumps({"layers": {"M1": {"colour": "red"}}}))
        load_process(path)


def test_resistance_is_squares_times_sheet_resistance(geometry_a):
    process = load_process(PROCESS)
    result = estimate_rc(geometry_a, process)
    m0_geometry = geometry_a["conductors"]["M0"]
    m0 = next(row for row in result["layers"] if row["layer"] == "M0")

    squares = m0_geometry["total_length_um"] / m0_geometry["min_width_um"]
    expected = squares * process["layers"]["M0"]["sheet_resistance_ohm_sq"]
    # The reported squares are rounded for display; the ohms are not derived from
    # the rounded figure, which is what this checks.
    assert m0["squares"] == pytest.approx(squares, abs=1e-4)
    assert m0["resistance_ohm"] == pytest.approx(expected, rel=1e-9)


def test_capacitance_is_area_plus_fringe_plus_coupling(geometry_a):
    process = load_process(PROCESS)
    result = estimate_rc(geometry_a, process)
    row = geometry_a["conductors"]["M0"]
    constants = process["layers"]["M0"]
    expected = (row["area_um2"] * constants["area_cap_aF_per_um2"]
                + row["perimeter_um"] * constants["fringe_cap_aF_per_um"]
                + row["coupling_run_um"] * constants["coupling_cap_aF_per_um"])
    m0 = next(r for r in result["layers"] if r["layer"] == "M0")
    assert m0["capacitance_aF"] == pytest.approx(expected, rel=1e-9)


def test_via_resistance_is_counted_and_kept_separate(geometry_a):
    result = estimate_rc(geometry_a, load_process(PROCESS))
    assert result["totals"]["via_resistance_ohm"] > 0
    assert result["totals"]["resistance_ohm"] == pytest.approx(
        result["totals"]["wire_resistance_ohm"] + result["totals"]["via_resistance_ohm"])


def test_a_layer_the_process_file_does_not_price_is_named_not_defaulted(geometry_a,
                                                                       tmp_path):
    thin = tmp_path / "thin.json"
    thin.write_text(json.dumps({"layers": {"M0": {"sheet_resistance_ohm_sq": 60}}}))
    result = estimate_rc(geometry_a, load_process(thin))
    assert [row["layer"] for row in result["layers"]] == ["M0"]
    assert "M1" in result["unpriced_layers"]
    assert "BM0" in result["unpriced_layers"]
    # Nothing was invented for them: only M0 contributes.
    m0 = next(r for r in result["layers"] if r["layer"] == "M0")
    assert result["totals"]["wire_resistance_ohm"] == pytest.approx(m0["resistance_ohm"])


def test_the_estimate_says_it_is_lumped_not_a_timing_number(geometry_a):
    result = estimate_rc(geometry_a, load_process(PROCESS))
    assert "distributed" in result["not_derivable"]["distribution"]
    assert "timer" in result["not_derivable"]["distribution"]


# --- the comparison ---------------------------------------------------------

def test_the_verdict_names_the_file_not_a_letter(geometry_a, geometry_b):
    verdict = compare_geometry(geometry_a, geometry_b)
    assert A.name in verdict["resistance"]
    assert "whatever the process constants" in verdict["resistance"]


def test_agreeing_drivers_settle_the_direction(geometry_a, geometry_b):
    """A is longer and has more metal area, with the same vias and coupling run, so A
    is the higher-R and higher-C layout however the constants land."""
    verdict = compare_geometry(geometry_a, geometry_b)
    drivers = verdict["drivers"]
    assert drivers["wire_length_um"][0] > drivers["wire_length_um"][1]
    assert drivers["via_count"][0] == drivers["via_count"][1]
    assert A.name in verdict["resistance"] and A.name in verdict["capacitance"]


def test_identical_layouts_are_reported_as_indistinguishable(geometry_a):
    verdict = compare_geometry(geometry_a, geometry_a)
    assert "identical" in verdict["resistance"]
    assert "identical" in verdict["capacitance"]


def test_drivers_that_disagree_are_not_resolved(geometry_a, geometry_b):
    """The case that must not be answered.

    More wire but fewer vias: resistance depends on the sheet resistance and the via
    resistance, which are exactly the constants a layout does not carry. Picking a
    winner here would be inventing one.
    """
    longer = json.loads(json.dumps(geometry_a))
    longer["totals"]["wire_length_um"] += 1.0        # B gets more wire...
    longer["via_total"] = geometry_a["via_total"] - 3  # ...and fewer vias
    verdict = compare_geometry(geometry_a, longer)
    assert "opposite directions" in verdict["resistance"]
    assert "not in a layout" in verdict["resistance"]
    assert "Nothing here settles it" in verdict["resistance"]


# --- the answers --------------------------------------------------------

@pytest.fixture(scope="module")
def context(geometry_a, geometry_b, lm):
    xor_summary = {"comparable": True, "summary": {"identical": False,
                                                   "layers_changed": 4,
                                                   "layers_compared": 31,
                                                   "difference_regions": 19,
                                                   "total_xor_area_um2": 0.005308},
                   "layers": []}
    return {"xor": xor_summary,
            "a": {"file": A.name, "parasitics": geometry_a},
            "b": {"file": B.name, "parasitics": geometry_b}}


@pytest.mark.parametrize("question", [
    "Which layout has more capacitance?",
    "Which has higher resistance?",
    "Is the RC worse in B?",
    "Which has more coupling?",
    "Is there more parasitic loading in B?",
    "Which layout would have worse crosstalk?",
    "Any IR drop concern?",
    "Did the wire length change?",
])
def test_rc_questions_are_answered_from_the_drivers(context, question):
    answer = answer_pair(context, question)
    assert answer, question
    assert "total wire length" in answer
    assert "coupling run" in answer
    assert "vias" in answer


def test_without_a_process_file_it_asks_for_one_rather_than_inventing_ohms(context):
    answer = answer_pair(context, "Which has higher resistance?")
    assert "Ω" not in answer
    assert "ρ·L/(W·T)" in answer
    assert "ITF or technology file" in answer


def test_with_a_process_file_it_gives_ohms_and_farads(geometry_a, geometry_b):
    process = load_process(PROCESS)
    context = {
        "xor": {"comparable": True, "summary": {"identical": False}},
        "a": {"file": A.name, "parasitics": geometry_a,
              "rc": estimate_rc(geometry_a, process)},
        "b": {"file": B.name, "parasitics": geometry_b,
              "rc": estimate_rc(geometry_b, process)},
    }
    answer = answer_pair(context, "Which layout has more capacitance?")
    assert "Ω" in answer and "fF" in answer
    expected = estimate_rc(geometry_a, process)["totals"]["resistance_ohm"]
    assert f"{expected:g}" in answer
    # And it still says what a lumped figure is not.
    assert "not a distributed network" in answer


def test_an_rc_question_is_not_swallowed_by_the_better_refusal(context):
    """"Which is better for capacitance?" contains "better", and the measurable half
    is answered rather than refused."""
    answer = answer_pair(context, "Which one is better for capacitance?")
    assert "total wire length" in answer
    assert not answer.startswith("I cannot tell you")


def test_a_timing_question_is_still_refused_even_though_rc_drives_it(context):
    answer = answer_pair(context, "Will the extra resistance hurt timing?")
    assert "total wire length" in answer      # it answers the measurable half
    assert "extractor" in answer or "process constants" in answer


def test_rc_questions_fall_through_when_nothing_was_measured(context):
    bare = {"xor": context["xor"], "a": {"file": "a.gds"}, "b": {"file": "b.gds"}}
    assert answer_pair(bare, "Which has more capacitance?") is None
