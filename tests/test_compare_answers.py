"""The questions a reviewer asks of two layouts, and whether the answers are true.

Every expected value here is computed in the test, from the analyzers or from gdstk,
rather than copied from what the answerer said. An answer that agrees with itself
proves nothing.

Two failure modes are checked as carefully as correctness:

* **A general answer to a specific question.** "Did the transistor count change?"
  answered with an XOR area summary reads like an answer and is not one. Every
  question below asserts on the thing it actually asked about.
* **A confident answer to an unanswerable question.** Timing, power, yield and "is
  this safe to tape out" have to be refused, whatever the layouts contain.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai.compare import answer_pair
from analyzer.classify import classify
from analyzer.connectivity import analyze_connectivity, default_stack
from analyzer.drc import check_layout
from analyzer.edit import grid_audit
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import measure_layers, measure_vias, shape_outlines
from analyzer.netlist import extract as extract_netlist
from analyzer.pitch import analyze_pitch
from analyzer.xor_diff import xor_compare

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
A = SAMPLES / "DCAP0_1_RT_4.gds"
B = SAMPLES / "DCAP0_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _side(path, lm, stack):
    outlines = shape_outlines(path, lm)
    metadata = analyze_gds(path, layermap=lm)
    classification = classify(outlines, path, [path.name])
    classification["pitch"] = analyze_pitch(outlines, path.name)
    metadata["classification"] = classification
    metadata["pitch"] = classification["pitch"]
    metadata["outlines"] = outlines
    measurements = measure_layers(path, lm)
    measurements["vias"] = measure_vias(measurements)
    metadata["measurements"] = measurements
    return {
        "file": path.name,
        "metadata": metadata,
        "drc": check_layout(outlines),
        "connectivity": analyze_connectivity(path, lm, stack=stack),
        "netlist": extract_netlist(path, lm, stack),
        "grid": grid_audit(path, 1.0),
    }


@pytest.fixture(scope="module")
def context(lm):
    stack = default_stack(lm)
    return {"xor": xor_compare(A, B, lm), "a": _side(A, lm, stack),
            "b": _side(B, lm, stack)}


@pytest.fixture(scope="module")
def identical(lm):
    stack = default_stack(lm)
    return {"xor": xor_compare(A, A, lm), "a": _side(A, lm, stack),
            "b": _side(A, lm, stack)}


def ask(context, question):
    answer = answer_pair(context, question)
    assert answer, f"no answer for: {question}"
    return answer


# --- what changed -----------------------------------------------------------

def test_what_changed_reports_the_measured_totals(context, lm):
    truth = xor_compare(A, B, lm)["summary"]
    answer = ask(context, "What changed between these two layouts?")
    assert f"{truth['layers_changed']} of {truth['layers_compared']} layers" in answer
    assert f"{truth['difference_regions']} region" in answer
    assert f"{truth['total_xor_area_um2']:g}" in answer
    # A difference is not an error, and the answer has to say so.
    assert "not an error" in answer


def test_where_gives_coordinates_of_the_largest_regions(context):
    answer = ask(context, "Where are the differences?")
    assert "µm" in answer and "[" in answer
    assert "largest" in answer.lower()


def test_the_largest_difference_is_not_answered_with_a_summary(context, lm):
    truth = xor_compare(A, B, lm)["summary"]
    answer = ask(context, "What is the largest difference?")
    assert f"{truth['largest_single_difference_um2']:g}" in answer
    assert truth["largest_difference_on_layer"] in answer


def test_how_much_area_changed(context, lm):
    truth = xor_compare(A, B, lm)["summary"]
    answer = ask(context, "How much area changed?")
    for value in (truth["total_xor_area_um2"], truth["total_area_removed_um2"],
                  truth["total_area_added_um2"]):
        assert f"{value:g}" in answer


# --- mask impact ------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Is this a metal-only change?",
    "Do I need a base layer respin?",
    "Which masks are affected?",
    "Which layers changed?",
])
def test_mask_questions_name_the_layers_and_their_numbers(context, lm, question):
    changed = [row for row in xor_compare(A, B, lm)["layers"] if not row["identical"]]
    answer = ask(context, question)
    for row in changed:
        assert row["name"] in answer
        assert f"{row['layer']}/{row['datatype']}" in answer


# --- pins and labels --------------------------------------------------------

def test_pin_movement_is_measured_from_the_label_positions(context, lm):
    """These two revisions move labels without renaming any of them."""
    answer = ask(context, "Did any pin move?")
    assert "moved" in answer
    positions_a = {(l["text"], tuple(l["at_um"]))
                   for row in shape_outlines(A, lm)["layers"] for l in row["labels"]}
    positions_b = {(l["text"], tuple(l["at_um"]))
                   for row in shape_outlines(B, lm)["layers"] for l in row["labels"]}
    assert positions_a != positions_b, "the fixture no longer exercises moved labels"
    moved_names = {text for text, _ in positions_a ^ positions_b}
    assert any(name in answer for name in moved_names)


def test_pin_names_are_reported_separately_from_positions(context):
    answer = ask(context, "Did the pin names change?")
    assert "No label differs" in answer


def test_nothing_moved_is_said_plainly(identical):
    answer = ask(identical, "Did any pin move?")
    assert "No pin moved" in answer


# --- counts -----------------------------------------------------------------

def test_polygon_count(context, lm):
    truth_a = analyze_gds(A, layermap=lm)["design"]["polygon_count"]
    truth_b = analyze_gds(B, layermap=lm)["design"]["polygon_count"]
    answer = ask(context, "Did the number of polygons change?")
    assert str(truth_a) in answer
    if truth_a == truth_b:
        assert "unchanged" in answer


def test_via_count_agrees_with_the_rest_of_the_page(context, lm):
    """Vias and contacts are counted separately everywhere else, so they are counted
    separately here.

    `measure_vias` lists both under one key - summing it gives 14 where every table
    on the page says 10 vias and 4 contacts. An answer that quotes 14 contradicts
    the section directly above it, which is worse than saying nothing.
    """
    design = analyze_gds(A, layermap=lm)["design"]
    answer = ask(context, "Did the via count change?")
    assert f"unchanged at {design['via_count']}" in answer
    assert f"Contacts, counted separately: unchanged at {design['contact_count']}" in answer

    # And the two really are different numbers, or this test proves nothing.
    both = sum(row["count"] for row in measure_vias(measure_layers(A, lm))["via_layers"])
    assert both == design["via_count"] + design["contact_count"] != design["via_count"]


def test_transistor_count_comes_from_the_extracted_netlist(context, lm):
    truth = extract_netlist(A, lm, default_stack(lm))["summary"]["device_classes"]
    answer = ask(context, "Did the transistor count change?")
    assert str(sum(truth.values())) in answer
    for kind in truth:
        assert kind in answer
    # Same count is not the same circuit, and the answer must not imply it is.
    assert "LVS" in answer


def test_cell_size(context, lm):
    layout_a = analyze_gds(A, layermap=lm)["layout"]
    answer = ask(context, "Did the cell size change?")
    assert f"{layout_a['width_um']:g}" in answer
    assert f"{layout_a['height_um']:g}" in answer
    # The drawn extent is not a placement site, and saying so avoids a real mistake.
    assert "placement site" in answer


def test_pitch(context, lm):
    outlines = shape_outlines(A, lm)
    truth = analyze_pitch(outlines, A.name)["gate_pitch"]["cpp_nm"]
    answer = ask(context, "Did the gate pitch change?")
    assert f"{truth:g} nm" in answer


def test_density_uses_the_measured_percentages(context, lm):
    rows_a = {r["name"]: r.get("density_percent") for r in analyze_gds(A, layermap=lm)["layers"]}
    rows_b = {r["name"]: r.get("density_percent") for r in analyze_gds(B, layermap=lm)["layers"]}
    differing = {name for name, value in rows_a.items()
                 if rows_b.get(name) is not None and value is not None
                 and rows_b[name] != value}
    answer = ask(context, "Did metal density change?")
    for name in differing:
        assert name in answer, f"{name} differs in density and is not mentioned"


# --- layers, technology, rules, grid ----------------------------------------

def test_layer_sets_are_compared_by_name(context, lm):
    names_a = {row["name"] for row in analyze_gds(A, layermap=lm)["layers"]}
    answer = ask(context, "Does B use any layer that A does not?")
    assert str(len(names_a)) in answer


def test_technology_is_reported_as_inferred(context):
    answer = ask(context, "Are both layouts the same technology?")
    assert "GAA" in answer
    assert "inferred" in answer


def test_rule_results_are_compared_rule_by_rule(context, lm):
    drc = check_layout(shape_outlines(A, lm))
    answer = ask(context, "Does B introduce any DRC violations that A did not have?")
    assert str(drc["summary"]["rules_checked"]) in answer
    # A clean run on the checked rules is not "DRC clean", and the wording matters.
    assert "checkable rules" in answer or "checked rules" in answer


def test_the_grid_question_is_answered_from_a_measurement(context):
    truth_a = grid_audit(A, 1.0)["shapes"]
    truth_b = grid_audit(B, 1.0)["shapes"]
    answer = ask(context, "Is B still on grid?")
    assert str(truth_a) in answer and str(truth_b) in answer
    assert "1 nm" in answer or "1.0 nm" in answer


def test_connectivity_answers_with_net_counts_and_its_limit(context, lm):
    truth = analyze_connectivity(A, lm, stack=default_stack(lm))
    count = truth["nets"]["summary"]["net_count"]
    answer = ask(context, "Did connectivity change?")
    assert str(count) in answer
    assert "LVS" in answer          # same net count is not the same circuit


# --- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("question,subject", [
    ("Is B better than A?", "better"),
    ("Which one should I use?", "better"),
    ("Will B pass timing?", "timing"),
    ("Is B faster?", "timing"),
    ("Is this change safe to tape out?", "safe to release"),
    ("Can I sign this off?", "safe to release"),
    ("Did the leakage change?", "power"),
    ("Will this hurt yield?", "manufacturability"),
])
def test_judgement_questions_are_refused(context, question, subject):
    answer = ask(context, question)
    assert answer.startswith("I cannot tell you")
    assert subject in answer
    # A refusal still offers what *is* measured, or it is just a wall.
    assert "measured" in answer or "What it can give you" in answer


def test_a_refusal_wins_over_a_factual_branch(context):
    """"Is the cell size better in B?" contains a size question and a judgement."""
    answer = ask(context, "Is the cell size better in B?")
    assert answer.startswith("I cannot tell you")


# --- identical layouts ------------------------------------------------------

def test_identical_layouts_say_so_rather_than_listing_nothing(identical):
    answer = ask(identical, "What changed between these two layouts?")
    assert "identical" in answer.lower()


def test_identical_layouts_still_answer_specific_questions(identical):
    assert "unchanged" in ask(identical, "Did the via count change?")
    assert "unchanged" in ask(identical, "Did the number of polygons change?")
    assert "identical in both" in ask(identical, "Did the cell size change?")


# --- the things it must not pretend to answer -------------------------------

@pytest.mark.parametrize("question", [
    "What is the weather?",
    "Write me a testbench for this cell.",
    "How do I improve this layout?",
])
def test_questions_outside_its_reach_are_handed_on(context, question):
    """Returning None sends the question to the model rather than answering it with
    whatever branch happened to match. Silence is better than a wrong route."""
    assert answer_pair(context, question) is None


def test_an_uncomparable_pair_says_so_before_anything_else(lm):
    context = {"xor": {"comparable": False, "reason": "different top cells"},
               "a": {"file": "a.gds"}, "b": {"file": "b.gds"}}
    answer = answer_pair(context, "What changed?")
    assert "cannot be compared" in answer
    assert "different top cells" in answer


# --- phrasing ---------------------------------------------------------------
#
# The same question asked the way people actually ask it. A pattern that only
# matches the textbook phrasing sends the rest to the model, which then answers a
# question about two layouts from one layout's metadata.

@pytest.mark.parametrize("question,expect", [
    ("is the cell bigger?", "cell outline"),
    ("did the footprint change?", "cell outline"),
    ("more polygons in B?", "Polygons"),
    ("did we add vias?", "Vias"),
    ("same number of transistors?", "device count"),
    ("how many NMOS in each?", "NMOS"),
    ("did the pins shift?", "Label positions"),
    ("did any port move?", "Label positions"),
    ("were any labels renamed?", "label"),
    ("metal only?", "interconnect"),
    ("which masks do I re-make?", "mask"),
    ("can this be an ECO?", "ECO"),
    ("any new violations?", "checked rules"),
    ("is B denser?", "Density"),
    ("same CPP?", "gate pitch"),
    ("both GAA?", "GAA"),
    ("which corner changed?", "largest differences"),
    ("how big is the change?", "XOR area"),
])
def test_natural_phrasings_reach_the_right_answer(context, question, expect):
    answer = ask(context, question)
    assert expect.lower() in answer.lower(), f"{question!r} -> {answer[:120]}"
    # And none of them fell through to the general summary.
    assert not (answer.startswith("4 of 31 layers differ")
                and expect not in ("XOR area", "largest differences"))


@pytest.mark.parametrize("question", [
    "should I tape this out?",
    "can I sign this off?",
    "will this close timing?",
    "which is better for power?",
    "any yield risk?",
    "is B worse?",
])
def test_judgement_phrasings_are_all_refused(context, question):
    assert ask(context, question).startswith("I cannot tell you")


def test_a_shorts_question_does_not_claim_there_are_none(context):
    """"Any new shorts?" is a connectivity question, and the honest answer says what
    the net graph is and what it is not."""
    answer = ask(context, "any new shorts?")
    assert "physical connectivity" in answer
    assert "LVS" in answer
    assert "no shorts" not in answer.lower()
