"""Tests for the layout-versus-layout XOR comparison.

The XOR values are checked against a from-scratch KLayout computation, not against
the module's own output. Expected values were established independently before the
module was written.
"""
from __future__ import annotations

from pathlib import Path

import klayout.db as db
import pytest

from analyzer.layermap import default_layermap, load_lyp
from analyzer.xor_diff import compare_many, xor_compare

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
DCAP1, DCAP2 = SAMPLES / "DCAP0_1_RT_4.gds", SAMPLES / "DCAP0_2_RT_4.gds"
NR1, NR2 = SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


def _independent_xor(a: Path, b: Path):
    """Per-layer XOR region count and area, computed from scratch."""
    def load(path):
        layout = db.Layout()
        layout.read(str(path))
        top = sorted(layout.top_cells(), key=lambda c: c.name)[0]
        out = {}
        for li in layout.layer_indexes():
            info = layout.get_info(li)
            region = db.Region()
            it = top.begin_shapes_rec(li)
            while not it.at_end():
                s, t = it.shape(), it.trans()
                if s.is_box():
                    region.insert(db.Polygon(s.box).transformed(t))
                elif s.is_polygon():
                    region.insert(s.polygon.transformed(t))
                elif s.is_path():
                    region.insert(s.path.polygon().transformed(t))
                it.next()
            if not region.is_empty():
                out[(info.layer, info.datatype)] = region.merged()
        return float(layout.dbu), out

    dbu, ra = load(a)
    _, rb = load(b)
    result = {}
    for key in set(ra) | set(rb):
        x = ra.get(key, db.Region()) ^ rb.get(key, db.Region())
        if x.is_empty():
            continue
        result[key] = {
            "count": x.count(),
            "area": round(sum(float(p.area()) * dbu * dbu for p in x.each()), 12),
            "removed": (ra.get(key, db.Region()) - rb.get(key, db.Region())).count(),
            "added": (rb.get(key, db.Region()) - ra.get(key, db.Region())).count(),
        }
    return result


@pytest.mark.parametrize("a,b", [(DCAP1, DCAP2), (NR1, NR2), (DCAP1, NR1)])
def test_xor_matches_independent_computation(lm, a, b):
    ref = _independent_xor(a, b)
    result = xor_compare(a, b, lm)
    assert result["comparable"]
    mine = {(r["layer"], r["datatype"]): r for r in result["layers"] if not r["identical"]}
    assert set(mine) == set(ref)
    for key, exp in ref.items():
        row = mine[key]
        assert row["xor"]["count"] == exp["count"], key
        assert row["xor"]["area_um2"] == pytest.approx(exp["area"], abs=1e-12), key
        assert row["removed"]["count"] == exp["removed"], key
        assert row["added"]["count"] == exp["added"], key


def test_dcap_revision_difference_is_exactly_as_measured(lm):
    """Values established independently before the module existed."""
    s = xor_compare(DCAP1, DCAP2, lm)["summary"]
    assert s["layers_changed"] == 4
    assert s["difference_regions"] == 19
    assert s["total_xor_area_um2"] == pytest.approx(0.005308, abs=1e-9)
    assert s["largest_single_difference_um2"] == pytest.approx(0.0005, abs=1e-9)
    assert s["largest_difference_on_layer"] == "DVB"
    assert set(xor_compare(DCAP1, DCAP2, lm)["changed_layers"]) == {
        "P-VIAT", "DVB", "M0", "VIA0"}


def test_a_layout_compared_with_itself_is_identical(lm):
    for gds in (DCAP1, NR1):
        result = xor_compare(gds, gds, lm)
        assert result["summary"]["identical"]
        assert result["summary"]["layers_changed"] == 0
        assert result["changed_layers"] == []
        assert "identical on every layer" in result["mask_impact"]["observation"]


def test_removed_and_added_are_reported_separately(lm):
    """A reviewer needs to know which side a difference came from."""
    result = xor_compare(NR1, NR2, lm)
    rows = {r["name"]: r for r in result["layers"] if not r["identical"]}
    # M1 exists only in revision 2, so all of it is an addition.
    m1 = rows["M1"]
    assert m1["present_in_a"] is False and m1["present_in_b"] is True
    assert m1["removed"]["count"] == 0
    assert m1["added"]["count"] == m1["xor"]["count"]
    assert result["summary"]["layers_only_in_b"], "M1 should be listed as new in B"


