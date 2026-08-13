"""Tests for the connectivity questions answered deterministically.

The point of these is the refusals as much as the answers: a question about a
short or an open has exactly one correct response when no netlist was supplied,
and it is not a number.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai.deterministic import answer
from analyzer import connectivity as C
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import load_lyp

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
DCAP = SAMPLES / "DCAP0_1_RT_4.gds"
NR2D1 = SAMPLES / "NR2D1_1_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(SAMPLES / "Titan_layer_properties.lyp")


@pytest.fixture(scope="module")
def stack(lm):
    return C.load_stack(SAMPLES / "Titan_stack.json", lm)


def _meta(gds, lm, **kwargs):
    m = analyze_gds(gds, layermap=lm)
    m["connectivity"] = C.analyze_connectivity(gds, lm, **kwargs)
    return m


@pytest.fixture(scope="module")
def meta_no_stack(lm):
    return _meta(NR2D1, lm)


@pytest.fixture(scope="module")
def meta_stacked(lm, stack):
    return _meta(NR2D1, lm, stack=stack)


# --- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "Are there any shorts in this design?",
    "Does this layout have a short circuit?",
    "Show me shorted nets",
])
def test_shorts_are_always_refused(meta_stacked, q):
    reply = answer(meta_stacked, q)
    assert "not determinable" in reply
    assert "intended netlist" in reply
    # Must not answer with a count of anything.
    assert not any(ch.isdigit() for ch in reply)


@pytest.mark.parametrize("q", [
    "Are there any opens?",
    "Is there an open circuit in this cell?",
])
def test_opens_are_refused_but_offer_the_measurable_proxy(meta_stacked, q):
    reply = answer(meta_stacked, q)
    assert "not determinable" in reply
    assert "overlaps no conductor" in reply


def test_rule_guard_still_wins_over_connectivity(meta_stacked):
    """A question naming DRC must reach the rule branch, not the connectivity one.

    With no `drc` block present the rule branch says the results are absent, which
    is the honest answer - the connectivity branch would have answered a different
    question entirely.
    """
    reply = answer(meta_stacked, "are there DRC violations in the connectivity?")
    assert "No design rule results are available" in reply


# --- net questions ----------------------------------------------------------

def test_net_count_without_a_stack_explains_what_is_missing(meta_no_stack):
    reply = answer(meta_no_stack, "How many nets does this design have?")
    assert "not built" in reply
    assert "no layer elevations" in reply or "records no layer elevations" in reply
    # It must still give what IS available rather than only refusing.
    assert "within-layer conductors" in reply


def test_net_count_with_a_stack_is_answered(meta_stacked):
    reply = answer(meta_stacked, "How many nets does this design have?")
    assert "7 physical net(s)" in reply
    assert "5 spanning more than one layer" in reply
    assert "not electrical intent" in reply


def test_net_answer_marks_an_inferred_stack_as_provisional(lm):
    meta = _meta(NR2D1, lm, accept_proposed_stack=True)
    reply = answer(meta, "how many nets are there?")
    assert "provisional" in reply


def test_net_answer_distinguishes_a_sidecar_derived_stack(lm):
    """A stack read off the sidecar's via names is not a geometric guess.

    Describing it as "inferred from measured overlap" understates it, which is
    exactly what a live model did when the metadata collapsed the two cases.
    """
    from analyzer.sidecar_parser import analyze_sidecar
    derived = C.stack_from_sidecar(analyze_sidecar(NR2D1.with_suffix(".json")), lm)
    meta = _meta(NR2D1, lm, stack=derived)
    reply = answer(meta, "how many nets are there?")
    assert "via layer names" in reply
    assert "naming convention" in reply
    assert "provisional" not in reply


def test_floating_question_is_routed_to_nets(meta_stacked):
    reply = answer(meta_stacked, "Are there any floating shapes?")
    assert "net(s) use no via or contact" in reply


# --- intra-layer questions --------------------------------------------------

def test_component_question_is_exact_and_says_so(meta_stacked):
    reply = answer(meta_stacked, "How many separate physical conductors are there?")
    assert "60 conducting shapes" in reply
    assert "54 separate physical conductors" in reply
    assert "needs no process-stack data" in reply


def test_abutting_layers_are_named(meta_stacked):
    reply = answer(meta_stacked, "Which layers have shapes that touch?")
    assert "NPOLY-PATTERN-CUT" in reply
    assert "4 shapes forming 1 conductor" in reply


# --- landing questions ------------------------------------------------------

def test_via_landing_answer_refuses_to_call_overlap_connection(meta_stacked):
    reply = answer(meta_stacked, "What do the vias land on?")
    assert "not the same as connection" in reply
    assert "no Z axis" in reply


def test_stack_question_without_a_stack_offers_the_proposal_only(meta_no_stack):
    reply = answer(meta_no_stack, "What is the connection stack?")
    assert "inferred" in reply and "review only" in reply
    assert "Confirm it" in reply or "Confirm" in reply


def test_stack_question_with_a_stack_reports_it(meta_stacked):
    reply = answer(meta_stacked, "What connection stack is being used?")
    assert "supplied explicitly" in reply
    assert "VIA0 joins M0 and M1" in reply


# --- graceful degradation ---------------------------------------------------

def test_connectivity_question_without_analysis_says_what_is_needed():
    meta = analyze_gds(NR2D1)
    reply = answer(meta, "How many nets does this design have?")
    assert "No connectivity analysis is available" in reply
    assert ".lyp" in reply


def test_non_connectivity_questions_are_unaffected(meta_stacked):
    """The trigger must not swallow ordinary questions."""
    assert "top cell" in answer(meta_stacked, "What is the top cell?").lower()
    reply = answer(meta_stacked, "How many polygons does this design have in total?")
    assert "60" in reply
