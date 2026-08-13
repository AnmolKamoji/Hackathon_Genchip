"""Regression tests for the analyzer defects found by code review.

Each test names the wrong behaviour it prevents. Numbers here are cross-checked
against KLayout directly rather than against the analyzer's own output, so a
shared bug in both cannot make a test pass.
"""
import json

import klayout.db as db
import pytest

from ai.deterministic import answer, answer_comparison
from analyzer.comparison import compare_metadata
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds
from analyzer.sidecar_parser import _dbu_um, analyze_sidecar

SAMPLES = "data/samples"
GDS1 = f"{SAMPLES}/NR2D1_1_RT_4.gds"
JSON1 = f"{SAMPLES}/NR2D1_1_RT_4.json"


@pytest.fixture(scope="module")
def fused():
    return analyze_pair(GDS1, JSON1)


def klayout_union_area(gds_path, pairs):
    """Ground-truth merged area over an explicit set of (layer, datatype)."""
    layout = db.Layout()
    layout.read(str(gds_path))
    top = list(layout.top_cells())[0]
    region = db.Region()
    for layer, datatype in pairs:
        li = layout.find_layer(layer, datatype)
        if li is None:
            continue
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            s = it.shape()
            if s.is_box():
                region.insert(db.Polygon(s.box).transformed(it.trans()))
            elif s.is_polygon():
                region.insert(s.polygon.transformed(it.trans()))
            elif s.is_path():
                region.insert(s.path.polygon().transformed(it.trans()))
            it.next()
    return float(region.merged().area()) * float(layout.dbu) ** 2


# --------------------------------------------------------- H1: cross-layer union

def test_area_is_not_unioned_across_distinct_layer_numbers(fused):
    """`Diffusion_Break` sits on 102/1, 103/1 and 121/0 - three separate mask
    layers that overlap in x/y. Unioning across them reported 0.0069 um2 instead
    of 0.01725, understating the drawn area by 2.5x, and the AI review quoted it.
    """
    g = next(x for x in fused["layer_groups"] if x["label"] == "Diffusion_Break")
    assert sorted(g["layer_numbers"]) == [102, 103, 121]

    # Union within each layer number, then add across them.
    expected = sum(klayout_union_area(GDS1, [k for k in map(tuple, g["datatypes"]) if k[0] == num])
                   for num in g["layer_numbers"])
    assert g["union_area_um2"] == pytest.approx(expected, abs=1e-9)
    assert g["union_area_um2"] == pytest.approx(0.01725, abs=1e-9)
    # The old, wrong value must not come back.
    assert g["union_area_um2"] != pytest.approx(0.0069, abs=1e-9)


def test_single_layer_number_duplication_still_unions(fused):
    """The opposite case must not regress: datatypes 0 and 2 of layer 300 hold
    identical geometry, so BSPowerRail's coverage is the union, not the sum."""
    g = next(x for x in fused["layer_groups"] if x["label"] == "BSPowerRail")
    assert g["layer_numbers"] == [300]
    assert g["geometry_duplicated_across_datatypes"] is True
    assert g["union_area_um2"] == pytest.approx(klayout_union_area(GDS1, [(300, 0), (300, 2)]))
    assert g["union_area_um2"] == pytest.approx(0.02295, abs=1e-9)
    assert g["sum_of_datatype_areas_um2"] == pytest.approx(0.0459, abs=1e-9)


# ------------------------------------------------- H2: non-exclusive attribution

def test_shared_layer_datatype_is_disclosed_as_an_upper_bound(fused):
    """(102,1) carries both Diffusion_Break and NMOSGate, so neither name owns
    the measured geometry outright."""
    shared = [g for g in fused["layer_groups"] if g["area_is_exclusive_to_this_name"] is False]
    assert {g["label"] for g in shared} == {"Diffusion_Break", "NMOSGate", "PMOSGate"}
    for g in shared:
        assert g["area_shared_with_other_layer_names"]

    reply = answer(fused, "What is the area of Diffusion_Break?")
    assert "0.017250" in reply
    assert "upper bound" in reply
    assert "distinct layer numbers" in reply


def test_exclusive_layers_are_not_hedged(fused):
    reply = answer(fused, "What is the area of Boundary?")
    assert "upper bound" not in reply


# ------------------------------- H3: records vs flattened instance placements

