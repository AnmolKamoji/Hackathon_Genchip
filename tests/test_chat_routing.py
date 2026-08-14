"""Which answerer a question reaches, and whether it answered what was asked.

Every branch in this repository can be individually correct while the chat still
answers the wrong question. With two files open the comparison answerer used to be
tried first for *everything*, so:

    "What is the gate pitch?"      -> "gate pitch (CPP) unchanged at 45 nm; M0 pitch
                                      unchanged at 21 nm; M1 ...; M2 ..."
    "Is this layout DRC clean?"    -> both files' rule counts, and no refusal at all
    "The vias overlap both M0 and
     M1, so VIA0 connects them?"   -> "Vias: unchanged at 10"

The first states the right number and does not answer the question. The second drops
a refusal the tool exists to make. The third answers something else entirely. All
three grade as "correct" on any check that only looks for a number, which is why the
routing needs tests of its own.

The rule: a question about one layout is answered from that layout, a question about
the pair from the pair, and neither is used as a fallback for the other until the
right one has declined.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai.compare import is_pair_question
from analyzer.connectivity import default_stack
from analyzer.document import build_document
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.parasitics import wire_geometry
from analyzer.xor_diff import xor_compare
from ui.sections import chat

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
A, B = SAMPLES / "DCAP0_1_RT_4.gds", SAMPLES / "DCAP0_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def page(lm):
    """What the page holds with both files uploaded."""
    stack = default_stack(lm)
    names = [A.name, B.name]
    docs = {p.name: build_document(p, lm, stack=stack, all_filenames=names)
            for p in (A, B)}
    xor = xor_compare(A, B, lm)
    extras = {p.name: {"outlines": shape_outlines(p, lm),
                       "parasitics": wire_geometry(
                           p, lm, role_overrides=stack.get("role_overrides"))}
              for p in (A, B)}
    pair = chat.pair_context(docs[A.name], docs[B.name], xor, extras)
    return {"pair": pair, "xor": xor,
            "metadata": chat.enriched_metadata(docs[A.name])}


def ask(page, question: str) -> str:
    """Exactly what the page asks, including the model fallback being unavailable."""
    return chat.answer_for(question, page["metadata"], history=[],
                           pair=page["pair"], xor=page["xor"])


# --- a detail question gets a detail answer ---------------------------------

@pytest.mark.parametrize("question,wanted", [
    ("What is the gate pitch?", "45"),
    ("What is the CPP?", "45"),
    ("What is the M0 pitch?", "21"),
    ("What is the M1 pitch?", "30"),
    ("What is the M2 pitch?", "28"),
    ("How many polygons are there?", "56"),
    ("How wide is the cell?", "90"),
    ("How many routing tracks are there?", "4"),
])
def test_a_detail_question_is_answered_about_the_layout(page, question, wanted):
    answer = ask(page, question)
    assert wanted in answer, answer
    # "unchanged at 45 nm" is the comparison answerer talking. It contains the right
    # number and answers a question that was not asked.
    assert "unchanged at" not in answer, answer
    assert B.name not in answer, answer


def test_a_restraint_question_about_one_layout_still_refuses(page):
    """The failure this replaced: the pair answerer reported both files' rule counts
    and never said the verdict was unavailable."""
    answer = ask(page, "Is this layout DRC clean?")
    low = answer.lower()
    assert "cannot" in low or "unavailable" in low or "not" in low
    assert "fails 0 of the" not in answer


def test_a_leading_question_addresses_its_premise(page):
    """"The vias overlap both M0 and M1, so VIA0 connects them, correct?" is about
    one layout. "both" here is two layers, not two files."""
    answer = ask(page, "The vias overlap both M0 and M1, so VIA0 connects them, "
                       "correct?")
    assert "overlap" in answer.lower(), answer
    assert "unchanged at" not in answer, answer


def test_a_single_layout_refusal_does_not_borrow_the_pairs_numbers(page):
    """A refusal offers what is measured *of what was asked about*. The XOR area is
    not among this cell's measurements."""
    answer = ask(page, "What is the timing of this cell?")
    assert answer.startswith("I cannot tell you")
    assert "XOR area" not in answer


# --- a comparison question gets a comparison --------------------------------

@pytest.mark.parametrize("question", [
    "What changed between these two layouts?",
    "Which layers changed?",
    "Did the cell size change?",
    "Did the via count change?",
    "Did any pin move?",
    "Are both layouts the same technology?",
    "Is this a metal-only change?",
    "Can this be an ECO?",
    "Which masks are affected?",
    "Do I need a base layer respin?",
    "Does B use any layer that A does not?",
])
def test_a_comparison_question_is_answered_about_the_pair(page, question):
    answer = ask(page, question)
    assert answer
    # A pair answer names both files, shows a before → after, states a difference,
    # or states that there is none. A one-file answer does none of those.
    assert (B.name in answer or "→" in answer or "unchanged" in answer
            or "differ" in answer or "both" in answer.lower()
            or "identical" in answer), answer


def test_a_question_neither_side_claims_still_reaches_the_pair(page):
    """"Any IR drop concern?" names no file and contains no comparison word, and is
    still answerable from what was measured in both."""
    answer = ask(page, "Any IR drop concern?")
    assert "total wire length" in answer
    assert A.name in answer and B.name in answer


# --- the signal itself ------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("Are both layouts the same technology?", True),
    ("The vias overlap both M0 and M1, correct?", False),
    ("Did any pin move?", True),
    ("Is B still on grid?", True),
    ("Does B introduce any DRC violations that A did not have?", True),
    ("Can this be an ECO?", True),
    ("Which one should I use?", True),
    ("What is the gate pitch?", False),
    ("How many polygons are there?", False),
    ("Is this layout DRC clean?", False),
])
def test_is_pair_question(question, expected):
    assert is_pair_question(question, A.name, B.name) is expected, question


def test_naming_a_file_makes_it_a_pair_question():
    assert is_pair_question(f"How many vias in {B.name}?", A.name, B.name)
    assert not is_pair_question("How many vias are there?", A.name, B.name)


# --- one number, one value, wherever it is shown ----------------------------

def test_the_pair_answer_quotes_the_page_via_count(page, lm):
    """Vias and contacts are separate counts on every table, so the answer keeps them
    separate. Folding them together says 14 where the page says 10 and 4."""
    design = analyze_gds(A, layermap=lm)["design"]
    answer = ask(page, "Did the via count change?")
    assert f"unchanged at {design['via_count']}" in answer
    assert str(design["via_count"] + design["contact_count"]) not in answer


def test_the_chat_and_the_parasitics_tool_measure_the_same_wire(page, lm):
    """Both must use the stack's role overrides. Without them NDIFFCON and PDIFFCON
    are read as contacts rather than local interconnect, and the chat quotes a wire
    length 0.14 µm shorter than the tool on the same page."""
    stack = default_stack(lm)
    tool = wire_geometry(A, lm, role_overrides=stack.get("role_overrides"))
    quoted = page["pair"]["a"]["parasitics"]["totals"]["wire_length_um"]
    assert quoted == tool["totals"]["wire_length_um"]
    assert f"{tool['totals']['wire_length_um']:g} µm" in ask(
        page, "Which layout has more capacitance?")