def test_every_difference_has_a_location(lm):
    """"M0 changed" is not actionable; a reviewer must be able to navigate to it."""
    result = xor_compare(DCAP1, DCAP2, lm)
    for row in result["layers"]:
        if row["identical"]:
            continue
        assert row["xor"]["bbox_um"] and len(row["xor"]["bbox_um"]) == 4
        assert row["xor"]["locations"], row["name"]
        for loc in row["xor"]["locations"]:
            assert len(loc["centre_um"]) == 2
            assert loc["area_um2"] > 0
            assert loc["width_um"] > 0 and loc["height_um"] > 0
        # Locations are largest-first, so review order is by significance.
        areas = [loc["area_um2"] for loc in row["xor"]["locations"]]
        assert areas == sorted(areas, reverse=True), row["name"]


def test_locations_fall_inside_the_reported_bounding_box(lm):
    result = xor_compare(NR1, NR2, lm)
    for row in result["layers"]:
        if row["identical"]:
            continue
        left, bottom, right, top = row["xor"]["bbox_um"]
        for loc in row["xor"]["locations"]:
            x, y = loc["centre_um"]
            assert left <= x <= right, (row["name"], loc)
            assert bottom <= y <= top, (row["name"], loc)


def test_area_delta_agrees_with_the_per_layer_areas(lm):
    result = xor_compare(NR1, NR2, lm)
    for row in result["layers"]:
        assert row["area_delta_um2"] == pytest.approx(
            row["area_b_um2"] - row["area_a_um2"], abs=1e-12)


def test_tolerance_bins_small_differences_without_losing_them(lm):
    """Raising the tolerance moves differences between bins; it never drops any."""
    total = None
    previous_above = None
    for tol in (0.0, 0.013, 0.05):
        result = xor_compare(NR1, NR2, lm, tolerance_um=tol)
        changed = [r for r in result["layers"] if not r["identical"]]
        above = sum(r["above_tolerance"]["count"] for r in changed)
        below = sum(r["at_or_below_tolerance_count"] for r in changed)
        regions = result["summary"]["difference_regions"]
        assert above + below == regions, tol
        if total is None:
            total = regions
        else:
            assert regions == total, "the raw difference count must not depend on tolerance"
        if previous_above is not None:
            assert above <= previous_above, "a larger tolerance cannot classify more as significant"
        previous_above = above


def test_mask_impact_separates_interconnect_from_base_layers(lm):
    """The question that drives re-spin scope: which kinds of layer moved."""
    dcap = xor_compare(DCAP1, DCAP2, lm)["mask_impact"]
    assert dcap["base_layers_changed"] == []
    assert set(dcap["interconnect_layers_changed"]) == {"P-VIAT", "DVB", "M0", "VIA0"}
    assert "confined to interconnect" in dcap["observation"]

    # NR2D1's revision also touches NDIFFCON/PDIFFCON, which the name heuristic
    # calls contacts - a base role - so the verdict escalates.
    nr = xor_compare(NR1, NR2, lm)["mask_impact"]
    assert nr["base_layers_changed"] == ["NDIFFCON", "PDIFFCON"]
    assert "M1" in nr["interconnect_layers_changed"]
    assert "not confined to the interconnect" in nr["observation"]


def test_mask_impact_verdict_follows_corrected_roles(lm):
    """The verdict depends on the role classification, so a correction changes it.

    NDIFFCON reads as a contact by name but is local interconnect in this
    technology. With that corrected, the same geometric difference stops being a
    base-layer change - which is why the caveat names this exact case.
    """
    plain = xor_compare(NR1, NR2, lm)["mask_impact"]
    assert plain["base_layers_changed"] == ["NDIFFCON", "PDIFFCON"]

    corrected = xor_compare(NR1, NR2, lm,
                            role_overrides={"NDIFFCON": "metal", "PDIFFCON": "metal"})
    impact = corrected["mask_impact"]
    assert impact["base_layers_changed"] == []
    assert "confined to interconnect" in impact["observation"]
    assert {"NDIFFCON", "PDIFFCON"} <= set(impact["interconnect_layers_changed"])
    # And the corrected roles are no longer listed as name-inferred.
    assert "NDIFFCON" not in impact["roles_inferred_from_names"]


