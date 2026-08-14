"""Section 5 - What Changed.

Entirely deterministic. It prints `comparison_document.observations` in the order
they were built and computes nothing.

Comparisons that could not be made are still built - `comparison_document.notes`
carries them, prefixed "not compared" - but they are no longer displayed here. They
were never mixed into the change list and must not be if they are shown again: an
unavailable comparison is not a change, and mixing them meant an identical pair of
files never produced an empty change list, which is the most useful answer this
section can give.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from ui.theme import hint, section


def render(doc: dict[str, Any]) -> None:
    st.markdown(section("What Changed"), unsafe_allow_html=True)

    observations = doc.get("observations") or []
    if not observations:
        st.success("No measured change between these two revisions.")
    else:
        for line in observations:
            st.markdown(f"- {line}")

    with st.expander("What a comparison cannot tell you"):
        st.markdown(
            "- **Intent.** Why a change was made is in neither file.\n"
            "- **Functional equivalence.** Whether the revision behaves like the "
            "reference needs an extracted netlist and an LVS run, neither of which "
            "a `.gds` and a `.lyp` provide.")
        st.markdown(hint("Every line above is a measured fact. No line says "
                         "probably, likely, appears to be, or what the designer "
                         "intended."), unsafe_allow_html=True)
