"""Regression tests for bugs found by adversarial QA.

Each test here corresponds to a defect that shipped at some point, so the
docstrings record the wrong behaviour as well as the right one.
"""
import json

import klayout.db as db
import pytest

from ai.deterministic import answer
from ai.llm import _compact, candidate_hosts
from analyzer.comparison import compare_metadata
from analyzer.gds_parser import analyze_gds
from analyzer.sidecar_parser import analyze_sidecar


def write_gds(tmp_path, name, build):
    layout = db.Layout()
    layout.dbu = 0.001
    build(layout)
    p = tmp_path / f"{name}.gds"
    layout.write(str(p))
    return p


# --------------------------------------------------------------- GDS geometry

def test_path_shapes_are_counted_and_measured(tmp_path):
    """`shape.path.polygon` is a method; the old code inserted the bound method."""
    def build(ly):
        top = ly.create_cell("TOP")
        top.shapes(ly.layer(10, 0)).insert(db.Path([db.Point(0, 0), db.Point(1000, 0)], 100))
    m = analyze_gds(write_gds(tmp_path, "paths", build))
    assert m["design"]["polygon_count"] == 1
    assert m["layers"][0]["area_um2"] == pytest.approx(0.1)


def test_non_rectangular_polygon_area(tmp_path):
    def build(ly):
        top = ly.create_cell("TOP")
        top.shapes(ly.layer(20, 0)).insert(
            db.Polygon([db.Point(0, 0), db.Point(1000, 0), db.Point(0, 1000)]))
    m = analyze_gds(write_gds(tmp_path, "tri", build))
    assert m["layers"][0]["area_um2"] == pytest.approx(0.5)


def test_instance_transforms_are_applied(tmp_path):
    """Shapes are yielded in the child's coordinates; ignoring the iterator
    transform would collapse both placements onto one another and halve the area."""
    def build(ly):
        child = ly.create_cell("CHILD")
        child.shapes(ly.layer(30, 0)).insert(db.Box(0, 0, 100, 100))
        top = ly.create_cell("TOP")
        top.insert(db.CellInstArray(child.cell_index(), db.Trans(db.Vector(0, 0))))
        top.insert(db.CellInstArray(child.cell_index(), db.Trans(db.Vector(500, 500))))
    m = analyze_gds(write_gds(tmp_path, "hier", build))
    assert m["design"]["polygon_count"] == 2
    assert m["layers"][0]["area_um2"] == pytest.approx(0.02)


def test_array_instances_are_expanded(tmp_path):
    def build(ly):
        child = ly.create_cell("CHILD")
        child.shapes(ly.layer(40, 0)).insert(db.Box(0, 0, 100, 100))
        top = ly.create_cell("TOP")
        top.insert(db.CellInstArray(child.cell_index(), db.Trans(),
                                    db.Vector(200, 0), db.Vector(0, 200), 3, 2))
    m = analyze_gds(write_gds(tmp_path, "arr", build))
    assert m["design"]["polygon_count"] == 6
    assert m["layers"][0]["area_um2"] == pytest.approx(0.06)


def test_multiple_top_cells_are_reported_not_hidden(tmp_path):
    """Only one top cell can be analyzed, but reporting half a design as the
    whole thing without saying so is a silent wrong answer."""
    def build(ly):
        li = ly.layer(60, 0)
        ly.create_cell("TOP_A").shapes(li).insert(db.Box(0, 0, 100, 100))
        ly.create_cell("TOP_B").shapes(li).insert(db.Box(5000, 5000, 5100, 5100))
    m = analyze_gds(write_gds(tmp_path, "multitop", build))
    assert m["design"]["top_cell_count"] == 2
    assert sorted(m["design"]["top_cells"]) == ["TOP_A", "TOP_B"]
    # Deterministic choice: KLayout's top_cells() order is not guaranteed.
    assert m["design"]["top_cell"] == "TOP_A"
    assert any("top-level cells" in w for w in m["warnings"])
    assert "TOP_B" in answer(m, "Give me a summary of this GDS.")


def test_empty_layout_has_no_sentinel_bounding_box(tmp_path):
    """KLayout's empty box reports a 2**32 dbu extent, i.e. 4294967.294 um."""
    def build(ly):
        ly.create_cell("EMPTY")
    m = analyze_gds(write_gds(tmp_path, "empty", build))
    assert m["layout"]["width_um"] == 0.0
    assert m["layout"]["height_um"] == 0.0
    assert m["layout"]["bbox_area_um2"] == 0.0
    assert m["cells"][0]["area_um2"] == 0.0
    # And the Q&A must survive it.
    for q in ["Give me a summary of this GDS.", "What is the layout size?",
              "What is the largest cell?", "Which layer has the highest density?"]:
        answer(m, q)