def _write_hierarchical_pair(tmp_path):
    """A GDS whose top places a leaf twice, plus a correct matching sidecar."""
    layout = db.Layout()
    layout.dbu = 0.001
    leaf = layout.create_cell("LEAF")
    m1, via = layout.layer(200, 0), layout.layer(201, 0)
    leaf.shapes(m1).insert(db.Box(0, 0, 100, 100))
    leaf.shapes(via).insert(db.Box(10, 10, 40, 40))
    top = layout.create_cell("TOP")
    top.shapes(m1).insert(db.Box(0, 500, 100, 600))
    top.insert(db.CellInstArray(leaf.cell_index(), db.Trans(db.Vector(0, 0))))
    top.insert(db.CellInstArray(leaf.cell_index(), db.Trans(db.Vector(300, 0))))
    gds = tmp_path / "hier.gds"
    layout.write(str(gds))

    def poly(x0, y0, x1, y1):
        return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]

    sidecar = {
        "version": 600, "units": [1e-3, 1e-9], "name": "LIB",
        "structures": [
            {"name": "LEAF", "elements": [
                {"element": "boundary", "layer": 200, "datatype": 0, "layer_name": "M1",
                 "isVia": False, "xy": poly(0, 0, 100, 100)},
                {"element": "boundary", "layer": 201, "datatype": 0, "layer_name": "VIA1",
                 "isVia": True, "xy": poly(10, 10, 40, 40)},
            ]},
            {"name": "TOP", "elements": [
                {"element": "boundary", "layer": 200, "datatype": 0, "layer_name": "M1",
                 "isVia": False, "xy": poly(0, 500, 100, 600)},
                {"element": "sref", "sname": "LEAF", "xy": [[0, 0]]},
                {"element": "sref", "sname": "LEAF", "xy": [[300, 0]]},
            ]},
        ],
    }
    js = tmp_path / "hier.json"
    js.write_text(json.dumps(sidecar))
    return gds, js


def test_hierarchical_design_keeps_its_via_semantics(tmp_path):
    """A correct sidecar for a hierarchical design was rejected because the GDS
    count is flattened (5 placements) and the sidecar's is records (3), which
    discarded the only source of via data in the file.
    """
    gds, js = _write_hierarchical_pair(tmp_path)
    m = analyze_pair(gds, js)

    assert m["design"]["polygon_count"] == 5          # flattened placements
    assert m["design"]["polygon_record_count"] == 3   # as-stored records
    assert m["consistency"]["count_mismatches"] == []
    assert m["consistency"]["agrees"] is True
    assert m["design"]["via_count"] == 1              # not dropped to None
    assert all(not r.get("via_semantics_rejected") for r in m["layers"])
    assert not any("does not describe this GDS" in w for w in m["warnings"])


def test_genuinely_mismatched_sidecar_is_still_rejected():
    """The guard must keep working for the case it was built for."""
    m = analyze_pair(GDS1, f"{SAMPLES}/NR2D1_2_RT_4.json")
    assert m["consistency"]["agrees"] is False
    assert m["design"]["via_count"] is None
    assert any("does not describe this GDS" in w for w in m["warnings"])


def test_top_cell_is_the_parent_not_a_leaf(tmp_path):
    """GDSII writes children before parents, so structures[0] is normally a leaf."""
    _, js = _write_hierarchical_pair(tmp_path)
    assert analyze_sidecar(js, "hier.gds")["design"]["top_cell"] == "TOP"


# ------------------------------------------- H4/H5: unusable sidecar coordinates

def _mutate(tmp_path, name, fn):
    d = json.loads(open(JSON1).read())
    fn(d)
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(d))
    return p


def test_unparseable_coordinates_give_unavailable_not_zero(tmp_path):
    """A flat coordinate list made every layer report area_um2 == 0.0 while still
    claiming 60 polygons - a measured zero for geometry that exists."""
    p = _mutate(tmp_path, "flat_xy", lambda d: [
        e.__setitem__("xy", [c for pt in e["xy"] for c in pt])
        for s in d["structures"] for e in s["elements"] if e.get("xy")])
    m = analyze_sidecar(p, "x.gds")
    assert m["design"]["polygon_count"] == 60
    assert all(r["area_um2"] is None for r in m["layers"])
    assert all(r["density_percent"] is None for r in m["layers"])
    assert any("coordinates" in w for w in m["warnings"])


