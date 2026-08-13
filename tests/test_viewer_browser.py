"""Drive the viewer in a real browser.

The viewer is JavaScript on a canvas, so Python assertions cannot reach it: nothing
in the analyzer knows whether a click selects the right shape, whether the toolbar
is reachable, or whether the ruler snaps. These tests open the real document in
headless Chromium and interact with it the way a user does.

Skipped when Chromium is not installed, so the suite still runs on a machine that
has not fetched it (`python -m playwright install chromium`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from analyzer.layermap import default_layermap, load_lyp  # noqa: E402
from analyzer.measurements import shape_outlines  # noqa: E402
from ui.viewer import document  # noqa: E402
from ui.viewer_data import build  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "AN2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def html_doc():
    layermap = load_lyp(default_layermap())
    outlines = shape_outlines(GDS, layermap)
    return document(build(outlines, title="AN2D1"), "gv")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:                       # binary not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, html_doc):
    """A fresh page per test, with any JS error failing the test.

    A silent console error is how a canvas app rots: the picture still draws, and
    only one interaction in ten stops working. Collecting them here means a broken
    handler fails a test rather than waiting to be noticed.
    """
    pg = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
          if m.type == "error" else None)
    pg.set_content(html_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(150)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def canvas_point(page, world_x_expr):
    """Page coordinates for a world point, so a test can click real geometry."""
    return page.evaluate("""(expr) => {
      const v = window.GDSViewer.instances.gv;
      const p = eval(expr);
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return {px: c.left + v.sx(p[0]), py: c.top + v.sy(p[1])};
    }""", world_x_expr)


def shape_centre(page, layer_name, index=0):
    return page.evaluate("""([name, i]) => {
      const v = window.GDSViewer.instances.gv;
      const l = v.A.layers.find(x => x.name === name);
      const s = l.shapes[i];
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return {px: c.left + v.sx(s.cx), py: c.top + v.sy(s.cy),
              w: s.w, h: s.h, layer: name};
    }""", [layer_name, index])


def state(page, expr):
    return page.evaluate(f"() => {{ const v = window.GDSViewer.instances.gv; return {expr}; }}")


# --- it renders ------------------------------------------------------------

def test_the_viewer_mounts_and_paints(page):
    assert page.locator("canvas.gv-canvas").count() == 1
    lit = page.evaluate("""() => {
      const c = document.querySelector('canvas.gv-canvas');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 25 || d[i+1] > 30 || d[i+2] > 35) n++;
      return n;
    }""")
    assert lit > 10000, f"canvas looks blank ({lit} lit pixels)"


def test_every_toolbar_button_renders_an_icon(page):
    """The first build used emoji, which render as empty boxes without the font."""
    buttons = page.locator(".gv-btn")
    assert buttons.count() >= 15
    with_icon = page.locator(".gv-btn svg").count()
    text_only = page.locator(".gv-btn:not(:has(svg))").count()
    labelled = page.evaluate("""() => Array.from(document.querySelectorAll('.gv-btn'))
        .filter(b => !b.querySelector('svg') && b.textContent.trim().length).length""")
    assert with_icon >= 15
    assert text_only == labelled, "a button has neither an icon nor a label"


def test_the_toolbar_is_always_visible_not_hover_revealed(page):
    """The Plotly modebar appeared on hover and vanished on the way to it.

    Nothing here may depend on hover, so the toolbar must be visible and clickable
    with the pointer parked far away from it.
    """
    page.mouse.move(5, 700)
    page.wait_for_timeout(80)
    bar = page.locator(".gv-toolbar")
    assert bar.is_visible()
    box = page.locator(".gv-btn").first.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0
    opacity = page.evaluate("() => getComputedStyle(document.querySelector('.gv-toolbar')).opacity")
    assert float(opacity) == 1.0


# --- picking ---------------------------------------------------------------

def test_clicking_a_small_shape_over_a_large_one_selects_the_small_one(page):
    """A gate sits inside a diffusion inside a power rail.

    Ordering the hit test by layer selected whichever layer happened to sort last -
    clicking the gate returned the rail. The smallest containing shape is what the
    user is aiming at.
    """
    target = shape_centre(page, "NPOLY")
    page.mouse.click(target["px"], target["py"])
    page.wait_for_timeout(120)
    picked = state(page, "v.selection && {layer: v.selection.layer, w: v.selection.shape.w}")
    assert picked, "clicking a shape selected nothing"
    assert picked["layer"] == "NPOLY", f"selected {picked['layer']} instead of NPOLY"
    assert abs(picked["w"] - target["w"]) < 1e-9


def test_the_selection_is_reported_with_measured_numbers(page):
    target = shape_centre(page, "NDIFFCON")
    page.mouse.click(target["px"], target["py"])
    page.wait_for_timeout(120)
    text = " ".join(page.locator(".gv-irow").all_inner_texts())
    assert "20 nm" in text, text          # NDIFFCON is 20 nm wide in this technology
    assert "Area" in text and "Centre" in text


def test_clicking_empty_space_selects_nothing(page):
    page.mouse.click(80, 650)             # outside the cell
    page.wait_for_timeout(100)
    assert state(page, "v.selection") is None


# --- navigation ------------------------------------------------------------

def test_wheel_zoom_keeps_the_point_under_the_cursor(page):
    """Zooming must anchor on the cursor, or the thing being inspected slides away."""
    px, py = 640, 360
    before = page.evaluate("""([x, y]) => {
      const v = window.GDSViewer.instances.gv;
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return [v.wx(x - c.left), v.wy(y - c.top)];
    }""", [px, py])
    page.mouse.move(px, py)
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(120)
    after = page.evaluate("""([x, y]) => {
      const v = window.GDSViewer.instances.gv;
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return [v.wx(x - c.left), v.wy(y - c.top)];
    }""", [px, py])
    assert abs(after[0] - before[0]) < 2e-3, (before, after)
    assert abs(after[1] - before[1]) < 2e-3, (before, after)
    assert state(page, "v.scale") > 1


def test_fit_restores_the_whole_cell(page):
    page.mouse.move(640, 360)
    page.mouse.wheel(0, -600)
    page.wait_for_timeout(100)
    zoomed = state(page, "v.scale")
    page.locator(".gv-btn").first.click()      # fit
    page.wait_for_timeout(150)
    assert state(page, "v.scale") < zoomed
    covers = page.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const [x0, y0, x1, y1] = v.worldBounds();
      return v.sx(x0) >= -1 && v.sx(x1) <= v.vw + 1 && v.sy(y1) >= -1 && v.sy(y0) <= v.vh + 1;
    }""")
    assert covers, "fit did not bring the whole layout on screen"


def test_drag_pans_and_records_history(page):
    start = state(page, "[v.cx, v.cy]")
    page.mouse.move(600, 400)
    page.mouse.down()
    page.mouse.move(500, 340, steps=6)
    page.mouse.up()
    page.wait_for_timeout(120)
    moved = state(page, "[v.cx, v.cy]")
    assert abs(moved[0] - start[0]) > 1e-6 or abs(moved[1] - start[1]) > 1e-6
    page.keyboard.press("Backspace")           # previous view
    page.wait_for_timeout(120)
    assert state(page, "v.historyIndex") >= 0


def test_a_drag_does_not_also_select(page):
    """Pan and select share the left button, so a drag must not leave a selection."""
    page.mouse.move(600, 400)
    page.mouse.down()
    page.mouse.move(520, 330, steps=5)
    page.mouse.up()
    page.wait_for_timeout(120)
    assert state(page, "v.selection") is None


# --- layer panel -----------------------------------------------------------

