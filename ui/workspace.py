"""Viewer panels and the expanded workspace.

Kept out of app.py because the expanded view and the inline view must render the
same viewer from the same payload - two copies of that wiring would drift, and the
difference would only show up as "the big view behaves differently", which is
exactly the complaint a reviewer cannot debug.

The division of labour with the browser component is deliberate: everything that
is a *view* decision (zoom, layers, ruler, which compare mode) lives in the iframe
and never re-runs Python; everything that needs Python (which files to compare,
opening the workspace, the chatbot) is a Streamlit widget outside it.
"""
from __future__ import annotations

from typing import Any, Callable

import streamlit as st

from ui.viewer import render as render_viewer
from ui.viewer_data import build, build_comparison

FOCUS_KEY = "gv_focus"


def request_focus(kind: str, **payload: Any) -> None:
    st.session_state[FOCUS_KEY] = {"kind": kind, **payload}


def clear_focus() -> None:
    st.session_state.pop(FOCUS_KEY, None)


def focus_request() -> dict[str, Any] | None:
    return st.session_state.get(FOCUS_KEY)


def layout_panel(outlines: dict[str, Any], key: str, colours: dict[str, str],
                 title: str = "", height: int = 640, expandable: bool = True) -> None:
    """One layout in the interactive viewer."""
    payload = build(outlines, fallback_colours=colours, title=title)
    if not payload["layers"]:
        st.info("This layout contains no drawable geometry.")
        return
    for warning in payload["warnings"]:
        st.warning(warning)

    if expandable:
        head = st.columns([5, 1.15])
        head[0].markdown(
            f'**{payload["topCell"] or payload["title"]}** — '
            f'{payload["width"] * 1000:g} × {payload["height"] * 1000:g} nm')
        if head[1].button("⤢ Expand", key=f"{key}_expand", width="stretch",
                          help="Full-screen workspace with the chatbot beside the layout"):
            request_focus("layout", key=key, title=title)
            st.rerun()
    render_viewer(payload, height=height, key=key)


def compare_panel(xor: dict[str, Any], outlines_a: dict[str, Any],
                  outlines_b: dict[str, Any], colours: dict[str, str],
                  name_a: str, name_b: str, key: str = "cmp",
                  height: int = 660, expandable: bool = True) -> None:
    """Two layouts in one viewer, with the difference regions on top."""
    payload = build_comparison(
        xor,
        build(outlines_a, fallback_colours=colours, title=name_a),
        build(outlines_b, fallback_colours=colours, title=name_b),
    )
    payload["names"] = {"a": name_a, "b": name_b}
    payload["a"]["file"] = name_a
    payload["b"]["file"] = name_b

    if expandable:
        head = st.columns([5, 1.15])
        head[0].markdown(f"**{name_a}** → **{name_b}** — "
                         f"{payload['summary']['regionCount']} differing region(s)")
        if head[1].button("⤢ Expand", key=f"{key}_expand", width="stretch",
                          help="Full-screen comparison with the chatbot beside it"):
            request_focus("compare", key=key, a=name_a, b=name_b)
            st.rerun()
    render_viewer(payload, height=height, key=key)


def pick_pair(names: list[str], key: str = "pair") -> tuple[str, str]:
    """Which two of the uploaded files to compare.

    With more than two files the old page always compared the first two, which is
    only right by accident. The second box excludes the first, so the pair can
    never be a file against itself.
    """
    left, arrow, right = st.columns([3, 0.5, 3])
    a = left.selectbox("Compare", names, index=0, key=f"{key}_a")
    arrow.markdown("<div style='text-align:center;padding-top:32px'>→</div>",
                   unsafe_allow_html=True)
    others = [n for n in names if n != a] or names
    default = 0
    stored = st.session_state.get(f"{key}_b")
    if stored in others:
        default = others.index(stored)
    b = right.selectbox("against", others, index=default, key=f"{key}_b")
    return a, b


def workspace(request: dict[str, Any], render_view: Callable[[], None],
              answer: Callable[[str], str], height: int = 720) -> None:
    """The expanded view: the drawing large and centred, the chatbot beside it.

    The layer panel is not a third column here - it lives inside the viewer, which
    keeps the toggles next to the drawing they affect and means switching a layer
    still costs no page reload.
    """
    bar = st.columns([1, 5])
    if bar[0].button("← Back", key="ws_back", width="stretch"):
        clear_focus()
        st.rerun()
    bar[1].markdown(
        f"### {request.get('title') or request.get('a', '')}"
        + (f" → {request['b']}" if request.get("b") else ""))

    view, chat = st.columns([3.15, 1.15], gap="medium")
    with view:
        render_view()
    with chat:
        st.markdown("#### Ask about this layout")
        history_key = "ws_chat"
        st.session_state.setdefault(history_key, [])
        box = st.container(height=height - 190)
        with box:
            for msg in st.session_state[history_key]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
        question = st.chat_input("Ask about what you are looking at",
                                 key="ws_input")
        if question:
            st.session_state[history_key].append({"role": "user", "content": question})
            with st.spinner("Analyzing..."):
                reply = answer(question)
            st.session_state[history_key].append({"role": "assistant", "content": reply})
            st.rerun()
        if st.session_state[history_key] and st.button("Clear", key="ws_clear"):
            st.session_state[history_key] = []
            st.rerun()
