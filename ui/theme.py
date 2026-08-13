"""Visual theme: colour tokens, CSS and the plotly template.

Grounded in two things rather than taste.

**What the tools engineers already use look like.** Virtuoso, Innovus and KLayout
are dark, and layout viewers are dark for a practical reason: bright per-layer
colours separate much better against a dark canvas. Jakob's Law says a new tool
should behave like the ones people already know, so this one is dark too.

**The UX laws, applied to a data-dense professional screen:**

* *Miller's Law* - working memory holds about seven items, so metrics come in
  clusters of four or five, never fourteen in a row.
* *Von Restorff* - the thing that differs is the thing remembered, so the verdict
  is the only element with a coloured left border and a larger type size.
* *Law of Proximity / Similarity* - related figures sit in one bordered card with
  identical styling, so the grouping is read without needing a label.
* *Serial Position Effect* - the verdict goes first and the "not derivable"
  boundaries last, because those are the two positions people retain.
* *Hick's Law* - controls sit next to what they affect rather than in a global bar
  of options.
* *Tesler's Law* - the complexity is real and is not removed, only deferred: the
  full tables stay one click away instead of being deleted.

Numbers are set in a tabular monospace face. A column of coordinates that does not
align is measurably slower to scan, and this screen is mostly columns of figures.
"""
from __future__ import annotations

# Palette. Dark surfaces from a well-tested ramp for long reading sessions, with
# semantic colours that keep exactly one meaning throughout the app.
BG = "#0d1117"
SURFACE = "#161b22"
SURFACE_2 = "#1c2128"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
OK = "#3fb950"
WARN = "#d29922"
DANGER = "#f85149"

# Verdict states map to one colour each, used for the banner border and its icon
# and nothing else. Reusing these for decoration would break the association.
STATE_COLOUR = {
    "identical": OK,
    "interconnect-only": ACCENT,
    "base-layers": WARN,
    "blocked": DANGER,
    "none": MUTED,
}
STATE_ICON = {
    "identical": "✓",
    "interconnect-only": "◆",
    "base-layers": "▲",
    "blocked": "✕",
    "none": "·",
}