def test_layer_toggle_changes_what_is_drawn_without_a_reload(page):
    before = state(page, "v.visible.size")
    # A layer that starts on: the first row is CELL-BOUNDARY, which starts off, so
    # unchecking it is a no-op and would prove nothing. The index is resolved once -
    # a ":checked" locator is lazy and would point at a different row after the
    # first toggle, so re-checking would tick an unrelated layer.
    index = page.evaluate("""() => Array.from(
        document.querySelectorAll('.gv-lrow input[type=checkbox]')).findIndex(b => b.checked)""")
    box = page.locator(".gv-lrow input[type=checkbox]").nth(index)
    box.uncheck()
    page.wait_for_timeout(100)
    assert state(page, "v.visible.size") == before - 1
    box.check()
    page.wait_for_timeout(100)
    assert state(page, "v.visible.size") == before


def test_solo_isolates_one_layer_and_restores(page):
    before = state(page, "Array.from(v.visible).sort()")
    page.locator(".gv-lrow .gv-solo").nth(3).click()
    page.wait_for_timeout(100)
    assert state(page, "v.visible.size") == 1
    page.locator(".gv-lrow .gv-solo").nth(3).click()
    page.wait_for_timeout(100)
    assert state(page, "Array.from(v.visible).sort()") == before


def test_all_none_and_drawing_buttons(page):
    total = state(page, "v.A.layers.length")
    page.locator(".gv-quick .gv-btn", has_text="All").click()
    page.wait_for_timeout(80)
    assert state(page, "v.visible.size") == total
    page.locator(".gv-quick .gv-btn", has_text="None").click()
    page.wait_for_timeout(80)
    assert state(page, "v.visible.size") == 0
    page.locator(".gv-quick .gv-btn", has_text="Drawing").click()
    page.wait_for_timeout(80)
    assert 0 < state(page, "v.visible.size") < total


def test_the_layer_filter_hides_rows(page):
    page.locator(".gv-search").fill("poly")
    page.wait_for_timeout(120)
    shown = page.evaluate("""() => Array.from(document.querySelectorAll('.gv-lrow'))
        .filter(r => r.style.display !== 'none').length""")
    total = page.locator(".gv-lrow").count()
    assert 0 < shown < total
    names = page.evaluate("""() => Array.from(document.querySelectorAll('.gv-lrow'))
        .filter(r => r.style.display !== 'none').map(r => r.dataset.name)""")
    assert all("POLY" in n.upper() for n in names), names


# --- measurement -----------------------------------------------------------

def test_the_ruler_measures_a_known_distance(page):
    """Two clicks on opposite edges of a shape must report that shape's width.

    Snapping is what makes this exact: without it the answer depends on which pixel
    was clicked, and a ruler that is approximately right is not a measurement.
    """
    geom = page.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const l = v.A.layers.find(x => x.name === 'NPOLY');
      const s = l.shapes[0];
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return {
        left:  {px: c.left + v.sx(s.x),       py: c.top + v.sy(s.cy)},
        right: {px: c.left + v.sx(s.x + s.w), py: c.top + v.sy(s.cy)},
        width: s.w,
      };
    }""")
    page.mouse.move(geom["left"]["px"], geom["left"]["py"])
    page.keyboard.press("r")
    page.wait_for_timeout(80)
    assert state(page, "v.mode") == "ruler"
    page.mouse.click(geom["left"]["px"], geom["left"]["py"])
    page.mouse.move(geom["right"]["px"], geom["right"]["py"])
    page.mouse.click(geom["right"]["px"], geom["right"]["py"])
    page.wait_for_timeout(120)
    measured = state(page, """v.rulers.length ?
        Math.hypot(v.rulers[0].x1 - v.rulers[0].x0, v.rulers[0].y1 - v.rulers[0].y0) : null""")
    assert measured is not None, "no ruler was recorded"
    assert abs(measured - geom["width"]) < 1e-6, (measured, geom["width"])


def test_snapping_lands_on_geometry_and_can_be_turned_off(page):
    """With snapping on, a click near an edge must land exactly on it."""
    near = page.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const s = v.A.layers.find(x => x.name === 'NPOLY').shapes[0];
      return {snapped: v.snap(s.x + 0.0004, s.cy), edge: s.x};
    }""")
    assert near["snapped"]["kind"] in ("edge", "vertex")
    assert abs(near["snapped"]["x"] - near["edge"]) < 1e-9
    page.mouse.move(600, 400)
    page.keyboard.press("s")
    page.wait_for_timeout(80)
    assert state(page, "v.snapOn") is False
    off = page.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const s = v.A.layers.find(x => x.name === 'NPOLY').shapes[0];
      return v.snap(s.x + 0.0004, s.cy);
    }""")
    assert off["kind"] is None


def test_escape_clears_measurements(page):
    page.mouse.move(600, 400)
    page.keyboard.press("r")
    page.mouse.click(500, 400)
    page.mouse.click(600, 430)
    page.wait_for_timeout(100)
    assert state(page, "v.rulers.length") == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert state(page, "v.rulers.length") == 0


# --- keyboard --------------------------------------------------------------

@pytest.mark.parametrize("key,expr,expected", [
    ("g", "v.gridOn", False),
    ("l", "v.labelsOn", False),
    ("o", "v.fillOn", False),
    ("r", "v.mode", "ruler"),
    ("a", "v.mode", "area"),
    ("p", "v.mode", "probe"),
    ("v", "v.mode", "pan"),
])
def test_keyboard_shortcuts(page, key, expr, expected):
    page.mouse.move(600, 400)                 # keys act only over the viewer
    page.keyboard.press(key)
    page.wait_for_timeout(80)
    assert state(page, expr) == expected


def test_keys_are_ignored_while_typing_in_the_filter(page):
    """The layer filter is a text box inside the viewer; typing 'r' in it must not
    switch to the ruler, or the panel becomes unusable."""
    page.locator(".gv-search").click()
    page.locator(".gv-search").type("ruler")
    page.wait_for_timeout(120)
    assert state(page, "v.mode") == "pan"
    assert page.locator(".gv-search").input_value() == "ruler"


# --- comparison ------------------------------------------------------------

@pytest.fixture(scope="module")
def compare_doc():
    from analyzer.xor_diff import xor_compare
    from ui.viewer_data import build_comparison

    layermap = load_lyp(default_layermap())
    a = SAMPLES / "DCAP0_1_RT_4.gds"
    b = SAMPLES / "DCAP0_2_RT_4.gds"
    payload = build_comparison(
        xor_compare(a, b, layermap),
        build(shape_outlines(a, layermap), title=a.name),
        build(shape_outlines(b, layermap), title=b.name),
    )
    payload["names"] = {"a": a.name, "b": b.name}
    return document(payload, "gv")


@pytest.fixture
def cpage(browser, compare_doc):
    pg = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.set_content(compare_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(150)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def test_the_comparison_carries_both_layouts_and_the_regions(cpage):
    info = cpage.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      return {compare: v.compare, hasB: !!v.B, regions: v.data.regions.length,
              layersA: v.A.layers.length, layersB: v.B.layers.length};
    }""")
    assert info["compare"] and info["hasB"]
    assert info["regions"] > 0
    assert info["layersA"] and info["layersB"]


@pytest.mark.parametrize("mode", ["a", "b", "overlay", "xor", "swipe", "blink"])
def test_every_compare_mode_draws(cpage, mode):
    """Each mode must paint. A mode that silently draws nothing looks like a hang."""
    cpage.evaluate(f"() => window.GDSViewer.instances.gv.setCompareMode('{mode}')")
    cpage.wait_for_timeout(150)
    lit = cpage.evaluate("""() => {
      const c = document.querySelector('canvas.gv-canvas');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 25 || d[i+1] > 30 || d[i+2] > 35) n++;
      return n;
    }""")
    assert lit > 5000, f"{mode} drew almost nothing ({lit} lit pixels)"
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.compareMode") == mode


def test_xor_shows_the_differences_over_a_ghosted_layout(cpage):
    """Context is the point: the old difference map showed the regions alone, so a
    finding had to be carried to another tool to see what it sat in."""
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('xor')")
    cpage.wait_for_timeout(150)
    counts = cpage.evaluate("""() => {
      const c = document.querySelector('canvas.gv-canvas');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let red = 0, green = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i] > 90 && d[i+1] < 70) red++;
        if (d[i+1] > 90 && d[i] < 70) green++;
      }
      return {red, green};
    }""")
    assert counts["red"] > 200 and counts["green"] > 200, counts


