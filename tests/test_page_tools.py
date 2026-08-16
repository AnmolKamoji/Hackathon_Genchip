"""The page's own wiring: the workspace, the editor and the tool bench.

Every unit test in this suite can pass while the page still shows none of it. A
tool that no menu opens, an Expand button wired to nothing and an editor whose
commits are never applied are all invisible from below - they are only visible from
here, driving the real `app.py` with the real sample files.

So these tests assert on reachability rather than on numbers: that the button
exists, that pressing it renders the thing, and that the thing acted on the file it
was given. The measurements themselves are checked in the analyzer tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from analyzer.edit import apply_to_bytes
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from ui.sections.tools import TOOL_BY_ID, TOOL_TABS

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
APP = ROOT / "app.py"
A, B = "DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"


class _Upload:
    """Stands in for Streamlit's UploadedFile."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name

    def getvalue(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def run_app(monkeypatch):
    """Run the real app with chosen uploads and session state."""

    def runner(gds: list[str] = None, **state):
        files = [_Upload(SAMPLES / name) for name in (gds or [A, B])]

        def fake_uploader(label, *args, **kwargs):
            return files if "GDS" in label else None

        monkeypatch.setattr(st, "file_uploader", fake_uploader)
        at = AppTest.from_file(str(APP), default_timeout=900)
        for key, value in state.items():
            at.session_state[key] = value
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    return runner


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _keys(at) -> set[str]:
    return {b.key for b in at.button}


def _headings(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _state(at, key, default=None):
    """AppTest's session state has no `.get`, and a missing key raises."""
    return at.session_state[key] if key in at.session_state else default


def _metric(at, label: str, occurrence: int = 0) -> str:
    """One metric by label. Several sections legitimately show the same label for
    different files, so which one is being read has to be said."""
    return [m.value for m in at.metric if m.label == label][occurrence]


# --- the page still stands ---------------------------------------------------

def test_the_page_renders_with_one_file_and_with_two(run_app):
    assert not run_app([A]).error
    assert not run_app([A, B]).error


def test_every_layout_and_the_comparison_can_be_expanded(run_app):
    """The workspace is reachable from each viewer, not from a menu somewhere else."""
    keys = _keys(run_app([A, B]))
    assert {"lv0_expand", "lv1_expand", "cmp_expand"} <= keys


def test_expanding_a_layout_opens_the_workspace(run_app):
    at = run_app([A, B])
    at.button(key="lv0_expand").click().run()
    assert at.session_state["gv_focus"]["kind"] == "layout"
    assert "ws_back" in _keys(at)
    assert [c.key for c in at.chat_input] == ["ws_input"]
    assert f"### {A}" in _headings(at)


def test_expanding_the_comparison_opens_it_with_both_names(run_app):
    at = run_app([A, B])
    at.button(key="cmp_expand").click().run()
    assert at.session_state["gv_focus"] == {"kind": "compare", "key": "cmp",
                                            "a": A, "b": B}
    assert f"### {A} → {B}" in _headings(at)


def test_back_leaves_the_workspace(run_app):
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A})
    at.button(key="ws_back").click().run()
    assert "gv_focus" not in at.session_state
    assert {"lv0_expand", "cmp_expand"} <= _keys(at)


def test_the_workspace_owns_the_screen(run_app):
    """It renders instead of the page, not below it - so the six sections are gone
    and the page's own chat is not competing with the workspace's."""
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A})
    assert [c.key for c in at.chat_input] == ["ws_input"]
    assert "cmp_expand" not in _keys(at)


def test_the_workspace_chat_answers_from_the_measurements(run_app):
    at = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp", "a": A, "b": B})
    at.chat_input(key="ws_input").set_value("What changed between these two layouts?").run()
    reply = at.session_state["ws_chat"][-1]
    assert reply["role"] == "assistant"
    assert "µm²" in reply["content"] or "nm²" in reply["content"]


def test_a_judgement_is_still_refused_in_the_workspace(run_app):
    at = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp", "a": A, "b": B})
    at.chat_input(key="ws_input").set_value("Is B better than A?").run()
    assert at.session_state["ws_chat"][-1]["content"].startswith("I cannot tell you")