CSS = f"""
<style>
/* Type: UI in the system sans, every number in a tabular monospace so columns of
   coordinates and areas line up digit for digit. */
[data-testid="stMetricValue"], [data-testid="stMetricDelta"],
.stDataFrame, .stDataFrame div, code, pre, .mono {{
  font-family: "SF Mono", "JetBrains Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}}

/* Chrome removal. The default header is dead space at the top of the page, which
   is the most valuable area on the screen. */
#MainMenu, footer {{ visibility: hidden; height: 0; }}
header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
.block-container {{ padding-top: 1.2rem; padding-bottom: 3rem; max-width: 1500px; }}

/* Metrics as cards: a border plus consistent padding makes each cluster read as
   one group (proximity + similarity) rather than as floating numbers. */
[data-testid="stMetric"] {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 8px;
  padding: 12px 14px;
}}
[data-testid="stMetricLabel"] p {{
  font-size: 0.72rem !important;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: {MUTED} !important;
}}
[data-testid="stMetricValue"] {{ font-size: 1.4rem !important; }}

/* The verdict. The only element on the page with this treatment, so it is where
   the eye lands first. */
.verdict {{
  border-left: 4px solid var(--vc);
  background: {SURFACE};
  border-radius: 6px;
  padding: 14px 18px;
  margin: 2px 0 16px 0;
}}
.verdict .vtitle {{
  font-size: 1.1rem; font-weight: 600; color: {TEXT};
  display: flex; align-items: baseline; gap: 10px;
}}
.verdict .vicon {{ color: var(--vc); font-size: 1.15rem; }}
.verdict .vdetail {{
  color: {MUTED}; font-size: 0.86rem; margin-top: 7px; line-height: 1.55;
}}

/* Section headings: quiet, so they organise without competing with the data. */
.section {{
  font-size: 0.74rem; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: {MUTED};
  border-bottom: 1px solid {BORDER};
  padding-bottom: 6px; margin: 26px 0 12px 0;
}}

/* Tables: tighter rows fit more in without shrinking the type. */
.stDataFrame {{ font-size: 0.83rem; }}
.stDataFrame thead tr th {{
  background: {SURFACE_2} !important; color: {MUTED} !important;
  text-transform: uppercase; font-size: 0.69rem; letter-spacing: .04em;
}}

/* Expanders should read as "there is more here", not as empty boxes. */
[data-testid="stExpander"] {{
  border: 1px solid {BORDER}; border-radius: 8px; background: {SURFACE};
}}
[data-testid="stExpander"] summary {{ font-size: 0.87rem; color: {TEXT}; }}

/* Tabs: a clear active state. The default inactive/active contrast is nearly
   invisible on a dark background. */
.stTabs [data-baseweb="tab-list"] {{ gap: 2px; border-bottom: 1px solid {BORDER}; }}
.stTabs [data-baseweb="tab"] {{
  height: 36px; padding: 0 15px; font-size: 0.85rem;
  background: transparent; border-radius: 6px 6px 0 0;
}}
.stTabs [aria-selected="true"] {{
  background: {SURFACE}; border-bottom: 2px solid {ACCENT}; color: {TEXT};
}}

/* Provenance chips. Every derived figure here carries a source, and a chip states
   it without spending a sentence of prose on it. */
.chip {{
  display: inline-block; padding: 2px 9px; margin: 2px 6px 2px 0;
  border-radius: 999px; font-size: 0.71rem; border: 1px solid {BORDER};
  background: {SURFACE_2}; color: {MUTED};
  font-family: "SF Mono", Menlo, monospace;
}}
.chip.exact {{ border-color: {OK}; color: {OK}; }}
.chip.measured {{ border-color: {ACCENT}; color: {ACCENT}; }}
.chip.inferred {{ border-color: {WARN}; color: {WARN}; }}
.chip.unavailable {{ border-color: {DANGER}; color: {DANGER}; }}

.hint {{ color: {MUTED}; font-size: 0.8rem; line-height: 1.55; }}

/* Layer panel, modelled on KLayout's layer list: a dense right-hand column of
   rows, each a swatch plus the layer name and its layer/datatype, toggled by
   clicking. Streamlit's default checkbox rows are far too tall for a list of
   twenty layers, so they are compressed here. */
.lp-head {{
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: .08em;
  color: {MUTED}; border-bottom: 1px solid {BORDER};
  padding-bottom: 5px; margin-bottom: 2px;
}}
.layer-panel [data-testid="stCheckbox"] {{ margin: 0 !important; padding: 0 !important; }}
.layer-panel [data-testid="stCheckbox"] label {{
  padding: 1px 0 !important; min-height: 0 !important; gap: 6px !important;
}}
.layer-panel [data-testid="stCheckbox"] label p {{
  font-family: "SF Mono", Menlo, monospace; font-size: 0.76rem !important;
  margin: 0 !important; line-height: 1.35 !important;
}}
.layer-panel [data-testid="stVerticalBlock"] {{ gap: 0 !important; }}
.layer-panel [data-testid="stHorizontalBlock"] {{ gap: 4px !important; }}
.swatch {{
  display: inline-block; width: 11px; height: 11px; border-radius: 2px;
  border: 1px solid rgba(255,255,255,.35); vertical-align: -1px;
}}
.lp-row {{
  display: flex; align-items: center; gap: 7px;
  font-family: "SF Mono", Menlo, monospace; font-size: 0.75rem;
  color: {TEXT}; padding: 1px 0;
}}
.lp-row .ld {{ color: {MUTED}; font-size: 0.68rem; }}
.lp-row .n {{ color: {MUTED}; font-size: 0.68rem; margin-left: auto; }}
.title-row {{
  display: flex; align-items: baseline; gap: 12px; margin-bottom: 2px;
}}
.title-row h1 {{ font-size: 1.35rem !important; margin: 0 !important; padding: 0 !important; }}
.title-row .sub {{ color: {MUTED}; font-size: 0.83rem; }}
</style>
"""

# Availability of a figure, shown as a chip. The wording matches CAPABILITIES.md so
# the interface and the documentation cannot drift apart.
CHIP_CLASS = {
    "GDS-only": "exact",
    "GDS + LYP": "measured",
    "GDS + sidecar": "measured",
    "measured": "measured",
    "exact": "exact",
    "inferred": "inferred",
    "requires PDK": "unavailable",
    "requires netlist": "unavailable",
    "unavailable": "unavailable",
}


def chip(label: str, kind: str = "") -> str:
    return f'<span class="chip {CHIP_CLASS.get(kind, "")}">{label}</span>'


def chips(*pairs: tuple[str, str]) -> str:
    return "".join(chip(label, kind) for label, kind in pairs)


def verdict_html(state: str, title: str, detail: str = "") -> str:
    colour = STATE_COLOUR.get(state, MUTED)
    icon = STATE_ICON.get(state, "·")
    body = f'<div class="vdetail">{detail}</div>' if detail else ""
    return (f'<div class="verdict" style="--vc:{colour}">'
            f'<div class="vtitle"><span class="vicon">{icon}</span><span>{title}</span></div>'
            f'{body}</div>')


def section(label: str) -> str:
    return f'<div class="section">{label}</div>'


def hint(text: str) -> str:
    return f'<div class="hint">{text}</div>'


def swatch(colour: str | None) -> str:
    """A KLayout-style colour chip for a layer."""
    return f'<span class="swatch" style="background:{colour or MUTED}"></span>'


def style_figure(fig):
    """Match a plotly figure to the app theme.

    A default-white chart on a dark page is the most jarring thing a dashboard can
    do, and it also destroys the layer-colour contrast the difference map depends
    on.
    """
    if fig is None:
        return None
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=BG,
        font=dict(family="SF Mono, Menlo, monospace", size=11, color=TEXT),
        title=dict(font=dict(size=13, color=TEXT)),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        margin=dict(l=56, r=18, t=44, b=44),
    )
    fig.update_xaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER)
    fig.update_yaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER)
    return fig
