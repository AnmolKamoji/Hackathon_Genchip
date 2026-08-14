"""The landing page, the animated background and the loading state.

The background is not decoration borrowed from somewhere else - it is the subject.
It draws what this tool reads: a routing grid, metal tracks on their pitch, poly
running the other way, and vias landing where they cross. It loops forever and it
never uses a semantic colour, because in this app green, amber and red each mean
exactly one thing and a moving background that borrowed one would be saying it.

Everything here is CSS and SVG. No GIF: a raster loop cannot match a palette it was
exported before, it cannot scale to a 4K panel, and it costs a megabyte to say what
three keyframes say. This is a few kilobytes, sharp at any size, and drawn from the
same tokens as the rest of the interface.

Three states, and the transition between them is the point:

    before an upload   the hero, over the moving grid
    while reading      the grid fades out once, a scan line runs
    after that         no background at all - the data is the interface
"""
from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from ui.theme import ACCENT, BG, BORDER, MUTED, SURFACE, SURFACE_2, TEXT

# Decorative hues only, and deliberately not OK / WARN / DANGER: those three carry
# meaning everywhere else in the app, and a drifting background in the same green
# would read as a verdict. Blue is the accent already; the violet and teal sit
# beside it on the wheel, so the three agree without competing.
TRACK = ACCENT           # #58a6ff - metal, the same blue as the accent
POLY = "#a371f7"         # violet - the other direction
VIA = "#39c5cf"          # teal - the landings

BACKDROP = f"""
<style>
/* The background sits behind everything and takes no clicks. Streamlit paints its
   own surface on .stApp, so that goes transparent and the colour moves to html. */
html, body {{ background: {BG} !important; }}
.stApp {{ background: transparent !important; }}
.stApp > header {{ background: transparent !important; }}
.gv-backdrop {{
  position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
  /* A vignette, so the moving geometry never competes with text in the middle. */
  mask-image: radial-gradient(120% 90% at 50% 8%, #000 0%, #000 42%, transparent 88%);
  -webkit-mask-image: radial-gradient(120% 90% at 50% 8%, #000 0%, #000 42%, transparent 88%);
}}
.gv-backdrop svg {{ width: 100%; height: 100%; display: block; }}
/* Content above the backdrop. Streamlit's own container is the stacking context. */
.stApp > div[data-testid="stAppViewContainer"],
section.main, section.stMain,
div[data-testid="stMainBlockContainer"] {{ position: relative; z-index: 1; }}

/* Streamlit anchors this container to its bottom - the test id is literally
   stAppScrollToBottomContainer - because `st.chat_input` is on the page. That is
   what sent every upload to the end of the analysis. The anchor is released here
   and the position is reset explicitly when the uploaded set changes. */
[data-testid="stAppScrollToBottomContainer"] {{ overflow-anchor: none; }}

/* The loop. Three speeds so the eye never catches a period, and transforms only -
   animating a transform stays on the compositor and costs no layout. */
@keyframes gv-drift-x {{ from {{ transform: translateX(0); }}
                         to   {{ transform: translateX(84px); }} }}
@keyframes gv-drift-y {{ from {{ transform: translateY(0); }}
                         to   {{ transform: translateY(-63px); }} }}
@keyframes gv-sweep   {{ 0%   {{ transform: translateX(-32%); opacity: 0; }}
                         12%  {{ opacity: .5; }}
                         88%  {{ opacity: .5; }}
                         100% {{ transform: translateX(132%); opacity: 0; }} }}
@keyframes gv-pulse   {{ 0%, 100% {{ opacity: .18; }} 50% {{ opacity: .5; }} }}
.gv-grid   {{ animation: gv-drift-x 9s linear infinite; }}
.gv-poly   {{ animation: gv-drift-y 17s linear infinite; }}
.gv-sweep  {{ animation: gv-sweep 11s cubic-bezier(.4,0,.6,1) infinite; }}
.gv-vias   {{ animation: gv-pulse 4.5s ease-in-out infinite; }}
/* Anyone who has asked for less motion gets a still drawing, not a moving one. */
@media (prefers-reduced-motion: reduce) {{
  .gv-grid, .gv-poly, .gv-sweep, .gv-vias {{ animation: none !important; }}
}}
/* Leaving the landing page: the grid fades once and does not come back. The node is
   rebuilt on every rerun, so a transition would have nothing to move from - a
   one-shot keyframe is what actually animates here. */
@keyframes gv-retire {{ from {{ opacity: 1; }} to {{ opacity: 0; visibility: hidden; }} }}
.gv-backdrop.is-retiring {{ animation: gv-retire 900ms ease-in-out forwards; }}
</style>
"""


