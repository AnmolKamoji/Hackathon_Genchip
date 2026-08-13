"""Tests for how results are presented, not just what they are.

The analysis was already correct when these were written; the complaint was that
it read badly. Four specific faults, each pinned here so a later change cannot
quietly reintroduce them:

* lengths quoted in µm at a node whose features are 12-25 nm;
* absolute areas with nothing to compare them against;
* 17 of 28 layer rows being pin/label/duplicate copies of other rows;
* findings ordered by layer number, burying the largest change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.gds_parser import analyze_gds
from analyzer.layermap import default_layermap, load_lyp
from analyzer.plots import (change_hotspot, density_profile, difference_grid,
                            difference_map, similarity_matrix)
from analyzer.present import (findings, headline, nm, pct_of, scale_note,
                              split_primary, um2)
from analyzer.xor_diff import compare_many, xor_compare

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
DCAP1, DCAP2 = SAMPLES / "DCAP0_1_RT_4.gds", SAMPLES / "DCAP0_2_RT_4.gds"
NR1, NR2 = SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def dcap_xor(lm):
    return xor_compare(DCAP1, DCAP2, lm)


# --- units ------------------------------------------------------------------

@pytest.mark.parametrize("um,expected", [
    (0.012, "12 nm"), (0.025, "25 nm"), (0.0005, "0.5 nm"), (0.115, "115 nm"),
    (None, "n/a"),
])
def test_lengths_are_shown_in_nanometres(um, expected):
    """A minimum-width wire reads as "12 nm", not "0.012 µm"."""
    assert nm(um) == expected


def test_areas_stay_in_square_microns():
    """Area is quoted in µm² in practice, so it is left alone."""
    assert um2(0.005308) == "0.005308 µm²"
    assert um2(None) == "n/a"


# --- scale ------------------------------------------------------------------

def test_absolute_figures_get_a_relative_one():
    """"0.005308 µm²" is unanswerable until you know the cell is 0.021 µm²."""
    assert pct_of(0.005308, 0.021) == "25.28%"
    assert pct_of(0.005308, None) == "n/a"          # unknown base, not 0%
    assert pct_of(None, 0.021) == "n/a"


@pytest.mark.parametrize("area,cell,phrase", [
    (0.005308, 0.021, "substantial"),      # 25% of the cell
    (0.0004, 0.021, "1.9% of the cell"),   # the middle band, stated plainly
    (0.0001, 0.021, "small edit"),         # 0.48%
    (0.000001, 0.021, "very localised"),   # under 0.1%
])
def test_scale_note_puts_a_difference_in_context(area, cell, phrase):
    assert phrase in scale_note(area, cell)


def test_scale_note_is_silent_when_the_base_is_unknown():
    assert scale_note(0.005, None) == ""


# --- the verdict comes first ------------------------------------------------

def test_headline_states_the_verdict_in_one_line(dcap_xor):
    head = headline(dcap_xor, 0.021)
    assert head["state"] == "interconnect-only"
    assert "4 of 31 layers differ" in head["headline"]
    assert "19 regions" in head["headline"]
    assert "of the cell" in head["headline"]          # scale, not a bare number
    assert "confined to interconnect" in head["detail"]


def test_headline_for_identical_layouts_says_so_plainly(lm):
    head = headline(xor_compare(DCAP1, DCAP1, lm), 0.021)
    assert head["state"] == "identical"
    assert "Identical" in head["headline"]
    assert "including labels" in head["detail"]


def test_headline_escalates_when_base_layers_move(lm):
    """A base-layer change must not read the same as a metal-only one."""
    head = headline(xor_compare(NR1, NR2, lm), 0.03)
    assert head["state"] == "base-layers"
    assert "not confined to the interconnect" in head["detail"]


def test_headline_handles_incomparable_and_absent_input(lm, tmp_path):
    import klayout.db as db
    layout = db.Layout()
    layout.dbu = 0.002
    layout.create_cell("X").shapes(layout.layer(200, 0)).insert(db.Box(0, 0, 10, 10))
    other = tmp_path / "coarse.gds"
    layout.write(str(other))
    assert headline(xor_compare(DCAP1, other, lm), 0.021)["state"] == "blocked"
    assert headline(None, 0.021)["state"] == "none"


# --- findings are ordered by magnitude --------------------------------------

def test_findings_are_largest_first_with_a_location(dcap_xor):
    rows = findings(dcap_xor, 0.021, limit=6)
    assert rows, "a changed layout must produce findings"
    areas = [r["area_um2"] for r in rows]
    assert areas == sorted(areas, reverse=True), "the biggest change must be first"
    top = rows[0]
    assert top["layer"] == "DVB"                     # established independently
    assert top["size"] == "20 nm × 25 nm"            # nm, not µm
    assert top["share_of_cell"].endswith("%")
    assert len(top["at_um"]) == 2
    # The change names the file it is in. "removed" begged the question
    # "relative to what?" and made the reader track which file was the baseline.
    assert top["change"] in ("only in DCAP0_1_RT_4", "only in DCAP0_2_RT_4")


def test_findings_name_the_file_a_difference_belongs_to(lm, dcap_xor):
    rows = findings(dcap_xor, 0.021, limit=40)
    labels = {r["change"] for r in rows}
    assert labels == {"only in DCAP0_1_RT_4", "only in DCAP0_2_RT_4"}
    # Every difference region gets a row, and each belongs to exactly one file.
    assert len(rows) == dcap_xor["summary"]["difference_regions"]


def test_findings_respect_the_limit_and_are_empty_when_identical(lm, dcap_xor):
    assert len(findings(dcap_xor, 0.021, limit=3)) == 3
    assert findings(xor_compare(DCAP1, DCAP1, lm), 0.021) == []


# --- duplicate rows are hidden by default -----------------------------------

def test_duplicate_layer_copies_are_separated_out(lm):
    """NDIFF and NDIFF-DUPLICATE showing the same area reads as double counting."""
    rows = analyze_gds(NR1, layermap=lm)["layers"]
    primary, derived = split_primary(rows, lm)
    assert len(rows) == 28
    assert len(primary) == 11 and len(derived) == 17
    names = {r["name"] for r in derived}
    assert {"NDIFF-DUPLICATE", "M0-PIN", "M0-LABEL", "M0-TRACK-GUIDE"} <= names
    # The real layers survive.
    assert {"M0", "BM0", "NDIFF", "NPOLY", "DVB"} <= {r["name"] for r in primary}
    # Nothing is lost: the two parts reconstruct the whole.
    assert len(primary) + len(derived) == len(rows)


def test_without_a_layer_map_nothing_is_hidden():
    """Guessing which rows are copies would risk hiding a real layer."""
    rows = analyze_gds(NR1)["layers"]
    primary, derived = split_primary(rows, None)
    assert derived == []
    assert len(primary) == len(rows)


# --- the figures ------------------------------------------------------------

def test_difference_map_draws_every_region_it_is_given(dcap_xor):
    fig = difference_map(dcap_xor, [0.0, 0.0, 0.105, 0.2])
    assert fig is not None
    assert len(fig.data) == dcap_xor["summary"]["difference_regions"] == 19
    # Equal aspect: a layout drawn out of proportion misleads.
    assert fig.layout.yaxis.scaleanchor == "x"
    assert fig.layout.yaxis.scaleratio == 1
    # Every trace carries its layer name, so the legend filters by layer.
    assert all(t.name for t in fig.data)


def test_difference_map_is_none_when_there_is_nothing_to_draw(lm):
    assert difference_map(xor_compare(DCAP1, DCAP1, lm)) is None


def test_difference_map_colours_additions_and_removals_differently(lm):
    """Red for gone, green for new — a reviewer should not have to read a key."""
    from analyzer.plots import ADDED, REMOVED
    fig = difference_map(xor_compare(NR1, NR2, lm))
    colours = {t.fillcolor for t in fig.data}
    assert ADDED in colours, "M1 is new in revision 2, so additions must appear"
    assert REMOVED in colours or "#ff7f0e" in colours


def test_density_profile_is_ordered_densest_first(lm):
    rows = analyze_gds(NR1, layermap=lm)["layers"]
    primary, _ = split_primary(rows, lm)
    fig = density_profile(primary)
    assert fig is not None
    values = list(fig.data[0].x)
    assert values == sorted(values, reverse=True)


def test_density_profile_is_none_without_density(lm):
    assert density_profile([{"name": "X", "polygon_count": 1}]) is None


def test_similarity_matrix_only_appears_for_three_or_more(lm):
    two = compare_many([DCAP1, DCAP2], lm)
    assert similarity_matrix(two) is None
    four = compare_many([DCAP1, DCAP2, NR1, NR2], lm)
    fig = similarity_matrix(four)
    assert fig is not None
    z = fig.data[0].z
    assert len(z) == 4 and len(z[0]) == 4
    # The diagonal is a file against itself, so it carries no value.
    assert all(z[i][i] is None for i in range(4))


# --- the difference map colours every region individually --------------------

def test_every_region_is_coloured_removed_or_added_never_a_third_colour(lm):
    """The fault this replaced: colour was decided per *layer*, so on a layer
    present in both files every region fell through to one "modified" colour -
    which was every region in a typical revision, making the map one flat colour."""
    from analyzer.plots import ADDED, REMOVED
    for a, b in ((DCAP1, DCAP2), (NR1, NR2)):
        fig = difference_map(xor_compare(a, b, lm))
        colours = {t.fillcolor for t in fig.data}
        assert colours <= {ADDED, REMOVED}, colours


def test_map_draws_exactly_the_removed_plus_added_regions(lm):
    """removed and added partition the XOR, so the counts must line up."""
    from analyzer.plots import ADDED, REMOVED
    xor = xor_compare(DCAP1, DCAP2, lm)
    fig = difference_map(xor)
    drawn_removed = sum(1 for t in fig.data if t.fillcolor == REMOVED)
    drawn_added = sum(1 for t in fig.data if t.fillcolor == ADDED)
    changed = [r for r in xor["layers"] if not r["identical"]]
    assert drawn_removed == sum(r["removed"]["count"] for r in changed)
    assert drawn_added == sum(r["added"]["count"] for r in changed)
    assert drawn_removed + drawn_added == xor["summary"]["difference_regions"]


def test_map_legend_has_one_entry_per_file_not_per_layer(lm):
    """The legend was eight rows for four layers, each repeating "removed"/"added"
    against the same two colours - the colour already carried that, and "removed"
    begged the question "from what?". Naming the two files answers it and leaves
    exactly two entries."""
    fig = difference_map(xor_compare(DCAP1, DCAP2, lm))
    shown = [t.name for t in fig.data if t.showlegend]
    assert shown == ["only in DCAP0_1_RT_4", "only in DCAP0_2_RT_4"]
    assert {t.legendgroup for t in fig.data} == set(shown)


def test_map_can_be_narrowed_to_chosen_layers(lm):
    """Isolating a layer belongs in a control, not in the legend."""
    xor = xor_compare(DCAP1, DCAP2, lm)
    everything = difference_map(xor)
    just_m0 = difference_map(xor, only_layers={"M0"})
    m0_regions = next(r for r in xor["layers"] if r["name"] == "M0")["xor"]["count"]
    assert len(just_m0.data) == m0_regions == 7
    assert len(just_m0.data) < len(everything.data)
    assert difference_map(xor, only_layers={"NOT_A_LAYER"}) is None


def test_grid_legend_names_the_reference(lm):
    multi = compare_many([NR1, NR2], lm)
    shown = [t.name for t in difference_grid(multi, NR1.name).data if t.showlegend]
    assert "only in NR2D1_1_RT_4" in shown
    assert "only in this layout" in shown


def test_outline_cap_is_large_enough_not_to_under_draw(lm):
    """The cap was 8, and two layers already sat on it - a bigger diff would have
    been silently drawn incomplete."""
    xor = xor_compare(DCAP1, DCAP2, lm)
    for row in xor["layers"]:
        if row["identical"]:
            continue
        for block in ("removed", "added"):
            part = row[block]
            assert len(part["locations"]) == part["count"], (row["name"], block)


# --- multi-file views -------------------------------------------------------

@pytest.fixture(scope="module")
def multi(lm):
    return compare_many([DCAP1, DCAP2, NR1, NR2], lm)


def test_difference_grid_has_one_panel_per_non_reference_layout(multi):
    fig = difference_grid(multi, DCAP1.name, [0.0, 0.0, 0.105, 0.2])
    assert fig is not None
    titles = [a.text for a in fig.layout.annotations]
    assert DCAP1.name not in titles
    assert set(titles) == {DCAP2.name, NR1.name, NR2.name}


def test_difference_grid_flips_the_sense_when_the_reference_is_the_second_file(lm):
    """"removed" means gone from B. If the reference is B the sense inverts, and
    drawing it unflipped would tell the reviewer the opposite of the truth."""
    from analyzer.plots import ADDED, REMOVED
    # NR2D1_2 adds M1 relative to NR2D1_1. With _1 as reference, M1 is *extra*.
    multi = compare_many([NR1, NR2], lm)
    with_ref_a = difference_grid(multi, NR1.name)
    extra = sum(1 for t in with_ref_a.data if t.fillcolor == ADDED)
    missing = sum(1 for t in with_ref_a.data if t.fillcolor == REMOVED)
    # With _2 as the reference the same geometry is now *missing*.
    with_ref_b = difference_grid(multi, NR2.name)
    extra_b = sum(1 for t in with_ref_b.data if t.fillcolor == ADDED)
    missing_b = sum(1 for t in with_ref_b.data if t.fillcolor == REMOVED)
    assert (extra, missing) == (missing_b, extra_b)
    assert extra != missing, "this pair is asymmetric, so the flip is observable"


def test_difference_grid_returns_none_for_a_lone_reference(lm):
    multi = compare_many([DCAP1, DCAP2], lm)
    assert difference_grid(multi, "not_a_file.gds") is not None    # both are "others"
    single = {"files": [DCAP1.name], "pairs": [], "matrix": {}}
    assert difference_grid(single, DCAP1.name) is None


def test_change_hotspot_bins_difference_area_over_the_cell(multi):
    fig = change_hotspot(multi, [0.0, 0.0, 0.15, 0.2], bins=12)
    assert fig is not None
    z = fig.data[0].z
    assert len(z[0]) == 12
    # Aspect-aware binning: a taller cell gets more rows than columns.
    assert len(z) > len(z[0])
    total = sum(v for row in z for v in row)
    assert total > 0
    # Every difference lands in a bin, so the total is the sum over all pairs.
    expected = sum(p["detail"]["summary"]["total_xor_area_um2"]
                   for p in multi["pairs"] if p.get("comparable"))
    assert total == pytest.approx(expected, rel=1e-9)


def test_change_hotspot_can_be_scoped_to_one_reference(multi):
    scoped = change_hotspot(multi, [0.0, 0.0, 0.105, 0.2], reference=DCAP1.name)
    assert scoped is not None
    assert DCAP1.name in scoped.layout.title.text
    scoped_total = sum(v for row in scoped.data[0].z for v in row)
    all_total = sum(v for row in change_hotspot(multi).data[0].z for v in row)
    assert scoped_total < all_total, "scoping to one reference must use fewer pairs"


def test_change_hotspot_is_none_when_nothing_differs(lm):
    same = compare_many([DCAP1, DCAP1], lm)
    assert change_hotspot(same) is None


# --- technology colours and the layer panel ---------------------------------

def test_lyp_supplies_a_colour_for_every_layer(lm):
    """The panel swatches and the technology-colour mode both need these, and they
    were being parsed and thrown away."""
    entries = lm["by_key"]
    assert all(e.get("fill_color") for e in entries.values())
    assert entries[(200, 0)]["fill_color"] == "#f3ff80"      # M0
    assert entries[(202, 0)]["fill_color"] == "#0000ff"      # M1
    assert entries[(111, 0)]["fill_color"] == "#ff0000"      # DVB
    assert all(e.get("dither_pattern") for e in entries.values())


def test_map_can_colour_by_technology_layer(lm):
    """The KLayout association: a layer is identified by its own colour, and the
    side is carried by the outline style instead."""
    xor = xor_compare(DCAP1, DCAP2, lm)
    colours = {e["technology_name"]: e["fill_color"] for e in lm["by_key"].values()}
    fig = difference_map(xor, colour_by="layer", layer_colours=colours)
    shown = [t.name for t in fig.data if t.showlegend]
    assert set(shown) == {"M0", "VIA0", "DVB", "P-VIAT"}
    assert colours["M0"] in {t.fillcolor for t in fig.data}
    # Solid for the first file, dotted for the second.
    assert {t.line.dash for t in fig.data} == {"solid", "dot"}


def test_colour_by_layer_falls_back_when_no_colours_are_supplied(lm):
    """With no layer map there are no technology colours, so it must not blank out."""
    from analyzer.plots import ADDED, REMOVED
    fig = difference_map(xor_compare(DCAP1, DCAP2, lm), colour_by="layer",
                         layer_colours=None)
    assert {t.fillcolor for t in fig.data} <= {ADDED, REMOVED}


def test_deselecting_every_layer_draws_nothing(lm):
    """An empty selection once fell back to "no filter" and drew every layer -
    the exact opposite of what was asked for."""
    xor = xor_compare(DCAP1, DCAP2, lm)
    assert difference_map(xor, only_layers=set()) is None
    assert difference_map(xor, only_layers=None) is not None


def test_swatch_renders_a_colour_chip():
    from ui.theme import swatch
    assert "#f3ff80" in swatch("#f3ff80")
    assert 'class="swatch"' in swatch(None)      # degrades rather than breaking
