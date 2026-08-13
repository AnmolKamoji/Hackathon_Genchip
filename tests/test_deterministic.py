"""Every question in the documented demo script must be answerable without an LLM."""
from pathlib import Path

import pytest

from ai.deterministic import answer, answer_comparison, is_comparison_question
from analyzer.comparison import compare_metadata
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


@pytest.fixture(scope="module")
def fused():
    return analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")


@pytest.fixture(scope="module")
def raw():
    return analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds")


@pytest.fixture(scope="module")
def comparison():
    a = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")
    b = analyze_pair(SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    return compare_metadata(a, b)


# The demo script from the setup notes, minus the two questions that are
# deliberately left to the LLM because they ask for rephrasing, not for facts.
DEMO_QUESTIONS = [
    "Give me a summary of this GDS.",
    "How many polygons are there?",
    "Which layers are used?",
    "What layers are used?",
    "How many vias are present?",
    "How many vias?",
    "What is the largest cell?",
    "What is the top cell?",
    "Which layer has the highest density?",
    "What is the layout size?",
    "How many polygons are on M0?",
    "Does this design contain M1?",
    "How many cells are there?",
]


@pytest.mark.parametrize("question", DEMO_QUESTIONS)
def test_demo_questions_answered_without_llm(fused, question):
    reply = answer(fused, question)
    assert reply, f"no deterministic answer for {question!r}"


def test_specific_answers(fused):
    assert "60" in answer(fused, "How many polygons are there?")
    assert "6" in answer(fused, "How many vias are present?")
    assert "NR2D1" in answer(fused, "What is the top cell?")
    assert "BSPowerRail" in answer(fused, "Which layer has the highest density?")
    assert "0.15" in answer(fused, "What is the layout size?")


def test_per_layer_polygon_count(fused):
    reply = answer(fused, "How many polygons are on M0?")
    assert "M0" in reply and "6" in reply


def test_absent_layer_is_not_answered_with_the_design_total(fused):
    """The old code answered this with the whole-design total, reading as if M9 held it."""
    reply = answer(fused, "How many polygons are on M9?")
    assert "no layer named" in reply.lower()
    assert "60" not in reply


def test_layer_presence(fused):
    assert answer(fused, "Does this design contain M1?").startswith("No.")
    assert answer(fused, "Does this design contain M0?").startswith("Yes.")


def test_vias_unavailable_is_explained_not_zero(raw):
    reply = answer(raw, "How many vias are present?")
    assert "unavailable" in reply.lower()
    assert "0 vias" not in reply


def test_summary_marks_unavailable_facts(raw, fused):
    assert "unavailable" in answer(raw, "Give me a summary of this GDS.")
    assert "6" in answer(fused, "Give me a summary of this GDS.")


def test_rephrasing_questions_defer_to_the_llm(fused):
    """Audience-facing narrative is the model's job, so these must return None."""
    assert answer(fused, "Explain this layout to a non-expert.") is None
    assert answer(fused, "Explain these changes to a non-layout engineer.") is None


def test_most_populated_layer_is_not_shadowed(fused):
    reply = answer(fused, "Which layer has the most polygons?")
    assert "most polygons" in reply


def test_comparison_questions_are_recognised():
    for q in ["What changed between the two layouts?", "Compare NR2D1_1_RT_4 and NR2D1_2_RT_4.",
              "What is the difference?", "file A vs file B"]:
        assert is_comparison_question(q)
    assert not is_comparison_question("How many polygons are there?")


def test_comparison_answer_names_the_added_layer(comparison):
    reply = answer_comparison(comparison, "What changed between the two layouts?")
    assert "M1" in reply
    assert "VIA_M0_M1" in reply
    assert "+7" in reply   # polygons
    assert "+3" in reply   # vias


def test_comparison_answer_is_none_for_unrelated_questions(comparison):
    assert answer_comparison(comparison, "How many polygons are there?") is None