# --- the tool bench ----------------------------------------------------------

@pytest.mark.parametrize("tool", TOOL_TABS)
def test_every_tool_opens_and_renders(run_app, tool):
    at = run_app([A, B], tool_request=tool, tool_request_file=A)
    assert f"{tool} — {A}" in _headings(at)
    assert at.session_state["tool_open"] == tool


def test_every_menu_id_names_a_tool_that_exists():
    """The viewer's menu and the page's tool list cannot drift apart."""
    assert set(TOOL_BY_ID.values()) <= set(TOOL_TABS)


def test_a_tool_renders_under_the_viewer_that_asked_for_it(run_app):
    at = run_app([A, B], tool_request="Netlist", tool_request_file=B)
    assert f"Netlist — {B}" in _headings(at)
    assert f"Netlist — {A}" not in _headings(at)
    assert f"tool_close_{B}" in _keys(at)


def test_closing_a_tool_leaves_the_page(run_app):
    at = run_app([A, B], tool_request="Netlist", tool_request_file=A)
    at.button(key=f"tool_close_{A}").click().run()
    assert "tool_open" not in at.session_state
    assert f"Netlist — {A}" not in _headings(at)


def test_the_tools_that_need_a_file_ask_for_it_rather_than_guessing(run_app):
    """LVS without a schematic and the 2.5D view without a stack say what is missing
    and in what format. Neither invents one."""
    for tool, wanted in (("LVS", "schematic"), ("2.5D view", "stack")):
        at = run_app([A, B], tool_request=tool, tool_request_file=A)
        said = " ".join(i.value for i in at.info).lower()
        assert wanted in said, tool


def test_the_bundled_rule_check_runs_without_any_upload(run_app):
    at = run_app([A, B], tool_request="DRC", tool_request_file=A)
    assert any("The manual has" in c.value and "rules" in c.value for c in at.caption)
    # A count, not a fraction: the Inspect section shows "21/71" for the same idea,
    # and this is the tool's own metric.
    assert any(m.label == "Rules checked" and m.value.isdigit() for m in at.metric)


def test_parasitics_reports_geometry_and_says_what_it_cannot_price(run_app):
    at = run_app([A, B], tool_request="Parasitics", tool_request_file=A)
    vias = [m.value for m in at.metric if m.label == "Vias"]
    # The tool's own count is the last one, and it has to agree with the count the
    # Inspect section above it is showing for the same file.
    assert len(set(vias)) == 1, vias
    assert "µm" in next(m.value for m in at.metric if m.label == "Wire length")
    assert "ohms and farads" in _headings(at)


def test_parasitics_gives_no_ohms_until_it_is_given_the_constants(run_app):
    """The half a layout settles is shown; the half it does not is not invented."""
    at = run_app([A, B], tool_request="Parasitics", tool_request_file=A)
    assert not any("Ω" in m.value for m in at.metric)
    assert "not in a GDSII" in _headings(at)


def test_the_diff_tool_needs_two_distinct_files(run_app):
    at = run_app([A], tool_request="Diff", tool_request_file=A)
    assert any("second" in i.value for i in at.info)


# --- the editor --------------------------------------------------------------

def _delete_first(lm, name: str, layer: str):
    """The bytes of `name` with one shape on `layer` deleted, and the edit itself."""
    path = SAMPLES / name
    outlines = shape_outlines(path, lm, include_identity=True)
    row = next(l for l in outlines["layers"] if l["name"] == layer)
    edit = {"op": "delete", "target": {"layer": layer, **row["shapes"][0]["id"]}}
    data, report = apply_to_bytes(path.read_bytes(), name, [edit], layermap=lm)
    return data, edit, report


def test_edit_mode_is_offered_on_a_layout_and_not_on_a_comparison(run_app):
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A})
    assert [t.key for t in at.toggle] == ["ws_editing"]
    at = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp", "a": A, "b": B})
    assert not [t.key for t in at.toggle]


