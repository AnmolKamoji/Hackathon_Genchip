"""A layer typed six ways is one layer, and no question goes unanswered.

Nobody types a mask name the way the `.lyp` spells it. `P-VIAT`, `p-viat`, `p viat`,
`P VIAT`, `pviat` and `p_viat` are one layer, and the answer must not depend on which
one a reviewer reached for. It used to: the exact spelling reached the layer, `pviat`
reached the via-parameter table, and `p viat` reached neither and fell through to a
branch that answered a different question.

The second half is what happens when nothing matches. "AI narrative is disabled" is a
statement about our configuration, not about the layout, and it is what a reviewer
saw for any question outside the bank. Answering from the measurements that *do* bear
on the question is the job, even when the answer is "not this, but here is what is
measured".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import ai.llm
from ai.deterministic import _key, _mentioned_layer, answer as deterministic_answer
from ai.deterministic import last_resort
from analyzer.connectivity import default_stack
from analyzer.document import build_document
from analyzer.layermap import default_layermap, load_lyp
from analyzer.techparams import parameter, tech_parameters
from ui.sections import chat

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "DCAP0_1_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def meta(lm):
    doc = build_document(GDS, lm, stack=default_stack(lm), all_filenames=[GDS.name])
    return chat.enriched_metadata(doc)


# --- the matcher ------------------------------------------------------------

def test_the_key_drops_case_and_every_separator():
    assert _key("P-VIAT") == _key("p viat") == _key("pviat") == _key("p_viat") == "pviat"
    assert _key("NPOLY-EXTENDED") == _key("npoly extended") == "npolyextended"


SPELLINGS = {
    "P-VIAT": ["P-VIAT", "p-viat", "P-viat", "p viat", "P VIAT", "P Viat", "pviat",
               "PVIAT", "p_viat", "  p-VIAT  "],
    "NPOLY": ["NPOLY", "npoly", "n poly", "N-POLY", "n-poly", "N_POLY", "NPoly"],
    "M0": ["M0", "m0", "m 0", "m-0"],
    "NPOLY-EXTENDED": ["NPOLY-EXTENDED", "npoly extended", "n poly extended",
                       "npolyextended", "NPOLY_EXTENDED"],
    "CELL-BOUNDARY": ["CELL-BOUNDARY", "cell boundary", "cellboundary"],
    "NDIFFCON": ["NDIFFCON", "ndiffcon", "n diffcon", "N-DIFFCON"],
}


@pytest.mark.parametrize("wanted,spelling", [(k, s) for k, v in SPELLINGS.items()
                                             for s in v])
def test_every_spelling_finds_the_same_layer(meta, wanted, spelling):
    assert _mentioned_layer(f"What is the width of {spelling}?",
                            meta["layers"]) == wanted


def test_the_longest_name_wins_over_a_prefix_of_it(meta):
    """"npoly extended" contains `NPOLY` on a word boundary, and names NPOLY-EXTENDED."""
    assert _mentioned_layer("how many npoly extended shapes",
                            meta["layers"]) == "NPOLY-EXTENDED"
    assert _mentioned_layer("how many npoly shapes", meta["layers"]) == "NPOLY"


def test_words_either_side_of_a_comma_are_not_joined(meta):
    """Joining across a comma would invent a layer nobody named."""
    assert _mentioned_layer("compare m0, and via0 please",
                            meta["layers"]) in ("M0", "VIA0")


def test_an_english_sentence_names_no_layer(meta):
    for question in ("What changed between these two layouts?",
                     "Is this safe to tape out?",
                     "How many polygons are there?"):
        assert _mentioned_layer(question, meta["layers"]) is None, question


# --- the answers do not depend on the spelling ------------------------------

@pytest.mark.parametrize("template", [
    "What is the width of {}?",
    "How many {} shapes are there?",
    "What is the area of {}?",
    "What is the spacing on {}?",
])
def test_one_layer_six_spellings_one_answer(meta, template):
    spellings = SPELLINGS["P-VIAT"][:9]
    answers = {deterministic_answer(meta, template.format(s)) for s in spellings}
    assert len(answers) == 1, answers
    only = answers.pop()
    assert only and "P-VIAT" in only


@pytest.mark.parametrize("spelling", ["N-poly width", "n poly width", "N POLY WIDTH",
                                      "npoly width", "n-poly width", "Npoly width"])
def test_a_tech_parameter_survives_every_spelling(meta, spelling):
    """`N-poly width` is a measured parameter and answers in nanometres. The layer
    branch would answer the same dimension in micrometres, which is the same fact
    and a different answer - so the spelling must not decide which one replies."""
    reply = deterministic_answer(meta, f"What is the {spelling}?")
    assert reply and "15" in reply


def test_the_parameter_lookup_prefers_the_longest_agreement(lm):
    """"n diffcon width" contains the parameter `diffcon`. Answering with via geometry
    because it matched first is how a width question stopped being a width question."""
    params = tech_parameters(GDS, lm)
    for needle in ("n diffcon width", "n-diffcon width", "ndiffconwidth"):
        assert parameter(params, needle)["parameter"] == "N-diffcon width", needle
    # And a question that really is about the via parameter still reaches it.
    assert parameter(params, "diffcon")["parameter"] == "diffcon"


def test_a_layer_question_is_not_answered_by_the_parameter_table(meta):
    """P-VIAT has no width parameter, so "the width of P-VIAT" is the layer's measured
    width. The table used to reply "that parameter is not one of the 30 measured
    here", which reads as if the layer were unknown."""
    reply = deterministic_answer(meta, "What is the width of P-VIAT?")
    assert "not one of the" not in reply
    assert "0.012" in reply


