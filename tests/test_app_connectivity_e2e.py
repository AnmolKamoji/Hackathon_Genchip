"""End-to-end tests driving the real Streamlit app with the real sample files.

These exist because every unit test can pass while the page still raises: the
duplicate-element-ID crash and a `.lyp` being fed to the JSON stack parser were
both only visible from here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
APP = ROOT / "app.py"


class _Upload:
    """Stands in for Streamlit's UploadedFile."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name

    def getvalue(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def run_app(monkeypatch):
    """Run app.py with chosen uploads, returning the finished AppTest."""

    def runner(gds: list[str], *, lyp: bool = True, stack: bool = True, json: bool = False):
        def fake_uploader(label, *args, **kwargs):
            # Order matters: the stack uploader's own label mentions ".lyp", so a
            # naive ".lyp" check hands it the XML layer-properties file and it
            # fails to parse as JSON.
            if "connection stack" in label:
                return _Upload(SAMPLES / "Titan_stack.json") if stack else None
            if "GDS" in label:
                return [_Upload(SAMPLES / g) for g in gds]
            if "sidecar" in label:
                return ([_Upload(SAMPLES / (g[:-4] + ".json")) for g in gds] if json else [])
            if ".lyp" in label:
                return _Upload(SAMPLES / "Titan_layer_properties.lyp") if lyp else None
            return None

        monkeypatch.setattr(st, "file_uploader", fake_uploader)
        at = AppTest.from_file(str(APP), default_timeout=600).run()
        assert not at.exception, [e.value for e in at.exception]
        assert not at.error, [e.value for e in at.error]
        return at

    return runner


def _metrics(at) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


@pytest.mark.parametrize("gds,shapes,components", [
    ("DCAP0_1_RT_4.gds", "56", "50"),
    ("NR2D1_1_RT_4.gds", "60", "54"),
])
def test_intra_layer_metrics_render(run_app, gds, shapes, components):
    m = _metrics(run_app([gds]))
    assert m["Conducting shapes"] == shapes
    assert m["Physical conductors"] == components
    assert m["Layers with abutting shapes"] == "2"


def test_net_graph_renders_with_a_stack(run_app):
    m = _metrics(run_app(["NR2D1_1_RT_4.gds"]))
    assert m["Nets"] == "7"
    assert m["Multi-layer nets"] == "5"
    # Two PMOSInterconnect stubs no via reaches in this revision.
    assert m["Floating nets"] == "2"


def test_bundled_stack_builds_the_net_graph_with_only_a_gds(run_app):
    """The realistic case: a `.gds` on its own, no sidecar and no stack upload.

    The bundled technology stack fills the gap, so the net graph is available -
    and the page states where the stack came from, since it is not PDK-verified.
    """
    at = run_app(["NR2D1_1_RT_4.gds"], stack=False, json=False, lyp=False)
    assert _metrics(at)["Nets"] == "7"
    captions = " ".join(c.value for c in at.caption)
    assert "bundled technology stack" in captions
    assert "not PDK-verified" in captions


def test_bundled_layer_map_is_used_when_none_is_uploaded(run_app):
    """The layer map is a default, not an optional extra.

    Uploading only a `.gds` is the common case, and without a map it loses layer
    names, roles, via counts and every role aggregate — so the bundled technology
    map is applied unless the user supplies their own.
    """
    at = run_app(["NR2D1_1_RT_4.gds"], lyp=False, stack=True)
    infos = " ".join(i.value for i in at.info)
    assert "bundled by default" in infos
    # With the map present the stack resolves, so the net graph is built.
    assert _metrics(at)["Nets"] == "7"
    warnings = " ".join(w.value for w in at.warning)
    assert "needs a .lyp as well" not in warnings


def test_uploaded_layer_map_overrides_the_bundled_one(run_app):
    at = run_app(["NR2D1_1_RT_4.gds"], lyp=True)
    infos = " ".join(i.value for i in at.info)
    assert "(uploaded)" in infos


def test_gds_only_reports_the_exact_tier_one_numbers(run_app):
    """Tier 1 is GDS-only and exact, whatever else is or is not supplied."""
    at = run_app(["NR2D1_1_RT_4.gds"], lyp=False, stack=False)
    m = _metrics(at)
    assert m["Conducting shapes"] == "60"
    assert m["Physical conductors"] == "54"
    assert m["Layers with abutting shapes"] == "2"
    # Via-ness comes from the bundled layer map's via layer names.
    assert m["Vias"] == "6"


def test_decap_resolves_to_four_nets_with_no_complaints(run_app):
    """DCAP0 under the corrected stack: two terminals plus two power taps."""
    at = run_app(["DCAP0_1_RT_4.gds"])
    assert _metrics(at)["Nets"] == "4"
    warnings = " ".join(w.value for w in at.warning)
    assert "single net" not in warnings
    assert "disagrees with the layout" not in warnings


def test_sidecar_supplies_the_stack_without_a_stack_upload(run_app):
    """A sidecar names its vias after their endpoints, so it can stand in for the
    uploaded stack file."""
    at = run_app(["DCAP0_1_RT_4.gds"], stack=False, json=True)
    assert _metrics(at)["Nets"] == "4"
    infos = " ".join(i.value for i in at.info)
    assert "net graph was not built" not in infos


def test_two_files_with_sidecars_and_connectivity(run_app):
    """The heaviest path: two uploads, fused metadata, comparison and both
    connectivity analyses on one page."""
    at = run_app(["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"], json=True)
    m = _metrics(at)
    assert m["Vias"] == "10"            # fused mode supplies via semantics
    assert m["Nets"] == "4"
    assert m["Layers changed"] == "4/31"
    # Duplicate element IDs previously aborted the whole page here.
    assert len(list(at.dataframe)) >= 8


def test_same_file_uploaded_twice_does_not_crash(run_app):
    """Identical uploads produce identical auto-generated widget keys."""
    at = run_app(["NR2D1_1_RT_4.gds", "NR2D1_1_RT_4.gds"])
    assert _metrics(at)["Conducting shapes"] == "60"


# --- multi-file XOR comparison ----------------------------------------------

def test_four_files_render_the_xor_matrix(monkeypatch):
    """Engineers review families of revisions, not just pairs."""
    files = ["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds",
             "NR2D1_1_RT_4.gds", "NR2D1_2_RT_4.gds"]

    def fake_uploader(label, *args, **kwargs):
        if "connection stack" in label:
            return None
        if "GDS" in label:
            return [_Upload(SAMPLES / f) for f in files]
        if "sidecar" in label:
            return []
        if ".lyp" in label:
            return None
        return None

    monkeypatch.setattr(st, "file_uploader", fake_uploader)
    at = AppTest.from_file(str(APP), default_timeout=900).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]

    m = _metrics(at)
    # The matrix picks the two revisions of one cell as closest.
    assert "DCAP0_1_RT_4.gds" in m["Closest pair"]
    assert "DCAP0_2_RT_4.gds" in m["Closest pair"]
    # And the first pair's XOR detail, established independently.
    assert m["Layers changed"] == "4/31"
    assert m["Regions"] == "19"
    assert m["XOR area"] == "0.005308 µm²"
    # The verdict leads, before any table. It is a themed element rather than a
    # stock alert box, so it renders as markdown.
    verdicts = [m.value for m in at.markdown if 'class="verdict"' in m.value]
    assert verdicts, "the verdict banner must be on the page"
    assert "4 of 31 layers differ" in " ".join(verdicts)