def test_a_committed_edit_is_written_and_everything_is_re_run_on_it(run_app, lm,
                                                                   monkeypatch):
    """The whole point of editing here: the page does not keep showing the file you
    just changed. The commit writes a new GDSII and every section re-reads it."""
    import ui.sections.tools as page_tools

    _, edit, _ = _delete_first(lm, A, "M0")
    event = {"type": "commit", "nonce": "n1", "edits": [edit]}
    monkeypatch.setattr(page_tools, "editor_panel", lambda *a, **k: event)

    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A},
                 ws_editing=True)
    assert at.session_state["edited_files"][A] != (SAMPLES / A).read_bytes()
    assert at.session_state["ws_edit_report"]["applied"] == 1
    assert at.session_state["ws_edit_error"] is None
    assert any("re-run" in s.value for s in at.success)


def test_the_edited_file_is_what_the_sections_measure(run_app, lm):
    """56 polygons in the upload; 55 after one is deleted. A is the first file on the
    page, and its count has to move while B's stays where it was."""
    data, _, _ = _delete_first(lm, A, "M0")
    before = run_app([A, B])
    after = run_app([A, B], edited_files={A: data})
    assert _metric(before, "Polygons", 0) == "56"
    assert _metric(after, "Polygons", 0) == "55"
    assert _metric(before, "Polygons", 1) == _metric(after, "Polygons", 1)


def test_the_edited_file_can_be_downloaded_and_the_upload_restored(run_app, lm):
    data, _, _ = _delete_first(lm, A, "M0")
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A},
                 edited_files={A: data})
    assert [d.label for d in at.get("download_button")] == ["Download edited .gds"]
    at.button(key="ws_revert").click().run()
    assert not _state(at, "edited_files", {}).get(A)
    assert not at.get("download_button")


def test_an_impossible_edit_writes_nothing_and_says_so(run_app, lm, monkeypatch):
    """Atomic: a target that is not in the file must not leave a half-applied GDS."""
    import ui.sections.tools as page_tools

    stale = {"op": "delete", "target": {"layer": "M0", "cell": "nope",
                                        "local_dbu": [[0, 0], [1, 0], [1, 1]],
                                        "dup": 0, "in_top": True, "trans": "r0 0,0"}}
    event = {"type": "commit", "nonce": "n2", "edits": [stale]}
    monkeypatch.setattr(page_tools, "editor_panel", lambda *a, **k: event)

    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A},
                 ws_editing=True)
    assert at.session_state["ws_edit_error"]
    assert not (_state(at, "edited_files") or {}).get(A)
    assert any("Nothing was written" in e.value for e in at.error)


def test_discarding_leaves_the_file_alone(run_app, monkeypatch):
    import ui.sections.tools as page_tools

    monkeypatch.setattr(page_tools, "editor_panel",
                        lambda *a, **k: {"type": "discard", "nonce": "n3"})
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A},
                 ws_editing=True)
    assert not (_state(at, "edited_files") or {}).get(A)
    assert not _state(at, "ws_edit_error")


def test_an_event_is_acted_on_once(run_app, monkeypatch):
    """A component keeps returning its last value. Without the nonce guard the same
    commit would be re-applied on every rerun, and a tool could never be closed."""
    import ui.sections.tools as page_tools

    seen = []

    def editor(*args, **kwargs):
        seen.append(1)
        return {"type": "discard", "nonce": "same"}

    monkeypatch.setattr(page_tools, "editor_panel", editor)
    at = run_app([A, B], gv_focus={"kind": "layout", "key": "lv0", "title": A},
                 ws_editing=True)
    revision = at.session_state["ws_revision"]
    at.run()
    # The second run saw the same event and did nothing with it.
    assert at.session_state["ws_revision"] == revision


def test_a_tool_asked_for_in_the_comparison_appears_there(run_app):
    """The comparison's two halves run on two different files, so a request has to
    carry which viewer asked as well as which file - otherwise the result renders
    inside a collapsed Inspect expander the user is not looking at."""
    at = run_app([A, B], tool_request="Netlist", tool_request_file=B,
                 tool_request_owner="cmp")
    assert at.session_state["tool_owner"] == "cmp"
    assert f"Netlist — {B}" in _headings(at)
    assert "tool_close_cmp" in _keys(at)
    # ...and not under the Inspect viewer for B.
    assert f"tool_close_{B}" not in _keys(at)


