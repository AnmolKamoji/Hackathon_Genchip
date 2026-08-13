"""Tests for per-layer measurements and cell-hierarchy analysis.

Several of these exist because the question-routing layer used to answer these
questions with a *different* metric: "how many vertices?" came back with a polygon
count, "total metal area?" with the cell bounding-box area, and "what size are the
vias?" with the layout bounding box. A plausible number answering the wrong
question is the worst failure this tool can produce, so the measurements are
checked against independent computations and the routing is checked separately.
"""
from __future__ import annotations

import math
from pathlib import Path

import klayout.db as db
import pytest

from ai.deterministic import answer
from analyzer.gds_parser import analyze_gds, rank_top_cells
from analyzer.hierarchy import analyze_hierarchy
from analyzer.layermap import load_lyp
from analyzer.measurements import _arrangement, measure_layers, measure_vias

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
NR2D1 = SAMPLES / "NR2D1_1_RT_4.gds"
DCAP = SAMPLES / "DCAP0_1_RT_4.gds"
ALL = [NR2D1, DCAP, SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "DCAP0_2_RT_4.gds"]


@pytest.fixture(scope="module")
def lm():
    return load_lyp(SAMPLES / "Titan_layer_properties.lyp")


def _meta(gds, lm):
    m = analyze_gds(gds, layermap=lm)
    m["measurements"] = measure_layers(gds, lm)
    m["measurements"]["vias"] = measure_vias(m["measurements"])
    m["hierarchy"] = analyze_hierarchy(gds)
    return m


# --- measurements verified against independent computation -------------------

def _independent(gds: Path):
    """Perimeter, vertex count and min width from raw coordinates in plain Python."""
    layout = db.Layout()
    layout.read(str(gds))
    top = rank_top_cells(layout)[0]
    dbu = float(layout.dbu)
    out = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        polys = []
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            s, t = it.shape(), it.trans()
            if s.is_box():
                polys.append(db.Polygon(s.box).transformed(t))
            elif s.is_polygon():
                polys.append(s.polygon.transformed(t))
            elif s.is_path():
                polys.append(s.path.polygon().transformed(t))
            it.next()
        if not polys:
            continue
        per = 0.0
        vtx = 0
        min_w = None
        for p in polys:
            pts = [(q.x, q.y) for q in p.each_point_hull()]
            vtx += len(pts)
            for i in range(len(pts)):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % len(pts)]
                per += math.hypot(x2 - x1, y2 - y1)
            if len(pts) == 4:
                b = p.bbox()
                w = min(b.width(), b.height())
                min_w = w if min_w is None else min(min_w, w)
        out[(info.layer, info.datatype)] = (round(per * dbu, 9), vtx,
                                           round(min_w * dbu, 9) if min_w else None)
    return out


@pytest.mark.parametrize("gds", ALL)
def test_measurements_match_independent_computation(gds, lm):
    ref = _independent(gds)
    # measure_layers also reports text-only layers, which have no polygons for the
    # independent helper to measure, so compare only the layers carrying geometry.
    mine = {(r["layer"], r["datatype"]): r for r in measure_layers(gds, lm)["layers"]
            if r["shape_count"]}
    assert set(mine) == set(ref)
    for key, (per, vtx, min_w) in ref.items():
        row = mine[key]
        assert row["perimeter_um"] == pytest.approx(per, abs=1e-9), row["name"]
        assert row["vertex_count"] == vtx, row["name"]
        if min_w is not None:
            assert row["observed_min_width_um"] == pytest.approx(min_w, abs=1e-9), row["name"]


def test_as_drawn_and_merged_perimeter_differ_where_shapes_abut(lm):
    """A KLayout Region uses merged semantics, so asking an unmerged region for its
    perimeter returns the merged outline. Both numbers must be reported, distinctly."""
    rows = {r["name"]: r for r in measure_layers(NR2D1, lm)["layers"]}
    cut = rows["NPOLY-PATTERN-CUT"]
    assert cut["perimeter_um"] > cut["merged_perimeter_um"]
    m0 = rows["M0"]
    assert m0["perimeter_um"] == m0["merged_perimeter_um"]     # nothing abuts


def test_role_aggregate_area_is_a_real_sum(lm):
    """"Total metal area" must sum the metal layers, not report the cell bbox."""
    meas = measure_layers(NR2D1, lm)
    metal = meas["role_aggregates"]["metal"]
    assert sorted(metal["layers"]) == ["BM0", "M0"]
    rows = {r["name"]: r for r in meas["layers"]}
    assert metal["total_area_um2"] == pytest.approx(
        rows["M0"]["area_um2"] + rows["BM0"]["area_um2"])
    # And it must not equal the bounding-box area, which is what used to be given.
    assert metal["total_area_um2"] != pytest.approx(0.03)