def test_identical_uploads_report_no_differences(monkeypatch):
    def fake_uploader(label, *args, **kwargs):
        if "connection stack" in label:
            return None
        if "GDS" in label:
            return [_Upload(SAMPLES / "DCAP0_1_RT_4.gds")] * 2
        if "sidecar" in label:
            return []
        if ".lyp" in label:
            return None
        return None

    monkeypatch.setattr(st, "file_uploader", fake_uploader)
    at = AppTest.from_file(str(APP), default_timeout=900).run()
    assert not at.exception, [e.value for e in at.exception]
    verdicts = " ".join(m.value for m in at.markdown if 'class="verdict"' in m.value)
    assert "Identical — no geometric difference" in verdicts


# --- choosing which two layouts to compare ---------------------------------

def test_the_pair_to_compare_can_be_chosen_from_more_than_two_uploads(run_app):
    """With four uploads there are six pairs, and the section follows the choice."""
    at = run_app(["AN2D1_2_RT_4.gds", "DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds",
                  "NR2D1_1_RT_4.gds"])
    picker = next(s for s in at.selectbox if s.label == "Which two layouts to compare")
    assert len(picker.options) == 6                    # 4 choose 2
    assert all("→" in label for label in picker.options)
    first = picker.value

    other = next(o for o in picker.options if o != first)
    at = picker.select(other).run()
    assert not at.exception, [e.value for e in at.exception]
    picker = next(s for s in at.selectbox if s.label == "Which two layouts to compare")
    assert picker.value == other
    # The section below is about the chosen pair, not the default one.
    a, b = other.split(" → ")
    assert any(a in m.value or b in m.value for m in at.markdown), \
        "the comparison section did not follow the selection"


def test_the_expanded_comparison_opens_on_the_chosen_pair(run_app):
    at = run_app(["AN2D1_2_RT_4.gds", "DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"])
    picker = next(s for s in at.selectbox if s.label == "Which two layouts to compare")
    chosen = picker.options[-1]
    at = picker.select(chosen).run()
    expand = next(b for b in at.button if b.key == "cmp_expand")
    at = expand.click().run()
    assert not at.exception, [e.value for e in at.exception]
    focus = at.session_state["gv_focus"]
    assert focus["kind"] == "compare"
    assert f'{focus["a"]} → {focus["b"]}' == chosen
    # The workspace owns the screen: the chat input beside the layout is present.
    assert at.chat_input


