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


# --- cost accounting ---------------------------------------------------------

def test_cost_uses_the_cache_discounts():
    """Cached reads bill at a tenth of input and writes at 1.25x.

    Getting this wrong would misreport the cost of a run by an order of magnitude,
    since the metadata block dominates every call and is nearly all cache after the
    first question about a file.
    """
    totals = {"input": 1_000_000, "output": 0, "cache_write": 0, "cache_read": 0}
    assert J.cost_of("claude-opus-5", totals) == pytest.approx(5.00)

    totals = {"input": 0, "output": 1_000_000, "cache_write": 0, "cache_read": 0}
    assert J.cost_of("claude-opus-5", totals) == pytest.approx(25.00)

    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 1_000_000}
    assert J.cost_of("claude-opus-5", totals) == pytest.approx(0.50)

    totals = {"input": 0, "output": 0, "cache_write": 1_000_000, "cache_read": 0}
    assert J.cost_of("claude-opus-5", totals) == pytest.approx(6.25)


def test_cost_is_none_for_an_unlisted_model():
    """Better to report nothing than to price a model at another's rate."""
    assert J.cost_of("some-future-model", {"input": 1, "output": 1,
                                           "cache_write": 0, "cache_read": 0}) is None


def test_usage_totals_start_empty_and_sum():
    from ai.llm import USAGE, reset_usage, usage_totals

    reset_usage()
    assert usage_totals() == {"calls": 0, "input": 0, "output": 0,
                              "cache_write": 0, "cache_read": 0}
    USAGE.append({"input": 10, "output": 5, "cache_write": 100, "cache_read": 0})
    USAGE.append({"input": 2, "output": 3, "cache_write": 0, "cache_read": 100})
    assert usage_totals() == {"calls": 2, "input": 12, "output": 8,
                              "cache_write": 100, "cache_read": 100}
    reset_usage()


def test_the_model_is_read_from_the_environment():
    """Switching models is a .env change, not a code change."""
    import os

    from ai.llm import DEFAULT_ANTHROPIC_MODEL, anthropic_model

    saved = os.environ.pop("ANTHROPIC_MODEL", None)
    try:
        assert anthropic_model() == DEFAULT_ANTHROPIC_MODEL
        os.environ["ANTHROPIC_MODEL"] = "claude-haiku-4-5"
        assert anthropic_model() == "claude-haiku-4-5"
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_MODEL", None)
        else:
            os.environ["ANTHROPIC_MODEL"] = saved


# --- the answers are kept ----------------------------------------------------

def test_every_graded_answer_is_written_to_the_transcript(tmp_path):
    """A paid answer that is graded and discarded has to be paid for again to look at.

    On a small budget that means it never gets looked at, so the judge writes each
    answer as it is produced - one JSON object per line, so a run that is killed
    part-way keeps everything it had.
    """
    transcript = tmp_path / "run.jsonl"
    results = J.judge(AN2D1, use_model=False, transcript=transcript,
                      model_name="deterministic")
    saved = J.load_transcript(transcript)
    assert len(saved) == len(results)

    record = saved[0]
    for field in ("question", "verdict", "reply", "axis", "file", "model",
                  "grounding_values"):
        assert field in record, field
    assert record["file"] == AN2D1.name
    assert record["reply"], "the answer text itself must be kept, not just the verdict"


def test_a_transcript_can_be_regraded_without_any_api_call(tmp_path, monkeypatch):
    """The point of keeping the answers: the grader can be changed and every past
    answer re-checked for free."""
    transcript = tmp_path / "run.jsonl"
    J.judge(AN2D1, use_model=False, transcript=transcript)

    def explode(*args, **kwargs):
        raise AssertionError("re-grading must not call a model")

    import ai.llm
    monkeypatch.setattr(ai.llm, "ask_llm", explode)
    assert J.regrade(transcript) == 0


def test_regrading_uses_the_current_graders_not_the_recorded_verdict(tmp_path):
    """A stricter grader must be able to fail an answer that passed when it was
    recorded - otherwise re-grading only ever confirms the old verdict."""
    transcript = tmp_path / "run.jsonl"
    J.log_answer(transcript, {
        "question": "What is the Gate extension?", "file": AN2D1.name,
        "model": "test", "axis": "correctness", "verdict": "PASS",
        "reasons": [], "reply": "The gate extension is 999 nm.",
        "metadata_digest": {},
    })
    assert J.regrade(transcript) == 1     # the oracle says 12 nm, so this must fail


def test_the_transcript_survives_a_killed_run(tmp_path):
    """One JSON object per line, appended - not a single document written at the end.

    A run that is interrupted after 20 paid answers must keep those 20.
    """
    transcript = tmp_path / "run.jsonl"
    for index in range(3):
        J.log_answer(transcript, {"question": f"q{index}", "verdict": "PASS"})
    lines = transcript.read_text().strip().splitlines()
    assert len(lines) == 3
    assert all(line.startswith("{") and line.endswith("}") for line in lines)


def test_the_stored_values_cover_every_metadata_block(tmp_path):
    """A re-grade must accept every number the live run would have accepted.

    The first version stored a hand-picked list of blocks and left `connectivity`
    out, so a re-grade failed a Haiku answer that had correctly cited
    `connectivity.intra_layer.total_components` - the number was real, the stored
    copy just didn't have it. Storing the derived value set instead cannot omit a
    block, which is what this pins: every value the live metadata would allow is in
    the saved set.
    """
    from analyzer.layermap import default_layermap, load_lyp
    from tools.factcheck import audit, values_in

    transcript = tmp_path / "run.jsonl"
    J.judge(AN2D1, use_model=False, transcript=transcript)
    stored = set(J.load_transcript(transcript)[0]["grounding_values"])

    live = values_in(J.build_metadata(AN2D1, load_lyp(default_layermap())))
    assert live - stored == set(), f"{len(live - stored)} values would be lost"

    # The specific number that exposed the omission, and a control.
    assert audit("q", "There are 83 connected components.", {}, stored)[1] == []
    assert audit("q", "There are 4242 components.", {}, stored)[1] == ["4242"]


def test_a_regrade_does_not_change_verdicts_it_should_not(tmp_path):
    """Re-grading with unchanged graders must reproduce the original verdicts.

    Any drift here means the saved state is lossy, which is exactly the bug the
    connectivity omission caused.
    """
    transcript = tmp_path / "run.jsonl"
    original = J.judge(AN2D1, use_model=False, transcript=transcript)
    graded = {r["question"]: r["verdict"] for r in original
              if r["verdict"] in ("PASS", "FAIL")}
    assert J.regrade(transcript) == 0
    saved = {r["question"]: r["verdict"] for r in J.load_transcript(transcript)}
    for question, verdict in graded.items():
        assert saved[question] == verdict, question
