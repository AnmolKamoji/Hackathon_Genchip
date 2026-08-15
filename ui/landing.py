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

from pathlib import Path

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
  /* Only the edges are softened. The copy is protected by the scrim below, not by
     erasing the artwork - masking it away to keep text legible left a background so
     faint there was no reason to draw it. */
  mask-image: radial-gradient(150% 130% at 62% 55%, #000 0%, #000 62%, transparent 99%);
  -webkit-mask-image: radial-gradient(150% 130% at 62% 55%, #000 0%, #000 62%, transparent 99%);
}}
.gv-backdrop svg {{ width: 100%; height: 100%; display: block; }}

/* The scrim: the page colour, opaque where the words are and clear where they are
   not. Full-bleed artwork behind a gradient is what keeps both - a legible column of
   copy on the left, and the floorplan running out to the right at full strength. */
.gv-scrim {{
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background:
    linear-gradient(101deg, {BG} 0%, {BG} 46%, rgba(13,17,23,.93) 60%,
                    rgba(13,17,23,.62) 76%, rgba(13,17,23,.18) 100%),
    linear-gradient(to bottom, {BG} 0%, rgba(13,17,23,.35) 26%,
                    rgba(13,17,23,0) 46%);
}}
/* Content above the backdrop. Streamlit's own container is the stacking context. */
.stApp > div[data-testid="stAppViewContainer"],
section.main, section.stMain,
div[data-testid="stMainBlockContainer"] {{ position: relative; z-index: 1; }}

/* Streamlit anchors this container to its bottom - the test id is literally
   stAppScrollToBottomContainer - because `st.chat_input` is on the page. That is
   what sent every upload to the end of the analysis. The anchor is released here
   and the position is reset explicitly when the uploaded set changes. */
[data-testid="stAppScrollToBottomContainer"] {{ overflow-anchor: none; }}

/* Motion is transforms and opacity only: both stay on the compositor, so a full
   standard-cell row animates without costing a single layout pass. */
@keyframes gv-drift-x {{ from {{ transform: translateX(0); }}
                         to   {{ transform: translateX(84px); }} }}
@keyframes gv-row     {{ from {{ transform: translateX(0); }}
                         to   {{ transform: translateX(calc(var(--period) * -1)); }} }}
@keyframes gv-sweep   {{ 0%   {{ transform: translateX(-32%); opacity: 0; }}
                         12%  {{ opacity: .5; }}
                         88%  {{ opacity: .5; }}
                         100% {{ transform: translateX(132%); opacity: 0; }} }}
.gv-grid  {{ animation: gv-drift-x 9s linear infinite; }}
.gv-sweep {{ animation: gv-sweep 11s cubic-bezier(.4,0,.6,1) infinite; }}
/* Each band slides by exactly one repeat of the cell pattern, so the loop has no
   seam, and at its own speed, which is what gives the floorplan depth. */
.gv-band  {{ animation: gv-row var(--dur, 26s) linear infinite; }}

/* The build-up. Every mask holds a low base opacity and brightens as the wave
   reaches it; the delay is its position in the stack, so the highlight climbs from
   diffusion to backside metal and starts again. One keyframe, thirty-odd delays. */
@keyframes gv-mask {{
  0%    {{ opacity: .30; }}
  6%    {{ opacity: .95; }}
  18%   {{ opacity: .46; }}
  100%  {{ opacity: .30; }}
}}
.gv-mask {{
  opacity: .30;
  animation: gv-mask 13s ease-in-out infinite;
  animation-delay: calc(var(--step) * 0.42s);
}}
/* Track guides are not drawn geometry - they declare where the tracks are - so they
   stay a constant hairline instead of joining the wave. */
.gv-guide {{ animation: none; opacity: .13; }}

/* Anyone who has asked for less motion gets a still drawing, not a moving one. */
@media (prefers-reduced-motion: reduce) {{
  .gv-grid, .gv-sweep, .gv-band, .gv-mask {{ animation: none !important; }}
  .gv-mask {{ opacity: .30; }}
}}
/* Leaving the landing page: the grid fades once and does not come back. The node is
   rebuilt on every rerun, so a transition would have nothing to move from - a
   one-shot keyframe is what actually animates here. */
