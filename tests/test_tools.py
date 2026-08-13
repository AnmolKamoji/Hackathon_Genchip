"""The tool bench: rule decks, structural diff, density and the 2.5D view.

Each of these is a tool KLayout has. The tests care about two things: that the
measurement is right, and that a missing input is refused rather than guessed - a
deck with no limit, a stack file with no thickness, a layer the file does not have.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzer.deck import load_deck, run as run_deck
from analyzer.density import density_map
from analyzer.diff import diff
from analyzer.edit import apply_edits
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.stack3d import build_slabs, load_stack3d, mesh
from analyzer.xor_diff import xor_compare

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "AN2D1_2_RT_4.gds"
DCAP_A = SAMPLES / "DCAP0_1_RT_4.gds"
DCAP_B = SAMPLES / "DCAP0_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


# --- rule decks -------------------------------------------------------------

def test_a_deck_is_validated_before_it_runs(tmp_path):
    with pytest.raises(ValueError, match="no 'rules' list"):
        load_deck(write(tmp_path, "a.json", {"technology": "x"}))
    with pytest.raises(ValueError, match="unknown type"):
        load_deck(write(tmp_path, "b.json", {"rules": [{"type": "vibes", "layer": "M0"}]}))
    with pytest.raises(ValueError, match="names no layer"):
        load_deck(write(tmp_path, "c.json", {"rules": [{"type": "width", "min_nm": 5}]}))
    with pytest.raises(ValueError, match="no 'type'"):
        load_deck(write(tmp_path, "d.json", {"rules": [{"layer": "M0"}]}))


def test_a_bare_list_of_rules_is_accepted(tmp_path):
    deck = load_deck(write(tmp_path, "e.json",
                           [{"id": "1", "type": "width", "layer": "M0", "min_nm": 5}]))
    assert deck["rule_count"] == 1


def test_a_width_rule_finds_what_is_narrower(lm, tmp_path):
    deck = load_deck(write(tmp_path, "w.json", {"rules": [
        {"id": "wide", "type": "width", "layer": "M0", "min_nm": 5},
        {"id": "strict", "type": "width", "layer": "M0", "min_nm": 50},
    ]}))
    result = run_deck(GDS, lm, deck)
    rows = {row["id"]: row for row in result["results"]}
    assert rows["wide"]["status"] == "pass"
    assert rows["strict"]["status"] == "violation"
    assert rows["strict"]["count"] > 0
    # It says where, so the viewer can zoom to it.
    assert len(rows["strict"]["locations"]) == rows["strict"]["count"]
    assert rows["strict"]["observed"]["worst_nm"] < 50


def test_a_rule_with_no_limit_is_unusable_not_passed(lm, tmp_path):
    deck = load_deck(write(tmp_path, "u.json",
                           {"rules": [{"id": "n", "type": "width", "layer": "M0"}]}))
    result = run_deck(GDS, lm, deck)
    assert result["results"][0]["status"] == "unusable"
    assert result["summary"]["pass"] == 0


def test_a_rule_on_a_missing_layer_is_not_applicable_not_passed(lm, tmp_path):
    deck = load_deck(write(tmp_path, "m.json", {"rules": [
        {"id": "ghost", "type": "width", "layer": "NOT-A-LAYER", "min_nm": 10}]}))
    result = run_deck(GDS, lm, deck)
    assert result["results"][0]["status"] == "not applicable"
    assert result["summary"]["pass"] == 0


def test_an_overlap_rule_finds_the_gate(lm, tmp_path):
    """NPOLY over NDIFF is a transistor gate - three of them in this cell."""
    deck = load_deck(write(tmp_path, "o.json", {"rules": [
        {"id": "gate", "type": "not_overlapping", "layer": "NPOLY", "with": "NDIFF"},
        {"id": "cross", "type": "not_overlapping", "layer": "NPOLY", "with": "PDIFF"},
    ]}))
    rows = {row["id"]: row for row in run_deck(GDS, lm, deck)["results"]}
    assert rows["gate"]["status"] == "violation" and rows["gate"]["count"] == 3
    assert rows["cross"]["status"] == "pass"


def test_an_inside_rule_checks_containment(lm, tmp_path):
    deck = load_deck(write(tmp_path, "i.json", {"rules": [
        {"id": "v0", "type": "inside", "layer": "VIA0", "of": "M0"}]}))
    assert run_deck(GDS, lm, deck)["results"][0]["status"] == "pass"


def test_a_grid_rule_measures_against_the_stated_grid(lm, tmp_path):
    deck = load_deck(write(tmp_path, "g.json", {"rules": [
        {"id": "one", "type": "grid", "layer": "M0", "grid_nm": 1},
        {"id": "seven", "type": "grid", "layer": "M0", "grid_nm": 7},
    ]}))
    rows = {row["id"]: row for row in run_deck(GDS, lm, deck)["results"]}
    assert rows["one"]["status"] == "pass"
    assert rows["seven"]["status"] == "violation"


def test_a_density_rule_uses_windows_not_the_whole_cell(lm, tmp_path):
    deck = load_deck(write(tmp_path, "d.json", {"rules": [
        {"id": "band", "type": "density", "layer": "M0", "min_pct": 0, "max_pct": 100,
         "window_nm": 100},
        {"id": "tight", "type": "density", "layer": "M0", "min_pct": 40, "max_pct": 60,
         "window_nm": 100},
    ]}))
    rows = {row["id"]: row for row in run_deck(GDS, lm, deck)["results"]}
    assert rows["band"]["status"] == "pass"
    assert rows["tight"]["status"] == "violation"
    assert rows["tight"]["observed"]["window_nm"] == 100


def test_a_deck_run_says_it_only_covers_its_own_rules(lm, tmp_path):
    deck = load_deck(write(tmp_path, "x.json", {"rules": [
        {"id": "a", "type": "width", "layer": "M0", "min_nm": 1}]}))
    result = run_deck(GDS, lm, deck)
    assert "not that it is DRC clean" in result["not_derivable"]["rules_not_in_the_deck"]


def test_one_broken_rule_does_not_stop_the_deck(lm, tmp_path):
    deck = load_deck(write(tmp_path, "b.json", {"rules": [
        {"id": "bad", "type": "area", "layer": "M0", "min_nm2": "not a number"},
        {"id": "good", "type": "width", "layer": "M0", "min_nm": 1},
    ]}))
    result = run_deck(GDS, lm, deck)
    rows = {row["id"]: row for row in result["results"]}
    assert rows["bad"]["status"] == "error"
    assert rows["good"]["status"] == "pass"


# --- structural diff --------------------------------------------------------

def test_a_file_does_not_differ_from_itself(lm):
    result = diff(DCAP_A, DCAP_A, lm)
    assert result["identical"] is True
    assert all(v == 0 for v in result["totals"].values())


def test_two_revisions_differ_in_shapes_and_texts(lm):
    result = diff(DCAP_A, DCAP_B, lm)
    assert result["identical"] is False
    assert result["totals"]["shapes_only_in_a"] > 0
    assert result["totals"]["texts_only_in_a"] > 0
    assert result["cells_compared"] == 1


def test_the_diff_sees_a_split_the_xor_cannot(lm, tmp_path):
    """The reason both tools exist.

    Splitting one rectangle into two halves that cover exactly the same area is
    invisible to an XOR and obvious to a structural diff.
    """
    outlines = shape_outlines(DCAP_A, lm, include_identity=True)
    row = next(l for l in outlines["layers"] if l["name"] == "M0")
    shape = row["shapes"][0]
    x0, y0 = shape["left_um"], shape["bottom_um"]
    w, h = shape["width_um"], shape["height_um"]
    mid = x0 + w / 2
    split = tmp_path / "split.gds"
    apply_edits(DCAP_A, [
        {"op": "delete", "target": {"layer": "M0", **shape["id"]}},
        {"op": "insert", "layer": "M0",
         "points": [[x0, y0], [x0, y0 + h], [mid, y0 + h], [mid, y0]]},
        {"op": "insert", "layer": "M0",
         "points": [[mid, y0], [mid, y0 + h], [x0 + w, y0 + h], [x0 + w, y0]]},
    ], split, layermap=lm)

    assert xor_compare(DCAP_A, split, lm)["summary"]["total_xor_area_um2"] == 0
    structural = diff(DCAP_A, split, lm)
    assert structural["identical"] is False
    assert structural["totals"]["shapes_only_in_a"] == 1
    assert structural["totals"]["shapes_only_in_b"] == 2


def test_the_diff_says_how_it_differs_from_an_xor(lm):
    assert "XOR" in diff(DCAP_A, DCAP_B, lm)["difference_from_xor"]


def test_a_missing_cell_is_reported_by_name(lm, tmp_path):
    import klayout.db as db

    extra = tmp_path / "extra.gds"
    layout = db.Layout()
    layout.read(str(DCAP_A))
    layout.create_cell("SPARE")
    layout.write(str(extra))
    result = diff(DCAP_A, extra, lm)
    assert result["cells_only_in_b"] == ["SPARE"]
    assert result["cells_only_in_a"] == []


# --- density ----------------------------------------------------------------

def test_density_tiles_the_cell_and_reports_the_extremes(lm):
    result = density_map(GDS, lm, layers=["M0"], window_nm=50)
    entry = result["layers"]["M0"]
    assert entry["tile_count"] == entry["columns"] * entry["rows"]
    assert 0 <= entry["min_pct"] <= entry["mean_pct"] <= entry["max_pct"] <= 100
    assert entry["densest"][0]["pct"] == entry["max_pct"]
    assert entry["sparsest"][0]["pct"] == entry["min_pct"]


def test_density_counts_merged_geometry_not_a_sum_of_areas(lm, tmp_path):
    """Two shapes drawn on top of each other cover their union, not twice the area."""
    doubled = tmp_path / "doubled.gds"
    outlines = shape_outlines(GDS, lm, include_identity=True)
    row = next(l for l in outlines["layers"] if l["name"] == "M0")
    shape = row["shapes"][0]
    apply_edits(GDS, [{"op": "insert", "layer": "M0",
                       "points": shape["outline_um"]}], doubled, layermap=lm)
    before = density_map(GDS, lm, layers=["M0"], window_nm=1000)["layers"]["M0"]
    after = density_map(doubled, lm, layers=["M0"], window_nm=1000)["layers"]["M0"]
    assert after["overall_pct"] == pytest.approx(before["overall_pct"])


def test_a_window_that_would_tile_forever_is_refused(lm):
    result = density_map(GDS, lm, layers=["M0"], window_nm=0.5)
    entry = result["layers"]["M0"]
    assert entry["available"] is False
    assert "past the" in entry["reason"]


def test_layers_can_be_combined_into_one_map(lm):
    result = density_map(GDS, lm, layers=["M0", "M1"], window_nm=100, combine=True)
    assert list(result["layers"]) == ["(combined)"]
    separate = density_map(GDS, lm, layers=["M0", "M1"], window_nm=100)
    combined = result["layers"]["(combined)"]["overall_pct"]
    assert combined >= separate["layers"]["M0"]["overall_pct"]


def test_density_says_what_it_cannot_settle(lm):
    result = density_map(GDS, lm, layers=["M0"], window_nm=100)
    assert "fill_requirements" in result["not_derivable"]


# --- the 2.5D view ----------------------------------------------------------

def test_a_stack_file_is_validated(tmp_path):
    with pytest.raises(ValueError, match="no 'layers' object"):
        load_stack3d(write(tmp_path, "a.json", {"technology": "x"}))
    with pytest.raises(ValueError, match="needs both"):
        load_stack3d(write(tmp_path, "b.json", {"layers": {"M0": {"elevation_nm": 1}}}))
    with pytest.raises(ValueError, match="must be positive"):
        load_stack3d(write(tmp_path, "c.json",
                           {"layers": {"M0": {"elevation_nm": 1, "thickness_nm": 0}}}))


def test_slabs_take_their_heights_from_the_stack_file(lm, tmp_path):
    stack = load_stack3d(write(tmp_path, "s.json", {"layers": {
        "M0": {"elevation_nm": 70, "thickness_nm": 25},
        "M1": {"elevation_nm": 115, "thickness_nm": 30}}}))
    slabs = build_slabs(GDS, lm, stack)
    assert slabs["available"] is True
    assert slabs["height_nm"] == 145
    for slab in slabs["slabs"]:
        if slab["layer"] == "M0":
            assert (slab["bottom_nm"], slab["top_nm"]) == (70, 95)


def test_a_layer_the_stack_does_not_mention_is_not_drawn(lm, tmp_path):
    stack = load_stack3d(write(tmp_path, "s.json", {"layers": {
        "M0": {"elevation_nm": 70, "thickness_nm": 25}}}))
    slabs = build_slabs(GDS, lm, stack)
    assert {s["layer"] for s in slabs["slabs"]} == {"M0"}
    assert "NPOLY" in slabs["layers_not_in_the_stack"]


def test_the_2_5d_view_says_where_its_heights_came_from(lm, tmp_path):
    stack = load_stack3d(write(tmp_path, "s.json", {"layers": {
        "M0": {"elevation_nm": 70, "thickness_nm": 25}}}))
    slabs = build_slabs(GDS, lm, stack)
    assert "GDSII stores no Z" in slabs["not_derivable"]["elevations"]


def test_each_slab_meshes_into_a_closed_box(lm, tmp_path):
    stack = load_stack3d(write(tmp_path, "s.json", {"layers": {
        "M0": {"elevation_nm": 70, "thickness_nm": 25}}}))
    meshes = mesh(build_slabs(GDS, lm, stack))
    assert meshes
    for entry in meshes:
        # A rectangular slab: 8 vertices, 12 triangles - two per face.
        assert len(entry["vertices"]) % 8 == 0
        assert len(entry["triangles"]) == len(entry["vertices"]) // 8 * 12
        zs = {round(v[2], 9) for v in entry["vertices"]}
        assert zs == {0.07, 0.095}


def test_a_concave_outline_is_cut_into_convex_pieces(lm, tmp_path):
    """A fan triangulation of an L shape puts metal where there is none."""
    import klayout.db as db

    path = tmp_path / "ell.gds"
    layout = db.Layout()
    layout.dbu = 0.001
    index = layout.layer(200, 0)                     # M0
    cell = layout.create_cell("ELL")
    cell.shapes(index).insert(db.Polygon([
        db.Point(0, 0), db.Point(0, 100), db.Point(40, 100),
        db.Point(40, 40), db.Point(100, 40), db.Point(100, 0)]))
    layout.write(str(path))

    stack = load_stack3d(write(tmp_path, "s.json", {"layers": {
        "M0": {"elevation_nm": 0, "thickness_nm": 10}}}))
    meshes = mesh(build_slabs(path, lm, stack))
    assert len(meshes) == 1
    # Cut into two convex pieces: 16 vertices rather than the 12 a single fan
    # over six points would produce.
    assert len(meshes[0]["vertices"]) == 16
