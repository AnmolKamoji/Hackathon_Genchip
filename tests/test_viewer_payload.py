"""What crosses into the browser, checked in Python.

The browser tests drive the viewer; these check the payload it is handed. Both are
needed: a payload can be perfectly shaped and still be wrong about the layout, and
the assertions for "is this number the measured one?" belong here, where the
analyzer is in reach.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.connectivity import default_stack, extract_nets
from analyzer.drc import check_layout
from analyzer.hierarchy import analyze_hierarchy, instance_tree
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines
from analyzer.pitch import analyze_pitch
from ui.viewer_data import (build, cells_payload, markers_payload, nets_payload,
                            to_json, tracks_payload, with_analysis)

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = SAMPLES / "AN2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def outlines(lm):
    return shape_outlines(GDS, lm)


@pytest.fixture(scope="module")
def hier_gds(tmp_path_factory):
    """A hierarchical layout: 2x2 array of MID, each holding two LEAF copies.

    Every bundled sample is a flat standard cell, so nothing in the repository
    exercises accumulated transforms. One LEAF is rotated, which is what makes the
    difference between composing transforms and merely reading the child's box.
    """
    import klayout.db as db

    path = tmp_path_factory.mktemp("tree") / "hier.gds"
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


# --- the cell tree ---------------------------------------------------------

def test_a_flat_cell_has_no_placements(lm):
    tree = instance_tree(GDS)
    assert tree["flat"] is True
    assert tree["maxDepth"] == 0
    assert tree["placements"] == []
    assert tree["cells"] == [c for c in tree["cells"] if c["isTop"]]
    assert "flat" in tree["note"]


def test_the_top_cell_box_is_the_measured_extent(lm, outlines):
    tree = instance_tree(GDS)
    assert tree["topBbox"] == pytest.approx(outlines["cell_bbox_um"])


def test_every_placement_of_an_array_is_enumerated(hier_gds):
    tree = instance_tree(hier_gds)
    assert tree["top"] == "TOP"
    assert tree["maxDepth"] == 2
    # 4 copies of MID, each holding 2 LEAF copies.
    assert len(tree["placements"]) == 12
    by_cell: dict[str, int] = {}
    for p in tree["placements"]:
        by_cell[p["cell"]] = by_cell.get(p["cell"], 0) + 1
    assert by_cell == {"MID": 4, "LEAF": 8}
    assert {p["depth"] for p in tree["placements"]} == {1, 2}


def test_transforms_are_accumulated_down_the_tree(hier_gds):
    """A LEAF inside the array must be boxed where it lands, not where it was drawn."""
    tree = instance_tree(hier_gds)
    rotated = [p for p in tree["placements"] if p["orient"] == "R90"]
    assert len(rotated) == 4
    # 100 x 50 nm rotated by 90 degrees measures 50 x 100 nm.
    for p in rotated:
        x0, y0, x1, y1 = p["bbox"]
        assert round((x1 - x0) * 1000, 6) == 50
        assert round((y1 - y0) * 1000, 6) == 100
    # The four array copies sit at four distinct places.
    assert len({tuple(p["bbox"]) for p in rotated}) == 4
    # ... and the second array row is 300 nm above the first.
    ys = sorted({round(p["bbox"][1] * 1000, 6) for p in rotated})
    assert ys[1] - ys[0] == 300


def test_the_placement_walk_is_capped(hier_gds):
    tree = instance_tree(hier_gds, max_placements=5)
    assert tree["truncated"] is True
    assert len(tree["placements"]) == 5
    assert "truncated" in tree["note"]


def test_a_missing_top_cell_is_refused(tmp_path):
    empty = tmp_path / "empty.gds"
    import klayout.db as db
    db.Layout().write(str(empty))
    with pytest.raises(ValueError):
        instance_tree(empty)


# --- the payload -----------------------------------------------------------

def test_cells_payload_carries_both_the_tree_and_the_structure(hier_gds):
    payload = cells_payload(analyze_hierarchy(hier_gds), instance_tree(hier_gds))
    assert payload["top"] == "TOP"
    assert payload["maxDepth"] == 2
    assert len(payload["placements"]) == 12
    assert payload["structure"]["cellCount"] == 3
    assert payload["structure"]["depth"] == 2


def test_cells_payload_survives_a_missing_tree(lm):
    payload = cells_payload(analyze_hierarchy(GDS), None)
    assert payload["top"] == "AN2D1"
    assert payload["structure"]["description"].startswith("flat")
    assert "placements" not in payload          # nothing invented in its place


def test_cells_payload_is_empty_without_either(lm):
    assert cells_payload(None, None) == {}


def test_markers_carry_the_layers_the_check_read(outlines):
    markers = markers_payload(check_layout(outlines))
    assert len(markers) > 10
    assert all(m["layers"] for m in markers), \
        "a rule result with no layer link cannot be cross-probed"
    # Failures first, so the list opens on what needs looking at.
    assert markers[0]["status"] == "violation"


def test_markers_are_absent_when_checking_was_not_possible():
    assert markers_payload(None) == []
    assert markers_payload({"available": False}) == []


def test_nets_carry_their_polygons_only_when_asked(lm):
    stack = default_stack(lm)
    without = nets_payload({"nets": extract_nets(GDS, lm, stack)})
    assert without == [], "shapes must be opt-in, or every digest grows"
    with_shapes = nets_payload({"nets": extract_nets(GDS, lm, stack,
                                                    collect_shapes=True)})
    assert len(with_shapes) > 1
    assert all(n["shapes"] for n in with_shapes)


def test_a_net_polygon_count_matches_its_reported_shape_count(lm):
    nets = nets_payload({"nets": extract_nets(GDS, lm, default_stack(lm),
                                              collect_shapes=True)})
    for net in nets:
        assert len(net["shapes"]) == net["shapeCount"]


def test_tracks_carry_the_measured_pitch_not_a_recomputed_one(outlines):
    pitch = analyze_pitch(outlines, GDS.name)
    tracks = tracks_payload(pitch)
    assert tracks, "this technology has track guides"
    for metal, entry in pitch["metal_pitches"].items():
        if not entry or not entry.get("positions_nm"):
            continue
        assert tracks[metal]["pitchNm"] == entry["pitch_nm"]
        assert tracks[metal]["positionsNm"] == entry["positions_nm"]
        assert tracks[metal]["axis"] in ("x", "y")
    assert tracks["_cpp"]["cppNm"] == pitch["gate_pitch"]["cpp_nm"]


def test_with_analysis_attaches_every_review_list(lm, outlines):
    payload = with_analysis(
        build(outlines, title="AN2D1"),
        drc=check_layout(outlines),
        connectivity={"nets": extract_nets(GDS, lm, default_stack(lm),
                                           collect_shapes=True)},
        pitch=analyze_pitch(outlines, GDS.name),
        hierarchy=analyze_hierarchy(GDS),
        tree=instance_tree(GDS),
    )
    assert payload["markers"] and payload["nets"] and payload["tracks"]
    assert payload["tree"]["top"] == "AN2D1"
    assert payload["netsAvailable"] is True
    # It has to survive the trip through JSON: a NaN or an infinity here would
    # break JSON.parse in the browser and blank the whole viewer.
    assert to_json(payload)


def test_a_geometry_only_payload_still_opens(outlines):
    payload = with_analysis(build(outlines, title="AN2D1"))
    assert payload["markers"] == []
    assert payload["nets"] == []
    assert payload["tracks"] == {}
    assert payload["tree"] == {}
    assert payload["netsAvailable"] is False