@keyframes gv-retire {{ from {{ opacity: 1; }} to {{ opacity: 0; visibility: hidden; }} }}
.gv-backdrop.is-retiring, .gv-scrim.is-retiring {{
  animation: gv-retire 900ms ease-in-out forwards;
}}
</style>
"""


# The cells drawn in the background. Real files, read with the real layer map, so
# the drawing is the tool's own subject rather than an illustration of it.
_ROW = ("AN2D1_2_RT_4.gds", "NR2D1_1_RT_4.gds", "DCAP0_1_RT_4.gds", "NR2D1_2_RT_4.gds")
# Drawn layers only. The duplicates, extensions, pattern cuts and label layers are
# bookkeeping - drawing them turns a legible cell into a smear.
_SKIP = ("-DUPLICATE", "-EXTENDED", "-PATTERN-CUT", "-LABEL", "DUMMY-")
_SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"

# A cell row is 118 units tall in a 1600x900 viewBox, and the drawing stacks seven of
# them. At that size a cell is not a colour block - it is legible geometry, poly and
# diffcon and metal, the way a floorplan looks a few steps back from the screen. One
# enormous row read as four smudges; this reads as a chip.
_ROW_HEIGHT = 118.0
_BANDS = 7


def _outlines(name: str):
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.measurements import shape_outlines
    return shape_outlines(_SAMPLES / name, load_lyp(default_layermap()))


@st.cache_resource(show_spinner=False)
def _cell_row() -> tuple[str, float]:
    """A standard-cell row, drawn from the sample layouts' own geometry.

    Cells abut the way they do in a real row and alternate ones are flipped, which is
    how a placer lays them down - the power rails have to meet. Every polygon here was
    read out of a `.gds` in `data/samples/` and every colour came from the `.lyp`, so
    nothing about this drawing was invented for decoration.

    Returns the drawing and the width of one repeat of the pattern, which is what the
    slide has to travel for the loop to have no seam.

    Cached as a resource: it is the same string on every rerun and costs one read.
    """
    try:
        cells = [(name, _outlines(name)) for name in _ROW
                 if (_SAMPLES / name).exists()]
    except Exception:
        return "", 0.0                  # a background is never worth an error page
    cells = [(n, o) for n, o in cells if o.get("layers")]
    if not cells:
        return "", 0.0

    height_um = max(o["cell_height_um"] or 0.2 for _, o in cells) or 0.2
    scale = _ROW_HEIGHT / height_um

    # Group by layer so a whole mask lights up at once, and keep process order: the
    # numbers in this technology already run bottom-up, diffusion through backside.
    # One repeat of the pattern is every cell laid down once. The row is drawn a full
    # repeat wider than the viewBox so the slide can wrap without a visible seam.
    period = sum(o["cell_width_um"] or 0.2 for _, o in cells) * scale

    by_layer: dict[tuple, dict] = {}
    x_um = 0.0
    index = 0
    while x_um * scale < 1600 + period + 40:
        name, outlines = cells[index % len(cells)]
        flip = (index % 2) == 1
        box = outlines["cell_bbox_um"] or [0, 0, 0, 0]
        width_um = outlines["cell_width_um"] or 0.2
        for row in outlines["layers"]:
            if any(mark in row["name"] for mark in _SKIP) or not row["shapes"]:
                continue
            key = (row["layer"], row["datatype"])
            entry = by_layer.setdefault(key, {"name": row["name"],
                                              "colour": row["colour"],
                                              "role": row["role"], "d": []})
            for shape in row["shapes"]:
                pts = []
                for px, py in shape["outline_um"]:
                    sx = (x_um + (px - box[0])) * scale
                    # y grows downward in SVG, and a flipped cell mirrors about the
                    # row's own centre line - which is what makes the power rails of
                    # neighbouring cells meet, as they must in a real row.
                    local = (py - box[1])
                    if flip:
                        local = height_um - local
                    sy = (height_um - local) * scale
                    pts.append(f"{sx:.1f},{sy:.1f}")
                if pts:
                    entry["d"].append("M" + "L".join(pts) + "Z")
        x_um += width_um
        index += 1

    # Outlines, not solids. Filled, the two backside power rails cover the whole cell
    # and the drawing is a magenta wash with a few blocks in it; stroked, the same
    # geometry reads the way a layout viewer draws it at low zoom - fine, technical
    # and legible - and no layer can drown the rest by being large.
    #
    # Bottom-up, so the highlight wave climbs the stack the way the process builds it.
    groups = []
    for step, key in enumerate(sorted(by_layer)):
        row = by_layer[key]
        if not row["d"]:
            continue
        guide = "TRACK-GUIDE" in row["name"]
        # Vias and contacts are small enough to fill: they are the punctuation of a
        # layout and read as dots rather than as boxes.
        solid = row["role"] in ("via", "contact")
        paint = (f'fill="{row["colour"]}" fill-opacity="0.5" '
                 f'stroke="{row["colour"]}" stroke-width="1"' if solid
                 else f'fill="none" stroke="{row["colour"]}" '
                      f'stroke-width="{0.8 if guide else 1.4}"')
        groups.append(
            f'<g class="gv-mask{" gv-guide" if guide else ""}" '
            f'style="--step:{step}" {paint} stroke-linejoin="round" '
            f'data-layer="{row["name"]}"><path d="{"".join(row["d"])}"/></g>')
    return "".join(groups), period


def _svg() -> str:
    """The backdrop: a substrate grid, and a real cell row building up over it."""
    grid = "".join(
        f'<line x1="{x}" y1="0" x2="{x}" y2="900" />' for x in range(0, 1700, 42)
    ) + "".join(
        f'<line x1="0" y1="{y}" x2="1600" y2="{y}" />' for y in range(0, 964, 63)
    )
    row, period = _cell_row()
    # The row is drawn once and instanced. Alternate bands are mirrored, which is how
    # rows abut in a real block, and each drifts at its own speed so the floorplan has
    # depth rather than sliding as one flat sheet.
    # Two groups per band, and the split is load-bearing: a CSS `transform` animation
    # replaces the element's `transform` attribute outright rather than composing with
    # it. With placement and motion on the same group, every band animated back to
    # y=0 and the seven rows drew on top of each other.
    bands = []
    for i in range(_BANDS):
        y = i * _ROW_HEIGHT
        place = (f"translate({-(i % 3) * 130:.0f},{y + _ROW_HEIGHT:.1f}) scale(1,-1)"
                 if i % 2 else f"translate({-(i % 3) * 130:.0f},{y:.1f})")
        bands.append(
            f'<g transform="{place}">'
            f'<g class="gv-band" style="--period:{period:.1f}px;'
            f'--dur:{26 + (i % 3) * 9}s"><use href="#gv-cellrow"/></g></g>')
    return f"""