def test_overlapping_shapes_use_merged_area(tmp_path):
    def build(ly):
        li = ly.layer(90, 0)
        top = ly.create_cell("TOP")
        top.shapes(li).insert(db.Box(0, 0, 1000, 1000))
        top.shapes(li).insert(db.Box(500, 500, 1500, 1500))
    m = analyze_gds(write_gds(tmp_path, "ov", build))
    row = m["layers"][0]
    assert row["area_um2"] == pytest.approx(1.75)   # 2.0 would mean unmerged
    assert row["polygon_count"] == 2                # records
    assert row["merged_polygon_count"] == 1         # distinct shapes


def test_density_never_exceeds_100_percent(tmp_path):
    def build(ly):
        top = ly.create_cell("TOP")
        top.shapes(ly.layer(1, 0)).insert(db.Box(0, 0, 1000, 1000))
        top.shapes(ly.layer(2, 0)).insert(db.Box(0, 0, 1000, 1000))
    m = analyze_gds(write_gds(tmp_path, "dens", build))
    for row in m["layers"]:
        assert 0.0 <= row["density_percent"] <= 100.0


# ------------------------------------------------- datatype duplication

def test_duplicate_geometry_across_datatypes_is_not_double_counted():
    """Layer 300 datatypes 0 and 2 hold identical rectangles in the samples.

    Adding the per-datatype areas doubles the reported coverage of BSPowerRail.
    """
    from analyzer.fused import analyze_pair
    m = analyze_pair("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_1_RT_4.json")
    group = next(g for g in m["layer_groups"] if g["label"] == "BSPowerRail")
    assert group["geometry_duplicated_across_datatypes"] is True
    assert group["union_area_um2"] == pytest.approx(0.02295)
    assert group["sum_of_datatype_areas_um2"] == pytest.approx(0.0459)
    assert group["polygon_records"] == 4
    assert group["unique_polygons"] == 2

    reply = answer(m, "What is the area of BSPowerRail?")
    assert "0.022950" in reply
    assert "0.045900" in reply       # the sum is disclosed, not hidden
    reply2 = answer(m, "How many polygons are on M0?")
    assert "6 polygon records" in reply2 and "3 distinct" in reply2


# ------------------------------------------------------------- Q&A phrasing

@pytest.fixture(scope="module")
def fused():
    from analyzer.fused import analyze_pair
    return analyze_pair("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_1_RT_4.json")


def test_trailing_preposition_is_not_read_as_a_layer_name(fused):
    """"...have in total?" answered "`in` does not appear in the layer list"."""
    reply = answer(fused, "How many polygons does this design have in total?")
    assert reply == "The design contains 60 polygons in total."


def test_english_words_are_never_reported_as_missing_layers(fused):
    for q in ["Does it have good density?", "Does this design have any issues?",
              "Does the layout include something interesting?"]:
        reply = answer(fused, q)
        if reply:
            assert "does not appear in the layer list" not in reply, q


def test_rule_questions_are_answered_deterministically_or_refused(fused):
    """The model must never get the chance to speculate about violations.

    This used to be a blanket refusal, which was right while no rule deck existed.
    With the GENCHIP Design Rule Manual the geometric rules can be answered - but
    only from checked results, and only for rules the manual states. Here `fused`
    carries no `drc` block, so every rule question must say the results are absent
    rather than guess.
    """
    for q in ["Does this design contain any DRC violations?", "Are there DRC errors?",
              "does this pass DRC?", "any rule check failures?"]:
        reply = answer(fused, q)
        assert reply is not None, q
        assert "No design rule results are available" in reply, q
        assert "GENCHIP Design Rule Manual" in reply, q

    # LVS and ERC stay refused whatever rule data exists: they need a netlist.
    for q in ["Is the design LVS clean?", "Does it pass ERC?"]:
        reply = answer(fused, q)
        assert "LVS and ERC are not available" in reply, q
        assert "schematic or a netlist" in reply, q


def test_rule_answers_never_appear_without_a_manual(fused):
    """A rule verdict must be traceable to the manual, never to a guess."""
    reply = answer(fused, "Is M0 width within the design rule?")
    assert "No design rule results are available" in reply


