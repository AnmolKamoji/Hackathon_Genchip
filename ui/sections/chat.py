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

from ai.compare import answer_pair
from ai.deterministic import answer as deterministic_answer
from ai.deterministic import answer_comparison, answer_xor, is_comparison_question
from ai.llm import ask_llm, provider_status
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
            # Ohms and farads need process constants a layout cannot supply, and
            # nothing on this page asks for them, so this stays unavailable.
            "rc": None,
        }

    return {"xor": xor_detail or {}, "a": side(doc_a), "b": side(doc_b)}


def answer_for(question: str, metadata: dict[str, Any],
               history: list | None = None,
               pair: dict[str, Any] | None = None,
               xor: dict[str, Any] | None = None,
               comparison: dict[str, Any] | None = None) -> str:
    """Answer one question, deterministic first and the model only as a fallback."""
    if pair:
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
    return ask_llm(context, question, history=history or [])


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
