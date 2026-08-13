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

from ui.viewer import interactive as mount_viewer
from ui.viewer import render as render_viewer
from ui.viewer_data import build, build_comparison, with_analysis

FOCUS_KEY = "gv_focus"


def request_focus(kind: str, **payload: Any) -> None:
    st.session_state[FOCUS_KEY] = {"kind": kind, **payload}


def clear_focus() -> None:
    st.session_state.pop(FOCUS_KEY, None)


def focus_request() -> dict[str, Any] | None:
    return st.session_state.get(FOCUS_KEY)


def layout_panel(outlines: dict[str, Any], key: str, colours: dict[str, str],
                 title: str = "", height: int = 640, expandable: bool = True,
                 drc: dict[str, Any] | None = None,
                 connectivity: dict[str, Any] | None = None,
                 pitch: dict[str, Any] | None = None,
                 hierarchy: dict[str, Any] | None = None,
                 tree: dict[str, Any] | None = None,
                 editable: dict[str, Any] | None = None,
                 revision: int = 0,
                 interactive: bool = False) -> dict[str, Any] | None:
    """One layout in the interactive viewer.

    The analysis blocks are optional but change what the viewer is: with them, rule
    results become clickable markers, nets become traceable, cells become navigable
    and the routing grid can be drawn. Without them it is still a viewer, just not a
    review surface.
    """
    payload = with_analysis(build(outlines, fallback_colours=colours, title=title),
                            drc=drc, connectivity=connectivity, pitch=pitch,
                            hierarchy=hierarchy, tree=tree, editable=editable)
    if not payload["layers"]:
        st.info("This layout contains no drawable geometry.")
        return None
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
    if interactive:
        # Mounted as a component so the viewer's tool menu can ask the page for a
        # tool it cannot run itself. Everything else about it is unchanged: view
        # decisions still never reach Python.
        return mount_viewer(payload, height=height, key=key, revision=revision)
    render_viewer(payload, height=height, key=key)
    return None


def editor_panel(outlines: dict[str, Any], key: str, colours: dict[str, str],
                 editable: dict[str, Any], revision: int = 0, title: str = "",
                 height: int = 700, **analysis) -> dict[str, Any] | None:
    """The editing mount, which is the only one that can send anything back.

    Read-only views stay embedded documents: nothing they do needs Python, and a
    rerun for a zoom is exactly what this design avoids. Editing is the one thing
    that does need Python - the file is written there - so it gets the component.
    """
    payload = with_analysis(build(outlines, fallback_colours=colours, title=title),
                            editable=editable, **analysis)
    if not payload["layers"]:
        st.info("This layout contains no drawable geometry.")
        return None
    for warning in payload["warnings"]:
        st.warning(warning)
    return mount_viewer(payload, height=height, key=key, revision=revision)


def compare_panel(xor: dict[str, Any], outlines_a: dict[str, Any],
                  outlines_b: dict[str, Any], colours: dict[str, str],
                  name_a: str, name_b: str, key: str = "cmp",
                  height: int = 660, expandable: bool = True,
                  side_by_side: bool = True) -> None:
    """The two layouts, A on the left and B on the right.

    Side by side is the arrangement people actually compare in: two drawings at the
    same size, each with its own zoom, pan and layer set, so scrolling one does not
    move the other. They are separate mounts, which is what makes that independence
    real rather than something to maintain.

    The overlay - A+B, XOR, wipe and blink in one canvas - is the other half of the
    job and is kept, one expander below. Neither replaces the other: side by side
    answers "what do these two look like?", the overlay answers "where exactly do
    they differ?".

    Both are built from the outlines the page already read. Nothing is parsed twice.
    """
    a_payload = build(outlines_a, fallback_colours=colours, title=name_a)
    b_payload = build(outlines_b, fallback_colours=colours, title=name_b)
    payload = build_comparison(xor, a_payload, b_payload)
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

    if not side_by_side:
        render_viewer(payload, height=height, key=key)
        return

    # A on the left, B in the middle, one layer panel on the right - all in a single
    # frame. The panel is shared, so a checkbox hides that layer in both drawings at
    # once; zoom, pan and rulers stay per-drawing. Splitting them across two frames
    # would put a Python round trip behind every checkbox, and the rerun would throw
    # away both zooms.
    render_viewer(payload, height=height, key=f"{key}_pair", dual=True)

    with st.expander("Overlay the two — A+B, XOR, wipe and blink in one view"):
        render_viewer(payload, height=height, key=key)


def workspace(request: dict[str, Any], render_view: Callable[[], None],
              answer: Callable[[str], str], height: int = 720,
              edit_bar: Callable[[Any], None] | None = None) -> None:
    """The expanded view: the drawing large and centred, the chatbot beside it.

    The layer panel is not a third column here - it lives inside the viewer, which
    keeps the toggles next to the drawing they affect and means switching a layer
    still costs no page reload.
    """
    bar = st.columns([1, 3.4, 1.8] if edit_bar else [1, 5])
    if bar[0].button("← Back", key="ws_back", width="stretch"):
        clear_focus()
        st.rerun()
    bar[1].markdown(
        f"### {request.get('title') or request.get('a', '')}"
        + (f" → {request['b']}" if request.get("b") else ""))
    if edit_bar:
        edit_bar(bar[2])

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
