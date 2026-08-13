"""Tests for the independent oracle and the answer judge.

The oracle reads the layout with `gdstk` and parses the .lyp itself, importing nothing
from `analyzer/`. These tests check that independence holds and that the two codebases
agree - and, just as importantly, that the judge is capable of failing a wrong answer.
A judge that passes everything is worse than none, because it certifies the failures.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools import judge as J
from tools.oracle import fact_sheet, layer_names

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = sorted(SAMPLES.glob("*.gds"))
AN2D1 = SAMPLES / "AN2D1_2_RT_4.gds"


# --- the oracle is independent ----------------------------------------------

def test_the_oracle_does_not_import_the_code_it_judges():
    """If the oracle used `analyzer`, it would pass every answer the analyzer gives."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "oracle.py").read_text()
    offenders = re.findall(r"^\s*(?:from|import)\s+(analyzer[\w.]*|klayout[\w.]*)",
                           source, re.M)
    assert not offenders, f"the oracle must not import: {offenders}"
    assert "import gdstk" in source


def test_the_oracle_parses_the_layer_map_itself():
    names = layer_names(SAMPLES / "Titan_layer_properties.lyp")
    assert names[(102, 0)] == "NPOLY"
    assert names[(300, 0)] == "BM0"
    assert names[(1, 0)] == "CELL-BOUNDARY"


@pytest.mark.parametrize("gds", GDS, ids=lambda p: p.name)
def test_the_oracle_and_the_analyzer_agree(gds):
    """Two codebases, no shared code, same numbers."""
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.measurements import shape_outlines
    from analyzer.pitch import analyze_pitch
    from analyzer.techparams import tech_parameters

    layermap = load_lyp(default_layermap())
    oracle = fact_sheet(gds)
    mine = tech_parameters(gds, layermap)["parameters"]
    pitch = analyze_pitch(shape_outlines(gds, layermap), gds.name)

    for name, expected in oracle["tech_parameters_nm"].items():
        if expected is None:
            continue
        record = mine.get(name)
        assert record and record["available"], f"{gds.name}: {name} not measured"
        assert record["value"] == pytest.approx(expected), (
            f"{gds.name} {name}: analyzer {record['value']}, oracle {expected}")

    assert (pitch["gate_pitch"] or {}).get("cpp_nm") == pytest.approx(
        oracle["gate_pitch_nm"])
    for metal in ("M0", "M1", "M2"):
        assert (pitch["metal_pitches"][metal] or {})["pitch_nm"] == pytest.approx(
            oracle[f"{metal.lower()}_pitch_nm"])


@pytest.mark.parametrize("gds", GDS, ids=lambda p: p.name)
def test_the_oracle_agrees_on_the_cell_and_the_counts(gds):
    from analyzer.gds_parser import analyze_gds
    from analyzer.layermap import default_layermap, load_lyp

    oracle = fact_sheet(gds)
    mine = analyze_gds(gds, layermap=load_lyp(default_layermap()))
    assert mine["design"]["top_cell"] == oracle["top_cell"]
    assert mine["design"]["polygon_count"] == oracle["polygon_count"]


def test_the_oracle_matches_the_supplied_tech_file():
    """The strongest check available: a third party's numbers."""
    from analyzer.techparams import find_reference, load_reference

    stated = load_reference(find_reference(AN2D1))["stated"]
    measured = fact_sheet(AN2D1)["tech_parameters_nm"]
    for name, value in measured.items():
        if value is None or name not in stated:
            continue
        assert value == pytest.approx(float(stated[name]["value"])), name


# --- the judge can fail things ----------------------------------------------

def test_the_judge_self_test_passes():
    ran: list[int] = []
    problems = J.self_test(ran)
    assert problems == [], problems
    assert ran[0] >= 14, "the negative controls have shrunk"


@pytest.mark.parametrize("reply,should_flag", [
    # A refusal names the claim it refuses, so the scan has to read context.
    ("I cannot say whether the layout is DRC clean.", False),
    ("DRC clean status is unavailable - there is no design_rules block.", False),
    ("There is no way to say whether there are shorts without a netlist.", False),
    # These assert it.
    ("The layout is DRC clean.", True),
    ("The layout is DRC clean, though I cannot check the timing.", True),
    ("There is a short between M0 and M1.", True),
    ("This design passes LVS.", True),
])
def test_the_overclaim_scan_reads_negation(reply, should_flag):
    flagged = any(not J._negated(reply, m.start(), m.end())
                  for pattern, _ in J.OVERCLAIMS
                  for m in re.finditer(pattern, reply, re.I))
    assert flagged is should_flag, reply


def test_a_wrong_value_is_caught_even_when_well_grounded():
    """The point of the correctness axis: a grounded number can be the wrong number.

    45 nm is in the metadata, so the grounding axis passes it. Only the oracle's
    expected value catches that the question asked for a count, not a pitch.
    """
    metadata = {"classification": {"pitch": {"gate_pitch": {"cpp_nm": 45.0}}}}
    result = J.grade_one("How many gate pitches?", "The gate pitch is 45 nm.",
                         J.states(5.0), "correctness", metadata)
    assert result["verdict"] == "FAIL"
    assert any("did not state 5" in r for r in result["reasons"])


