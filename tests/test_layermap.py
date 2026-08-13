"""The KLayout .lyp layer map: technology names for numeric (layer, datatype)."""
import re
from pathlib import Path

import pytest

from ai.deterministic import answer
from analyzer.fused import analyze_pair
from analyzer.gds_parser import analyze_gds
from analyzer.layermap import find_layermap, load_lyp

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"
LYP = SAMPLES / "Titan_layer_properties.lyp"
GDS1 = SAMPLES / "NR2D1_1_RT_4.gds"
JSON1 = SAMPLES / "NR2D1_1_RT_4.json"


@pytest.fixture(scope="module")
def layermap():
    return load_lyp(LYP)


def test_parses_the_shipped_layer_map(layermap):
    assert layermap["entry_count"] == 49
    assert layermap["warnings"] == []
    assert layermap["by_key"][(300, 0)]["technology_name"] == "BM0"
    assert layermap["by_key"][(1, 0)]["technology_name"] == "CELL-BOUNDARY"


def test_secondary_layer_roles_are_classified(layermap):
    """The technology marks copies with -PIN / -DUPLICATE suffixes, which
    independently corroborates the duplication the parser detects by unioning
    regions."""
    by_key = layermap["by_key"]
    assert by_key[(300, 0)]["role"] == "drawing"
    assert by_key[(300, 2)]["role"] == "pin"
    assert by_key[(100, 1)]["role"] == "duplicate"
    assert by_key[(100, 0)]["role"] == "drawing"


def test_raw_gds_gains_real_layer_names(layermap):
    """Without a layer map a raw GDS can only offer `layer_300`."""
    plain = analyze_gds(GDS1)
    named = analyze_gds(GDS1, layermap)

    assert {r["name"] for r in plain["layers"]} & {"layer_300", "layer_1"}
    by_key = {(r["layer"], r["datatype"]): r for r in named["layers"]}
    assert by_key[(300, 0)]["name"] == "BM0"
    assert by_key[(300, 2)]["name"] == "BM0-PIN"
    assert by_key[(300, 2)]["technology_role"] == "pin"
    # A layer map removes two unavailable facts at once: it names the layers, and
    # because it names the *via* layers, it makes the via count derivable too.
    # What a .lyp still cannot supply is which levels each via joins.
    assert "layer_names" not in named["technology"]["unavailable_facts"]
    assert "via_count" not in named["technology"]["unavailable_facts"]
    assert "connectivity_stack" in named["technology"]["unavailable_facts"]
    assert plain["design"]["via_count"] is None          # bare GDS says nothing
    assert named["design"]["via_count"] == 6             # agrees with the sidecar
    assert named["design"]["via_count_source"].startswith("layer names")
    assert named["technology"]["layer_map_used"] == LYP.name


def test_geometry_is_unchanged_by_naming(layermap):
    """A layer map renames; it must not move a single number."""
    plain = analyze_gds(GDS1)
    named = analyze_gds(GDS1, layermap)
    assert plain["design"]["polygon_count"] == named["design"]["polygon_count"]
    assert plain["layout"] == named["layout"]
    a = {(r["layer"], r["datatype"]): r["area_um2"] for r in plain["layers"]}
    b = {(r["layer"], r["datatype"]): r["area_um2"] for r in named["layers"]}
    assert a == b


def test_raw_mode_area_uses_the_union_not_the_sum(layermap):
    """Groups were keyed by layer number while rows were named `layer_300`, so
    the lookup missed and the answer summed the two datatypes of layer 300:
    0.0459 instead of 0.02295.

    With a layer map the two datatypes have *different* names (BM0 and BM0-PIN),
    so each group holds one datatype and there is no duplication left to
    disclose - the technology file resolves what the geometric check had to
    detect. Either way the answer must never be the doubled 0.0459.
    """
    m = analyze_gds(GDS1, layermap)
    reply = answer(m, "What is the area of BM0?")
    assert "0.022950" in reply
    assert "0.045900" not in reply

    drawing = next(x for x in m["layer_groups"] if x["label"] == "BM0")
    pin = next(x for x in m["layer_groups"] if x["label"] == "BM0-PIN")
    assert drawing["datatypes"] == [[300, 0]]
    assert pin["datatypes"] == [[300, 2]]
    assert drawing["union_area_um2"] == pytest.approx(0.02295)
    assert drawing["geometry_duplicated_across_datatypes"] is False


