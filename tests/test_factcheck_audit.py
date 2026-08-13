"""Tests for the number audit that guards model answers.

`tools/factcheck.py` is what catches the model stating a figure the analyzer never
measured. It is the last line of defence for the rule that the tool must never invent
a measurement - so it needs testing itself, in both directions: it must not flag
correct writing, and it must still catch a number that is not in the data.
"""
from __future__ import annotations

from tools.factcheck import audit


META = {
    "summary": {"area_delta_um2": -0.0003, "polygon_delta": 7},
    "layer": {"name": "M0", "area_um2": 0.00246, "density_percent": 11.7143},
}


def test_a_positive_figure_may_report_a_negative_delta():
    """"decreased by 0.0003" is the right way to say -0.0003."""
    _, bad = audit("x", "The M0 area decreased by 0.0003 um2.", META)
    assert bad == []


def test_a_negative_sign_the_data_does_not_support_is_still_caught():
    """The allowance is one-directional: prose may drop a minus, not add one."""
    _, bad = audit("x", "The polygon count changed by -7.", META)
    assert "-7" in bad, bad


def test_a_truncated_figure_is_caught():
    """11.7143 is not 11 - dropping the decimals overstates nothing but is not the
    measurement, and rounding must be rounding rather than truncation."""
    _, bad = audit("x", "M0 covers 11 percent of the cell.", META)
    assert "11" in bad, bad
    _, ok = audit("x", "M0 covers 11.7 percent of the cell.", META)
    assert ok == [], ok


def test_a_summed_total_is_caught():
    """The model must quote the metadata's own total, not add up a list."""
    _, bad = audit("x", "There are 9 shapes in total.", META)
    assert "9" in bad, bad


def test_an_exact_figure_passes():
    _, bad = audit("x", "M0 measures 0.00246 um2 across 7 more polygons.", META)
    assert bad == []


def test_the_developers_env_does_not_reach_the_tests():
    """Importing this module runs tools/factcheck.py, which calls load_dotenv().

    That import happens at collection, so a .env with ANTHROPIC_MODEL set used to
    change what the model-selection tests measured depending on which files were
    collected. conftest clears the provider configuration; this pins it, because the
    failure only shows up in a full-suite run and is easy to reintroduce.
    """
    import os

    from tests.conftest import CONFIG_VARS

    leaked = {name: "set" for name in CONFIG_VARS if name in os.environ}
    assert not leaked, f"configuration leaked into the tests: {sorted(leaked)}"


# --- the nanometre rule ------------------------------------------------------

def test_the_nanometre_rule_catches_a_wrong_figure():
    """Tech-file parameters are stated in nanometres, and no rule checked an nm claim
    before, so "the gate extension is 13 nm" passed unexamined."""
    from tools.claimcheck import Checker

    meta = {"classification": {"tech_parameters": {"parameters": {
        "Gate extension": {"value": 12.0, "unit": "nm"},
        "Metal0": {"value": [15.0, 12.0, 9.0], "sequence_nm": [15.0, 12.0, 9.0]}}}}}
    checker = Checker(meta)
    assert checker.nanometres == {9.0, 12.0, 15.0}

    _, bad = checker.audit("The gate extension is 12 nm.")
    assert bad == []
    _, bad = checker.audit("The gate extension is 13 nm.")
    assert any("13" in b for b in bad), bad
    _, bad = checker.audit("The profile starts with a 15 nm margin.")
    assert bad == []
    _, bad = checker.audit("The margin is 16 nm.")
    assert any("16" in b for b in bad), bad


def test_a_list_valued_pitch_field_is_admitted():
    """`steps_nm` is a list. Admitting only scalars flagged the exception step that
    the analyzer itself published in its own `note`, which is a faithful quotation."""
    from tools.claimcheck import Checker

    checker = Checker({"pitch": {"metal_pitches": {"M0": {
        "pitch_nm": 21.0, "steps_nm": [21.0, 31.0],
        "note": "2 of 3 steps are 21 nm; the exception(s) are 31 nm"}}}})
    assert {21.0, 31.0} <= checker.nanometres
    _, bad = checker.audit("2 of 3 steps are 21 nm; the exception is 31 nm.")
    assert bad == [], bad
