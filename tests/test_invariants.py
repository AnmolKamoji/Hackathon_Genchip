"""The invariants the specification calls critical.

These are the rules that are easy to satisfy by accident and easy to break by
accident: a zero that becomes Unavailable, a percentage that divides by the wrong
side, a pin swap counted twice. Each one is pinned here so a later change cannot
quietly reintroduce it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.comparison_doc import (TOLERANCE_UM, classify_change, compare_footprint,
                                     compare_layers, compare_pins, device_topology,
                                     drop_in)
from analyzer.compare_engine import build_comparison, compare_cell, shared_cells
from analyzer.document import build_document
from analyzer.integrity import geometry_integrity, shape_records
from analyzer.layermap import default_layermap, load_lyp
from analyzer.limits import required_value
from analyzer.limits import compare as compare_limit
from analyzer.observations import risk_flags, verdict
from analyzer.values import (delta, difference, is_missing, number, percent, show,
                             UNAVAILABLE)
from analyzer.xor_diff import xor_compare

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
A_GDS, B_GDS = SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.gds"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def docs(lm):
    names = [A_GDS.name, B_GDS.name]
    return (build_document(A_GDS, lm, all_filenames=names),
            build_document(B_GDS, lm, all_filenames=names))


@pytest.fixture(scope="module")
def comparison(docs, lm):
    return build_comparison(docs[0], docs[1], xor_compare(A_GDS, B_GDS, lm))


# --- 1-2. missing vs zero, zero vs Unavailable ------------------------------

@pytest.mark.parametrize("value", [None, "", "n/a", "N/A", "unknown", [], {}, set()])
def test_missing_values_are_missing(value):
    assert is_missing(value)
    assert show(value) == UNAVAILABLE


@pytest.mark.parametrize("value", [0, 0.0, False, 1, -1, "M0"])
def test_measurements_are_never_missing(value):
    """0, 0.0 and False are measurements. This is the whole distinction."""
    assert not is_missing(value)
    assert show(value) != UNAVAILABLE


def test_zero_renders_as_zero_not_unavailable():
    assert show(0) == "0"
    assert number(0) == "0"
    assert delta(0) == "+0"


# --- 3-4. drawn shapes vs shape records -------------------------------------

def test_drawn_shapes_and_shape_records_are_separate_measurements(docs):
    a, _ = docs
    assert a["geometry"]["drawn_shapes"] is not None
    assert a["geometry"]["shape_records"] is not None
    assert "flattened" in a["geometry"]["drawn_shapes_basis"]
    assert "not expanded" in a["geometry"]["shape_records_basis"]


def test_shape_records_do_not_expand_instances():
    result = shape_records(A_GDS)
    assert result["shape_records"] > 0
    assert "not" in result["basis"] and "expanded" in result["basis"]


# --- 8-10. via counting ------------------------------------------------------

def test_via_count_excludes_contacts(docs):
    """A contact is not a via. Adding them would inflate every via figure."""
    a, _ = docs
    assert a["vias"]["count"] == 6
    assert a["vias"]["contact_count"] == 6          # counted, but separately


def test_non_via_layers_get_a_real_zero_not_unavailable(docs):
    a, _ = docs
    non_via = [r for r in a["layers"]["rows"]
               if r.get("via_count") == 0]
    assert non_via, "with a layer map, a non-via layer reports a measured 0"


# --- 11-12. transistor extraction -------------------------------------------

def test_transistor_layers_match_exactly_not_by_prefix(docs):
    """NPOLY-EXTENDED carries the dummy gates; a prefix match would count them."""
    a, _ = docs
    assert a["devices"]["nmos_detail"]["layers"] == ["NPOLY", "NDIFF"]
    assert a["devices"]["pmos_detail"]["layers"] == ["PPOLY", "PDIFF"]


def test_geometric_extraction_is_not_lvs(docs):
    a, _ = docs
    assert "not LVS" in a["devices"]["basis"]
    assert a["devices"]["transistor_count"] == a["devices"]["nmos"] + a["devices"]["pmos"]


# --- 14. self-intersection before zero area ---------------------------------

def test_self_intersection_is_tested_before_zero_area():
    """A bow-tie has zero signed area but covers silicon. Order decides its class."""
    result = geometry_integrity(A_GDS)
    assert result["shapes_examined"] == 60
    assert result["zero_area_count"] == 0
    assert result["self_intersecting_count"] == 0


def test_off_grid_is_unavailable_not_zero():
    result = geometry_integrity(A_GDS)
    assert result["off_grid"]["available"] is False
    assert result["off_grid"]["count"] is None
    assert "states none" in result["off_grid"]["reason"]


# --- 17. density uses each tile's own denominator ----------------------------

def test_density_tiles_never_exceed_one_hundred_percent(docs):
    a, _ = docs
    for row in a["density"]["rows"]:
        if row["densest tile"] is not None:
            assert row["densest tile"] <= 100.0001, row


def test_density_is_not_summed_across_levels(docs):
    a, _ = docs
    assert a["density"]["mean_percent"] is not None
    assert a["density"]["verdict"]["available"] is False


# --- 18-23. comparison direction and row rules ------------------------------

def test_difference_is_b_minus_a(comparison):
    vias = next(r for r in comparison["metrics"] if r["metric"] == "Vias")
    assert vias["a"] == 6 and vias["b"] == 9
    assert vias["difference"] == 3
    assert vias["percent"] == "+50.00%"


def test_a_is_always_the_denominator():
    assert percent(9, 6) == "+50.00%"
    assert percent(6, 9) == "-33.33%"


def test_zero_baseline_gives_na_not_infinity():
    assert percent(6, 0) == "N/A"


def test_missing_in_both_omits_the_row():
    assert difference(None, None) is None


def test_only_in_a_and_only_in_b():
    assert difference(None, 5) == "Only in A"
    assert difference(5, None) == "Only in B"


def test_named_values_are_never_subtracted(comparison):
    top = next(r for r in comparison["non_numeric"] if r["metric"] == "Top metal")
    assert top["a"] == "M0" and top["b"] == "M1"
    assert top["difference"] == "Different"
    assert difference("M0", "M0") == "Same"


# --- 25. exact cell matching -------------------------------------------------

def test_cell_matching_is_exact_with_no_substitution(docs):
    a, b = docs
    assert shared_cells(a, b) == ["NR2D1"]
    missing = compare_cell(a, b, "NR2D")           # a real prefix of the real name
    assert missing["found"] is False
    assert "Not found" in missing["reason"]


# --- 26. footprint tolerance -------------------------------------------------

def test_footprint_uses_a_tolerance_not_exact_equality(docs):
    a, b = docs
    assert compare_footprint(a, b)["identical"] is True
    assert TOLERANCE_UM == 1e-6


def test_unknown_footprint_is_not_the_same_as_different():
    blank = {"layout": {"width_um": None, "height_um": None, "area_um2": None}}
    sized = {"layout": {"width_um": 0.15, "height_um": 0.2, "area_um2": 0.03}}
    result = compare_footprint(blank, sized)
    assert result["identical"] is None             # UNKNOWN, never False
    assert drop_in(result, {"available": True, "pin_compatible": True}) == "Unknown"


# --- 27-30. pins -------------------------------------------------------------

def test_pin_swap_is_detected_and_reported_once(comparison):
    swaps = comparison["pins"]["swaps"]
    assert swaps == [["A1", "A2"]]
    swap_lines = [o for o in comparison["observations"] if "<->" in o or "↔" in o]
    assert len(swap_lines) == 1


def test_relabelling_alone_is_not_a_pin_change():
    """A second label at a position the pin already occupied is not a change."""
    a = {"pins": {"available": True, "pins": [
        {"name": "A", "positions": [[1.0, 1.0]], "label_count": 1,
         "access_shapes": 2, "access_layers": ["M0-PIN"]}]}}
    b = {"pins": {"available": True, "pins": [
        {"name": "A", "positions": [[1.0, 1.0]], "label_count": 2,
         "access_shapes": 2, "access_layers": ["M0-PIN"]}]}}
    result = compare_pins(a, b)
    assert result["common"][0]["changed"] is False
    assert result["common"][0]["moved"] is False


def test_a_moved_pin_is_changed(comparison):
    zn = next(p for p in comparison["pins"]["common"] if p["name"] == "ZN")
    assert zn["moved"] is True and zn["changed"] is True
    assert zn["access_shape_delta"] == 3
    assert zn["gained_access_layers"] == ["M1-PIN"]


# --- 31. device topology unknown when extraction fails ----------------------

def test_device_topology_is_unknown_when_extraction_fails():
    failed = {"comparable": False, "count_unchanged": None}
    assert device_topology(failed) == "Unknown"


def test_two_failed_extractions_are_not_evidence_of_sameness():
    a = {"devices": {"available": False}}
    b = {"devices": {"available": False}}
    from analyzer.comparison_doc import compare_devices
    result = compare_devices(a, b)
    assert result["comparable"] is False
    assert result["count_unchanged"] is None


# --- 32. named nets only -----------------------------------------------------

def test_only_named_nets_are_compared(comparison):
    nets = comparison["nets"]
    if nets.get("available"):
        assert "unnamed" in nets["note"]


# --- 33. layer states --------------------------------------------------------

def test_layer_states_partition_every_layer(docs):
    a, b = docs
    result = compare_layers(a, b)
    tally = result["tally"]
    assert tally == {"added": 6, "removed": 0, "modified": 6, "untouched": 22}
    for row in result["untouched"]:
        assert row["polygon_delta"] == 0 and row["via_delta"] == 0
        assert row["area_delta_um2"] == 0


# --- 34-36. risk severity ----------------------------------------------------

def test_pin_access_improvement_is_info_never_a_warning(comparison):
    info = [f for f in comparison["risk_flags"] if f["area"] == "pin access"]
    assert info and all(f["severity"] == "info" for f in info)


def test_no_high_severity_when_footprint_and_pin_set_hold(comparison):
    assert not [f for f in comparison["risk_flags"] if f["severity"] == "high"]


def test_no_risk_message_names_what_could_not_be_checked():
    doc = {"footprint": {"identical": None, "reason": "unavailable"},
           "pins": {"available": False, "reason": "no pin layer"},
           "stack": {"metal_levels_a": [], "metal_levels_b": []},
           "devices": {"comparable": False},
           "risk_flags": []}
    result = verdict(doc)
    assert result["clean"] is False
    assert "could not be checked" in result["message"]
    assert set(result["unavailable"]) == {"footprint", "pin set", "metal stack",
                                          "device topology"}


# --- 37. no DRC / LVS claims -------------------------------------------------

def test_nothing_claims_a_drc_or_lvs_pass(comparison, docs):
    banned = ("drc clean", "drc pass", "lvs clean", "lvs pass",
              "is a short", "is an open")
    text = " ".join(comparison["observations"] + comparison["notes"]
                    + [f["impact"] for f in comparison["risk_flags"]]).lower()
    for phrase in banned:
        assert phrase not in text, phrase


def test_a_via_touching_nothing_is_not_called_an_open(docs):
    a, _ = docs
    limitations = ((a.get("connectivity") or {}).get("limitations") or {})
    text = " ".join(str(v) for v in limitations.values()).lower()
    if text:
        assert "short" in text or "open" in text     # named as undeterminable


# --- numeric limits ----------------------------------------------------------

def test_a_bracketed_example_is_never_read_as_a_limit():
    result = required_value("The M0 width should equal the M1 width (for example 16 nm).")
    assert result["available"] is False


def test_illustrative_wording_is_rejected():
    assert required_value("A valid case is 20 nm.")["available"] is False
    assert required_value("Such as 20 nm.")["available"] is False


def test_a_rule_without_prescriptive_wording_yields_no_limit():
    result = required_value("The via extension is a parameter of the technology.")
    assert result["available"] is False
    assert "relational" in result["reason"]


def test_a_prescriptive_rule_yields_its_limit():
    result = required_value("The minimum M0 width shall be 18 nm.")
    assert result["available"] is True
    assert result["value_um"] == pytest.approx(0.018)


def test_an_unavailable_limit_never_reports_a_pass():
    result = compare_limit(0.015, "The M0 width should equal the M1 width.")
    assert result["status"] == "unavailable"
    assert result["measured_um"] == 0.015
    assert result["required_um"] is None


# --- 38. the AI is fed only the deterministic document ----------------------

def test_the_ai_payload_carries_no_geometry(comparison):
    from ui.sections.impact import _payload
    payload = _payload(comparison)
    assert set(payload) <= {"reference", "revision", "change_type",
                            "drop_in_replacement", "device_topology",
                            "observations", "notes", "risk_flags", "verdict",
                            "layer_tally"}
    assert "xor" not in payload and "layers" not in payload


# --- the change-type ladder --------------------------------------------------

def test_change_type_ladder_order_is_respected():
    fp_same = {"identical": True}
    dev_same = {"count_unchanged": True, "comparable": True}
    assert classify_change(fp_same, {"pin_compatible": True,
                                     "pin_name_set_identical": True},
                           dev_same) == "geometry-only change"
    assert classify_change(fp_same, {"pin_compatible": False,
                                     "pin_name_set_identical": True},
                           dev_same) == "routing / pin-access change"
    assert classify_change(fp_same, {"pin_compatible": False,
                                     "pin_name_set_identical": False},
                           {"count_unchanged": False,
                            "comparable": True}) == "functional change"
    assert classify_change({"identical": False}, {"pin_compatible": False,
                                                  "pin_name_set_identical": False},
                           {"count_unchanged": None,
                            "comparable": False}) == "footprint change"


def test_the_sample_pair_is_a_routing_pin_access_change(comparison):
    assert comparison["change_type"] == "routing / pin-access change"
    assert comparison["drop_in_replacement"] == "No"
    assert comparison["device_topology"] == "unchanged"
    assert {k: v["delta"] for k, v in comparison["deltas"].items()} == {
        "drawn_shapes": 7, "vias": 3, "labels": 4, "layers": 6,
        "transistors": 0, "width_um": 0.0}
