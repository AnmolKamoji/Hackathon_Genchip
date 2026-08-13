import json
from pathlib import Path

import pytest

from analyzer.sidecar_parser import _dbu_um, analyze_sidecar

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"
REFERENCE = ROOT / "reports_reference"


def test_reference_sidecars():
    a = analyze_sidecar(SAMPLES / "NR2D1_1_RT_4.json", "NR2D1_1_RT_4.gds")
    b = analyze_sidecar(SAMPLES / "NR2D1_2_RT_4.json", "NR2D1_2_RT_4.gds")
    assert a["design"]["top_cell"] == "NR2D1"
    assert b["design"]["top_cell"] == "NR2D1"
    assert a["design"]["polygon_count"] == 60
    assert b["design"]["polygon_count"] == 67
    assert a["design"]["via_count"] == 6
    assert b["design"]["via_count"] == 9


@pytest.mark.parametrize("stem", ["NR2D1_1_RT_4", "NR2D1_2_RT_4"])
def test_matches_shipped_reference_reports(stem):
    """The bundled reports_reference/ files are the author's expected output.

    Every field the reference records must still match. Newer fields may be
    added alongside, so this is a subset check rather than dict equality.
    """
    got = analyze_sidecar(SAMPLES / f"{stem}.json", f"{stem}.gds")
    expected = json.loads((REFERENCE / f"{stem}.metadata.json").read_text())
    for field, value in expected["design"].items():
        assert got["design"][field] == value, f"design.{field}"
    assert got["layout"] == expected["layout"]

    # Compare keyed by identity, not by position: the row sort key was fixed to
    # include datatype (it previously ordered "10" before "2" lexicographically),
    # so display order legitimately differs from the reference while the data
    # must not.
    def keyed(meta):
        return {(r["layer"], r["datatype"], r["name"]): (r["polygon_count"], r["via_count"],
                                                         r["text_count"], r["shape_count"])
                for r in meta["layers"]}

    assert keyed(got) == keyed(expected)


def test_dbu_comes_from_the_metre_field():
    """GDSII UNITS is [dbu_in_user_units, dbu_in_metres]; only the second is physical."""
    assert _dbu_um({"units": [5e-05, 5e-11]}) == pytest.approx(5e-05)
    # A layout whose user unit is 1 nm rather than 1 um: the two fields diverge,
    # and taking units[0] would be wrong by 1000x.
    assert _dbu_um({"units": [1e-03, 1e-12]}) == pytest.approx(1e-06)
    assert _dbu_um({"units": []}) is None
    assert _dbu_um({}) is None


def test_geometry_is_derived_from_coordinates():
    a = analyze_sidecar(SAMPLES / "NR2D1_1_RT_4.json", "NR2D1_1_RT_4.gds")
    # Cell extent is recoverable from the sidecar xy data.
    assert a["cells"][0]["area_um2"] == pytest.approx(0.03)
    # Densities agree with KLayout's merged-region measurement for these files.
    dense = max((x for x in a["layers"] if x["density_percent"] is not None),
                key=lambda x: x["density_percent"])
    assert dense["name"] == "BSPowerRail"
    assert dense["density_percent"] == pytest.approx(76.5, abs=0.01)
    assert a["technology"]["area_method"] == "sum_of_polygons_unmerged"


def test_empty_sidecar_is_rejected(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"structures": []}))
    with pytest.raises(ValueError):
        analyze_sidecar(p)