def test_the_overlay_can_be_expanded_on_its_own(run_app):
    """The overlay answers "where exactly do they differ?" and lives in an expander,
    which is the smallest place on the page to read it. It gets its own Expand."""
    at = run_app([A, B])
    assert "cmp_overlay_expand" in _keys(at)
    at.button(key="cmp_overlay_expand").click().run()
    assert at.session_state["gv_focus"] == {"kind": "compare", "key": "cmp",
                                            "a": A, "b": B, "overlay": True}
    assert f"### {A} → {B} — overlay" in _headings(at)
    assert [c.key for c in at.chat_input] == ["ws_input"]


def test_the_two_comparison_views_expand_to_different_things(run_app):
    """Expanding the pair opens the pair; expanding the overlay opens the overlay."""
    def expanders(at):
        return [e.label for e in at.get("expander")]

    pair = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp", "a": A, "b": B})
    overlay = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp",
                                        "a": A, "b": B, "overlay": True})
    # The pair view carries the side-by-side's own overlay expander; the overlay view
    # does not, because the overlay is what is already filling the screen.
    assert any("Overlay the two" in label for label in expanders(pair))
    assert not any("Overlay the two" in label for label in expanders(overlay))
    assert "— overlay" in _headings(overlay)
    assert "— overlay" not in _headings(pair)


def test_the_overlay_workspace_chat_answers_about_the_pair(run_app):
    at = run_app([A, B], gv_focus={"kind": "compare", "key": "cmp", "a": A, "b": B,
                                   "overlay": True})
    at.chat_input(key="ws_input").set_value("Which layers changed?").run()
    reply = at.session_state["ws_chat"][-1]["content"]
    assert "M0" in reply and "VIA0" in reply


# --- the landing page, the backdrop and the loading state --------------------

def _html(at) -> str:
    """The rendered bodies, with the stylesheets left out.

    Every class name this page uses also appears in a `<style>` block, so a search
    over all the markdown would find `title-row` on the landing page and `gv-reading`
    on a page that is not reading anything.
    """
    return " ".join(m.value for m in at.markdown if "<style>" not in m.value)


def test_the_landing_page_is_what_an_empty_session_shows(run_app, monkeypatch):
    """No files: the hero over the moving grid, and none of the review furniture."""
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: None)
    at = AppTest.from_file(str(APP), default_timeout=900).run()
    assert not at.exception, [e.value for e in at.exception]
    html = _html(at)
    assert "gv-backdrop" in html            # the animation is on the page
    assert "gv-hero" in html                # ...and so is the hero
    assert html.count("gv-card") >= 4       # the four capability cards
    assert "title-row" not in html          # the masthead belongs to the review page
    assert "is-retiring" not in html        # nothing to fade out yet


def test_the_backdrop_retires_once_a_file_is_open(run_app):
    html = _html(run_app([A, B]))
    assert "is-retiring" in html            # the fade-out plays exactly once
    assert "gv-hero" not in html            # the hero is gone
    assert "title-row" in html              # the masthead has taken its place


def test_the_stylesheets_travel_alone(run_app):
    """Streamlit drops a <style> tag when the same markdown carries other markup, so
    the stylesheet has to be its own element - and an st.empty() slot holds one
    element, so it cannot live in one of those either."""
    at = run_app([A, B])
    sheets = [m.value for m in at.markdown if "gv-backdrop {" in m.value]
    assert len(sheets) == 1, "the backdrop stylesheet must be exactly one element"
    assert "<div" not in sheets[0], "a stylesheet element must carry nothing else"
    for needed in ("gv-hero h1", "gv-reading", "gv-card"):
        assert needed in sheets[0], needed