def test_blink_alternates_and_stops_when_the_mode_changes(cpage):
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('blink')")
    first = cpage.evaluate("() => window.GDSViewer.instances.gv.blinkShowA")
    cpage.wait_for_timeout(800)
    second = cpage.evaluate("() => window.GDSViewer.instances.gv.blinkShowA")
    assert first != second, "blink did not alternate"
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('overlay')")
    cpage.wait_for_timeout(100)
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.blinkTimer") is None


def test_the_swipe_divider_moves_and_is_hidden_otherwise(cpage):
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('swipe')")
    cpage.wait_for_timeout(120)
    assert cpage.locator(".gv-swipe").is_visible()
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('xor')")
    cpage.wait_for_timeout(120)
    assert not cpage.locator(".gv-swipe").is_visible()


def test_the_readout_is_hidden_until_the_pointer_is_over_the_canvas(cpage):
    """An empty readout still drew its border, leaving a stray pill in the corner."""
    assert not cpage.locator(".gv-readout").is_visible()
    cpage.mouse.move(600, 400)
    cpage.wait_for_timeout(120)
    assert cpage.locator(".gv-readout").is_visible()


# --- review surface: markers, nets, tracks, saved views ---------------------
#
# The plain `page` fixture carries geometry only, which is the right default for
# the drawing tests. These tests need the analysis attached, because that is what
# turns rule results into clickable markers and nets into traceable objects.

@pytest.fixture(scope="module")
def rich_doc():
    from analyzer.connectivity import default_stack, extract_nets
    from analyzer.drc import check_layout
    from analyzer.hierarchy import analyze_hierarchy, instance_tree
    from analyzer.pitch import analyze_pitch
    from ui.viewer_data import with_analysis

    layermap = load_lyp(default_layermap())
    outlines = shape_outlines(GDS, layermap)
    payload = with_analysis(
        build(outlines, title="AN2D1"),
        drc=check_layout(outlines),
        connectivity={"nets": extract_nets(GDS, layermap, default_stack(layermap),
                                           collect_shapes=True)},
        pitch=analyze_pitch(outlines, GDS.name),
        hierarchy=analyze_hierarchy(GDS),
        tree=instance_tree(GDS),
    )
    return document(payload, "gv")