def _svg() -> str:
    """The drawing: a routing grid, tracks on pitch, poly across, vias at crossings.

    Coordinates are a 1600x900 viewBox scaled to the viewport, so the same drawing
    holds together on a laptop and on a wall display.
    """
    grid = "".join(
        f'<line x1="{x}" y1="0" x2="{x}" y2="900" />' for x in range(0, 1700, 42)
    ) + "".join(
        f'<line x1="0" y1="{y}" x2="1600" y2="{y}" />' for y in range(0, 964, 63)
    )
    # Metal tracks: on a regular pitch, varying lengths, the way a cell is wired.
    tracks = "".join(
        f'<rect x="{x}" y="{y}" width="{w}" height="7" rx="1.5" />'
        for x, y, w in ((120, 126, 360), (560, 126, 190), (300, 252, 470),
                        (880, 252, 300), (180, 378, 250), (520, 378, 540),
                        (240, 504, 420), (760, 504, 260), (140, 630, 300),
                        (520, 630, 480), (1120, 378, 260), (1040, 630, 220))
    )
    poly = "".join(
        f'<rect x="{x}" y="60" width="6" height="780" rx="1.5" />'
        for x in (210, 336, 462, 588, 714, 840, 966, 1092, 1218, 1344)
    )
    vias = "".join(
        f'<rect x="{x - 6}" y="{y - 6}" width="12" height="12" rx="2" />'
        for x, y in ((336, 126), (588, 252), (462, 378), (840, 252), (714, 504),
                     (966, 378), (588, 630), (1218, 378), (210, 504), (1092, 630))
    )
    return f"""
<div class="gv-backdrop" id="gv-backdrop">
  <svg viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice"
       xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id="gv-sweep-grad" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%"   stop-color="{TRACK}" stop-opacity="0"/>
        <stop offset="50%"  stop-color="{TRACK}" stop-opacity="0.30"/>
        <stop offset="100%" stop-color="{TRACK}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <g class="gv-grid" stroke="{BORDER}" stroke-width="1" opacity="0.34">{grid}</g>
    <g class="gv-poly" fill="{POLY}" opacity="0.10">{poly}</g>
    <g fill="{TRACK}" opacity="0.14">{tracks}</g>
    <g class="gv-vias" fill="{VIA}">{vias}</g>
    <rect class="gv-sweep" x="0" y="0" width="300" height="900"
          fill="url(#gv-sweep-grad)"/>
  </svg>
</div>
"""


def backdrop(retiring: bool = False) -> None:
    """Draw the looping background. `retiring` fades it out once and leaves it gone.

    The drawing only - `styles()` carries the stylesheet, and says why.
    """
    svg = _svg()
    if retiring:
        svg = svg.replace('class="gv-backdrop"', 'class="gv-backdrop is-retiring"')
    st.markdown(svg, unsafe_allow_html=True)


def styles() -> None:
    """Every stylesheet on this page, in one call, before anything that needs them.

    Two constraints, both learned the hard way. Streamlit drops a `<style>` tag when
    the same markdown call also contains other markup, so a stylesheet has to travel
    alone. And an `st.empty()` slot holds one element, so a stylesheet sent into one
    is replaced by whatever is drawn there next. Hence: styles here, bodies there.
    """
    st.markdown(BACKDROP + HERO_CSS + LOADING_CSS, unsafe_allow_html=True)


