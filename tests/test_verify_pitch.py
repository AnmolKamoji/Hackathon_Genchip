"""Confirm the pitch numbers against KLayout measured a second, independent way.

`tests/test_pitch.py` checks `analyzer/pitch.py` against values measured by hand. That
catches a wrong answer but not a wrong *method*: the analyzer derives CPP three ways,
and all three read per-shape bounding boxes through the same `begin_shapes_rec`
iterator, so a fault in that extraction would corrupt all three identically and they
would still agree.

`tools/verify_pitch.py` measures the same quantities through different KLayout
machinery - the edge collections and the DRC edge-pair engine, which is what the ruler
tool and a rule deck use - by three methods that would not fail the same way:

    A  centre to centre of consecutive shapes  (Region.each() extents)
    B  same-side edge to same-side edge        (Region.edges() coordinates)
    C  measured gap plus measured width        (space_check / width_check)

These tests pin that agreement, pin the determinism of the measurement across repeated
runs, and - importantly - prove the comparison is capable of failing, so a pass means
something.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.verify_pitch import compare, measure

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
GDS = sorted(SAMPLES.glob("*.gds"))


@pytest.mark.parametrize("path", GDS, ids=lambda p: p.name)
def test_independent_klayout_measurement_agrees(path):
    """Three independent methods, three runs, must all match the analyzer."""
    ok, notes = compare(path, runs=3)
    assert ok, f"{path.name}:\n" + "\n".join(f"  {n}" for n in notes)
    # A pass with nothing checked would be vacuous, so require the substantive lines.
    joined = "\n".join(notes)
    assert "3 independent runs identical" in joined
    assert "CPP" in joined and "confirmed by" in joined
    assert "x CPP exactly" in joined


@pytest.mark.parametrize("path", GDS, ids=lambda p: p.name)
def test_measurement_is_deterministic(path):
    """Identical output across runs: nothing may depend on iteration order."""
    assert measure(path) == measure(path)


@pytest.mark.parametrize("path", GDS, ids=lambda p: p.name)
def test_cpp_confirmed_by_at_least_four_measurements(path):
    """CPP is 45 nm in every sample, and each of A, B and C must find it.

    Reported from the poly layers where they carry geometry and from the diffusion
    interconnects otherwise, which is why the count varies between the samples.
    """
    ref = measure(path)
    found = set()
    for name in ("NPOLY", "PPOLY", "NDIFFCON", "PDIFFCON"):
        entry = ref["layers"].get(name)
        if not entry:
            continue
        if 45.0 in entry["A_centre_to_centre_nm"]:
            found.add(f"{name}/A")
        if 45.0 in entry["B_edge_to_edge_nm"]:
            found.add(f"{name}/B")
        if entry["C_space_plus_width"]["pitch_nm"] == 45.0:
            found.add(f"{name}/C")
    assert len(found) >= 4, f"{path.name}: only {sorted(found)} found 45 nm"
    assert {m.split("/")[1] for m in found} == {"A", "B", "C"}, (
        f"{path.name}: not all three methods confirmed CPP - {sorted(found)}")


@pytest.mark.parametrize("path", GDS, ids=lambda p: p.name)
def test_metal_pitches_match_independent_measurement(path):
    """M0 21 nm, M1 30 nm, M2 28 nm from the track guides, methods A and B."""
    ref = measure(path)
    for guide, expected in (("M0-TRACK-GUIDE", 21.0), ("M1-TRACK-GUIDE", 30.0),
                            ("M2-TRACK-GUIDE", 28.0)):
        entry = ref["layers"][guide]
        for method in ("A_centre_to_centre_nm", "B_edge_to_edge_nm"):
            steps = entry[method]
            dominant = max(set(steps), key=steps.count)
            assert dominant == expected, (
                f"{path.name} {guide} {method}: dominant step {dominant}, "
                f"expected {expected}")


def test_comparison_can_fail():
    """Negative control: a wrong analyzer answer must be caught.

    Without this, every green run above could mean the comparison never checks
    anything. Feeding a deliberately wrong CPP through the same code path proves the
    mismatch is detected and named.
    """
    import analyzer.pitch as pitch_module

    real = pitch_module.analyze_pitch

    def wrong(outlines, filename=None):
        result = real(outlines, filename)
        result["gate_pitch"]["cpp_nm"] = 44.0        # one nanometre off
        return result

    pitch_module.analyze_pitch = wrong
    try:
        ok, notes = compare(GDS[0], runs=1)
    finally:
        pitch_module.analyze_pitch = real

    assert not ok, "a 44 nm CPP was accepted - the comparison does not check CPP"
    assert any("CPP MISMATCH" in n for n in notes), notes


def test_run_disagreement_is_caught(monkeypatch):
    """Negative control for the determinism gate.

    The multi-run requirement only means something if differing runs are rejected, so
    make the measurement return something different each call.
    """
    import tools.verify_pitch as vp

    calls = {"n": 0}
    real = vp.measure

    def unstable(path):
        calls["n"] += 1
        result = real(path)
        result["dbu_um"] = calls["n"]               # differs on every run
        return result

    monkeypatch.setattr(vp, "measure", unstable)
    ok, notes = vp.compare(GDS[0], runs=3)
    assert not ok
    assert any("did not agree with each other" in n for n in notes), notes