def test_the_scan_line_is_reserved_only_while_a_new_set_is_read(run_app):
    """It marks the wait and nothing else: a progress indicator that outlives the work
    it described is worse than none.

    The element itself is transient - it is emptied the moment the last document is
    built - so what is asserted here is the decision that gates it. The one signal
    covers both jobs: a changed upload set is exactly when an analysis runs and
    exactly when the page must be returned to the top.
    """
    from ui.landing import READING

    at = run_app([A, B])
    assert at.session_state["gv_scroll_signature"] == f"{A}|{B}"   # it fired
    at.run()
    assert at.session_state["gv_scroll_signature"] == f"{A}|{B}"   # and not again
    # The markup it would render, and the stylesheet that makes it move.
    assert "gv-reading" in READING and "<style>" not in READING
    sheet = next(m.value for m in at.markdown if "gv-backdrop {" in m.value)
    assert "gv-scan" in sheet and "animation: gv-scan" in sheet


def test_the_page_is_returned_to_the_top_when_the_set_changes(run_app):
    at = run_app([A, B])
    assert at.session_state["gv_scroll_signature"] == f"{A}|{B}"
    before = len(at.get("html"))
    at.run()
    # No second scroll script on a rerun of the same set - that would fight anyone
    # who had scrolled down to read.
    assert len(at.get("html")) < before or before == 0


def test_the_backdrop_is_drawn_from_the_sample_layouts(run_app, monkeypatch):
    """The artwork is the tool's own subject, not an illustration of it: every
    polygon came out of a `.gds` in data/samples and every colour out of the `.lyp`.
    A drawing that only looked like a layout could drift from the technology without
    anything failing."""
    from ui.landing import _cell_row

    row, period = _cell_row.__wrapped__()
    assert period > 0
    for layer in ("M0", "NPOLY", "NDIFFCON", "VIA0"):
        assert f'data-layer="{layer}"' in row, layer
    # Outlines, not solids: filled, the backside power rails cover the whole cell.
    assert 'fill="none"' in row and "stroke=" in row
    # The bookkeeping layers are left out, or the cell is a smear.
    for skipped in ("-DUPLICATE", "-PATTERN-CUT", "-EXTENDED"):
        assert skipped not in row, skipped


def test_the_stat_strip_is_measured_not_claimed(monkeypatch):
    """A landing page asserting precision has to demonstrate it, so the figures under
    the hero are read from the bundled cells by the same code the review page uses."""
    from ui.landing import _sample_facts

    html = _sample_facts.__wrapped__()
    assert "45 nm" in html and "gate pitch (CPP)" in html   # the measured CPP
    assert "21 nm" in html and "M0 pitch" in html
    assert "—" not in html          # nothing unmeasurable is ever shown as a dash


def test_every_band_has_geometry_under_it_for_the_whole_slide():
    """The glitch this pins: bands popping in on the right instead of gliding.

    Each band slides left by one repeat of the cell pattern. The window it reads from
    therefore reaches `viewBox + period`, and the drawing has to extend at least that
    far or the band runs off the end - the right-hand strip empties and then fills in
    a single frame when the loop restarts.

    Staggering bands sideways is what broke it: an offset moves that window without
    moving the drawing. Bands are staggered by animation phase instead, so this also
    checks no horizontal offset has crept back in.
    """
    import re

    from ui.landing import _BANDS, _cell_row, _svg

    row, period = _cell_row.__wrapped__()
    assert period > 0
    xs = [float(v) for v in re.findall(r"[ML](-?\d+\.?\d*),", row)]
    assert min(xs) <= 8, "the drawing must start at the left edge"
    assert max(xs) >= 1600 + period, (
        f"the drawing reaches {max(xs):.0f} but a band reads out to "
        f"{1600 + period:.0f} at the end of its slide")

    svg = _svg()
    # Placement translates carry the vertical stack only. A non-zero x here is the bug.
    offsets = {m for m in re.findall(r"translate\((-?[\d.]+),", svg)}
    assert offsets == {"0"}, f"bands must not be offset sideways, found {offsets}"
    # ...and the stagger is there, as phase.
    phases = re.findall(r"--phase:(-?[\d.]+)s", svg)
    assert len(phases) == _BANDS
    assert len({float(p) for p in phases}) > 1, "the bands must not move in lockstep"