@pytest.fixture
def rpage(browser, rich_doc):
    pg = browser.new_page(viewport={"width": 1360, "height": 760})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
          if m.type == "error" else None)
    pg.set_content(rich_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(150)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def open_tab(page, label):
    page.locator(".gv-tab", has_text=label).click()
    page.wait_for_timeout(150)


def test_the_panel_has_a_tab_for_every_review_list(rpage):
    # Each tab reads "Layers 33" - the label plus its count.
    labels = [t.split()[0] for t in rpage.locator(".gv-tab").all_inner_texts()]
    assert labels == ["Layers", "Rules", "Nets", "Cells", "Views"]


def test_the_rules_tab_lists_every_result_with_failures_first(rpage):
    open_tab(rpage, "Rules")
    rows = rpage.locator(".gv-mkr")
    assert rows.count() == len(state(rpage, "v.markers"))
    assert rows.count() > 10                       # the DRM catalogue, not a stub
    first = rows.first.inner_text()
    assert "VIOLATION" in first.upper()


def test_clicking_a_result_isolates_the_layers_it_read_and_zooms(rpage):
    """Cross-probing. A result that cannot be clicked back to the geometry leaves
    the reviewer typing coordinates by hand."""
    open_tab(rpage, "Rules")
    before = state(rpage, "v.scale")
    rpage.locator(".gv-mkr").first.click()
    rpage.wait_for_timeout(250)
    marker = state(rpage, "v.activeMarker")
    assert marker is not None
    named = [n for n in marker["layers"]
             if n in state(rpage, "v.A.layers.map(l => l.name)")]
    assert sorted(state(rpage, "[...v.visible]")) == sorted(named)
    assert state(rpage, "v.scale") > before


def test_stepping_through_results_marks_them_visited(rpage):
    open_tab(rpage, "Rules")
    rpage.locator("canvas.gv-canvas").click(position={"x": 5, "y": 5})
    rpage.keyboard.press("n")
    rpage.wait_for_timeout(150)
    first = state(rpage, "v.activeMarker.id")
    rpage.keyboard.press("n")
    rpage.wait_for_timeout(150)
    second = state(rpage, "v.activeMarker.id")
    assert first != second
    assert state(rpage, "v.visited.size") == 2
    rpage.keyboard.press("Shift+N")
    rpage.wait_for_timeout(150)
    assert state(rpage, "v.activeMarker.id") == first


def test_failures_only_narrows_the_result_list(rpage):
    open_tab(rpage, "Rules")
    everything = rpage.locator(".gv-mkr").count()
    rpage.locator(".gv-check input").check()
    rpage.wait_for_timeout(150)
    failures = rpage.locator(".gv-mkr").count()
    assert 0 < failures < everything
    assert all("VIOLATION" in t.upper()
               for t in rpage.locator(".gv-mkr").all_inner_texts())


def test_waiving_a_result_marks_the_row_without_deleting_it(rpage):
    open_tab(rpage, "Rules")
    rpage.locator(".gv-mkr").first.click()
    rpage.wait_for_timeout(200)
    rpage.locator('.gv-info [data-act="waive"]').click()
    rpage.wait_for_timeout(200)
    assert rpage.locator(".gv-mkr.gv-waived").count() == 1
    assert rpage.locator(".gv-mkr").count() == len(state(rpage, "v.markers"))


def test_clicking_a_net_highlights_every_shape_on_it(rpage):
    open_tab(rpage, "Nets")
    assert rpage.locator(".gv-mkr.gv-net").count() == len(state(rpage, "v.nets"))
    rpage.locator(".gv-mkr.gv-net").first.click()
    rpage.wait_for_timeout(250)
    net = state(rpage, "v.netHighlight")
    assert net and len(net["shapes"]) > 1
    # Every layer the net touches has to be on, or its shapes hide behind a
    # layer that happened to be switched off.
    visible = state(rpage, "[...v.visible]")
    assert all(name in visible for name in net["layers"])


def test_the_net_probe_answers_same_net_or_not(rpage):
    """Two-point connectivity, which is the question a reviewer actually asks."""
    rpage.evaluate("() => window.GDSViewer.instances.gv.setMode('net')")
    rpage.evaluate("() => { window.GDSViewer.instances.gv.traceLock = true; }")
    net = rpage.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const n = v.nets.find(n => n.shapes.length > 1);
      const pt = (o) => { let x = 0, y = 0; for (const p of o) { x += p[0]; y += p[1]; }
                          return [x / o.length, y / o.length]; };
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      const a = pt(n.shapes[0].o), b = pt(n.shapes[1].o);
      return {net: n.net,
              a: {px: c.left + v.sx(a[0]), py: c.top + v.sy(a[1])},
              b: {px: c.left + v.sx(b[0]), py: c.top + v.sy(b[1])}};
    }""")
    rpage.mouse.click(net["a"]["px"], net["a"]["py"])
    rpage.wait_for_timeout(200)
    assert state(rpage, "v.probeA && v.probeA.net") == net["net"]
    rpage.keyboard.down("Shift")
    rpage.mouse.click(net["b"]["px"], net["b"]["py"])
    rpage.keyboard.up("Shift")
    rpage.wait_for_timeout(200)
    assert state(rpage, "v.probeB && v.probeB.net") == net["net"]
    assert "same net" in rpage.locator(".gv-info").inner_text()


def test_the_routing_grid_draws_and_toggles_with_t(rpage):
    assert state(rpage, "v.tracksOn") is False
    before = rpage.evaluate("() => document.querySelector('canvas.gv-canvas').toDataURL()")
    rpage.locator("canvas.gv-canvas").click(position={"x": 5, "y": 5})
    rpage.keyboard.press("t")
    rpage.wait_for_timeout(200)
    assert state(rpage, "v.tracksOn") is True
    assert state(rpage, "Object.keys(v.tracks).length") > 0
    after = rpage.evaluate("() => document.querySelector('canvas.gv-canvas').toDataURL()")
    assert before != after
    rpage.keyboard.press("t")
    rpage.wait_for_timeout(150)
    assert state(rpage, "v.tracksOn") is False


def test_double_clicking_a_shape_measures_its_width_and_height(rpage):
    at = shape_centre(rpage, "PPOLY", 0)
    rpage.mouse.dblclick(at["px"], at["py"])
    rpage.wait_for_timeout(200)
    rulers = state(rpage, "v.rulers")
    assert len(rulers) == 2
    spans = sorted(round(abs(r["x1"] - r["x0"]) + abs(r["y1"] - r["y0"]), 6)
                   for r in rulers)
    assert spans == sorted([round(at["w"], 6), round(at["h"], 6)])


def test_shift_constrains_a_ruler_to_one_axis(rpage):
    rpage.evaluate("() => window.GDSViewer.instances.gv.setMode('ruler')")
    box = rpage.locator("canvas.gv-canvas").bounding_box()
    rpage.mouse.move(box["x"] + 400, box["y"] + 300)
    rpage.mouse.down()
    rpage.mouse.up()
    rpage.wait_for_timeout(100)
    rpage.keyboard.down("Shift")
    rpage.mouse.move(box["x"] + 560, box["y"] + 318)
    rpage.wait_for_timeout(150)
    pending = state(rpage, "v.pending")
    rpage.keyboard.up("Shift")
    assert pending is not None
    assert pending["y1"] == pending["y0"]          # the long axis wins
    assert pending["x1"] != pending["x0"]


def test_a_measurement_can_be_deleted_from_the_list(rpage):
    at = shape_centre(rpage, "PPOLY", 0)
    rpage.mouse.dblclick(at["px"], at["py"])
    rpage.wait_for_timeout(200)
    assert len(state(rpage, "v.rulers")) == 2
    rpage.locator(".gv-info [data-del]").first.click()
    rpage.wait_for_timeout(150)
    assert len(state(rpage, "v.rulers")) == 1


def test_a_view_can_be_copied_and_restored(rpage):
    rpage.evaluate("() => { const v = window.GDSViewer.instances.gv; v.zoomBy(2.2); }")
    rpage.evaluate("() => { const v = window.GDSViewer.instances.gv; v.visible = new Set(['PPOLY']); v.draw(); }")
    rpage.wait_for_timeout(120)
    token = rpage.evaluate("() => window.GDSViewer.instances.gv.copyState()")
    scale = state(rpage, "v.scale")
    rpage.evaluate("() => window.GDSViewer.instances.gv.fit()")
    rpage.wait_for_timeout(120)
    assert state(rpage, "v.scale") != scale
    assert rpage.evaluate("(t) => window.GDSViewer.instances.gv.applyState(t)", token) is True
    rpage.wait_for_timeout(120)
    assert state(rpage, "v.scale") == scale
    assert state(rpage, "[...v.visible]") == ["PPOLY"]


def test_a_bad_view_string_is_refused_not_applied(rpage):
    scale = state(rpage, "v.scale")
    assert rpage.evaluate("() => window.GDSViewer.instances.gv.applyState('nonsense')") is False
    assert state(rpage, "v.scale") == scale
    assert "copied view" in rpage.locator(".gv-toast").inner_text()


def test_escape_clears_the_highlights_as_well_as_the_rulers(rpage):
    open_tab(rpage, "Rules")
    rpage.locator(".gv-mkr").first.click()
    rpage.wait_for_timeout(200)
    assert state(rpage, "v.activeMarker") is not None
    rpage.locator("canvas.gv-canvas").click(position={"x": 5, "y": 5})
    rpage.keyboard.press("Escape")
    rpage.wait_for_timeout(200)
    assert state(rpage, "v.activeMarker") is None
    assert state(rpage, "v.netHighlight") is None


def test_a_flat_layout_says_so_in_the_cells_tab(rpage):
    open_tab(rpage, "Cells")
    assert state(rpage, "v.tree.flat") is True
    assert rpage.locator(".gv-mkr.gv-cell").count() == 1
    assert "flat" in rpage.locator(".gv-pbody .gv-hint").inner_text()


# --- cell tree on a hierarchical layout -------------------------------------

@pytest.fixture(scope="module")
def hier_gds(tmp_path_factory):
    """A small hierarchical layout, because every bundled sample is flat.

    2×2 array of MID, each holding two LEAF copies, one of them rotated: 4 + 8 =
    12 placements over two levels, with a rotation to prove the transforms are
    accumulated rather than assumed.
    """
    import klayout.db as db

    path = tmp_path_factory.mktemp("hier") / "hier.gds"
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(1, 0)
    leaf = layout.create_cell("LEAF")
    leaf.shapes(li).insert(db.Box(0, 0, 100, 50))
    mid = layout.create_cell("MID")
    mid.insert(db.CellInstArray(leaf.cell_index(), db.Trans(db.Vector(0, 0))))
    mid.insert(db.CellInstArray(leaf.cell_index(),
                                db.Trans(db.Trans.R90, db.Vector(200, 0))))
    top = layout.create_cell("TOP")
    top.insert(db.CellInstArray(mid.cell_index(), db.Trans(db.Vector(0, 0)),
                                db.Vector(400, 0), db.Vector(0, 300), 2, 2))
    layout.write(str(path))
    return path


@pytest.fixture(scope="module")
def hier_doc(hier_gds):
    from analyzer.hierarchy import analyze_hierarchy, instance_tree
    from ui.viewer_data import with_analysis

    payload = with_analysis(build(shape_outlines(hier_gds, None), title="TOP"),
                            hierarchy=analyze_hierarchy(hier_gds),
                            tree=instance_tree(hier_gds))
    return document(payload, "gv")


@pytest.fixture
def hpage(browser, hier_doc):
    pg = browser.new_page(viewport={"width": 1360, "height": 760})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
          if m.type == "error" else None)
    pg.set_content(hier_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(150)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def test_the_cell_tree_lists_definitions_and_placements(hpage):
    open_tab(hpage, "Cells")
    assert state(hpage, "v.tree.cells.length") == 3
    assert state(hpage, "v.tree.placements.length") == 12
    assert state(hpage, "v.tree.maxDepth") == 2
    rows = hpage.locator(".gv-mkr.gv-cell")
    assert rows.count() == 15                      # 3 definitions + 12 placements
    assert "top" in rows.first.inner_text()


def test_clicking_a_placement_zooms_to_that_copy_and_reports_it(hpage):
    open_tab(hpage, "Cells")
    before = state(hpage, "v.scale")
    hpage.locator(".gv-mkr.gv-cell", has_text="R90").first.click()
    hpage.wait_for_timeout(250)
    placement = state(hpage, "v.activePlacement")
    assert placement["cell"] == "LEAF"
    assert placement["orient"] == "R90"
    assert state(hpage, "v.scale") > before
    assert state(hpage, "v.cellBoxesOn") is True
    info = hpage.locator(".gv-info").inner_text()
    assert "TOP/MID/LEAF" in info and "R90" in info
    # A rotated 100 × 50 box measures 50 × 100 where it lands.
    assert "50 nm × 100 nm" in info


def test_the_depth_slider_limits_the_instance_boxes_drawn(hpage):
    open_tab(hpage, "Cells")
    hpage.locator(".gv-quick .gv-btn", has_text="Boxes").click()
    hpage.wait_for_timeout(200)
    assert state(hpage, "v.cellBoxesOn") is True
    deep = hpage.evaluate("() => document.querySelector('canvas.gv-canvas').toDataURL()")
    hpage.locator(".gv-depth input").fill("1")
    hpage.wait_for_timeout(250)
    assert state(hpage, "v.depthLimit") == 1
    shallow = hpage.evaluate("() => document.querySelector('canvas.gv-canvas').toDataURL()")
    assert deep != shallow


def test_h_toggles_the_instance_boxes(hpage):
    hpage.locator("canvas.gv-canvas").click(position={"x": 5, "y": 5})
    hpage.keyboard.press("h")
    hpage.wait_for_timeout(200)
    assert state(hpage, "v.cellBoxesOn") is True
    hpage.keyboard.press("h")
    hpage.wait_for_timeout(200)
    assert state(hpage, "v.cellBoxesOn") is False


def test_fit_top_drops_the_instance_selection(hpage):
    open_tab(hpage, "Cells")
    hpage.locator(".gv-mkr.gv-cell", has_text="R90").first.click()
    hpage.wait_for_timeout(200)
    assert state(hpage, "v.activePlacement") is not None
    hpage.locator(".gv-quick .gv-btn", has_text="Fit top").click()
    hpage.wait_for_timeout(200)
    assert state(hpage, "v.activePlacement") is None
    assert hpage.locator(".gv-mkr.gv-cell.gv-soloed").count() == 0


# --- the comparison's difference browser ------------------------------------

def test_the_comparison_opens_on_the_difference_list(cpage):
    labels = [t.split()[0] for t in cpage.locator(".gv-tab").all_inner_texts()]
    assert labels == ["Diffs", "Layers", "Views"]
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.tab") == "diffs"
    rows = cpage.locator(".gv-mkr.gv-diff")
    assert rows.count() == cpage.evaluate("() => window.GDSViewer.instances.gv.data.regions.length")
    assert rows.count() > 0


def test_the_difference_list_is_ordered_largest_first(cpage):
    areas = cpage.evaluate("() => window.GDSViewer.instances.gv.orderedRegions().map(r => r.a)")
    assert areas == sorted(areas, reverse=True)


def test_clicking_a_difference_zooms_to_it_and_reports_it(cpage):
    before = cpage.evaluate("() => window.GDSViewer.instances.gv.scale")
    cpage.locator(".gv-mkr.gv-diff").first.click()
    cpage.wait_for_timeout(300)
    region = cpage.evaluate("() => window.GDSViewer.instances.gv.activeRegion")
    assert region is not None
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.scale") > before
    info = cpage.locator(".gv-info").inner_text()
    assert region["layer"] in info
    assert "netlist" in info                       # geometric difference, not intent


def test_a_difference_forces_a_mode_that_can_show_it(cpage):
    """"Only in A" cannot be seen while only B is drawn."""
    cpage.evaluate("() => window.GDSViewer.instances.gv.setCompareMode('b')")
    cpage.locator(".gv-mkr.gv-diff").first.click()
    cpage.wait_for_timeout(250)
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.compareMode") == "overlay"


def test_n_steps_through_the_differences_in_the_comparison(cpage):
    cpage.locator("canvas.gv-canvas").click(position={"x": 6, "y": 6})
    cpage.keyboard.press("n")
    cpage.wait_for_timeout(200)
    first = cpage.evaluate("() => window.GDSViewer.instances.gv.activeRegion")
    cpage.keyboard.press("n")
    cpage.wait_for_timeout(200)
    second = cpage.evaluate("() => window.GDSViewer.instances.gv.activeRegion")
    assert first != second
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.visitedRegions.size") == 2
    assert cpage.locator(".gv-mkr.gv-diff.gv-soloed").count() == 1


def test_fit_all_frames_every_difference(cpage):
    cpage.locator(".gv-mkr.gv-diff").first.click()
    cpage.wait_for_timeout(250)
    cpage.locator(".gv-quick .gv-btn", has_text="Fit all").click()
    cpage.wait_for_timeout(250)
    assert cpage.evaluate("() => window.GDSViewer.instances.gv.activeRegion") is None
    covered = cpage.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      return v.data.regions.every(r => r.o.every(
        p => p[0] >= v.wx(0) && p[0] <= v.wx(v.vw) && p[1] >= v.wy(v.vh) && p[1] <= v.wy(0)));
    }""")
    assert covered


