"""Drive the viewer in a real browser.

The viewer is JavaScript on a canvas, so Python assertions cannot reach it: nothing
in the analyzer knows whether a click selects the right shape, whether the toolbar
is reachable, or whether the ruler snaps. These tests open the real document in
headless Chromium and interact with it the way a user does.

Skipped when Chromium is not installed, so the suite still runs on a machine that
has not fetched it (`python -m playwright install chromium`).
"""
from __future__ import annotations

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