def test_without_a_layer_map_the_duplication_is_still_disclosed():
    """No .lyp means both datatypes share the placeholder name `layer_300`, so
    the group does span them and the doubled sum must be disclosed, not used."""
    m = analyze_gds(GDS1)
    reply = answer(m, "What is the area of layer_300?")
    assert "0.022950" in reply
    assert "0.045900" in reply           # disclosed as the naive sum
    g = next(x for x in m["layer_groups"] if x["label"] == "layer_300")
    assert g["geometry_duplicated_across_datatypes"] is True
    assert g["union_area_um2"] == pytest.approx(0.02295)


def test_pin_and_drawing_layers_are_separated_by_the_layer_map(layermap):
    """The technology names the secondary copy, so it stops being an ambiguity."""
    m = analyze_gds(GDS1, layermap)
    by_name = {g["label"]: g for g in m["layer_groups"]}
    for drawing, secondary in [("BM0", "BM0-PIN"), ("M0", "M0-PIN"),
                               ("NDIFF", "NDIFF-DUPLICATE")]:
        assert drawing in by_name and secondary in by_name
        assert by_name[drawing]["geometry_duplicated_across_datatypes"] is False


def test_sidecar_names_are_not_overwritten(layermap):
    """The two files use different vocabularies: `BSPowerRail` (what the layer is
    for) and `BM0` (which mask it is). Neither may replace the other."""
    m = analyze_pair(GDS1, JSON1, layermap=layermap)
    row = next(r for r in m["layers"] if (r["layer"], r["datatype"]) == (300, 0))
    assert row["name"] == "BSPowerRail"
    assert row["technology_name"] == "BM0"
    assert row["name_source"] == "sidecar"


def test_placeholder_sidecar_names_adopt_the_mask_name(layermap):
    """Where the sidecar had no name it fell back to `layer_102`; the .lyp has a
    real name for that pair, so the placeholder is replaced."""
    m = analyze_pair(GDS1, JSON1, layermap=layermap)
    row = next(r for r in m["layers"] if (r["layer"], r["datatype"]) == (102, 2))
    assert row["name"] == "NPOLY-PATTERN-CUT"
    assert row["name_source"] == "lyp"


def test_questions_work_in_either_vocabulary(layermap):
    m = analyze_pair(GDS1, JSON1, layermap=layermap)
    for name in ("BSPowerRail", "BM0"):
        reply = answer(m, f"What is the area of {name}?")
        assert "0.022950" in reply, name
    assert answer(m, "Does this design contain BM0?").startswith("Yes.")


def test_fused_geometry_is_unchanged_by_naming(layermap):
    """The headline numbers must not move when a layer map is supplied."""
    plain = analyze_pair(GDS1, JSON1)
    named = analyze_pair(GDS1, JSON1, layermap=layermap)
    for field in ("polygon_count", "via_count", "text_count", "cell_count"):
        assert plain["design"][field] == named["design"][field]
    assert plain["layout"] == named["layout"]
    # Diffusion_Break keeps the corrected cross-layer sum.
    g = next(x for x in named["layer_groups"] if x["label"] == "Diffusion_Break")
    assert g["union_area_um2"] == pytest.approx(0.01725)


def test_lyp_resolves_the_sidecar_ambiguity(layermap):
    """The sidecar labels (102,1) as both Diffusion_Break and NMOSGate. The .lyp
    gives that pair one unambiguous mask name, which is what it physically is."""
    m = analyze_pair(GDS1, JSON1, layermap=layermap)
    rows = [r for r in m["layers"] if (r["layer"], r["datatype"]) == (102, 1)]
    assert {r["name"] for r in rows} == {"Diffusion_Break", "NMOSGate"}
    assert {r["technology_name"] for r in rows} == {"NPOLY-EXTENDED"}


def test_mismatched_layer_map_is_reported_not_applied(tmp_path):
    """A .lyp for another technology must not silently rename nothing in silence."""
    other = tmp_path / "other.lyp"
    other.write_text(
        '<?xml version="1.0"?><layer-properties>'
        '<properties><name>FOO</name><source>9001/0@1</source></properties>'
        '</layer-properties>')
    m = analyze_gds(GDS1, load_lyp(other))
    assert any("different technology" in w for w in m["warnings"])
    assert any(r["name"].startswith("layer_") for r in m["layers"])


def test_partial_coverage_is_reported(tmp_path):
    one = tmp_path / "one.lyp"
    one.write_text(
        '<?xml version="1.0"?><layer-properties>'
        '<properties><name>BM0</name><source>300/0@1</source></properties>'
        '</layer-properties>')
    m = analyze_gds(GDS1, load_lyp(one))
    assert any("not in the layer map" in w for w in m["warnings"])
    assert next(r for r in m["layers"] if (r["layer"], r["datatype"]) == (300, 0))["name"] == "BM0"