HERO_CSS = f"""
<style>
.gv-hero {{ padding: 3px 0 6px 0; }}
.gv-hero .mark {{
  display: inline-flex; align-items: center; gap: 9px;
  font-size: 0.7rem; letter-spacing: .16em; text-transform: uppercase;
  color: {MUTED}; margin-bottom: 14px;
}}
.gv-hero .mark i {{
  width: 7px; height: 7px; border-radius: 50%; background: {TRACK};
  box-shadow: 0 0 0 4px rgba(88,166,255,.14); font-style: normal;
}}
.gv-hero h1 {{
  font-size: clamp(1.9rem, 3.6vw, 3rem) !important; line-height: 1.06;
  margin: 0 0 12px 0 !important; padding: 0 !important; letter-spacing: -0.02em;
  font-weight: 640; color: {TEXT};
}}
.gv-hero h1 em {{
  font-style: normal;
  background: linear-gradient(96deg, {TRACK} 0%, {VIA} 52%, {POLY} 100%);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}}
.gv-hero p.lede {{
  font-size: 1.02rem; line-height: 1.6; color: {MUTED};
  max-width: 92ch; margin: 0 0 24px 0;
}}
.gv-hero p.lede b {{ color: {TEXT}; font-weight: 560; }}

.gv-cards {{
  display: grid; gap: 10px; margin: 0 0 20px 0;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
}}
.gv-card {{
  border: 1px solid {BORDER}; border-radius: 10px; padding: 13px 14px;
  background: linear-gradient(180deg, rgba(28,33,40,.82), rgba(22,27,34,.72));
  backdrop-filter: blur(3px);
}}
.gv-card .k {{
  font-size: 0.68rem; letter-spacing: .12em; text-transform: uppercase;
  color: {TRACK}; margin-bottom: 7px; font-weight: 600;
}}
.gv-card .t {{ font-size: 0.88rem; color: {TEXT}; margin-bottom: 4px; font-weight: 560; }}
.gv-card .d {{ font-size: 0.79rem; color: {MUTED}; line-height: 1.45; }}

.gv-need {{
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  font-size: 0.8rem; color: {MUTED}; margin: 0 0 4px 0;
}}
.gv-need code {{
  background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 5px;
  padding: 1px 6px; color: {TEXT}; font-size: 0.78rem;
}}
.gv-need .dot {{ color: {MUTED}; opacity: .55; }}

/* The uploader, on the landing page only: a target rather than a form field. */
.gv-landing div[data-testid="stFileUploaderDropzone"] {{
  background: linear-gradient(180deg, rgba(28,33,40,.72), rgba(22,27,34,.62)) !important;
  border: 1px dashed {BORDER} !important; border-radius: 12px !important;
  transition: border-color .18s ease, background .18s ease;
}}
.gv-landing div[data-testid="stFileUploaderDropzone"]:hover {{
  border-color: {TRACK} !important;
  background: linear-gradient(180deg, rgba(31,39,50,.86), rgba(22,27,34,.7)) !important;
}}

/* Entrances. Short, once, and staggered so the eye lands on the headline first. */
@keyframes gv-rise {{ from {{ opacity: 0; transform: translateY(9px); }}
                      to   {{ opacity: 1; transform: none; }} }}
.gv-hero, .gv-cards, .gv-need {{ animation: gv-rise .5s cubic-bezier(.22,1,.36,1) both; }}
.gv-cards {{ animation-delay: .07s; }}
.gv-need  {{ animation-delay: .13s; }}
@media (prefers-reduced-motion: reduce) {{
  .gv-hero, .gv-cards, .gv-need {{ animation: none; }}
}}
</style>
"""

HERO = f"""
<div class="gv-hero">
  <div class="mark"><i></i>GDS Layout Intelligence</div>
  <h1>Every number on this page was <em>measured</em>,<br/>not guessed.</h1>
  <p class="lede">Drop in a <b>.gds</b> and the geometry is read with KLayout — layers,
     nets, devices, pitch, density, rule results. Two files and you get the XOR, the
     mask impact and a chat that answers from the measurements. Nothing is inferred
     from a file that was not supplied, and anything a layout cannot settle is
     <b>named as unavailable</b> rather than filled in.</p>
</div>
<div class="gv-cards">
  <div class="gv-card">
    <div class="k">Read</div>
    <div class="t">Geometry, exactly</div>
    <div class="d">Areas, widths, spacings and pitch, per layer, cross-checked
        against a second parser.</div>
  </div>
  <div class="gv-card">
    <div class="k">Compare</div>
    <div class="t">Two revisions</div>
    <div class="d">Region-by-region XOR, which masks move, and whether it is an
        interconnect-only change.</div>
  </div>
  <div class="gv-card">
    <div class="k">Interrogate</div>
    <div class="t">Ask in plain words</div>
    <div class="d">Answered from the analyzer's own figures. A refusal when the inputs
        cannot support an answer.</div>
  </div>
  <div class="gv-card">
    <div class="k">Act</div>
    <div class="t">Edit and export</div>
    <div class="d">Draw, move and delete, then write a new GDSII. The upload is never
        modified.</div>
  </div>
</div>
<div class="gv-need">
  Needs only <code>.gds</code><span class="dot">·</span>the technology
  <code>.lyp</code> and the rule catalogue load themselves<span class="dot">·</span>
  a schematic, stack or process file unlocks LVS, 2.5D and RC — each tool says which
</div>
"""


def hero() -> None:
    st.markdown(HERO, unsafe_allow_html=True)