def test_one_malformed_vertex_does_not_halve_the_area(tmp_path):
    """Dropping the bad vertex and measuring the survivors reported exactly half
    of BSPowerRail's area (0.011475 vs 0.02295) with no warning."""
    def break_one(d):
        for s in d["structures"]:
            for e in s["elements"]:
                if e.get("layer") == 300 and e.get("datatype") == 0 and e.get("xy"):
                    e["xy"][0] = [e["xy"][0][0], e["xy"][0][1], 0]
    p = _mutate(tmp_path, "bad_vertex", break_one)
    m = analyze_sidecar(p, "x.gds")
    affected = [r for r in m["layers"] if r["layer"] == 300 and r["datatype"] == 0]
    assert affected and all(r["area_um2"] is None for r in affected)
    assert any("malformed" in w for w in m["warnings"])
    # Untouched layers keep their measurements.
    assert any(r["area_um2"] is not None for r in m["layers"])


# ------------------------------------ H6: multi-structure sidecar bounding box

def test_multi_structure_sidecar_refuses_to_guess_a_bounding_box(tmp_path):
    """Unioning raw coordinates across structures ignores placement, and reported
    a 1000 um extent for a 0.1 um design."""
    _, js = _write_hierarchical_pair(tmp_path)
    m = analyze_sidecar(js, "hier.gds")
    assert m["layout"]["width_um"] is None
    assert m["layout"]["bbox_area_um2"] is None
    assert all(r["density_percent"] is None for r in m["layers"])
    assert any("placement" in w for w in m["warnings"])


def test_flat_reference_sidecar_still_measures_its_bounding_box():
    m = analyze_sidecar(JSON1, "NR2D1_1_RT_4.gds")
    assert m["layout"]["width_um"] == pytest.approx(0.15)
    assert m["layout"]["bbox_area_um2"] == pytest.approx(0.03)
    assert m["warnings"] == []


# ---------------------------------------------------------- M6: dbu cross-check

def test_wrong_sidecar_units_are_detected(tmp_path):
    """Counts agree while every sidecar-derived area is off by the ratio; this
    previously passed with agrees: true and no warning."""
    p = _mutate(tmp_path, "bad_units", lambda d: d.__setitem__("units", [1e-3, 1e-9]))
    m = analyze_pair(GDS1, p)
    assert m["consistency"]["dbu_mismatch"] is not None
    assert m["consistency"]["agrees"] is False
    assert any("database unit" in w for w in m["warnings"])
    # Geometry still comes from the GDS, so it stays correct.
    assert m["layout"]["bbox_area_um2"] == pytest.approx(0.03)


def test_dbu_uses_the_metre_field():
    assert _dbu_um({"units": [5e-05, 5e-11]}) == pytest.approx(5e-05)
    assert _dbu_um({"units": [1e-03, 1e-12]}) == pytest.approx(1e-06)
    assert _dbu_um({"units": []}) is None


# ------------------------------------------------- M3: phantom placement layer

def test_placements_do_not_create_a_phantom_layer(tmp_path):
    """sref elements were bucketed as a layer row with layer=None, inflating
    layer_count for a design that has two real layers."""
    _, js = _write_hierarchical_pair(tmp_path)
    m = analyze_sidecar(js, "hier.gds")
    assert all(r["layer"] is not None for r in m["layers"])
    assert m["design"]["layer_count"] == 2
    assert m["design"]["placement_count"] == 2


# ------------------------------ M4/M5: cell scope and array placement counting

def test_cells_are_scoped_to_the_analyzed_top_cell(tmp_path):
    """The multi-top warning says other tops are excluded from every count, but
    cells[] and cell_count covered the whole file."""
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(60, 0)
    layout.create_cell("AAA").shapes(li).insert(db.Box(0, 0, 100, 100))
    sub = layout.create_cell("SUB_OF_ZZZ")
    sub.shapes(li).insert(db.Box(0, 0, 50, 50))
    zzz = layout.create_cell("ZZZ")
    zzz.shapes(li).insert(db.Box(500, 500, 600, 600))
    zzz.insert(db.CellInstArray(sub.cell_index(), db.Trans()))
    p = tmp_path / "multitop.gds"
    layout.write(str(p))

    m = analyze_gds(p)
    # ZZZ is chosen over the alphabetically-first AAA because it holds more
    # geometry (its own shape plus SUB_OF_ZZZ's). Picking by name alone once
    # selected an *empty* placeholder top cell, which reported a near-empty design
    # and listed every real cell as unreachable.
    assert m["design"]["top_cell"] == "ZZZ"
    assert sorted(c["name"] for c in m["cells"]) == ["SUB_OF_ZZZ", "ZZZ"]
    assert m["design"]["cell_count"] == 2
    assert m["design"]["total_cell_count_in_file"] == 3
    assert m["design"]["polygon_count"] == 2