def test_mask_impact_discloses_which_roles_were_only_inferred(lm):
    impact = xor_compare(DCAP1, DCAP2, lm)["mask_impact"]
    assert impact["roles_inferred_from_names"], "name-inferred roles must be disclosed"
    assert "NDIFFCON reads as a contact" in impact["caveat"]


def test_mask_impact_is_labelled_as_an_observation_not_a_cost_verdict(lm):
    impact = xor_compare(DCAP1, DCAP2, lm)["mask_impact"]
    assert "mask-plan and foundry question" in impact["caveat"]
    nd = xor_compare(DCAP1, DCAP2, lm)["not_derivable"]
    assert "intent" in nd and "cost" in nd and "rule_compliance" in nd


def test_label_changes_are_reported_separately_from_geometry(lm):
    """Labels change no geometry but do change what LVS matches."""
    result = xor_compare(NR1, NR2, lm)
    text_rows = [r for r in result["layers"]
                 if r.get("texts_added") or r.get("texts_removed")]
    assert text_rows, "revision 2 adds M1 labels"
    assert result["summary"]["layers_with_text_changes"]
    for row in text_rows:
        assert row["texts_a"] != row["texts_b"] or row["texts_added"] or row["texts_removed"]


def test_differing_dbu_is_refused_rather_than_compared(lm, tmp_path):
    """Coordinates on different grids cannot be XORed meaningfully."""
    layout = db.Layout()
    layout.dbu = 0.002                        # the samples use 5e-05
    li = layout.layer(200, 0)
    layout.create_cell("DCAP0").shapes(li).insert(db.Box(0, 0, 100, 100))
    other = tmp_path / "coarse.gds"
    layout.write(str(other))

    result = xor_compare(DCAP1, other, lm)
    assert result["comparable"] is False
    assert "database units" in result["reason"]


def test_different_top_cell_names_are_flagged(lm):
    result = xor_compare(DCAP1, NR1, lm)
    assert result["comparable"]
    assert any("top cells have different names" in w
               for w in result["warnings"]), result["warnings"]


# --- multi-file comparison --------------------------------------------------

def test_pairwise_matrix_covers_every_pair(lm):
    files = [DCAP1, DCAP2, NR1, NR2]
    result = compare_many(files, lm)
    assert result["pair_count"] == 6                 # 4 choose 2
    names = result["files"]
    for a in names:
        for b in names:
            if a != b:
                assert result["matrix"][a][b]["comparable"]
                # The matrix must be symmetric.
                assert (result["matrix"][a][b]["total_xor_area_um2"]
                        == result["matrix"][b][a]["total_xor_area_um2"])


def test_matrix_identifies_the_closest_and_furthest_pair(lm):
    result = compare_many([DCAP1, DCAP2, NR1, NR2], lm)
    # Two revisions of one cell are closer than two different cells.
    assert set(result["most_similar_pair"]) == {DCAP1.name, DCAP2.name}
    assert set(result["most_different_pair"]) == {DCAP1.name, NR2.name}


def test_duplicate_uploads_are_reported_as_identical(lm):
    result = compare_many([DCAP1, DCAP1], lm)
    assert result["identical_pairs"] == [(DCAP1.name, DCAP1.name)]
    assert result["matrix"][DCAP1.name][DCAP1.name]["identical"]


def test_comparing_fewer_than_two_layouts_is_an_error(lm):
    with pytest.raises(ValueError, match="at least two"):
        compare_many([DCAP1], lm)


def test_xor_works_without_a_layer_map():
    """Geometry is GDS-only; the map only supplies names and roles."""
    result = xor_compare(DCAP1, DCAP2, None)
    assert result["comparable"]
    assert result["summary"]["difference_regions"] == 19
    assert all(r["role"] == "unknown" for r in result["layers"])
    # With no roles, mask impact cannot separate interconnect from base.
    assert result["mask_impact"]["base_layers_changed"] == []
    assert result["mask_impact"]["interconnect_layers_changed"] == []