def test_area_units_are_not_mangled_by_the_row_styling(cpage):
    """`text-transform: uppercase` turned µm² into MM², a different unit."""
    text = cpage.locator(".gv-mkr.gv-diff").first.inner_text()
    assert "MM²" not in text
    assert "nm²" in text or "µm²" in text


# --- find shapes by measured size ------------------------------------------

@pytest.mark.parametrize("query,expected_layers", [
    ("w=15", {"NPOLY", "PPOLY", "N-VIAG", "P-VIAG"}),
    ("h>50", {"BM0", "M1"}),
])
def test_the_finder_matches_the_measured_dimensions(page, query, expected_layers):
    """The query runs over the analyzer's numbers, so the hits are checkable.

    These counts were confirmed against a separate pass over the same payload in
    Python; the point of the feature is that "which shapes are 15 nm wide?" needs
    no DRC script.
    """
    page.locator(".gv-find").fill(query)
    page.wait_for_timeout(200)
    hits = state(page, "v.findHits")
    assert hits, f"{query} found nothing"
    assert {h["layer"] for h in hits} == expected_layers
    assert query.replace("=", "").replace(">", "") or True
    assert f"{len(hits)} hit" in page.locator(".gv-fcount").inner_text()


def test_the_finder_only_searches_visible_layers(page):
    page.locator(".gv-find").fill("w<21")
    page.wait_for_timeout(200)
    before = len(state(page, "v.findHits"))
    page.evaluate("() => { const v = window.GDSViewer.instances.gv; v.visible = new Set(['NPOLY']); }")
    page.locator(".gv-find").fill("w<21 ")
    page.wait_for_timeout(200)
    after = state(page, "v.findHits")
    assert 0 < len(after) < before
    assert {h["layer"] for h in after} == {"NPOLY"}


def test_enter_steps_through_the_hits_and_reports_each_one(page):
    page.locator(".gv-find").fill("w=15")
    page.wait_for_timeout(200)
    page.locator(".gv-find").press("Enter")
    page.wait_for_timeout(250)
    assert state(page, "v.findIndex") == 0
    # The hit is centred and wholly on screen. Scale is the wrong assertion: a hit
    # taller than the current view has to be zoomed out to, not in on.
    framing = page.evaluate("""() => {
      const v = window.GDSViewer.instances.gv;
      const s = v.findHits[v.findIndex].shape;
      return {dx: Math.abs(v.cx - s.cx), dy: Math.abs(v.cy - s.cy),
              onScreen: v.sx(s.x) > 0 && v.sx(s.x + s.w) < v.vw &&
                        v.sy(s.y + s.h) > 0 && v.sy(s.y) < v.vh};
    }""")
    assert framing["onScreen"]
    assert framing["dx"] < 1e-6 and framing["dy"] < 1e-6
    first = state(page, "v.selection.shape")
    assert round(first["w"] * 1000, 3) == 15         # the measured width, not a guess
    page.locator(".gv-find").press("Enter")
    page.wait_for_timeout(250)
    assert state(page, "v.findIndex") == 1
    assert "15 nm" in page.locator(".gv-info").inner_text()


def test_a_nonsense_query_finds_nothing_and_says_so(page):
    page.locator(".gv-find").fill("wibble")
    page.wait_for_timeout(200)
    assert state(page, "v.findHits") == []
    assert page.locator(".gv-fcount").inner_text() == "?"


def test_clearing_the_query_clears_the_highlights(page):
    page.locator(".gv-find").fill("w<21")
    page.wait_for_timeout(200)
    assert state(page, "v.findHits")
    page.locator(".gv-find").press("Escape")
    page.wait_for_timeout(200)
    assert state(page, "v.findHits") == []
    assert page.locator(".gv-fcount").inner_text() == ""