def test_top_cell_choice_prefers_content_over_name(tmp_path):
    """An empty top cell must never be the one analysed.

    A library GDS whose alphabetically-first top cell is an empty placeholder was
    reported as a near-empty design with every real cell flagged as an orphan.
    """
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(1, 0)
    layout.create_cell("AAA_EMPTY")                        # no shapes, no instances
    layout.create_cell("ZZZ_REAL").shapes(li).insert(db.Box(0, 0, 100, 100))
    p = tmp_path / "emptytop.gds"
    layout.write(str(p))

    m = analyze_gds(p)
    assert m["design"]["top_cell"] == "ZZZ_REAL"
    assert m["design"]["polygon_count"] == 1
    # Ties still fall back to the name, so the choice stays reproducible.
    layout2 = db.Layout()
    layout2.dbu = 0.001
    li2 = layout2.layer(1, 0)
    layout2.create_cell("BBB").shapes(li2).insert(db.Box(0, 0, 10, 10))
    layout2.create_cell("AAA").shapes(li2).insert(db.Box(50, 50, 60, 60))
    p2 = tmp_path / "tie.gds"
    layout2.write(str(p2))
    assert analyze_gds(p2)["design"]["top_cell"] == "AAA"


def test_instance_count_counts_placements_not_records(tmp_path):
    """A 2x2 CellInstArray is one instance record but four placements; reporting
    1 disagreed with the 4 polygons counted in the same metadata object."""
    layout = db.Layout()
    layout.dbu = 0.001
    leaf = layout.create_cell("LEAF")
    leaf.shapes(layout.layer(40, 0)).insert(db.Box(0, 0, 100, 100))
    top = layout.create_cell("TOP")
    top.insert(db.CellInstArray(leaf.cell_index(), db.Trans(),
                                db.Vector(200, 0), db.Vector(0, 200), 2, 2))
    p = tmp_path / "array.gds"
    layout.write(str(p))

    m = analyze_gds(p)
    cell = next(c for c in m["cells"] if c["name"] == "TOP")
    assert cell["instance_count"] == 4
    assert cell["instance_record_count"] == 1
    assert m["design"]["polygon_count"] == 4


# ----------------------------------------------- M7: suppressed units warning

def test_units_warning_is_not_suppressed_by_another_warning(tmp_path):
    """`if dbu_um is None and not warnings` hid the dimensions-unavailable notice
    whenever any other warning existed."""
    def two_problems(d):
        d["units"] = [0, 0]
        d["structures"][0]["elements"][0]["isVia"] = "true"
    p = _mutate(tmp_path, "two_problems", two_problems)
    m = analyze_sidecar(p, "x.gds")
    assert any("units" in w for w in m["warnings"])
    assert any("isVia" in w for w in m["warnings"])
    assert all(r["area_um2"] is None for r in m["layers"])


# ---------------------------------------- LOW: deterministic row ordering

def test_layer_rows_sort_numerically_by_layer_and_datatype():
    """The old key omitted datatype and compared layer numbers as strings, so
    "10" sorted before "2"."""
    m = analyze_sidecar(JSON1, "x.gds")
    keys = [(r["layer"], r["datatype"]) for r in m["layers"]]
    assert keys == sorted(keys, key=lambda k: (str(k[0]), str(k[1])))
    # Same input must always produce the same order.
    assert keys == [(r["layer"], r["datatype"]) for r in analyze_sidecar(JSON1, "x.gds")["layers"]]


def test_zero_bbox_reports_unknown_density_not_zero(tmp_path):
    """density 0.0 claims 0% coverage; with no bounding box there is no
    denominator, so coverage is unknown. layer_groups already said None."""
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(7, 0)).insert(db.Box(0, 0, 1000, 0))  # zero height
    p = tmp_path / "flat.gds"
    layout.write(str(p))
    m = analyze_gds(p)
    assert m["layout"]["bbox_area_um2"] == 0.0
    assert all(r["density_percent"] is None for r in m["layers"])
    assert all(g["union_density_percent"] is None for g in m["layer_groups"])


# ------------------------- geometric verification of the sidecar pairing

DCAP1_GDS = f"{SAMPLES}/DCAP0_1_RT_4.gds"
DCAP1_JSON = f"{SAMPLES}/DCAP0_1_RT_4.json"
DCAP2_GDS = f"{SAMPLES}/DCAP0_2_RT_4.gds"
DCAP2_JSON = f"{SAMPLES}/DCAP0_2_RT_4.json"
LYP = f"{SAMPLES}/Titan_layer_properties.lyp"


