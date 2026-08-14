"""Section 6 - Integration Impact, and the optional AI summary.

Reads `comparison_document.risk_flags` and renders them. Each flag is what a
measured geometric change means for the downstream flow - **none of them is a DRC
or an LVS result**.

The AI control sits at the very bottom and is additive: the whole deterministic
comparison is already on screen before it is offered. The model receives the
finished document and writes prose from it. It calculates nothing and cannot add a
finding, because there is nothing in its input that was not computed first.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from ui.theme import hint, section

SEVERITY_RENDER = {
    "high": ("error", "HIGH"),
    "medium": ("warning", "MEDIUM"),
    "info": ("info", "INFO"),
}


def render(doc: dict[str, Any], ai_enabled: bool = False) -> None:
    """The risk flags and the verdict.

    Currently not called: the section is hidden. The flags are still computed and
    still travel in the comparison document, so showing it again is a one-line
    change at the call site.
    """
    st.markdown(section("Integration Impact"), unsafe_allow_html=True)

    flags = doc.get("risk_flags") or []
    for flag in flags:
        kind, label = SEVERITY_RENDER.get(flag["severity"], ("info", "INFO"))
        body = f"**{label} · {flag['area']}** — {flag['impact']}\n\n`{flag['detail']}`"
        getattr(st, kind)(body)

    verdict = doc.get("verdict") or {}
    if not flags:
        # Never simply "no risks": four things are checked and any of them may have
        # been unavailable rather than passed.
        if verdict.get("clean"):
            st.success(verdict["message"])
        else:
            st.warning(verdict["message"])

    st.markdown(hint(
        "None of these is a DRC or an LVS result. Each is the consequence of a "
        "measured geometric change for the downstream flow."), unsafe_allow_html=True)


def render_ai_summary(doc: dict[str, Any], ai_enabled: bool = False) -> None:
    """Offered last, and only ever fed the finished document."""
    st.markdown(section("AI comparison summary — optional"), unsafe_allow_html=True)
    if not ai_enabled:
        st.caption("Set ANTHROPIC_API_KEY in `.env` to enable this. Every figure "
                   "above is already computed and displayed without it.")
        return

    st.caption("The model receives the deterministic comparison document and writes "
               "prose from it. It calculates nothing, cannot add a finding, and "
               "cannot reinterpret Unavailable as zero.")
    if st.button("Generate AI comparison summary", key="ai_summary"):
        from ai.llm import generate_comparison
        with st.spinner("Summarising the comparison document..."):
            st.session_state["ai_summary_text"] = generate_comparison(_payload(doc))
    if st.session_state.get("ai_summary_text"):
        st.markdown(st.session_state["ai_summary_text"])


def _payload(doc: dict[str, Any]) -> dict[str, Any]:
    """Exactly the deterministic findings, and nothing the model could measure from.

    The geometry never goes: handing over polygons would let the model count, and
    a counted number is one this application did not compute.
    """
    return {
        "reference": doc["reference"], "revision": doc["revision"],
        "change_type": doc["change_type"],
        "drop_in_replacement": doc["drop_in_replacement"],
        "device_topology": doc["device_topology"],
        "observations": doc["observations"],
        "notes": doc["notes"],
        "risk_flags": doc["risk_flags"],
        "verdict": doc["verdict"],
        "layer_tally": doc["layers"]["tally"],
    }