def test_an_ungrounded_number_is_caught_even_when_correct():
    """And the mirror image: the value asked for is present, but so is an invented one."""
    metadata = {"design": {"polygon_count": 89}}
    result = J.grade_one("How many polygons?", "There are 89 polygons across 7 layers.",
                         J.states(89.0), "correctness", metadata)
    assert result["verdict"] == "FAIL"
    assert any("grounding" in r for r in result["reasons"])


def test_no_deterministic_branch_is_deferred_not_failed():
    """`None` from the local answerer means the app hands the question to the model.

    Counting that as a failure would penalise the tool for its own design.
    """
    result = J.grade_one("What is the timing?", None, J.states(1.0), "correctness", {})
    assert result["verdict"] == "DEFER"


# --- the battery ------------------------------------------------------------

def test_the_battery_is_built_from_the_oracle_not_hard_coded():
    """Every expected value must come from the fact sheet, so the battery travels to
    any layout rather than encoding one cell's answers."""
    from analyzer.techparams import find_reference, load_reference

    oracle = fact_sheet(AN2D1)
    stated = load_reference(find_reference(AN2D1))
    questions = J.battery(oracle, stated)
    assert len(questions) >= 30
    axes = {axis for _, _, axis in questions}
    assert axes == {"correctness", "restraint"}

    # A cell with fewer measurable parameters must produce a smaller battery, which
    # only happens if the questions are derived rather than listed.
    fewer = J.battery(fact_sheet(SAMPLES / "NR2D1_1_RT_4.gds"), None)
    assert len(fewer) < len(questions)


@pytest.mark.parametrize("gds", GDS, ids=lambda p: p.name)
def test_the_deterministic_answers_pass_the_judge(gds):
    """The whole point, run over every sample."""
    results = J.judge(gds, use_model=False)
    bad = [r for r in results if r["verdict"] == "FAIL"]
    assert not bad, "\n".join(f"{r['question']}: {r['reasons']}\n  > {r['reply'][:200]}"
                              for r in bad)
    graded = [r for r in results if r["verdict"] == "PASS"]
    assert len(graded) >= 25, f"only {len(graded)} questions were actually graded"


def test_a_connectivity_question_naming_a_via_is_not_answered_with_via_geometry():
    """Regression: the tech-parameter trigger matched "via0" and hijacked this.

    Answering "so VIA0 connects them, correct?" with via0's size accepts the premise
    by ignoring it, when the entire point is that the conclusion does not follow.
    """
    from analyzer.layermap import default_layermap, load_lyp
    from ai.deterministic import answer

    metadata = J.build_metadata(AN2D1, load_lyp(default_layermap()))
    reply = answer(metadata,
                   "The vias overlap both M0 and M1, so VIA0 connects them, correct?")
    assert "overlap" in reply.lower()
    assert "not the same as connection" in reply.lower()
    assert "size 18" not in reply


def test_interconnect_does_not_trip_the_connectivity_guard():
    """"Diff interconnect" contains "connect", but not at a word boundary."""
    from ai.deterministic import CONNECTIVITY_WORDS, TECHPARAM_TRIGGER

    # answer() lowercases before matching and the patterns carry no re.I, so the
    # triggers must be tested on the lowercased form the dispatcher actually sees.
    question = "What is the Diffusion to Diff interconnect spacing?".lower()
    assert TECHPARAM_TRIGGER.search(question)
    assert not CONNECTIVITY_WORDS.search(question)
    assert CONNECTIVITY_WORDS.search("Does VIA0 connect M0 to M1?".lower())


# --- rendering --------------------------------------------------------------

def test_the_renderer_produces_an_image(tmp_path):
    """Renders through KLayout's own LayoutView, so the colours are the .lyp's."""
    from tools.render import render

    target = render(AN2D1, tmp_path / "cell.png",
                    SAMPLES / "Titan_layer_properties.lyp", 800, 450)
    assert target.exists() and target.stat().st_size > 2000
    from PIL import Image
    with Image.open(target) as image:
        assert image.size == (800, 450)


def test_hiding_the_scaffolding_changes_the_image(tmp_path):
    """The track guides tile the whole cell, so leaving them on hides the design.

    Comparing bytes is enough: if the layer filter did nothing, the two renders of the
    same cell would be identical.
    """
    from tools.render import render

    lyp = SAMPLES / "Titan_layer_properties.lyp"
    full = render(AN2D1, tmp_path / "full.png", lyp, 600, 400)
    clean = render(AN2D1, tmp_path / "clean.png", lyp, 600, 400, hide_guides=True)
    assert full.read_bytes() != clean.read_bytes()


def test_no_analysis_module_reads_a_rendered_image():
    """An image is for sanity, not measurement - a pixel is about 0.15 nm here.

    This pins the boundary: if a measurement ever starts reading pixels, the numbers
    stop being exact and this fails.
    """
    root = Path(__file__).resolve().parent.parent
    for module in sorted((root / "analyzer").glob("*.py")):
        source = module.read_text()
        assert "save_image" not in source, module.name
        assert "klayout.lay" not in source, module.name
        assert "PIL" not in source and "Image.open" not in source, module.name