@pytest.fixture(scope="module")
def lyp():
    from analyzer.layermap import load_lyp
    return load_lyp(LYP)


def test_swapped_sidecar_is_caught_even_with_identical_counts(lyp):
    """The DCAP0 revisions have identical totals (56 polygons, 10 texts, 10 vias)
    and identical layer sets, so every count check passed for a swapped sidecar
    and the wrong revision's via semantics were attached silently. The sidecar's
    own coordinates are now rebuilt and compared with the measured geometry.
    """
    good = analyze_pair(DCAP1_GDS, DCAP1_JSON, layermap=lyp)
    assert good["consistency"]["agrees"] is True
    assert good["consistency"]["geometry_mismatches"] == []
    assert good["design"]["via_count"] == 10

    swapped = analyze_pair(DCAP1_GDS, DCAP2_JSON, layermap=lyp)
    assert swapped["consistency"]["agrees"] is False
    assert len(swapped["consistency"]["geometry_mismatches"]) == 4
    # Revision-specific semantics must not leak from the wrong file.
    assert swapped["design"]["via_count"] is None
    assert any("does not describe this GDS" in w for w in swapped["warnings"])


def test_geometry_mismatch_matches_an_independent_xor(lyp):
    """The layers the pairing check rejects must be exactly those a KLayout XOR
    finds, computed without reference to the analyzer."""
    def merged(path):
        layout = db.Layout()
        layout.read(path)
        top = list(layout.top_cells())[0]
        out = {}
        for li in layout.layer_indices():
            info = layout.get_info(li)
            r = db.Region()
            it = top.begin_shapes_rec(li)
            while not it.at_end():
                s = it.shape()
                if s.is_box():
                    r.insert(db.Polygon(s.box).transformed(it.trans()))
                elif s.is_polygon():
                    r.insert(s.polygon.transformed(it.trans()))
                it.next()
            if not r.is_empty():
                out[(info.layer, info.datatype)] = r.merged()
        return out

    A, B = merged(DCAP1_GDS), merged(DCAP2_GDS)
    xor_changed = {k for k in set(A) | set(B)
                   if not (A.get(k, db.Region()) ^ B.get(k, db.Region())).merged().is_empty()}

    swapped = analyze_pair(DCAP1_GDS, DCAP2_JSON, layermap=lyp)
    flagged = {(m["layer"], m["datatype"]) for m in swapped["consistency"]["geometry_mismatches"]}
    assert flagged == xor_changed


def test_moved_shapes_are_reported_by_the_comparison(lyp):
    """Identical counts and identical total areas, but shapes moved: the
    comparison previously said "No layer-level differences were detected"."""
    a = analyze_pair(DCAP1_GDS, DCAP1_JSON, layermap=lyp)
    b = analyze_pair(DCAP2_GDS, DCAP2_JSON, layermap=lyp)
    c = compare_metadata(a, b)

    assert c["summary"]["polygon_delta"] == 0
    assert c["summary"]["via_delta"] == 0
    assert c["summary"]["layers_with_geometry_change"] == 4

    reply = answer_comparison(c, "what changed?")
    assert "No layer-level differences" not in reply
    for tag in ("108/0", "111/0", "200/0", "201/0"):
        assert tag in reply, tag
    # M0 changed area as well as geometry and must not fall between the buckets.
    assert "-0.000300" in reply


def test_identical_input_reports_no_change(lyp):
    a = analyze_pair(DCAP1_GDS, DCAP1_JSON, layermap=lyp)
    c = compare_metadata(a, a)
    assert c["summary"]["layers_with_geometry_change"] == 0
    assert "identical" in answer_comparison(c, "what changed?")


def test_via_layer_count_is_stated_not_derived(lyp):
    """The model answered "5 via layers" for 6 because it had to count rows.
    The count is now a field, so it can be restated instead."""
    m = analyze_pair(DCAP1_GDS, DCAP1_JSON, layermap=lyp)
    rows_with_vias = [r for r in m["layers"] if r.get("via_count")]
    assert m["design"]["via_layer_count"] == len(rows_with_vias) == 6
    assert len(m["design"]["via_layer_names"]) == 6
    # And it must reach the prompt.
    from ai.llm import _compact
    assert '"via_layer_count":6' in _compact(m).replace(" ", "")