<div class="gv-backdrop" id="gv-backdrop">
  <svg viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice"
       xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
    <defs>
      <g id="gv-cellrow">{row}</g>
      <linearGradient id="gv-sweep-grad" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%"   stop-color="{TRACK}" stop-opacity="0"/>
        <stop offset="50%"  stop-color="{TRACK}" stop-opacity="0.22"/>
        <stop offset="100%" stop-color="{TRACK}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <g class="gv-grid" stroke="{BORDER}" stroke-width="1" opacity="0.42">{grid}</g>
    {"".join(bands)}
    <rect class="gv-sweep" x="0" y="0" width="360" height="900"
          fill="url(#gv-sweep-grad)"/>
  </svg>
</div>
<div class="gv-scrim" id="gv-scrim"></div>
"""


def backdrop(retiring: bool = False) -> None:
    """Draw the looping background. `retiring` fades it out once and leaves it gone.

    The drawing only - `styles()` carries the stylesheet, and says why.
    """
    svg = _svg()
    if retiring:
        svg = svg.replace('class="gv-backdrop"', 'class="gv-backdrop is-retiring"')
        svg = svg.replace('class="gv-scrim"', 'class="gv-scrim is-retiring"')
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
  position: relative; overflow: hidden;
  border: 1px solid {BORDER}; border-radius: 10px; padding: 14px 15px;
  background: linear-gradient(180deg, rgba(28,33,40,.94), rgba(22,27,34,.88));
  backdrop-filter: blur(6px);
  transition: transform .22s cubic-bezier(.22,1,.36,1), border-color .22s ease,
              box-shadow .22s ease;
}}
/* A hairline along the top edge, in the three decorative hues. It is the only
   ornament on the card, and it is what makes the four read as one set. */
.gv-card::before {{
  content: ""; position: absolute; inset: 0 0 auto 0; height: 1px;
  background: linear-gradient(90deg, {TRACK}, {VIA} 52%, {POLY});
  opacity: .5; transition: opacity .22s ease;
}}
.gv-card:hover {{
  transform: translateY(-2px); border-color: #3d4854;
  box-shadow: 0 10px 26px -14px rgba(0,0,0,.9);
}}
.gv-card:hover::before {{ opacity: 1; }}
.gv-card .k {{
  font-size: 0.68rem; letter-spacing: .12em; text-transform: uppercase;
  color: {TRACK}; margin-bottom: 7px; font-weight: 600;
}}
.gv-card .t {{ font-size: 0.88rem; color: {TEXT}; margin-bottom: 4px; font-weight: 560; }}
.gv-card .d {{ font-size: 0.79rem; color: {MUTED}; line-height: 1.45; }}

/* The measured strip. Figures in the tabular face the whole app uses for numbers,
   so a column of them lines up and reads as data rather than as marketing. */
.gv-stats {{
  display: grid; gap: 1px; margin: 0 0 8px 0;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  background: {BORDER}; border: 1px solid {BORDER}; border-radius: 10px;
  overflow: hidden;
}}
.gv-stat {{
  background: linear-gradient(180deg, rgba(22,27,34,.96), rgba(13,17,23,.93));
  padding: 11px 14px;
}}
.gv-stat b {{
  display: block; font-family: "SF Mono", Menlo, monospace;
  font-size: 1.06rem; color: {TEXT}; font-weight: 600; letter-spacing: -0.01em;
}}
.gv-stat span {{
  display: block; margin-top: 2px; font-size: 0.7rem; color: {MUTED};
  letter-spacing: .04em;
}}
.gv-statnote {{ font-size: 0.72rem; color: {MUTED}; opacity: .8; margin-bottom: 20px; }}
.gv-statnote code {{
  background: {SURFACE_2}; border-radius: 4px; padding: 0 4px; color: {MUTED};
}}

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
.gv-hero, .gv-cards, .gv-stats, .gv-statnote, .gv-need {{
  animation: gv-rise .5s cubic-bezier(.22,1,.36,1) both;
}}
.gv-cards    {{ animation-delay: .06s; }}
.gv-stats    {{ animation-delay: .12s; }}
.gv-statnote {{ animation-delay: .14s; }}
.gv-need     {{ animation-delay: .18s; }}
@media (prefers-reduced-motion: reduce) {{
  .gv-hero, .gv-cards, .gv-stats, .gv-statnote, .gv-need {{ animation: none; }}
  .gv-card {{ transition: none; }}
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


@st.cache_resource(show_spinner=False)
def _sample_facts() -> str:
    """The strip under the hero, measured from the bundled cells at start-up.

    A landing page that claims precision should demonstrate it. Every figure here was
    read out of `data/samples/` by the same code the review page uses, so the numbers
    a visitor sees before uploading anything are already real ones.
    """
    from analyzer.layermap import default_layermap, load_lyp
    from analyzer.measurements import shape_outlines
    from analyzer.pitch import analyze_pitch
    try:
        layermap = load_lyp(default_layermap())
        files = sorted(_SAMPLES.glob("*.gds"))
        if not files:
            return ""
        outlines = [shape_outlines(f, layermap) for f in files]
        pitch = analyze_pitch(outlines[0], files[0].name)
        shapes = sum(o["shape_total"] for o in outlines)
        layers = len({(r["layer"], r["datatype"])
                      for o in outlines for r in o["layers"]})
    except Exception:
        return ""

    def nm(value) -> str:
        return f"{value:g} nm" if value else "—"

    metals = pitch.get("metal_pitches") or {}
    stats = [(str(len(files)), "sample cells"),
             (f"{shapes:,}", "polygons read"),
             (str(layers), "technology layers"),
             (nm((pitch.get("gate_pitch") or {}).get("cpp_nm")), "gate pitch (CPP)"),
             (nm((metals.get("M0") or {}).get("pitch_nm")), "M0 pitch"),
             (nm((metals.get("M1") or {}).get("pitch_nm")), "M1 pitch")]
    # A figure that could not be measured is not shown at all - a landing page
    # demonstrating precision must not lead with an em dash.
    stats = [(v, k) for v, k in stats if v != "—"]
    cells = "".join(f'<div class="gv-stat"><b>{v}</b><span>{k}</span></div>'
                    for v, k in stats)
    return (f'<div class="gv-stats">{cells}</div>'
            f'<div class="gv-statnote">measured from <code>data/samples/</code> at '
            f'start-up by the same code the review page uses</div>')


def hero() -> None:
    st.markdown(HERO + _sample_facts(), unsafe_allow_html=True)


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
