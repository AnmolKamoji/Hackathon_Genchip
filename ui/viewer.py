"""Mount the interactive viewer inside Streamlit.

The viewer is a self-contained HTML document in an iframe. That is the point: a
layer toggle, a zoom or a ruler must not re-run the Python script. In the previous
Plotly version every checkbox click re-ran the page and rebuilt the figure, which
is what made the view jump back and the toolbar disappear mid-interaction.

Controls that genuinely need Python - choosing which files to compare, opening the
expanded workspace, talking to the chatbot - stay as Streamlit widgets outside the
frame, so nothing depends on iframe-to-parent messaging.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

_HERE = Path(__file__).resolve().parent
_JS = _HERE / "viewer.js"
_CSS = _HERE / "viewer.css"


def assets() -> tuple[str, str]:
    """The viewer's JS and CSS. Read at call time so an edit shows on rerun."""
    return _JS.read_text(encoding="utf-8"), _CSS.read_text(encoding="utf-8")


def document(payload: dict[str, Any], element_id: str = "gv") -> str:
    """The full HTML document for one viewer instance."""
    js, css = assets()
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    # The payload goes in a JSON script block rather than inline JavaScript, and
    # `<` is escaped to its JSON unicode form. Without that, a cell or layer named
    # `</script>` would close the block early and break the page - JSON.parse turns
    # \\u003c back into `<`, so the data itself is unchanged.
    safe = data.replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{css}</style></head>
<body>
<div id="{html.escape(element_id)}"></div>
<script type="application/json" id="{html.escape(element_id)}-data">{safe}</script>
<script>{js}</script>
<script>
(function () {{
  var el = document.getElementById({json.dumps(element_id + "-data")});
  var payload = JSON.parse(el.textContent);
  window.GDSViewer.mount({json.dumps(element_id)}, payload);
}})();
</script>
</body></html>"""


def render(payload: dict[str, Any], height: int = 620, key: str = "gv") -> None:
    """Draw the viewer at the given height.

    `st.iframe` is the current name; `components.v1.html` is deprecated and due for
    removal, but is kept as the fallback so the app still runs on the older
    Streamlit a user may already have installed.
    """
    doc = document(payload, element_id=key)
    embed = getattr(st, "iframe", None)
    if callable(embed):
        # st.iframe takes the HTML as `src` and has no `scrolling` argument - the
        # signature differs from components.v1.html, so this is not a rename.
        embed(doc, height=height)
    else:                                    # Streamlit < 1.49
        components.html(doc, height=height, scrolling=False)
