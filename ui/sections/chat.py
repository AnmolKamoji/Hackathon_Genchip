"""Ask the Layout - the chat, restored.

The answer ladder is deterministic first and the model only as a fallback, in this
order:

1. the **pair** context, when two layouts are open. Tried first and for every
   question, not only ones that look comparative: "did any pin move" names no
   comparison word and is unanswerable from one file;
2. the **XOR**, for a comparison question;
3. the **metadata comparison**, for a comparison question;
4. the **single-file** deterministic answer;
5. the **model**, given the context the question actually needs.

Step 5 matters most when nothing deterministic matched. Handing the model one
file's metadata is how "what changed?" gets answered from a single layout - so
when a pair is open, both sides go, and the XOR with them.

Nothing here measures anything. Every figure the chat quotes was computed by the
analyzer and arrived inside a FileAnalysisDocument.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from ai.compare import answer_pair, is_pair_question
from ai.deterministic import answer as deterministic_answer
from ai.deterministic import (answer_comparison, answer_xor,
                              is_comparison_question, last_resort)
from ai.llm import DISABLED_MESSAGE, FAILURE_PREFIX, ask_llm, provider_status
from analyzer.comparison import compare_metadata
from ui.theme import section

SUGGESTED = [
    "Give me a summary of this GDS.",
    "How many polygons are there?",
    "Which layers are used?",
    "How many vias are present?",
    "What is the largest cell?",
    "Which layer has the highest density?",
    "What changed between the two layouts?",
]


def enriched_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """The metadata the deterministic answerer expects, assembled from the document.

    The analyses are already in the document, so this attaches rather than
    recomputes: the chat cannot disagree with the tables above it, because it is
    reading the same numbers they are.
    """
    meta = dict(doc.get("metadata") or {})
    # The document's layout block carries figures the parser's does not - the aspect
    # ratio among them. Attaching it keeps the chat quoting a measured value rather
    # than dividing two of them itself, which is the line this whole design holds.
    if doc.get("layout"):
        meta["layout"] = {**(meta.get("layout") or {}), **doc["layout"]}
    meta["connectivity"] = doc.get("connectivity")
    meta["hierarchy"] = doc.get("hierarchy")
    meta["measurements"] = doc.get("measurements")
    meta["drc"] = doc.get("rules")
    classification = doc.get("classification")
    if classification:
        meta["classification"] = classification
        meta["pitch"] = classification.get("pitch")
        if classification.get("tech_parameters"):
            meta["tech_parameters"] = classification["tech_parameters"]
    return meta


def pair_context(doc_a: dict[str, Any], doc_b: dict[str, Any],
                 xor_detail: dict[str, Any] | None,
                 extras: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything the comparison chat can answer from, for one pair.

    Assembled from the analyses the page has already run - nothing here reads a
    file that has not been read. It exists so a question about the *pair* is never
    answered from one file's metadata, which is the one mistake a comparison chat
    can make that still looks like an answer.
    """
    extras = extras or {}

    def side(doc: dict[str, Any]) -> dict[str, Any]:
        name = doc["file"]["name"]
        extra = extras.get(name) or {}
        metadata = enriched_metadata(doc)
        # The flattened geometry carries the labels, and a pin question is answered
        # from label positions - without this "did any pin move?" reports that no
        # labels were read, which is a different claim from "no pin moved".
        if extra.get("outlines"):
            metadata["outlines"] = extra["outlines"]
        return {
            "file": name,
            "metadata": metadata,
            "drc": doc.get("rules"),
            "connectivity": doc.get("connectivity"),
            "netlist": extra.get("netlist"),
            "grid": extra.get("grid"),
            "parasitics": extra.get("parasitics"),
            # Ohms and farads need process constants a layout cannot supply. They
            # arrive only if the Parasitics tool was given a process file; without
            # one this stays None and the RC answers report geometry instead of
            # inventing a sheet resistance.
            "rc": extra.get("rc"),
        }

    return {"xor": xor_detail or {}, "a": side(doc_a), "b": side(doc_b)}


def _names_a_layer(metadata: dict[str, Any], question: str) -> bool:
    """Does the question name one of this layout's layers, however it was spelled?"""
    from ai.deterministic import _mentioned_layer
    return bool(_mentioned_layer(question, metadata.get("layers") or []))