def test_layer_presence_questions_mentioning_the_word_layer(fused):
    assert answer(fused, "Is there an M1 layer?").startswith("No.")
    assert answer(fused, "Is there an M0 layer?").startswith("Yes.")


def test_via_presence_question(fused):
    assert "6 vias" in answer(fused, "Does this design contain any vias?")


def test_per_layer_area_question(fused):
    assert "0.002460" in answer(fused, "What is the area of M0?")


def test_empty_and_nonsense_questions_defer_quietly(fused):
    for q in ["", "?", "polygons", "asdfgh"]:
        assert answer(fused, q) is None


# ------------------------------------------------------- sidecar robustness

def _mutate(tmp_path, name, fn):
    d = json.loads(open("data/samples/NR2D1_1_RT_4.json").read())
    fn(d)
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(d))
    return p


def test_unrecognised_element_kind_is_flagged(tmp_path):
    """A sidecar from another exporter reported 0 polygons with no complaint."""
    p = _mutate(tmp_path, "sref", lambda d: [e.__setitem__("element", "sref")
                                             for s in d["structures"] for e in s["elements"]])
    m = analyze_sidecar(p, "x.gds")
    assert m["design"]["polygon_count"] == 0
    assert any("could be classified" in w for w in m["warnings"])


def test_non_boolean_isvia_is_flagged(tmp_path):
    p = _mutate(tmp_path, "isvia", lambda d: [e.__setitem__("isVia", "true")
                                              for s in d["structures"] for e in s["elements"]])
    m = analyze_sidecar(p, "x.gds")
    assert m["design"]["via_count"] == 0
    assert any("non-boolean isVia" in w for w in m["warnings"])


def test_missing_units_is_flagged(tmp_path):
    p = _mutate(tmp_path, "nounits", lambda d: d.pop("units"))
    m = analyze_sidecar(p, "x.gds")
    assert m["layout"]["width_um"] is None
    assert any("units" in w for w in m["warnings"])


def test_reference_sidecars_emit_no_warnings():
    for stem in ("NR2D1_1_RT_4", "NR2D1_2_RT_4"):
        assert analyze_sidecar(f"data/samples/{stem}.json", f"{stem}.gds")["warnings"] == []


@pytest.mark.parametrize("mutation", [
    lambda d: [e.__setitem__("xy", [[0], [1, 2, 3], "nope", None, [4, 5]])
               for s in d["structures"] for e in s["elements"]],
    lambda d: [e.__setitem__("xy", [[0, 0], [1, 1]]) for s in d["structures"] for e in s["elements"]],
    lambda d: [e.pop("layer", None) for s in d["structures"] for e in s["elements"]],
    lambda d: [e.__setitem__("layerMap", None) for s in d["structures"] for e in s["elements"]],
    lambda d: d.__setitem__("units", [0, 0]),
])
def test_malformed_sidecars_do_not_crash_the_pipeline(tmp_path, mutation):
    p = _mutate(tmp_path, "fuzz", mutation)
    m = analyze_sidecar(p, "x.gds")
    real = analyze_sidecar("data/samples/NR2D1_1_RT_4.json", "a.gds")
    for q in ["Give me a summary of this GDS.", "How many polygons are there?",
              "What is the largest cell?", "Which layer has the highest density?"]:
        answer(m, q)
    json.dumps(compare_metadata(real, m))
    _compact(m)


def test_mismatched_sidecar_drops_via_semantics():
    """Pairing file 1's GDS with file 2's sidecar reported 9 vias for a 6-via file."""
    from analyzer.fused import analyze_pair
    m = analyze_pair("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_2_RT_4.json")
    assert m["consistency"]["agrees"] is False
    assert m["design"]["via_count"] is None
    assert all(row["via_count"] is None for row in m["layers"])
    assert any("does not describe this GDS" in w for w in m["warnings"])
    assert "unavailable" in answer(m, "How many vias are present?").lower()


# ------------------------------------------------------------------- hosts

def test_lan_router_is_never_offered_as_an_ollama_host(monkeypatch):
    """Under WSL mirrored networking the default route is the real router.

    Probing it would mean sending prompt data to an unrelated LAN device.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    hosts = candidate_hosts()
    assert "http://127.0.0.1:11434" in hosts
    assert not any(h.startswith("http://192.168.") for h in hosts)
