"""Mount the viewer inside Streamlit, read-only or as a two-way editor.

The viewer is a self-contained document in an iframe. That is the point: a layer
toggle, a zoom or a ruler must not re-run the Python script. In the previous Plotly
version every checkbox click re-ran the page and rebuilt the figure, which is what
made the view jump back and the toolbar disappear mid-interaction.

Editing changes that in exactly one direction. A *view* decision still stays in the
iframe; an *edit* has to reach Python, because the file is written there and nowhere
else. So there are two mounts:

* `render()` - one-way. The document is embedded and nothing comes back.
* `interactive()` - a declared Streamlit component, which is the only mount that can
  post a value to the script. It returns the editor's committed journal.

The component is served from a generated directory containing one file. Declaring it
against `ui/` itself would serve this module and its neighbours over HTTP, which is a
strange thing to do to read a layout.
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_JS = _HERE / "viewer.js"
_EDITOR_JS = _HERE / "editor.js"
_CSS = _HERE / "viewer.css"
_BUILD = _ROOT / "build" / "viewer_component"


def assets() -> tuple[str, str]:
    """The viewer's JS and CSS. Read at call time so an edit shows on rerun."""
    js = _JS.read_text(encoding="utf-8")
    if _EDITOR_JS.exists():
        js += "\n" + _EDITOR_JS.read_text(encoding="utf-8")
    return js, _CSS.read_text(encoding="utf-8")


def document(payload: dict[str, Any], element_id: str = "gv",
             dual: bool = False) -> str:
    """The full HTML document for one viewer instance, payload included.

    `dual` mounts the side-by-side comparison: two drawings and one shared layer
    panel in a single document. They share a frame because sharing the panel across
    two iframes would mean a round trip through Python for every checkbox - and a
    rerun would throw away the zoom in both.
    """
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
  window.GDSViewer.{"mountDual" if dual else "mount"}({json.dumps(element_id)}, payload);
}})();
</script>
</body></html>"""


# The component protocol, implemented by hand rather than pulled in from npm. It is
# four messages: ready, height, value out, render in. Streamlit's frontend keys
# incoming messages by the iframe's window and requires `isStreamlitMessage`.
_BRIDGE = """
(function () {
  var view = null;
  function post(type, extra) {
    var msg = {isStreamlitMessage: true, type: type};
    for (var k in (extra || {})) msg[k] = extra[k];
    window.parent.postMessage(msg, "*");
  }
  function send(value) {
    post("streamlit:setComponentValue", {value: JSON.stringify(value), dataType: "json"});
  }
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== "streamlit:render") return;
    var args = data.args || {};
    var payload = args.payload;
    if (typeof payload === "string") payload = JSON.parse(payload);
    if (!payload) return;
    if (!view) {
      var mount = args.dual ? "mountDual" : "mount";
      view = window.GDSViewer[mount]("gv", payload, {onEvent: send});
    } else if (args.revision !== view.revision && view.setPayload) {
      // A rerun after an edit was applied: the geometry is now what Python wrote,
      // so the drawing is replaced while the view - zoom, layers, tab - is kept.
      // Only the single viewer is editable, so only it has this.
      view.setPayload(payload, args.revision);
    }
    view.revision = args.revision;
    post("streamlit:setFrameHeight", {height: args.height || 640});
  });
  post("streamlit:componentReady", {apiVersion: 1});
})();
"""


def _component_html() -> str:
    js, css = assets()
    return (f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
            f"<style>{css}</style></head>\n<body>\n<div id=\"gv\"></div>\n"
            f"<script>{js}</script>\n<script>{_BRIDGE}</script>\n</body></html>")


def _component_dir() -> Path:
    """Write the component's single file, and only when it has changed.

    Rewriting it on every rerun would make the browser refetch the document and lose
    the view; comparing first means an unchanged viewer is served from cache.
    """
    doc = _component_html()
    _BUILD.mkdir(parents=True, exist_ok=True)
    target = _BUILD / "index.html"
    if not target.exists() or target.read_text(encoding="utf-8") != doc:
        target.write_text(doc, encoding="utf-8")
    return _BUILD


@st.cache_resource(show_spinner=False)
def _declare(signature: str):
    """Declare the component once per process. The signature is only there to
    invalidate the cache when the viewer's own code changes during development."""
    return components.declare_component("gds_viewer", path=str(_component_dir()))


def _signature() -> str:
    js, css = assets()
    return hashlib.sha256((js + css).encode("utf-8")).hexdigest()[:16]


def render(payload: dict[str, Any], height: int = 620, key: str = "gv",
           dual: bool = False) -> None:
    """Draw the viewer at the given height. Nothing comes back from it.

    `st.iframe` is the current name; `components.v1.html` is deprecated and due for
    removal, but is kept as the fallback so the app still runs on the older
    Streamlit a user may already have installed.
    """
    doc = document(payload, element_id=key, dual=dual)
    embed = getattr(st, "iframe", None)
    if callable(embed):
        # st.iframe takes the HTML as `src` and has no `scrolling` argument - the
        # signature differs from components.v1.html, so this is not a rename.
        embed(doc, height=height)
    else:                                    # Streamlit < 1.49
        components.html(doc, height=height, scrolling=False)


def interactive(payload: dict[str, Any], height: int = 620, key: str = "gv",
                revision: int = 0, dual: bool = False) -> dict[str, Any] | None:
    """Mount the viewer as a component and return whatever it last sent.

    The return value is the editor's doing: a committed journal of operations, or a
    request the page has to serve (save the file, run the checks). View state never
    comes back through here, because a rerun for a zoom is exactly what this whole
    design avoids.
    """
    component = _declare(_signature())
    raw = component(payload=json.dumps(payload, separators=(",", ":"), allow_nan=False),
                    revision=revision, height=height, key=key, dual=dual,
                    default=None)
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw
