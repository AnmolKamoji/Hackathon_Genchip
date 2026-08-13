"""Tests for design-rule checking against the GENCHIP Design Rule Manual.

A checker that never reports a violation is worthless, and the four reference
layouts are vendor cells that pass everything. So most of this file builds layouts
that deliberately break one rule each and asserts the break is caught - the same
negative-control discipline used for the fact-checker.

Two real defects were found this way and are pinned below:

* the M0 fixed-pitch rule was reported as violated because M0 sits on tracks at
  21, 42, 73 and 94 nm. The **track guide layer itself** has that spacing, so the
  layout follows the technology's declared grid and the violation was false;
* the "DVB must not extend beyond the cell boundary" check compared against the
  layout bounding box, which is computed *from* the geometry being tested - so
  anything outside merely enlarged the box and then sat inside it.
"""
from __future__ import annotations

from pathlib import Path

import klayout.db as db
import pytest

from analyzer.drc import (check_layout, detect_technology, load_rules,
                          rules_available)
from analyzer.layermap import default_layermap, load_lyp
from analyzer.measurements import shape_outlines

# The catalogue is transcribed from a manual that may not be redistributed, so it is
# not in version control. A fresh clone skips these rather than failing: the absence
# is expected, not a defect. `tools/extract_drm_rules.py` regenerates it.
pytestmark = pytest.mark.skipif(
    not rules_available(),
    reason="design rule catalogue absent - run tools/extract_drm_rules.py")

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
REFERENCE = ["DCAP0_1_RT_4.gds", "DCAP0_2_RT_4.gds",
             "NR2D1_1_RT_4.gds", "NR2D1_2_RT_4.gds"]


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def rules():
    return load_rules()


# --- the catalogue ----------------------------------------------------------

def test_rule_catalogue_matches_the_manual(rules):
    assert rules["rule_count"] == 71
    assert "GENCHIP Design Rule Manual" in rules["source"]
    # The sections the manual defines, all present.
    sections = {r["section"] for r in rules["rules"]}
    assert sections == {f"3.{n}" for n in range(1, 15)}
    # Every rule carries its own words, so a verdict can cite it.
    assert all(len(r["rule"]) > 12 for r in rules["rules"])


def test_technology_specific_rules_are_marked(rules):
    by_id = rules["by_id"]
    assert by_id["3.1.2"]["technologies"] == ["CFET"]        # exact pdiff/ndiff overlap
    assert by_id["3.6.1"]["technologies"] == ["FinFET"]      # pdiff in an nwell
    assert by_id["3.8.2"]["technologies"] == ["all"]         # M0 width uniformity


# --- technology inference ---------------------------------------------------

@pytest.mark.parametrize("gds", REFERENCE)
def test_reference_cells_are_identified_as_gaa(lm, gds):
    """pdiff and ndiff are separated (3.1.1) and there is no NWELL (3.6.1)."""
    tech = detect_technology(shape_outlines(SAMPLES / gds, lm))
    assert tech["technology"] == "GAA"
    assert tech["confidence"] == "inferred from geometry"
    assert "3.1.1" in tech["basis"]


# --- the reference cells pass ----------------------------------------------

@pytest.mark.parametrize("gds", REFERENCE)
def test_reference_cells_have_no_violations(lm, gds):
    result = check_layout(shape_outlines(SAMPLES / gds, lm))
    assert result["violations"] == [], [v["detail"] for v in result["violations"]]
    assert result["summary"]["pass"] >= 14


def test_m0_track_pitch_is_read_against_the_guide_not_assumed_uniform(lm):
    """The false violation this replaced.

    M0 sits at 21, 42, 73, 94 nm - gaps of 21, 31, 21. Demanding a uniform pitch
    reported a violation, but the M0 track guide has exactly that spacing, so the
    layout follows the technology's own grid.
    """
    outlines = shape_outlines(SAMPLES / "DCAP0_1_RT_4.gds", lm)
    check = next(r for r in check_layout(outlines)["results"] if r["id"] == "3.8.4")
    assert check["status"] == "pass"
    assert check["observed"]["guide_pitches_um"] == [0.021, 0.031]
    assert check["observed"]["guide_pitch_uniform"] is False
    assert "centred on a M0-TRACK-GUIDE track" in check["detail"]


def test_m1_spacing_equals_pitch_minus_width(lm):
    """Rule 3.10.5, and the axis bug that made the expected space negative."""
    outlines = shape_outlines(SAMPLES / "DCAP0_1_RT_4.gds", lm)
    check = next(r for r in check_layout(outlines)["results"] if r["id"] == "3.10.5")
    assert check["status"] == "pass"
    observed = check["observed"]
    assert observed["pitch_um"] == 0.03 and observed["width_um"] == 0.018
    assert observed["expected_space_um"] == pytest.approx(0.012)
    assert observed["measured_space_um"] == [pytest.approx(0.012)]


def test_a_clean_result_says_it_is_not_a_signoff_drc(lm):
    result = check_layout(shape_outlines(SAMPLES / "NR2D1_1_RT_4.gds", lm))
    assert "not a signoff DRC" in result["caveat"]
    assert result["summary"]["rules_checked"] < result["summary"]["rules_in_manual"]
    assert result["rules_not_checked"], "the unchecked rules must be listed"
    assert "absolute_limits" in result["not_derivable"]


# --- negative controls: each rule broken on purpose -------------------------

def _build(path: Path, mutate=None) -> Path:
    """A minimal GAA-style cell that passes every implemented rule."""
    layout = db.Layout()
    layout.dbu = 5e-05
    cell = layout.create_cell("TESTCELL")
    scale = 1e-3 / layout.dbu

    def box(layer, datatype, x0, y0, x1, y1):
        cell.shapes(layout.layer(layer, datatype)).insert(
            db.Box(int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))

    box(1, 0, 0, 0, 100, 120)              # cell boundary
    box(100, 0, 10, 10, 90, 25)            # ndiff, 15 nm tall
    box(101, 0, 10, 95, 90, 110)           # pdiff, 15 nm tall
    box(102, 0, 20, 10, 35, 49)            # npoly, 15 nm wide
    box(103, 0, 20, 71, 35, 110)           # ppoly, 15 nm wide
    box(104, 0, 45, 10, 65, 45)            # ndiffcon, 20 nm wide
    box(105, 0, 45, 75, 65, 110)           # pdiffcon, 20 nm wide
    box(121, 0, 0, 0, 15, 120)             # dummy gate, 15 nm wide
    for y in (21, 42, 73, 94):             # M0 on its guides, 12 nm wide
        box(220, 0, 0, y - 6, 100, y + 6)
        box(200, 0, 10, y - 6, 90, y + 6)
        box(200, 2, 10, y - 6, 90, y + 6)  # pin identical to drawing
    box(221, 0, 13.5, 0, 31.5, 120)        # M1 guide
    box(202, 0, 13.5, 10, 31.5, 110)       # M1, 18 nm wide, on the guide
    box(202, 2, 13.5, 10, 31.5, 110)
    box(201, 0, 13.5, 15, 31.5, 27)        # via0 over M0 and M1
    box(109, 0, 50, 15, 60, 27)            # nviat on ndiffcon
    box(107, 0, 25, 36, 30, 48)            # nviag on npoly
    box(300, 0, 0, 0, 100, 12)             # BM0
    box(300, 2, 0, 0, 100, 12)
    box(111, 0, 50, 0, 70, 12)             # DVB over BM0
    if mutate:
        mutate(box, cell, layout)
    layout.write(str(path))
    return path


def _violations(path: Path, lm) -> set[str]:
    return {v["id"] for v in check_layout(shape_outlines(path, lm))["violations"]}


def test_the_synthetic_reference_cell_is_clean(lm, tmp_path):
    """Without this, every negative control below could pass for the wrong reason."""
    assert _violations(_build(tmp_path / "clean.gds"), lm) == set()


@pytest.mark.parametrize("name,rule_id,mutate", [
    # 3.8.2: all M0 polygons must share one width.
    ("wide_m0", "3.8.2",
     lambda box, cell, ly: box(200, 0, 10, 110, 90, 128)),
    # 3.8.4: M0 must be centred on a track guide.
    ("offgrid_m0", "3.8.4",
     lambda box, cell, ly: box(200, 0, 10, 60, 90, 72)),
    # 3.1.5: pdiff and ndiff widths must be equal.
    ("diff_mismatch", "3.1.5",
     lambda box, cell, ly: (cell.shapes(ly.layer(101, 0)).clear(),
                            box(101, 0, 10, 95, 90, 115))),
    # 3.9.4: via0 must sit on M0 and M1.
    ("via_nowhere", "3.9.4",
     lambda box, cell, ly: box(201, 0, 80, 110, 92, 118)),
    # 3.11.3: DVB must stay inside the cell boundary.
    ("dvb_outside", "3.11.3",
     lambda box, cell, ly: box(111, 0, 90, 0, 130, 12)),
    # 3.14.3: the M0 pin layer must match M0 exactly.
    ("pin_mismatch", "3.14.3",
     lambda box, cell, ly: box(200, 2, 10, 110, 50, 122)),
    # 3.2.5: p-poly and n-poly widths must be equal.
    ("poly_mismatch", "3.2.5",
     lambda box, cell, ly: (cell.shapes(ly.layer(103, 0)).clear(),
                            box(103, 0, 20, 71, 30, 110))),
    # 3.4.5: dummy gate width must equal the poly width.
    ("dummy_mismatch", "3.4.5",
     lambda box, cell, ly: (cell.shapes(ly.layer(121, 0)).clear(),
                            box(121, 0, 0, 0, 25, 120))),
    # 3.12.2: BM0 width must be uniform.
    ("bm0_mismatch", "3.12.2",
     lambda box, cell, ly: box(300, 0, 0, 100, 100, 120)),
])
def test_each_broken_rule_is_caught(lm, tmp_path, name, rule_id, mutate):
    found = _violations(_build(tmp_path / f"{name}.gds", mutate), lm)
    assert rule_id in found, f"{name}: expected {rule_id}, got {sorted(found)}"


def test_out_of_bounds_is_not_masked_by_the_bounding_box(lm, tmp_path):
    """The bounding box grows to contain whatever is placed outside, so it can never
    detect an escape. The check must use the CELL-BOUNDARY layer."""
    path = _build(tmp_path / "escape.gds",
                  lambda box, cell, ly: box(111, 0, 90, 0, 130, 12))
    outlines = shape_outlines(path, lm)
    # The layout bbox has indeed expanded past the boundary layer...
    boundary = next(r for r in outlines["layers"] if r["name"] == "CELL-BOUNDARY")
    boundary_right = max(s["left_um"] + s["width_um"] for s in boundary["shapes"])
    assert outlines["cell_bbox_um"][2] > boundary_right
    # ...and the violation is still reported.
    assert "3.11.3" in _violations(path, lm)


def test_boundary_check_is_skipped_rather_than_faked_without_the_layer(lm, tmp_path):
    def drop_boundary(box, cell, ly):
        cell.shapes(ly.layer(1, 0)).clear()
        box(111, 0, 90, 0, 130, 12)
    path = _build(tmp_path / "noboundary.gds", drop_boundary)
    check = next(r for r in check_layout(shape_outlines(path, lm))["results"]
                 if r["id"] == "3.11.3")
    assert check["status"] == "not checked"
    assert "circular" in check["detail"] or "derived from this geometry" in check["detail"]


def test_cfet_only_rules_are_marked_not_applicable_on_a_gaa_cell(lm, tmp_path):
    """Rules the manual scopes to CFET must not be scored against a GAA layout."""
    result = check_layout(shape_outlines(_build(tmp_path / "gaa.gds"), lm))
    assert result["technology"]["used"] == "GAA"
    for row in result["results"]:
        if "CFET" in row["rule"] and row["status"] == "violation":
            pytest.fail(f"CFET rule {row['id']} scored against a GAA layout")


def test_supplied_technology_overrides_the_inference(lm, tmp_path):
    path = _build(tmp_path / "forced.gds")
    result = check_layout(shape_outlines(path, lm), technology="CFET")
    assert result["technology"]["used"] == "CFET"
    assert result["technology"]["supplied"] is True
    # And a GAA/FinFET-only rule is then set aside rather than scored.
    scoped = [r for r in result["results"]
              if r["id"] == "3.7.6" and r["status"] == "not applicable"]
    assert scoped, "a GAA/FinFET-only rule should be not-applicable under CFET"


def test_every_violation_cites_the_manual(lm, tmp_path):
    path = _build(tmp_path / "broken.gds",
                  lambda box, cell, ly: box(200, 0, 10, 110, 90, 128))
    for violation in check_layout(shape_outlines(path, lm))["violations"]:
        assert violation["id"] and violation["section"].startswith("3.")
        assert len(violation["rule"]) > 12
        assert violation["detail"]