def answer_for(question: str, metadata: dict[str, Any],
               history: list | None = None,
               pair: dict[str, Any] | None = None,
               xor: dict[str, Any] | None = None,
               comparison: dict[str, Any] | None = None) -> str:
    """Answer one question, deterministic first and the model only as a fallback.

    The order is by *intent*, not by what happens to be available. A comparison
    question goes to the pair; a question about one layout goes to that layout's
    measurements even when two files are open. Getting this backwards is the failure
    that looks most like success: "what is the gate pitch?" answered with "unchanged
    at 45 nm" states the right number and does not answer the question, and "is this
    layout DRC clean?" answered with both files' rule counts drops the refusal
    entirely.
    """
    about_the_pair = bool(pair) and (
        is_comparison_question(question)
        or is_pair_question(question, (pair["a"] or {}).get("file"),
                            (pair["b"] or {}).get("file")))

    if about_the_pair:
        reply = answer_pair(pair, question)
        if reply:
            return reply
    if xor is not None and is_comparison_question(question):
        reply = answer_xor(xor, question)
        if reply:
            return reply
    if comparison is not None and is_comparison_question(question):
        reply = answer_comparison(comparison, question)
        if reply:
            return reply
    reply = deterministic_answer(metadata, question)
    if reply:
        return reply
    # Nothing about one layout claimed it either, so let the pair try before the
    # model does: "any IR drop concern?" names neither file and is still answerable
    # from what was measured in both.
    #
    # Held back when the question names a layer: the pair has a branch for "where
    # are the differences?", and "where is the widest metal?" reaching it answers a
    # question about one layer with a list of XOR regions.
    if pair and not about_the_pair and not _names_a_layer(metadata, question):
        reply = answer_pair(pair, question, about_the_pair=False)
        if reply:
            return reply

    if pair:
        context = {
            "comparing": {"a": pair["a"].get("file"), "b": pair["b"].get("file")},
            "difference": (pair.get("xor") or {}).get("summary"),
            "layers_that_differ": [
                {"layer": row["name"], "regions": row["xor"]["count"],
                 "xor_area_um2": row["xor"]["area_um2"]}
                for row in (pair.get("xor") or {}).get("layers", [])
                if not row.get("identical")],
            "a": pair["a"].get("metadata"),
            "b": pair["b"].get("metadata"),
        }
    elif comparison and is_comparison_question(question):
        context = {"comparison": comparison}
    else:
        context = metadata

    reply = ask_llm(context, question, history=history or [])
    # The model is the right answerer for a question no branch claimed - it rephrases
    # measurements it was handed. When it is not there, saying so is a statement about
    # our configuration, not about the layout. Answer from the measurements instead.
    if not reply or reply.startswith(FAILURE_PREFIX) or reply == DISABLED_MESSAGE:
        return last_resort(metadata, question)
    return reply


def render(documents: list[dict[str, Any]],
           pair: dict[str, Any] | None = None,
           xor: dict[str, Any] | None = None) -> None:
    """The chat, at the bottom of the page."""
    st.markdown(section("💬 Ask the Layout"), unsafe_allow_html=True)

    status = provider_status()
    if status["ready"]:
        st.caption(f"AI enabled · {status['primary']}. Numbers never come from the "
                   "model: it only rephrases what the analyzer measured.")
    else:
        st.caption(f"AI narrative unavailable: {status['detail']}. Deterministic "
                   "answers still work — every number is computed locally.")

    metadata = enriched_metadata(documents[0])
    comparison = None
    if pair and len(documents) >= 2:
        comparison = compare_metadata(pair["a"]["metadata"], pair["b"]["metadata"])
        st.caption(f"Questions answer against **{pair['a']['file']}**. Comparison "
                   f"questions use both files.")

    if "chat" not in st.session_state:
        st.session_state.chat = []

    for message in st.session_state.chat:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    question = st.chat_input("Ask a question about the uploaded GDS")
    if question:
        st.session_state.chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Answering..."):
                reply = answer_for(question, metadata,
                                   history=st.session_state.chat[:-1][-6:],
                                   pair=pair, xor=xor, comparison=comparison)
            st.markdown(reply)
        st.session_state.chat.append({"role": "assistant", "content": reply})

    if st.session_state.chat and st.button("Clear conversation", key="chat_clear"):
        st.session_state.chat = []
        st.rerun()

    st.caption("Suggested questions: " + " · ".join(SUGGESTED))