def test_measurements_are_labelled_as_observed_not_compliant(lm):
    meas = measure_layers(NR2D1, lm)
    assert "NOT rule compliance" in meas["basis"]
    assert "PDK/DRC" in meas["not_derivable"]["rule_compliance"]
    for row in meas["layers"]:
        # Nothing may be named as a pass/fail.
        assert not any(k.startswith(("violation", "passes", "legal")) for k in row)


def test_role_aggregates_need_a_layermap():
    meas = measure_layers(NR2D1, None)
    assert meas["role_aggregates"] == {}
    assert any("no layer map" in w.lower() for w in meas["warnings"])
    # Per-layer geometry is GDS-only, so it must still be measured.
    assert all(r["perimeter_um"] is not None for r in meas["layers"] if r["shape_count"])


def test_via_sizes_are_per_shape_extents(lm):
    meas = measure_layers(NR2D1, lm)
    vias = {v["name"]: v for v in measure_vias(meas)["via_layers"]}
    assert vias["P-VIAG"]["uniform_size"] is True
    assert vias["P-VIAG"]["size_um"] == "0.015 x 0.012"
    assert vias["P-VIAG"]["count"] == 2


def test_no_paths_in_the_samples(lm):
    """These layouts are entirely boxes; the path branch must say so, not guess."""
    for row in measure_layers(NR2D1, lm)["layers"]:
        assert "path" not in (row["shape_types"] or {})
        assert row["path_widths_um"] is None


# --- arrangement / array detection -----------------------------------------

def test_arrangement_detects_a_row_a_grid_and_neither():
    dbu = 0.001
    row = _arrangement([(0, 0), (100, 0), (200, 0)], dbu)
    assert row["regular"] and "0.1 µm horizontal" in row["description"]

    grid = _arrangement([(0, 0), (100, 0), (0, 50), (100, 50)], dbu)
    assert grid["regular"] and "grid" in grid["description"]

    uneven = _arrangement([(0, 0), (100, 0), (350, 0)], dbu)
    assert not uneven["regular"] and "uneven" in uneven["description"]

    scattered = _arrangement([(0, 0), (37, 91)], dbu)
    assert not scattered["regular"] and "no array" in scattered["description"]

    single = _arrangement([(0, 0)], dbu)
    assert not single["regular"] and "single shape" in single["description"]


def test_gate_pitch_is_measured(lm):
    """The poly layers sit on a regular pitch; that is a checkable measurement."""
    rows = {r["name"]: r for r in measure_layers(NR2D1, lm)["layers"]}
    npoly = rows["NPOLY"]["arrangement"]
    assert npoly["regular"]
    assert npoly["horizontal_pitches_um"] == [0.045]


# --- hierarchy --------------------------------------------------------------

def test_samples_are_flat(lm):
    for gds in ALL:
        h = analyze_hierarchy(gds)
        assert h["max_depth_below_top"] == 0
        assert h["cell_count_total"] == 1
        assert h["empty_cells"] == [] and h["orphan_cells"] == []
        assert h["recursive_cells"] == [] and h["unresolved_reference_cells"] == []
        assert h["warnings"] == []


def test_hierarchy_depth_and_orphans_on_a_built_layout(tmp_path):
    layout = db.Layout()
    layout.dbu = 0.001
    li = layout.layer(1, 0)
    leaf = layout.create_cell("LEAF")
    leaf.shapes(li).insert(db.Box(0, 0, 100, 100))
    mid = layout.create_cell("MID")
    mid.insert(db.CellInstArray(leaf.cell_index(), db.Trans(), db.Vector(200, 0),
                                db.Vector(0, 0), 3, 1))
    top = layout.create_cell("TOP_A")
    top.insert(db.CellInstArray(mid.cell_index(), db.Trans()))
    other = layout.create_cell("TOP_B")
    other.shapes(li).insert(db.Box(0, 0, 10, 10))
    layout.create_cell("EMPTY_CELL")
    p = tmp_path / "hier.gds"
    layout.write(str(p))

    h = analyze_hierarchy(p)
    # TOP_A holds the most geometry, so it is the one analysed - not the
    # alphabetically-first EMPTY_CELL.
    assert h["top_cell"] == "TOP_A"
    assert h["max_depth_below_top"] == 2
    assert h["empty_cells"] == ["EMPTY_CELL"]
    assert sorted(h["orphan_cells"]) == ["EMPTY_CELL", "TOP_B"]
    cells = {c["name"]: c for c in h["cells"]}
    assert cells["MID"]["child_instance_placements"] == 3       # a 3x1 array
    assert cells["MID"]["child_instance_records"] == 1
    assert any("3 top-level cells" in w for w in h["warnings"])


# --- the routing that used to answer the wrong question ---------------------