# --- nothing goes blank -----------------------------------------------------

OUT_OF_BANK = [
    "What is the aspect ratio of this cell?",
    "What is the smallest gap anywhere in the layout?",
    "What is the total edge length I would have to OPC?",
    "Is this cell double-height?",
    "Why is BM0 so much denser than M0?",
    "How much headroom is left on M1?",
    "Could I shrink this cell by one CPP?",
    "What would a router do with this cell?",
    "Give me a bill of materials for the masks.",
    "Is anything drawn outside the cell boundary?",
]


@pytest.mark.parametrize("question", OUT_OF_BANK)
def test_a_question_outside_the_bank_is_still_answered(meta, monkeypatch, question):
    """With no model configured, the reply must still be about the layout."""
    monkeypatch.setattr(chat, "ask_llm",
                        lambda *a, **k: ai.llm.DISABLED_MESSAGE)
    reply = chat.answer_for(question, meta, history=[])
    assert reply and reply.strip()
    assert ai.llm.DISABLED_MESSAGE not in reply
    assert not reply.startswith(ai.llm.FAILURE_PREFIX)
    # It has to carry a measurement or name what is missing - not just an apology.
    assert re.search(r"\d", reply) or "not implemented" in reply or "needs" in reply


def test_the_last_resort_invents_no_number(meta):
    """Every figure it quotes has to be in the metadata it was given."""
    from tools.factcheck import audit
    for question in OUT_OF_BANK:
        reply = last_resort(meta, question)
        _, ungrounded = audit(question, reply, meta)
        assert not ungrounded, (question, ungrounded)


def test_the_last_resort_says_where_to_go_next(meta):
    reply = last_resort(meta, "What would a router do with this cell?")
    assert "More tools" in reply


def test_a_failed_model_call_does_not_surface_as_the_answer(meta, monkeypatch):
    monkeypatch.setattr(chat, "ask_llm",
                        lambda *a, **k: ai.llm.FAILURE_PREFIX + "\n\n- anthropic: 401")
    reply = chat.answer_for("What is the aspect ratio of this cell?", meta, history=[])
    assert "401" not in reply
    assert "0.525" in reply