def test_typing_a_query_does_not_trigger_the_shortcuts(page):
    """`w` and `a` are tool keys; typing them in the find box must not switch tools."""
    mode = state(page, "v.mode")
    page.locator(".gv-find").fill("a<300")
    page.wait_for_timeout(200)
    assert state(page, "v.mode") == mode


# --- the editor -------------------------------------------------------------
#
# The editor's contract is that its preview equals what KLayout writes. These tests
# drive the real tools and then check the journal they produce; the round-trip test
# at the end applies that journal for real and compares the result polygon by
# polygon, which is the only assertion that can catch the two sides drifting apart.

@pytest.fixture(scope="module")
def edit_doc():
    from analyzer.techparams import tech_parameters
    from ui.viewer_data import editable_payload, with_analysis

    layermap = load_lyp(default_layermap())
    outlines = shape_outlines(GDS, layermap, include_identity=True)
    payload = with_analysis(
        build(outlines, title="AN2D1"),
        editable=editable_payload(outlines, layermap, tech_parameters(GDS, layermap)))
    return document(payload, "gv")


@pytest.fixture
def epage(browser, edit_doc):
    pg = browser.new_page(viewport={"width": 1400, "height": 800})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
          if m.type == "error" else None)
    pg.set_content(edit_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    # Snapping to edges and tracks is right for a user and wrong for a test: it
    # moves the pointer's coordinates by design, so a drag no longer asserts what
    # it drew. Each test that needs exact coordinates turns them off.
    pg.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.snapOn = false; v.editState.trackSnap = false; }""")
    pg.wait_for_timeout(120)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def journal(page):
    return page.evaluate("() => window.GDSViewer.instances.gv.buildJournal()")


def test_the_editor_only_appears_when_the_layout_may_be_edited(page, epage):
    assert state(page, "!!v.edit") is False
    assert page.locator(".gv-tab", has_text="Edit").count() == 0
    assert state(epage, "!!v.edit") is True
    assert epage.locator(".gv-tab", has_text="Edit").count() == 1


def test_only_the_technologys_own_layers_can_be_drawn_on(epage):
    epage.locator(".gv-tab", has_text="Edit").click()
    epage.wait_for_timeout(150)
    options = epage.locator(".gv-erow select").first.locator("option").all_inner_texts()
    assert "M0" in options and "NPOLY" in options
    # A layer with no number cannot be written to a GDSII, so it is not offered.
    assert all(not name.startswith("layer_") for name in options)


def test_dragging_a_rectangle_creates_one_insert_at_the_drawn_size(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.setMode('rect'); v.editState.layer = 'M2'; v.editState.gridNm = 1; }""")
    box = epage.locator("canvas.gv-canvas").bounding_box()
    epage.mouse.move(box["x"] + 380, box["y"] + 300)
    epage.mouse.down()
    epage.mouse.move(box["x"] + 480, box["y"] + 360, steps=6)
    epage.mouse.up()
    epage.wait_for_timeout(200)
    ops = journal(epage)
    assert len(ops) == 1
    assert ops[0]["op"] == "insert" and ops[0]["layer"] == "M2"
    assert len(ops[0]["points"]) == 4
    drawn = state(epage, "v.selected[0].shape")
    # Every vertex is on the chosen grid: 1 nm here, and that is what gets written.
    for x, y in ops[0]["points"]:
        assert abs(round(x * 1000) - x * 1000) < 1e-6
        assert abs(round(y * 1000) - y * 1000) < 1e-6
    assert drawn["w"] > 0 and drawn["h"] > 0


def test_a_new_shape_is_marked_as_not_yet_written(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.editState.layer = 'M2';
      v.addShape('M2', [[0.2,0.2],[0.2,0.23],[0.24,0.23],[0.24,0.2]]); }""")
    epage.wait_for_timeout(150)
    assert state(epage, "v.A.layers.find(l=>l.name==='M2').shapes.some(s=>s._new)")
    assert journal(epage)[0]["op"] == "insert"


def test_moving_a_saved_shape_becomes_a_replace_naming_its_cell(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const row = v.A.layers.find(l => l.name === 'NPOLY');
      v.selected = [{layer: 'NPOLY', shape: row.shapes[0]}];
      v.moveSelection(0.01, 0, true); }""")
    epage.wait_for_timeout(150)
    ops = journal(epage)
    assert [op["op"] for op in ops] == ["replace"]
    target = ops[0]["target"]
    assert target["cell"] == "AN2D1"
    assert target["dup"] == 0
    assert target["trans"] == "r0 *1 0,0"
    # The identity is the *original* outline: that is what the file still holds.
    assert all(isinstance(v, int) for point in target["local_dbu"] for v in point)


def test_deleting_a_saved_shape_keeps_its_identity_for_the_journal(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const row = v.A.layers.find(l => l.name === 'NDIFFCON');
      v.selected = [{layer: 'NDIFFCON', shape: row.shapes[0]}];
      v.deleteSelection(); }""")
    epage.wait_for_timeout(150)
    ops = journal(epage)
    assert [op["op"] for op in ops] == ["delete"]
    assert ops[0]["target"]["cell"] == "AN2D1"
    # It is gone from the drawing but still in the payload, or the delete could not
    # be described to Python at all.
    assert state(epage, "v.A.layers.find(l=>l.name==='NDIFFCON').shapes[0]._del") is True


def test_undo_and_redo_walk_the_journal(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.addShape('M2', [[0.3,0.3],[0.3,0.32],[0.34,0.32],[0.34,0.3]]); }""")
    epage.wait_for_timeout(120)
    assert len(journal(epage)) == 1
    epage.evaluate("() => window.GDSViewer.instances.gv.editUndo()")
    epage.wait_for_timeout(120)
    assert journal(epage) == []
    epage.evaluate("() => window.GDSViewer.instances.gv.editRedo()")
    epage.wait_for_timeout(120)
    assert len(journal(epage)) == 1


def test_ten_moves_of_one_shape_are_one_replacement(epage):
    """The journal is a state difference, not a recording of what was done."""
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const row = v.A.layers.find(l => l.name === 'NPOLY');
      v.selected = [{layer: 'NPOLY', shape: row.shapes[0]}];
      for (let i = 0; i < 10; i++) v.moveSelection(0.001, 0, true); }""")
    epage.wait_for_timeout(200)
    ops = journal(epage)
    assert len(ops) == 1 and ops[0]["op"] == "replace"


def test_a_shape_drawn_and_then_deleted_leaves_nothing_to_apply(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const shape = v.addShape('M2', [[0.4,0.4],[0.4,0.42],[0.44,0.42],[0.44,0.4]]);
      v.selected = [{layer: 'M2', shape: shape}];
      v.deleteSelection(); }""")
    epage.wait_for_timeout(150)
    assert journal(epage) == []


def test_delete_needs_a_selection_and_says_so(epage):
    epage.evaluate("() => { const v = window.GDSViewer.instances.gv; v.setMode('edit'); v.selected = []; }")
    epage.locator("canvas.gv-canvas").click(position={"x": 5, "y": 5})
    epage.keyboard.press("Delete")
    epage.wait_for_timeout(150)
    assert journal(epage) == []
    assert "Nothing selected" in epage.locator(".gv-toast").inner_text()


def test_a_vertex_handle_reshapes_the_selected_shape(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.setMode('edit');
      const row = v.A.layers.find(l => l.name === 'M0');
      v.selected = [{layer: 'M0', shape: row.shapes[0]}];
      v.buildHandles(); }""")
    epage.wait_for_timeout(120)
    assert state(epage, "v.editState.handles.length") >= 4
    before = state(epage, "v.selected[0].shape.o.map(p => p.join(','))")
    handle = epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const h = v.editState.handles[0];
      const c = document.querySelector('canvas.gv-canvas').getBoundingClientRect();
      return {px: c.left + v.sx(h.x), py: c.top + v.sy(h.y)}; }""")
    epage.mouse.move(handle["px"], handle["py"])
    epage.mouse.down()
    epage.mouse.move(handle["px"] + 30, handle["py"] - 20, steps=5)
    epage.mouse.up()
    epage.wait_for_timeout(200)
    ops = journal(epage)
    assert [op["op"] for op in ops] == ["replace"]
    # A rectangle's corner can move without changing the bounding box, so the
    # outline is what has to differ - and it still has four points.
    after = state(epage, "v.selected[0].shape.o.map(p => p.join(','))")
    assert after != before
    assert len(after) == 4
    assert sum(1 for a, b in zip(after, before) if a != b) == 1