def test_unresolvable_sources_are_skipped_with_a_warning(tmp_path):
    p = tmp_path / "wild.lyp"
    p.write_text(
        '<?xml version="1.0"?><layer-properties>'
        '<properties><name>ALL</name><source>*/*@1</source></properties>'
        '<properties><name>BM0</name><source>300/0@1</source></properties>'
        '</layer-properties>')
    lm = load_lyp(p)
    assert lm["entry_count"] == 1
    assert any("could not be mapped" in w for w in lm["warnings"])


def test_conflicting_names_for_one_pair_are_reported(tmp_path):
    p = tmp_path / "dup.lyp"
    p.write_text(
        '<?xml version="1.0"?><layer-properties>'
        '<properties><name>FIRST</name><source>300/0@1</source></properties>'
        '<properties><name>SECOND</name><source>300/0@1</source></properties>'
        '</layer-properties>')
    lm = load_lyp(p)
    assert lm["by_key"][(300, 0)]["technology_name"] == "FIRST"
    assert any("more than one name" in w for w in lm["warnings"])


@pytest.mark.parametrize("body,reason", [
    ("not xml at all", "valid XML"),
    ('<?xml version="1.0"?><something-else/>', "layer-properties"),
    ('<?xml version="1.0"?><layer-properties/>', "no usable layer entries"),
])
def test_bad_layer_maps_are_rejected_clearly(tmp_path, body, reason):
    p = tmp_path / "bad.lyp"
    p.write_text(body)
    with pytest.raises(ValueError) as exc:
        load_lyp(p)
    assert reason in str(exc.value)


def test_layermap_is_auto_detected_next_to_the_gds():
    assert find_layermap(GDS1) == LYP


# ------------------------------------------------------- via counts from a .lyp

def test_via_count_is_derivable_from_the_layer_map(layermap):
    """A .lyp names the via layers, which makes the via count derivable.

    This was previously reported as unavailable even with a layer map present,
    while the measurements module counted the same shapes happily - the parser
    predated .lyp support and never revisited the rule.
    """
    m = analyze_gds(GDS1, layermap)
    d = m["design"]
    assert d["via_count"] == 6
    assert d["via_layer_count"] == 3
    assert sorted(d["via_layer_names"]) == ["DVB", "N-VIAT", "P-VIAG"]
    assert d["via_count_source"].startswith("layer names")


def test_a_bare_gds_still_reports_via_count_as_unknown():
    """The original invariant survives: with nothing to identify vias, `None`."""
    d = analyze_gds(GDS1)["design"]
    assert d["via_count"] is None
    assert d["via_count_source"] is None
    assert d["via_layer_names"] is None


def test_contacts_are_not_counted_as_vias(layermap):
    """Folding contacts in would have disagreed with the sidecar on every file."""
    d = analyze_gds(GDS1, layermap)["design"]
    assert d["contact_count"] == 6
    assert sorted(d["contact_layer_names"]) == ["NDIFFCON", "PDIFFCON"]
    assert d["via_count"] != d["via_count"] + d["contact_count"]


@pytest.mark.parametrize("gds,expected", [
    ("NR2D1_1_RT_4.gds", 6), ("NR2D1_2_RT_4.gds", 9),
    ("DCAP0_1_RT_4.gds", 10), ("DCAP0_2_RT_4.gds", 10),
])
def test_lyp_derived_via_count_agrees_with_the_sidecar(layermap, gds, expected):
    """Two independent sources: layer naming, and the sidecar's explicit isVia."""
    from analyzer.sidecar_parser import analyze_sidecar
    path = GDS1.parent / gds
    assert analyze_gds(path, layermap)["design"]["via_count"] == expected
    assert analyze_sidecar(path.with_suffix(".json"))["design"]["via_count"] == expected


def test_via_answers_never_mix_contacts_into_the_via_total(layermap):
    """The breakdown must sum to the headline count; it once listed 12 under 6."""
    from ai.deterministic import answer
    m = analyze_gds(GDS1, layermap)
    reply = answer(m, "How many vias are present?")
    assert "6 vias" in reply
    assert "`P-VIAG` (2), `N-VIAT` (2), `DVB` (2)" in reply
    assert "Contacts are counted separately" in reply
    listed = sum(int(n) for n in re.findall(r"\((\d+)\)", reply.split("Contacts")[0]))
    assert listed == m["design"]["via_count"]


def test_via_layer_question_is_not_swallowed_by_the_layer_listing(layermap):
    from ai.deterministic import answer
    reply = answer(analyze_gds(GDS1, layermap), "How many via layers are there?")
    assert "3 via layer(s)" in reply
    assert "layer entries are in use" not in reply