@pytest.mark.parametrize("question,must_contain,must_not_contain", [
    ("How many vertices do the polygons have?", "240 polygon vertices", "60 polygons"),
    ("What is the total metal area?", "Total metal area is 0.02541", "bounding-box area"),
    ("What size are the vias?", "0.015 × 0.012 µm", "bounding box"),
    ("What is the perimeter of M0?", "0.482 µm of perimeter", "layer entries are in use"),
    ("How many cell instances are placed?", "No cell instances are placed", "1 cell(s)."),
    ("What is the minimum metal width observed?", "narrowest observed width is 0.012", "0.030000"),
])
def test_questions_are_answered_with_the_metric_asked_for(lm, question, must_contain,
                                                          must_not_contain):
    reply = answer(_meta(NR2D1, lm), question)
    assert reply is not None, question
    assert must_contain in reply, reply
    assert must_not_contain not in reply, reply


@pytest.mark.parametrize("question,topic", [
    ("Is the layout symmetric?", "symmetry"),
    ("Are there repeated structures?", "repeated structures"),
    ("Are there any geometry anomalies?", "anomaly"),
    ("Analyse the spatial distribution of geometry.", "Spatial distribution"),
    ("What is the layout health score?", "health or risk score"),
    ("What is the physical layout risk score?", "health or risk score"),
])
def test_uncomputed_metrics_are_refused_by_name(lm, question, topic):
    """These must say what is missing, not answer with a nearby number."""
    reply = answer(_meta(NR2D1, lm), question)
    assert reply is not None
    assert topic.lower() in reply.lower()
    assert "not available" in reply
    assert "was not measured" in reply


def test_measurement_answers_disclose_that_they_are_not_rule_checks(lm):
    for q in ("What is the minimum metal width observed?",
              "What is the minimum metal spacing observed?"):
        assert "not rule compliance" in answer(_meta(NR2D1, lm), q)


# --- zero versus unknown ----------------------------------------------------
# The distinction the whole tool rests on: 0 means "measured, and it is zero";
# None means "could not be determined". They are not interchangeable, and which
# one is correct depends on what the inputs can support.

def test_via_count_is_zero_not_none_on_a_non_via_layer(lm):
    """Once the map says M0 is metal, "how many vias on M0" is 0, not unknown."""
    rows = {r["name"]: r for r in analyze_gds(NR2D1, layermap=lm)["layers"]}
    assert rows["M0"]["via_count"] == 0            # determined
    assert rows["P-VIAG"]["via_count"] == 2
    assert rows["NDIFFCON"]["via_count"] == 3


def test_via_count_is_none_everywhere_without_a_layer_map():
    """With nothing to identify vias, every layer is unknown - not 0."""
    rows = analyze_gds(NR2D1)["layers"]
    assert {r["via_count"] for r in rows} == {None}


def test_empty_set_area_is_zero_but_empty_set_minimum_is_undefined(lm):
    """A text-only layer covers nothing, so area is 0. Its narrowest width does
    not exist, so that is None. Reporting 0 there would invent a measurement."""
    rows = {r["name"]: r for r in measure_layers(NR2D1, lm)["layers"]}
    label = rows["M0-LABEL"]
    assert label["shape_count"] == 0
    assert label["area_um2"] == 0.0
    assert label["perimeter_um"] == 0.0
    assert label["vertex_count"] == 0
    assert label["observed_min_width_um"] is None
    assert label["observed_min_space_um"] is None
    assert "text only" in label["undefined_because"]


def test_the_two_tables_agree_on_empty_layers(lm):
    """The layers table said 0.0 while the measurements table said None for the
    same text-only layer. Two views of one layer must not disagree."""
    m = analyze_gds(NR2D1, layermap=lm)
    meas = {(r["layer"], r["datatype"]): r for r in measure_layers(NR2D1, lm)["layers"]}
    for row in m["layers"]:
        mine = meas[(row["layer"], row["datatype"])]
        assert (row["area_um2"] == 0.0) == (mine["area_um2"] == 0.0), row["name"]
        assert row["area_um2"] == pytest.approx(mine["area_um2"], abs=1e-9), row["name"]


def test_schema_rejects_none_via_count_when_the_map_identified_vias(lm):
    from models.metadata import SchemaError, validate_metadata
    m = analyze_gds(NR2D1, layermap=lm)
    m["layers"][0]["via_count"] = None
    with pytest.raises(SchemaError, match="would claim it was undeterminable"):
        validate_metadata(m)


def test_schema_rejects_a_number_when_nothing_identified_vias():
    from models.metadata import SchemaError, validate_metadata
    m = analyze_gds(NR2D1)
    m["layers"][0]["via_count"] = 0
    with pytest.raises(SchemaError, match="must be None"):
        validate_metadata(m)