def test_rotation_keeps_the_shape_on_its_own_centre(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const row = v.A.layers.find(l => l.name === 'NPOLY');
      v.selected = [{layer: 'NPOLY', shape: row.shapes[0]}]; }""")
    before = state(epage, "({w: v.selected[0].shape.w, h: v.selected[0].shape.h, "
                          "cx: v.selected[0].shape.cx, cy: v.selected[0].shape.cy})")
    epage.evaluate("() => window.GDSViewer.instances.gv.transformSelection(90, false)")
    epage.wait_for_timeout(150)
    after = state(epage, "({w: v.selected[0].shape.w, h: v.selected[0].shape.h, "
                         "cx: v.selected[0].shape.cx, cy: v.selected[0].shape.cy})")
    assert abs(after["w"] - before["h"]) < 1e-6
    assert abs(after["h"] - before["w"]) < 1e-6
    assert abs(after["cx"] - before["cx"]) < 1e-6
    assert abs(after["cy"] - before["cy"]) < 1e-6


def test_a_rubber_band_selects_what_it_encloses(epage):
    epage.evaluate("() => { const v = window.GDSViewer.instances.gv; v.setMode('edit'); v.fit(); }")
    epage.wait_for_timeout(150)
    box = epage.locator("canvas.gv-canvas").bounding_box()
    epage.mouse.move(box["x"] + 60, box["y"] + 60)
    epage.mouse.down()
    epage.mouse.move(box["x"] + 700, box["y"] + 600, steps=8)
    epage.mouse.up()
    epage.wait_for_timeout(200)
    assert state(epage, "v.selected.length") > 1


def test_the_wire_tool_draws_a_shape_of_the_layers_measured_width(epage):
    width = epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.setMode('wire'); v.editState.layer = 'M0'; v.editState.trackSnap = false;
      v.editState.pending = {kind: 'wire', points: [[0.02, 0.06], [0.12, 0.06]]};
      v.finishWire();
      return v.measuredWidthNm('M0'); }""")
    epage.wait_for_timeout(200)
    ops = journal(epage)
    assert len(ops) == 1 and ops[0]["op"] == "insert"
    shape = state(epage, "v.selected[0].shape")
    assert abs(shape["h"] * 1000 - width) < 1e-6      # the width it measured, not a guess
    assert abs(shape["w"] - 0.1) < 1e-6


def test_a_label_becomes_an_insert_text(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.addLabel('M1', 'NEWPIN', {x: 0.07, y: 0.065}); }""")
    epage.wait_for_timeout(150)
    ops = journal(epage)
    assert ops[0]["op"] == "insert_text"
    assert ops[0]["text"] == "NEWPIN" and ops[0]["at_um"] == [0.07, 0.065]


def test_the_edit_tab_lists_the_pending_changes(epage):
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.addShape('M2', [[0.5,0.5],[0.5,0.52],[0.54,0.52],[0.54,0.5]]);
      v.addLabel('M1', 'X', {x: 0.1, y: 0.1}); }""")
    epage.locator(".gv-tab", has_text="Edit").click()
    epage.wait_for_timeout(200)
    # inner_text returns what CSS renders, and the section heading is uppercased.
    text = epage.locator(".gv-pbody").inner_text().lower()
    assert "2 pending change" in text
    assert epage.locator(".gv-mkr.gv-cell").count() == 2


def test_a_read_only_viewer_refuses_to_apply(epage):
    """The document mount has nowhere to send a journal, and says so."""
    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.addShape('M2', [[0.6,0.6],[0.6,0.62],[0.64,0.62],[0.64,0.6]]);
      v.commitEdits(); }""")
    epage.wait_for_timeout(200)
    assert "read-only" in epage.locator(".gv-toast").inner_text()


def test_the_measured_width_is_offered_as_a_measurement_not_a_rule(epage):
    epage.locator(".gv-tab", has_text="Edit").click()
    epage.wait_for_timeout(150)
    text = epage.locator(".gv-pbody").inner_text()
    assert "measurement, not a rule" in text


def test_what_the_editor_previews_is_what_klayout_writes(epage, tmp_path):
    """The one test that can catch the browser and the file drifting apart.

    Six different edits, applied for real, then compared polygon by polygon against
    what the browser was showing when the journal was built.
    """
    from analyzer.edit import apply_edits

    epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      v.editState.layer = 'M1';
      v.addShape('M1', [[0.05,0.05],[0.05,0.08],[0.09,0.08],[0.09,0.05]]);
      v.addShape('M2', [[0.10,0.02],[0.10,0.05],[0.14,0.05],[0.14,0.02]]);
      const np = v.A.layers.find(l => l.name === 'NPOLY');
      v.selected = [{layer: 'NPOLY', shape: np.shapes[0]}];
      v.moveSelection(0.005, 0.010, true);
      const nd = v.A.layers.find(l => l.name === 'NDIFFCON');
      v.selected = [{layer: 'NDIFFCON', shape: nd.shapes[0]}];
      v.deleteSelection();
      const m0 = v.A.layers.find(l => l.name === 'M0');
      v.selected = [{layer: 'M0', shape: m0.shapes[0]}];
      v.reshape(v.selected[0], m0.shapes[0].o.map(p => [p[0], p[1] + 0.002]));
      v.addLabel('M1', 'ROUNDTRIP', {x: 0.07, y: 0.065}); }""")
    epage.wait_for_timeout(250)

    ops = journal(epage)
    assert sorted(op["op"] for op in ops) == [
        "delete", "insert", "insert", "insert_text", "replace", "replace"]
    preview = epage.evaluate("""() => { const v = window.GDSViewer.instances.gv;
      const out = {};
      for (const l of v.A.layers) out[l.name] = l.shapes.filter(s => !s._del).map(s => s.o);
      return out; }""")

    layermap = load_lyp(default_layermap())
    out = tmp_path / "roundtrip.gds"
    report = apply_edits(GDS, ops, out, layermap=layermap)
    assert report["applied"] == len(ops)
    assert report["refused"] == []

    written = shape_outlines(out, layermap)
    for row in written["layers"]:
        got = sorted(tuple(map(tuple, s["outline_um"])) for s in row["shapes"])
        want = sorted(tuple(map(tuple, o)) for o in preview.get(row["name"], []))
        assert got == want, f"{row['name']} differs between the preview and the file"
    assert any(l["text"] == "ROUNDTRIP"
               for row in written["layers"] for l in row["labels"])


# --- the side-by-side comparison: two drawings, one layer panel --------------

@pytest.fixture(scope="module")
def dual_doc():
    from analyzer.xor_diff import xor_compare
    from ui.viewer_data import build_comparison

    layermap = load_lyp(default_layermap())
    a = SAMPLES / "DCAP0_1_RT_4.gds"
    b = SAMPLES / "DCAP0_2_RT_4.gds"
    payload = build_comparison(
        xor_compare(a, b, layermap),
        build(shape_outlines(a, layermap), title=a.name),
        build(shape_outlines(b, layermap), title=b.name),
    )
    payload["names"] = {"a": a.name, "b": b.name}
    return document(payload, "gv", dual=True)