# --- the tool bench ---------------------------------------------------------

def _tool(at, name):
    """Open one tool. Only the chosen one runs, so each test selects its own."""
    return at.radio(key="tool_open").set_value(name).run()


def test_the_tools_section_offers_every_tool(run_app):
    at = run_app(["AN2D1_2_RT_4.gds"])
    options = at.radio(key="tool_open").options
    for name in ("Technology", "DRC", "LVS", "Netlist", "2.5D view", "Density map",
                 "Diff", "Browse shapes", "Browse instances"):
        assert name in options, f"the {name} tool is missing"


def test_the_netlist_tool_extracts_devices_without_any_extra_input(run_app):
    """The stack is bundled, so the netlist is there the moment a file is uploaded."""
    at = _tool(run_app(["AN2D1_2_RT_4.gds"]), "Netlist")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics.get("Devices") == "6"
    text = " ".join(m.value for m in at.markdown if m.value)
    assert "NMOS × 3" in " ".join(c.value for c in at.caption if c.value)


@pytest.mark.parametrize("tool,phrase,why", [
    ("LVS", "This needs a schematic netlist", "no schematic in a GDSII"),
    ("DRC", "This needs a design rule deck", "cannot be guessed"),
    ("2.5D view", "This needs a layer stack", "GDSII stores no Z"),
])
def test_a_tool_that_needs_an_input_says_which_one_and_why(run_app, tool, phrase, why):
    at = _tool(run_app(["AN2D1_2_RT_4.gds"]), tool)
    info = " ".join(i.value for i in at.info if i.value)
    assert phrase in info
    assert why in info


def test_the_technology_tab_says_what_is_loaded(run_app):
    at = _tool(run_app(["AN2D1_2_RT_4.gds"]), "Technology")
    frames = [f.value for f in at.dataframe]
    tech = next((f for f in frames if "input" in getattr(f, "columns", [])), None)
    assert tech is not None, "the technology table is missing"
    inputs = list(tech["input"])
    assert "Connection stack (.json)" in inputs
    assert "Schematic netlist (SPICE)" in inputs
    loaded = dict(zip(tech["input"], tech["loaded"]))
    assert loaded["Layer map (.lyp)"] == "yes"
    assert loaded["Schematic netlist (SPICE)"] == "no"


def test_the_diff_tool_needs_two_different_layouts(run_app):
    at = _tool(run_app(["AN2D1_2_RT_4.gds"]), "Diff")
    info = " ".join(i.value for i in at.info if i.value)
    assert "Upload a second, different layout" in info


def test_browse_instances_says_a_flat_cell_has_none(run_app):
    at = _tool(run_app(["AN2D1_2_RT_4.gds"]), "Browse instances")
    info = " ".join(i.value for i in at.info if i.value)
    assert "is flat" in info and "nothing to browse" in info


# --- the comparison is side by side -----------------------------------------

def test_the_comparison_shows_a_on_the_left_and_b_on_the_right(run_app):
    at = run_app(["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"])
    labels = [m.value for m in at.markdown if m.value and "—" in m.value]
    reference = next((l for l in labels if "A — Reference" in l), None)
    revision = next((l for l in labels if "B — Revision" in l), None)
    assert reference and revision, "the two viewers are not labelled A and B"
    # A is rendered before B, which is what puts it on the left of the row.
    order = [m.value for m in at.markdown if m.value]
    assert order.index(reference) < order.index(revision)
    # Each label carries its own filename.
    assert "DCAP0_1_RT_4.gds" in reference
    assert "DCAP0_2_RT_4.gds" in revision


def test_identical_uploads_still_render_both_sides(run_app):
    """Nothing differs, so the panel must still show two viewers rather than none."""
    at = run_app(["DCAP0_1_RT_4.gds", "DCAP0_1_RT_4.gds"])
    assert not at.exception
    labels = [m.value for m in at.markdown if m.value]
    assert any("A — Reference" in l for l in labels)
    assert any("B — Revision" in l for l in labels)


def test_the_overlay_is_kept_below_the_pair(run_app):
    """Side by side answers one question; XOR and wipe answer the other."""
    at = run_app(["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"])
    titles = [e.label for e in at.expander]
    assert any("Overlay the two" in t for t in titles)


def test_the_comparison_tables_are_unchanged(run_app):
    """The arrangement changed; the comparison itself did not."""
    at = run_app(["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds"])
    metrics = {m.label: m.value for m in at.metric}
    assert "Layers changed" in metrics
    assert "XOR area" in metrics
    assert "Regions" in metrics
    assert any("Largest differences" in (m.value or "") for m in at.markdown)
