"""End-to-end: the tech-file parameters as the page actually renders them.

Every unit test in `test_techparams.py` can pass while the page shows nothing, or shows
the right numbers under the wrong labels. This drives the real app with the real sample
and reads the rendered table, because that is what the engineer sees.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
APP = ROOT / "app.py"

# What the supplied tech file states, and so what the page must show.
EXPECTED = {
    "N-poly width": "15", "P-poly width": "15",
    "N-diffcon width": "20", "P-diffcon width": "20",
    "Diffusion width": "15", "Power rail width": "85",
    "N/P Diffusion spacing": "41", "Poly to Diffcon spacing": "5",
    "Gate Cut spacing": "17", "Diffcon ETE spacing": "21",
    "Gate extension": "12", "Diffcon extension": "10",
    "Metal0": "15, 12, 9, 12, 19, 12, 9, 12, 15",
    "Metal2": "21.5, 16, 12, 16, 12, 16, 21.5",
    "Technology": "gaa", "Routing Capability": "Three Metal Solution",
    "Orientation": "R0", "Number of routing tracks": "4", "Multiheight": "1",
}


class _Upload:
    def __init__(self, path: Path):
        self.path, self.name = Path(path), Path(path).name

    def getvalue(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def rendered(monkeypatch):
    def fake_uploader(label, *args, **kwargs):
        if "connection stack" in label:
            return _Upload(SAMPLES / "Titan_stack.json")
        if "GDS" in label:
            return [_Upload(SAMPLES / "AN2D1_2_RT_4.gds")]
        if "sidecar" in label:
            return []
        if ".lyp" in label:
            return _Upload(SAMPLES / "Titan_layer_properties.lyp")
        return None

    monkeypatch.setattr(st, "file_uploader", fake_uploader)
    at = AppTest.from_file(str(APP), default_timeout=900).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    return at


def _parameter_table(at):
    """The rendered tech-parameter table, as {parameter: row}."""
    for element in at.dataframe:
        frame = element.value
        if "Parameter" in getattr(frame, "columns", []) and "vs tech file" in frame.columns:
            return {row["Parameter"]: row for _, row in frame.iterrows()}
    raise AssertionError("the tech-file parameter table was not rendered")


@pytest.mark.parametrize("name,value", sorted(EXPECTED.items()))
def test_the_page_shows_each_measured_parameter(rendered, name, value):
    table = _parameter_table(rendered)
    assert name in table, f"{name} is missing from the rendered table"
    assert table[name]["Measured"] == value, table[name]


def test_the_page_marks_every_comparable_parameter_as_matching(rendered):
    table = _parameter_table(rendered)
    verdicts = {row["vs tech file"] for row in table.values()}
    assert "DISAGREES" not in verdicts, {
        name: row for name, row in table.items() if row["vs tech file"] == "DISAGREES"}
    assert sum(1 for row in table.values() if row["vs tech file"] == "matches") == 26


def test_the_page_separates_a_stated_figure_from_a_measurement(rendered):
    """The CFET-only parameter must show no measurement and be labelled tech-file only.

    Showing the stated 15 nm in the Measured column would present a figure from another
    file as a measurement of this cell.
    """
    row = _parameter_table(rendered)["Diffusion to Diff interconnect spacing"]
    assert row["Measured"] == "—"
    assert row["vs tech file"] == "tech file only"


def test_every_row_cites_the_rule_that_defines_it(rendered):
    table = _parameter_table(rendered)
    missing = [name for name, row in table.items() if not row["Rule"]]
    assert not missing, f"no design rule cited for: {missing}"


def test_the_metal_solution_metric_reports_the_capability(rendered):
    """M2 carries no geometry in this cell, and the tech file still says three-metal.

    The headline metrics must agree with the parameter table; reporting "2 metal" up
    top and "Three Metal Solution" in the table would be the same contradiction the
    drawn-metal reading used to produce, just spread across two places.
    """
    labels = {m.label: m.value for m in rendered.metric}
    assert labels.get("Routing") == "3 metal", labels
    table = _parameter_table(rendered)
    assert table["Routing Capability"]["Measured"] == "Three Metal Solution"


# --- the interactive viewer and the expanded workspace ----------------------

def test_the_layout_tab_renders_the_interactive_viewer(rendered):
    """AppTest cannot see inside an iframe, so this asserts on what it can see: the
    panel's own controls. The drawing itself is covered by the browser suite."""
    labels = [b.label for b in rendered.button]
    assert any("Expand" in label for label in labels), labels


def test_expanding_a_layout_opens_the_workspace_with_the_chat(monkeypatch):
    """Clicking Expand must give the full-screen view, not append one below."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    def fake_uploader(label, *args, **kwargs):
        if "connection stack" in label:
            return _Upload(SAMPLES / "Titan_stack.json")
        if "GDS" in label:
            return [_Upload(SAMPLES / "AN2D1_2_RT_4.gds")]
        if "sidecar" in label:
            return []
        if ".lyp" in label:
            return _Upload(SAMPLES / "Titan_layer_properties.lyp")
        return None

    monkeypatch.setattr(st, "file_uploader", fake_uploader)
    at = AppTest.from_file(str(APP), default_timeout=900)
    at.session_state["gv_focus"] = {"kind": "layout", "key": "lv0",
                                    "title": "AN2D1_2_RT_4.gds"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    # The workspace renders its own chat input and a way back.
    assert any("Back" in b.label for b in at.button), [b.label for b in at.button]
    assert at.chat_input, "the workspace has no chat box"
    # And it stops before the page body: the per-layout tabs must not be there.
    assert not at.tabs, "the workspace rendered on top of the full page"