@pytest.fixture
def dpage(browser, dual_doc):
    pg = browser.new_page(viewport={"width": 1600, "height": 820})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
          if m.type == "error" else None)
    pg.set_content(dual_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(200)
    yield pg
    assert not errors, "\n".join(errors)
    pg.close()


def side(page, index, name):
    return page.evaluate(
        f"() => window.GDSViewer.instances.gv.halves[{index}].viewer.visible.has('{name}')")


def scale(page, index):
    return page.evaluate(
        f"() => window.GDSViewer.instances.gv.halves[{index}].viewer.scale")


def test_a_is_left_b_is_centre_and_the_panel_is_on_the_right(dpage):
    halves = [dpage.locator(".gv-half").nth(i).bounding_box() for i in (0, 1)]
    panel = dpage.locator(".gv-shared").bounding_box()
    titles = dpage.locator(".gv-htitle").all_inner_texts()
    assert "A — Reference" in titles[0] and "DCAP0_1_RT_4.gds" in titles[0]
    assert "B — Revision" in titles[1] and "DCAP0_2_RT_4.gds" in titles[1]
    assert halves[0]["x"] < halves[1]["x"] < panel["x"]
    # The drawings get the space; the panel is clearly smaller than either.
    assert abs(halves[0]["width"] - halves[1]["width"]) < 2
    assert panel["width"] < halves[0]["width"] / 2
    assert halves[0]["x"] + halves[0]["width"] <= halves[1]["x"] + 1


def test_there_is_exactly_one_layer_panel(dpage):
    assert dpage.locator(".gv-shared").count() == 1
    assert dpage.locator(".gv-half .gv-panel").count() == 0
    assert dpage.locator(".gv-lrow").count() == dpage.locator(".gv-shared .gv-lrow").count()


def test_one_checkbox_hides_a_layer_in_both_drawings(dpage):
    row = dpage.locator(".gv-shared .gv-lrow", has_text="NPOLY").first
    box = row.locator("input[type=checkbox]")
    assert side(dpage, 0, "NPOLY") and side(dpage, 1, "NPOLY")
    box.uncheck()
    dpage.wait_for_timeout(150)
    assert not side(dpage, 0, "NPOLY") and not side(dpage, 1, "NPOLY")
    box.check()
    dpage.wait_for_timeout(150)
    assert side(dpage, 0, "NPOLY") and side(dpage, 1, "NPOLY")


@pytest.mark.parametrize("button,expected", [("None", 0), ("All", "every")])
def test_all_and_none_act_on_both(dpage, button, expected):
    dpage.locator(".gv-shared .gv-btn", has_text=button).click()
    dpage.wait_for_timeout(200)
    sizes = [dpage.evaluate(f"() => window.GDSViewer.instances.gv.halves[{i}].viewer.visible.size")
             for i in (0, 1)]
    if expected == 0:
        assert sizes == [0, 0]
    else:
        counts = [dpage.evaluate(f"() => window.GDSViewer.instances.gv.halves[{i}].viewer.A.layers.length")
                  for i in (0, 1)]
        assert sizes == counts


def test_drawing_acts_on_both_and_keeps_its_meaning(dpage):
    dpage.locator(".gv-shared .gv-btn", has_text="Drawing").click()
    dpage.wait_for_timeout(200)
    for index in (0, 1):
        derived_on = dpage.evaluate(f"""() => {{
          const v = window.GDSViewer.instances.gv.halves[{index}].viewer;
          return v.A.layers.filter(l => l.role === 'derived' && v.visible.has(l.name)).length;
        }}""")
        assert derived_on == 0


def test_zoom_and_pan_stay_independent(dpage):
    before = [scale(dpage, 0), scale(dpage, 1)]
    dpage.evaluate("() => window.GDSViewer.instances.gv.halves[0].viewer.zoomBy(2.5)")
    dpage.wait_for_timeout(150)
    assert scale(dpage, 0) > before[0]
    assert scale(dpage, 1) == before[1], "zooming A moved B"
    dpage.evaluate("""() => { const v = window.GDSViewer.instances.gv.halves[1].viewer;
      v.cx += 0.05; v.draw(); }""")
    dpage.wait_for_timeout(150)
    assert scale(dpage, 0) != scale(dpage, 1)


def test_the_filter_hides_rows_without_touching_visibility(dpage):
    row = dpage.locator(".gv-shared .gv-lrow", has_text="NPOLY").first
    row.locator("input[type=checkbox]").uncheck()
    dpage.wait_for_timeout(150)
    search = dpage.locator(".gv-shared .gv-search")
    search.fill("M1")
    dpage.wait_for_timeout(200)
    assert dpage.locator(".gv-shared .gv-lrow", has_text="NPOLY").count() == 0
    assert not side(dpage, 0, "NPOLY")            # still hidden, not reset
    search.fill("")
    dpage.wait_for_timeout(200)
    box = (dpage.locator(".gv-shared .gv-lrow", has_text="NPOLY").first
           .locator("input[type=checkbox]"))
    assert box.is_checked() is False, "clearing the filter reset the checkbox"


def test_layers_are_keyed_by_layer_and_datatype(dpage):
    """Two layers can share a display name; the key has to be what the file stores."""
    keys = dpage.evaluate("() => window.GDSViewer.instances.gv.layers().map(l => l.key)")
    assert len(keys) == len(set(keys))
    assert all("/" in key for key in keys)
    rows = dpage.locator(".gv-shared .gv-lrow").count()
    assert rows == len(keys)


def test_the_panel_keeps_the_layer_map_colours(dpage):
    swatches = dpage.evaluate("""() => Array.from(
        document.querySelectorAll('.gv-shared .gv-lrow input[type=color]')).map(i => i.value)""")
    colours = dpage.evaluate("""() => window.GDSViewer.instances.gv.halves[0].viewer.A.layers
        .map(l => (l.colour || '').toLowerCase())""")
    assert any(colour in swatches for colour in colours if colour)


# --- a layer that exists in only one of the two ------------------------------

@pytest.fixture(scope="module")
def lopsided_doc(tmp_path_factory):
    """B has a layer A does not. One row, controlling the side that has it."""
    from analyzer.edit import apply_edits
    from analyzer.xor_diff import xor_compare
    from ui.viewer_data import build_comparison

    layermap = load_lyp(default_layermap())
    a = SAMPLES / "DCAP0_1_RT_4.gds"
    b = tmp_path_factory.mktemp("lopsided") / "extra.gds"
    apply_edits(a, [{"op": "insert", "layer": "M2",
                     "points": [[0.2, 0.2], [0.2, 0.24], [0.26, 0.24], [0.26, 0.2]]}],
                b, layermap=layermap)
    payload = build_comparison(
        xor_compare(a, b, layermap),
        build(shape_outlines(a, layermap), title=a.name),
        build(shape_outlines(b, layermap), title="extra.gds"),
    )
    payload["names"] = {"a": a.name, "b": "extra.gds"}
    return document(payload, "gv", dual=True)


def test_a_layer_in_only_one_file_gets_one_row_and_no_errors(browser, lopsided_doc):
    pg = browser.new_page(viewport={"width": 1600, "height": 820})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content(lopsided_doc)
    pg.wait_for_function("() => window.GDSViewer && window.GDSViewer.instances.gv")
    pg.wait_for_timeout(200)

    # "M2" also matches M2-TRACK-GUIDE, so match the row whose name is exactly M2.
    rows = pg.locator(".gv-shared .gv-lrow").filter(
        has=pg.locator(".gv-lname", has_text=re.compile(r"^M2$")))
    assert rows.count() == 1, f"M2 should appear once, got {rows.all_inner_texts()}"
    only = pg.evaluate("""() => { const d = window.GDSViewer.instances.gv;
      const e = d.layers().find(l => l.name === 'M2');
      return {inA: !!e.sides.a, inB: !!e.sides.b}; }""")
    assert only == {"inA": False, "inB": True}

    row = rows.first
    row.locator("input[type=checkbox]").uncheck()
    pg.wait_for_timeout(150)
    assert not pg.evaluate("() => window.GDSViewer.instances.gv.halves[1].viewer.visible.has('M2')")
    row.locator("input[type=checkbox]").check()
    pg.wait_for_timeout(150)
    assert pg.evaluate("() => window.GDSViewer.instances.gv.halves[1].viewer.visible.has('M2')")
    assert not errors, errors
    pg.close()
