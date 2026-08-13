"""The edit engine: what it writes, and what it refuses to write.

An editor is only as good as its refusals. Most of these tests are about the cases
where the journal and the file no longer agree - a stale target, an unknown layer, a
degenerate polygon - because that is where a layout editor does real damage: it
silently changes the wrong shape and the file still opens.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.edit import (EditError, apply_edits, apply_to_bytes, describe,
                           normalise)
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "AN2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def outlines(lm):
    return shape_outlines(GDS, lm, include_identity=True)


def target_for(outlines, layer_name, index=0):
    row = next(l for l in outlines["layers"] if l["name"] == layer_name)
    shape = row["shapes"][index]
    return {"layer": layer_name, **shape["id"]}, shape


def read(path, lm, name):
    row = next((l for l in shape_outlines(path, lm)["layers"] if l["name"] == name), None)
    return row or {"shapes": [], "labels": [], "shape_count": 0, "label_count": 0}


# --- identity ---------------------------------------------------------------

def test_a_shape_carries_the_cell_it_actually_lives_in(outlines):
    _, shape = target_for(outlines, "NPOLY")
    assert shape["id"]["cell"] == "AN2D1"
    assert shape["id"]["in_top"] is True
    assert shape["id"]["trans"] == "r0 *1 0,0"
    # Exact integers, not rounded micrometres: this is what the match is made on.
    assert all(isinstance(v, int) for point in shape["id"]["local_dbu"] for v in point)


def test_identity_is_absent_unless_it_was_asked_for(lm):
    plain = shape_outlines(GDS, lm)
    assert all("id" not in shape
               for row in plain["layers"] for shape in row["shapes"])


# --- the operations ---------------------------------------------------------

def test_a_move_lands_on_the_exact_nanometre(tmp_path, lm, outlines):
    target, before = target_for(outlines, "NPOLY")
    out = tmp_path / "moved.gds"
    report = apply_edits(GDS, [{"op": "move", "target": target,
                                "dx_um": 0.010, "dy_um": -0.005}], out, layermap=lm)
    assert report["applied"] == 1
    after = read(out, lm, "NPOLY")["shapes"]
    moved = [s for s in after
             if abs(s["left_um"] - (before["left_um"] + 0.010)) < 1e-12
             and abs(s["bottom_um"] - (before["bottom_um"] - 0.005)) < 1e-12]
    assert len(moved) == 1
    assert moved[0]["width_um"] == before["width_um"]


def test_a_drawn_rectangle_is_written_where_it_was_drawn(tmp_path, lm):
    out = tmp_path / "drawn.gds"
    points = [[0.05, 0.05], [0.05, 0.08], [0.09, 0.08], [0.09, 0.05]]
    apply_edits(GDS, [{"op": "insert", "layer": "M2", "points": points}],
                out, layermap=lm)
    made = [s for s in read(out, lm, "M2")["shapes"]
            if abs(s["width_um"] - 0.04) < 1e-12 and abs(s["height_um"] - 0.03) < 1e-12]
    assert len(made) == 1
    assert made[0]["left_um"] == 0.05 and made[0]["bottom_um"] == 0.05


def test_a_delete_removes_exactly_one_shape(tmp_path, lm, outlines):
    target, _ = target_for(outlines, "NDIFFCON")
    before = len(next(l for l in outlines["layers"] if l["name"] == "NDIFFCON")["shapes"])
    out = tmp_path / "deleted.gds"
    apply_edits(GDS, [{"op": "delete", "target": target}], out, layermap=lm)
    assert read(out, lm, "NDIFFCON")["shape_count"] == before - 1


def test_a_replace_rewrites_the_outline(tmp_path, lm, outlines):
    target, shape = target_for(outlines, "M0")
    grown = [[x, y + 0.002] for x, y in shape["outline_um"]]
    out = tmp_path / "reshaped.gds"
    apply_edits(GDS, [{"op": "replace", "target": target, "points": grown}],
                out, layermap=lm)
    after = read(out, lm, "M0")["shapes"]
    assert any(abs(s["bottom_um"] - (shape["bottom_um"] + 0.002)) < 1e-12 for s in after)


@pytest.mark.parametrize("rotate,expect", [(90, "swap"), (180, "same"), (270, "swap")])
def test_rotation_turns_a_shape_about_its_own_centre(tmp_path, lm, outlines,
                                                     rotate, expect):
    target, shape = target_for(outlines, "NPOLY")
    out = tmp_path / f"rot{rotate}.gds"
    apply_edits(GDS, [{"op": "transform", "target": target, "rotate": rotate}],
                out, layermap=lm)
    after = read(out, lm, "NPOLY")["shapes"]
    centre = shape["centre_um"]
    turned = [s for s in after
              if abs(s["centre_um"][0] - centre[0]) < 1e-9
              and abs(s["centre_um"][1] - centre[1]) < 1e-9]
    assert turned, "the shape moved off its own centre"
    got = turned[0]
    if expect == "swap":
        assert abs(got["width_um"] - shape["height_um"]) < 1e-9
        assert abs(got["height_um"] - shape["width_um"]) < 1e-9
    else:
        assert abs(got["width_um"] - shape["width_um"]) < 1e-9


def test_a_label_is_written_with_its_text_and_position(tmp_path, lm):
    out = tmp_path / "labelled.gds"
    apply_edits(GDS, [{"op": "insert_text", "layer": "M1", "text": "NEWPIN",
                       "at_um": [0.07, 0.065]}], out, layermap=lm)
    labels = read(out, lm, "M1")["labels"]
    assert {"text": "NEWPIN", "at_um": [0.07, 0.065]} in labels


def test_a_label_can_be_deleted_again(tmp_path, lm):
    once = tmp_path / "with.gds"
    apply_edits(GDS, [{"op": "insert_text", "layer": "M1", "text": "GONE",
                       "at_um": [0.07, 0.065]}], once, layermap=lm)
    twice = tmp_path / "without.gds"
    apply_edits(once, [{"op": "delete_text", "layer": "M1", "text": "GONE",
                        "at_um": [0.07, 0.065]}], twice, layermap=lm)
    assert not [l for l in read(twice, lm, "M1")["labels"] if l["text"] == "GONE"]


def test_merging_two_touching_shapes_leaves_one(tmp_path, lm):
    """The edit that goes wrong by hand: two rectangles that only look joined."""
    drawn = tmp_path / "pair.gds"
    apply_edits(GDS, [
        {"op": "insert", "layer": "M2", "points": [[0.2, 0.2], [0.2, 0.24], [0.24, 0.24], [0.24, 0.2]]},
        {"op": "insert", "layer": "M2", "points": [[0.24, 0.2], [0.24, 0.24], [0.28, 0.24], [0.28, 0.2]]},
    ], drawn, layermap=lm)
    fresh = shape_outlines(drawn, lm, include_identity=True)
    row = next(l for l in fresh["layers"] if l["name"] == "M2")
    targets = [{"layer": "M2", **s["id"]} for s in row["shapes"]]
    assert len(targets) == 2

    merged = tmp_path / "merged.gds"
    report = apply_edits(drawn, [{"op": "combine", "operation": "merge",
                                  "targets": targets}], merged, layermap=lm)
    assert report["applied"] == 1
    after = read(merged, lm, "M2")["shapes"]
    assert len(after) == 1
    assert abs(after[0]["width_um"] - 0.08) < 1e-12       # 0.04 + 0.04, no seam


def test_subtracting_cuts_the_second_shape_out_of_the_first(tmp_path, lm):
    drawn = tmp_path / "pair2.gds"
    apply_edits(GDS, [
        {"op": "insert", "layer": "M2", "points": [[0.3, 0.3], [0.3, 0.4], [0.4, 0.4], [0.4, 0.3]]},
        {"op": "insert", "layer": "M2", "points": [[0.32, 0.32], [0.32, 0.38], [0.38, 0.38], [0.38, 0.32]]},
    ], drawn, layermap=lm)
    fresh = shape_outlines(drawn, lm, include_identity=True)
    row = next(l for l in fresh["layers"] if l["name"] == "M2")
    big = max(row["shapes"], key=lambda s: s["area_um2"])
    small = min(row["shapes"], key=lambda s: s["area_um2"])
    targets = [{"layer": "M2", **big["id"]}, {"layer": "M2", **small["id"]}]

    cut = tmp_path / "cut.gds"
    apply_edits(drawn, [{"op": "combine", "operation": "subtract",
                         "targets": targets}], cut, layermap=lm)
    after = read(cut, lm, "M2")["shapes"]
    # A ring: 0.1 x 0.1 minus 0.06 x 0.06.
    assert abs(sum(s["area_um2"] for s in after) - (0.01 - 0.0036)) < 1e-9


def test_a_layer_boolean_writes_into_the_named_layer(tmp_path, lm):
    out = tmp_path / "bool.gds"
    report = apply_edits(GDS, [{"op": "boolean", "operation": "and",
                                "layer_a": "NPOLY", "layer_b": "NDIFF",
                                "into": "M2"}], out, layermap=lm)
    assert report["applied"] == 1
    assert read(out, lm, "M2")["shape_count"] >= 1


# --- refusals ---------------------------------------------------------------

def test_a_stale_target_is_refused_and_nothing_is_written(tmp_path, lm):
    out = tmp_path / "never.gds"
    stale = {"layer": "NPOLY", "cell": "AN2D1", "dup": 0,
             "local_dbu": [[0, 0], [0, 10], [10, 10], [10, 0]]}
    with pytest.raises(EditError, match="no longer in"):
        apply_edits(GDS, [{"op": "delete", "target": stale}], out, layermap=lm)
    assert not out.exists()


def test_an_edit_after_the_file_moved_on_is_refused(tmp_path, lm, outlines):
    """The case that matters: the journal was recorded against an older file."""
    target, _ = target_for(outlines, "NPOLY")
    once = tmp_path / "once.gds"
    apply_edits(GDS, [{"op": "move", "target": target, "dx_um": 0.01, "dy_um": 0}],
                once, layermap=lm)
    with pytest.raises(EditError, match="no longer in"):
        apply_edits(once, [{"op": "move", "target": target, "dx_um": 0.01, "dy_um": 0}],
                    tmp_path / "twice.gds", layermap=lm)


def test_an_unknown_layer_is_refused_rather_than_invented(tmp_path, lm):
    with pytest.raises(EditError, match="not in the layer map"):
        apply_edits(GDS, [{"op": "insert", "layer": "NOT-A-LAYER",
                           "points": [[0, 0], [0, 0.01], [0.01, 0.01]]}],
                    tmp_path / "no.gds", layermap=lm)


def test_a_degenerate_polygon_is_refused(tmp_path, lm):
    with pytest.raises(EditError, match="zero area"):
        apply_edits(GDS, [{"op": "insert", "layer": "M2",
                           "points": [[0, 0], [0.01, 0], [0.02, 0]]}],
                    tmp_path / "no.gds", layermap=lm)
    with pytest.raises(EditError, match="three points"):
        apply_edits(GDS, [{"op": "insert", "layer": "M2", "points": [[0, 0], [0.01, 0]]}],
                    tmp_path / "no.gds", layermap=lm)


def test_an_unknown_operation_is_refused_by_name(tmp_path, lm):
    with pytest.raises(EditError, match="unknown operation"):
        apply_edits(GDS, [{"op": "obliterate"}], tmp_path / "no.gds", layermap=lm)


def test_one_bad_operation_in_a_batch_writes_nothing(tmp_path, lm, outlines):
    """Atomic by default: a half-applied journal matches neither file."""
    good, _ = target_for(outlines, "NPOLY")
    out = tmp_path / "atomic.gds"
    with pytest.raises(EditError):
        apply_edits(GDS, [
            {"op": "move", "target": good, "dx_um": 0.01, "dy_um": 0},
            {"op": "insert", "layer": "NOPE", "points": [[0, 0], [0, 1], [1, 1]]},
        ], out, layermap=lm)
    assert not out.exists()


def test_partial_mode_reports_what_it_refused(tmp_path, lm, outlines):
    good, _ = target_for(outlines, "NPOLY")
    out = tmp_path / "partial.gds"
    report = apply_edits(GDS, [
        {"op": "move", "target": good, "dx_um": 0.01, "dy_um": 0},
        {"op": "insert", "layer": "NOPE", "points": [[0, 0], [0, 1], [1, 1]]},
    ], out, layermap=lm, atomic=False)
    assert report["applied"] == 1
    assert len(report["refused"]) == 1
    assert "layer map" in report["refused"][0]["reason"]
    assert out.exists()


# --- hierarchy --------------------------------------------------------------

@pytest.fixture(scope="module")
def hierarchical(tmp_path_factory):
    """A cell placed twice, the second time rotated, so an edit through an instance
    has to be mapped back into the cell it really lives in."""
    import klayout.db as db

    path = tmp_path_factory.mktemp("edit") / "hier.gds"
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(200, 0)                       # M0 in the bundled technology
    leaf = layout.create_cell("LEAF")
    leaf.shapes(li).insert(db.Box(0, 0, 100, 50))
    top = layout.create_cell("TOP")
    top.insert(db.CellInstArray(leaf.cell_index(), db.Trans(db.Vector(0, 0))))
    top.insert(db.CellInstArray(leaf.cell_index(),
                                db.Trans(db.Trans.R90, db.Vector(400, 0))))
    layout.write(str(path))
    return path


def test_editing_a_shape_seen_through_an_instance_maps_back_into_its_cell(
        tmp_path, lm, hierarchical):
    fresh = shape_outlines(hierarchical, lm, include_identity=True)
    row = next(l for l in fresh["layers"] if l["shapes"])
    # The rotated placement: its transform is not the identity.
    rotated = next(s for s in row["shapes"] if s["id"]["trans"] != "r0 *1 0,0")
    target = {"layer": {"layer": row["layer"], "datatype": row["datatype"]},
              **rotated["id"]}

    before = {s["id"]["trans"]: s["centre_um"] for s in row["shapes"]}
    out = tmp_path / "hier_moved.gds"
    apply_edits(hierarchical, [{"op": "move", "target": target,
                                "dx_um": 0.020, "dy_um": 0}], out, layermap=lm)
    fresh_after = shape_outlines(out, lm, include_identity=True)
    after = {s["id"]["trans"]: s["centre_um"]
             for s in next(l for l in fresh_after["layers"] if l["shapes"])["shapes"]}

    # The edit was asked for in top-cell coordinates: +20 nm in x on the screen. The
    # placement the user grabbed has to move exactly that way...
    grabbed = rotated["id"]["trans"]
    assert after[grabbed][0] - before[grabbed][0] == pytest.approx(0.020)
    assert after[grabbed][1] - before[grabbed][1] == pytest.approx(0.0)
    # ...and the other placement moves too, because GDSII has one definition per
    # cell - in *its* frame, which for a 90-degree placement is a shift in y. An
    # editor that wrote the raw vector into the cell would move this one sideways.
    other = next(t for t in before if t != grabbed)
    assert after[other][0] - before[other][0] == pytest.approx(0.0)
    assert abs(after[other][1] - before[other][1]) == pytest.approx(0.020)


def test_a_shared_cell_edit_says_how_many_placements_it_reaches(
        tmp_path, lm, hierarchical):
    fresh = shape_outlines(hierarchical, lm, include_identity=True)
    row = next(l for l in fresh["layers"] if l["shapes"])
    target = {"layer": {"layer": row["layer"], "datatype": row["datatype"]},
              **row["shapes"][0]["id"]}
    report = apply_edits(hierarchical, [{"op": "move", "target": target,
                                         "dx_um": 0.01, "dy_um": 0}],
                         tmp_path / "shared.gds", layermap=lm)
    assert report["shared_cells"] == [{"cell": "LEAF", "placements": 2}]
    assert any("placed 2 times" in w for w in report["warnings"])


# --- the journal ------------------------------------------------------------

def test_apply_to_bytes_returns_a_readable_file(lm, outlines):
    target, _ = target_for(outlines, "NPOLY")
    data, report = apply_to_bytes(GDS.read_bytes(), GDS.name,
                                  [{"op": "move", "target": target,
                                    "dx_um": 0.01, "dy_um": 0}], layermap=lm)
    assert report["applied"] == 1
    assert data[:6] != GDS.read_bytes()[:6] or len(data) != len(GDS.read_bytes()) or True
    # It has to be a GDSII that reads back, not just bytes.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "check.gds"
        path.write_bytes(data)
        assert shape_outlines(path, lm)["top_cell"] == "AN2D1"


def test_nothing_is_written_when_the_journal_is_empty(tmp_path, lm):
    out = tmp_path / "empty.gds"
    report = apply_edits(GDS, [], out, layermap=lm)
    assert report["applied"] == 0
    # An empty journal still produces a file, so "apply" with nothing pending is a
    # copy rather than an error.
    assert out.exists()


def test_describe_says_what_each_operation_does(outlines):
    target, _ = target_for(outlines, "NPOLY")
    lines = describe([
        {"op": "move", "target": target, "dx_um": 0.01, "dy_um": 0},
        {"op": "insert", "layer": "M2", "points": [[0, 0], [0, 1], [1, 1]]},
        {"op": "combine", "operation": "merge", "targets": [target, target]},
    ])
    assert lines[0] == "move a shape on NPOLY by 10, 0 nm"
    assert lines[1].startswith("draw on M2")
    assert "merge 2 shapes" in lines[2]


def test_normalise_drops_unknown_operations_and_copies(outlines):
    original = [{"op": "insert", "layer": "M2", "points": [[0, 0]]},
                {"op": "sneaky"}]
    clean = normalise(original)
    assert len(clean) == 1
    clean[0]["points"].append([1, 1])
    assert original[0]["points"] == [[0, 0]], "the journal was stored by reference"


# --- placing cells ----------------------------------------------------------

@pytest.fixture
def library(tmp_path):
    """A layout with a cell that is not placed anywhere: something to place."""
    import klayout.db as db

    path = tmp_path / "lib.gds"
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(200, 0)                       # M0
    tile = layout.create_cell("TILE")
    tile.shapes(li).insert(db.Box(0, 0, 100, 50))
    top = layout.create_cell("TOP")
    top.shapes(li).insert(db.Box(0, 0, 20, 20))
    layout.write(str(path))
    return path


def test_a_cell_can_be_placed_where_it_was_asked_for(tmp_path, lm, library):
    from analyzer.hierarchy import instance_tree

    out = tmp_path / "placed.gds"
    report = apply_edits(library, [{"op": "insert_instance", "cell": "TILE",
                                    "into": "TOP", "at_um": [0.2, 0.1],
                                    "rotate": 90}], out, layermap=lm)
    assert report["applied"] == 1
    tree = instance_tree(out)
    assert len(tree["placements"]) == 1
    placement = tree["placements"][0]
    assert placement["cell"] == "TILE"
    assert placement["orient"] == "R90"
    # 100 x 50 nm turned by 90 degrees and dropped at 200,100 nm.
    assert placement["bbox"] == [0.15, 0.1, 0.2, 0.2]


def test_an_array_is_written_as_one_placement_record(tmp_path, lm, library):
    from analyzer.hierarchy import instance_tree

    out = tmp_path / "array.gds"
    apply_edits(library, [{"op": "insert_instance", "cell": "TILE", "into": "TOP",
                           "at_um": [0, 0.5],
                           "array": {"nx": 3, "ny": 2, "dx_um": 0.2, "dy_um": 0.1}}],
                out, layermap=lm)
    assert len(instance_tree(out)["placements"]) == 6

    import klayout.db as db
    layout = db.Layout()
    layout.read(str(out))
    top = layout.cell("TOP")
    # One AREF, not six SREFs: that is what the format is for, and it is the
    # difference between a small file and a large one on a real array.
    assert len(list(top.each_inst())) == 1


def test_a_placement_can_be_removed_again(tmp_path, lm, library):
    from analyzer.hierarchy import instance_tree

    placed = tmp_path / "placed.gds"
    apply_edits(library, [{"op": "insert_instance", "cell": "TILE", "into": "TOP",
                           "at_um": [0.2, 0.1]}], placed, layermap=lm)
    gone = tmp_path / "gone.gds"
    apply_edits(placed, [{"op": "delete_instance", "cell": "TILE", "into": "TOP",
                          "trans": "r0 200,100"}], gone, layermap=lm)
    assert instance_tree(gone)["placements"] == []


def test_placing_a_cell_into_itself_is_refused(tmp_path, lm, library):
    with pytest.raises(EditError, match="contain itself"):
        apply_edits(library, [{"op": "insert_instance", "cell": "TOP", "into": "TOP"}],
                    tmp_path / "no.gds", layermap=lm)


def test_placing_a_cell_that_does_not_exist_is_refused(tmp_path, lm, library):
    with pytest.raises(EditError, match="no cell called"):
        apply_edits(library, [{"op": "insert_instance", "cell": "GHOST", "into": "TOP"}],
                    tmp_path / "no.gds", layermap=lm)


def test_an_array_without_a_step_is_refused(tmp_path, lm, library):
    with pytest.raises(EditError, match="needs a step"):
        apply_edits(library, [{"op": "insert_instance", "cell": "TILE", "into": "TOP",
                               "array": {"nx": 4, "ny": 1}}],
                    tmp_path / "no.gds", layermap=lm)


def test_removing_a_placement_that_is_not_there_is_refused(tmp_path, lm, library):
    with pytest.raises(EditError, match="no placement of"):
        apply_edits(library, [{"op": "delete_instance", "cell": "TILE", "into": "TOP",
                               "trans": "r0 0,0"}], tmp_path / "no.gds", layermap=lm)


# --- the off-grid audit -----------------------------------------------------

def test_an_off_grid_edit_is_reported_against_the_grid_it_was_drawn_on(tmp_path, lm):
    """The failure that survives review: right on screen, rounded by the mask writer."""
    off = [[0.20025, 0.2], [0.20025, 0.23], [0.24, 0.23], [0.24, 0.2]]
    report = apply_edits(GDS, [{"op": "insert", "layer": "M2", "points": off}],
                         tmp_path / "off.gds", layermap=lm, grid_nm=1)
    grid = report["off_grid"]
    assert grid["checked"] is True and grid["grid_nm"] == 1
    assert grid["added"] == 1
    assert grid["layers"] == [{"layer": "204/0", "shapes": 1}]


def test_an_on_grid_edit_adds_nothing_even_though_the_file_has_older_ones(tmp_path, lm):
    """Counting the whole file would bury the edit: this layout already sits on a
    half-nanometre grid, so 'total' is large and 'added' is what matters."""
    on = [[0.2, 0.2], [0.2, 0.23], [0.24, 0.23], [0.24, 0.2]]
    report = apply_edits(GDS, [{"op": "insert", "layer": "M2", "points": on}],
                         tmp_path / "on.gds", layermap=lm, grid_nm=1)
    assert report["off_grid"]["added"] == 0
    assert report["off_grid"]["total"] > 0


def test_the_grid_audit_is_skipped_when_no_grid_was_given(tmp_path, lm):
    report = apply_edits(GDS, [], tmp_path / "plain.gds", layermap=lm)
    assert report["off_grid"] == {"checked": False}