def masthead(backdrop_slot, masthead_slot, status_slot, layermap: dict,
             rules_loaded: bool, has_files: bool) -> None:
    """Fill the three slots reserved at the top of the page.

    They are filled from below because what belongs there depends on whether anything
    was uploaded, and that is only known after the uploader has run. Reserving them
    keeps the headline above the drop target where it reads, without the page having
    to guess a rerun ahead.
    """
    with backdrop_slot:
        backdrop(retiring=has_files)
    if not has_files:
        with masthead_slot:
            hero()
        return
    with masthead_slot:
        st.markdown(
            '<div class="title-row"><h1>◧ GDS Design Reviewer</h1>'
            '<span class="sub">deterministic geometry · measured, never guessed</span>'
            '</div>', unsafe_allow_html=True)
    with status_slot:
        st.caption(
            f"Layer map `{layermap['file']}` — {layermap['entry_count']} technology "
            f"layer names, loaded automatically. "
            + ("Design rule catalogue loaded." if rules_loaded
               else "**No design rule catalogue**, so no rule will be checked — this "
                    "is not a clean rule result."))


LOADING_CSS = f"""
<style>
/* Streamlit's own spinner, restyled to the theme rather than replaced: it already
   appears exactly when a cached analysis runs, which is the moment worth marking. */
div[data-testid="stSpinner"] > div {{
  border-top-color: {TRACK} !important;
  border-right-color: rgba(88,166,255,.22) !important;
  border-bottom-color: rgba(88,166,255,.22) !important;
  border-left-color: rgba(88,166,255,.22) !important;
}}
div[data-testid="stSpinner"] {{
  font-size: 0.82rem; color: {MUTED};
}}
/* A scan line under the header while the page is working. Reads as a measurement
   pass over the layout, which is what is happening. */
.gv-reading {{
  position: relative; height: 2px; margin: 2px 0 12px 0; border-radius: 2px;
  background: {SURFACE}; overflow: hidden;
}}
.gv-reading::after {{
  content: ""; position: absolute; inset: 0 auto 0 0; width: 34%;
  background: linear-gradient(90deg, transparent, {TRACK}, {VIA}, transparent);
  animation: gv-scan 1.15s cubic-bezier(.45,0,.55,1) infinite;
}}
@keyframes gv-scan {{ from {{ transform: translateX(-100%); }}
                      to   {{ transform: translateX(320%); }} }}
@media (prefers-reduced-motion: reduce) {{
  .gv-reading::after {{ animation-duration: 3s; }}
}}
</style>
"""

READING = '<div class="gv-reading" role="status" aria-label="Reading the layout"></div>'



def reading() -> None:
    """The scan line. Rendered while the first analysis of a new upload runs."""
    st.markdown(READING, unsafe_allow_html=True)


def keep_at_top(signature: str) -> bool:
    """Scroll the page back to the top when the uploaded set changes.

    Streamlit keeps the scroll position across reruns and the chat input sits at the
    bottom of a long page, so uploading a file left the reader looking at the end of
    the analysis instead of the start of it. This fires once per change of upload -
    not on every rerun, which would fight anyone scrolling.

    Returns True on the run where the set changed, which is also the run where the
    analyses actually execute - so the caller can use it to decide whether the scan
    line is worth reserving space for.
    """
    if st.session_state.get("gv_scroll_signature") == signature:
        return False
    st.session_state["gv_scroll_signature"] = signature
    # The element that scrolls is Streamlit's own, and its test id says what it does:
    # stAppScrollToBottomContainer. It exists because `st.chat_input` is on the page,
    # and it is what put the reader at the end of the analysis on every upload.
    components.html(
        """<script>
        const outer = window.parent || window;
        const doc = outer.document;
        const targets = () => [
          doc.querySelector('[data-testid="stAppScrollToBottomContainer"]'),
          doc.querySelector('section.stMain'),
          doc.querySelector('div[data-testid="stAppViewContainer"]'),
        ].filter(Boolean);
        const go = () => {
          outer.scrollTo({top: 0, behavior: "instant"});
          for (const el of targets()) el.scrollTop = 0;
        };

        // Polled, not event-driven, and that is the whole trick. The container being
        // reset does not exist yet when this runs: Streamlit creates it further down
        // the same render, because `st.chat_input` is what makes the page
        // bottom-anchored - and it arrives already scrolled to the end. A mutation
        // observer attached to what is in the DOM now misses that, and a handful of
        // timeouts expires long before the slowest layout has finished being read.
        let live = true;
        const release = () => { live = false; };
        // The first deliberate scroll wins. Fighting a reader who is scrolling on
        // purpose is worse than the problem being fixed here.
        for (const ev of ["wheel", "touchmove", "keydown", "mousedown"]) {
          doc.addEventListener(ev, release, {passive: true, once: true});
        }
        go();
        const timer = setInterval(() => {
          if (!live) { clearInterval(timer); return; }
          for (const el of targets()) if (el.scrollTop > 0) el.scrollTop = 0;
        }, 120);
        // Long enough to cover reading several layouts, and released the moment the
        // reader touches the page.
        setTimeout(() => { release(); clearInterval(timer); }, 45000);
        </script>""",
        height=0,
    )
    return True
