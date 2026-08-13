"""The CLI must behave as the setup instructions describe it.

Specifically `python analyze.py data/samples/A.gds data/samples/B.gds` with no
extra flags, which is the command in the walkthrough.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


def run_cli(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(ROOT / "analyze.py"), *map(str, args)],
        capture_output=True, text=True, cwd=cwd,
    )


def test_documented_command_finds_sidecars_automatically(tmp_path):
    """The walkthrough omits --sidecar-dir, so sidecars must be auto-detected.

    Without this, the documented command produced no via counts and no layer
    names, and its output did not match reports_reference/.
    """
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_2_RT_4.gds", "--out", tmp_path)
    assert r.returncode == 0, r.stderr

    for stem in ("NR2D1_1_RT_4", "NR2D1_2_RT_4"):
        meta = json.loads((tmp_path / f"{stem}.metadata.json").read_text())
        assert meta["metadata_source"] == "fused"
        assert meta["design"]["via_count"] is not None

    comparison = json.loads((tmp_path / "comparison.json").read_text())
    assert comparison["summary"]["polygon_delta"] == 7
    assert comparison["summary"]["via_delta"] == 3
    assert comparison["comparable"] is True


def test_explicit_sidecar_dir_still_works(tmp_path):
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_2_RT_4.gds",
                "--sidecar-dir", "data/samples", "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "comparison.json").exists()


def test_gds_mode_ignores_sidecars(tmp_path):
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "--mode", "gds", "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    meta = json.loads((tmp_path / "NR2D1_1_RT_4.metadata.json").read_text())
    assert meta["metadata_source"] == "gds"
    assert meta["design"]["polygon_count"] == 60
    # The .lyp beside the samples is auto-detected, and it names the via layers, so
    # the via count is derived from those names rather than from the ignored
    # sidecar. It happens to agree with the sidecar, which is the point.
    assert meta["design"]["via_count"] == 6
    assert meta["design"]["via_count_source"].startswith("layer names")

    # With no layer map there is nothing to derive via-ness from, and a raw GDSII
    # stream does not label it, so it must be unavailable rather than 0.
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "--mode", "gds", "--layermap", "none",
                "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    bare = json.loads((tmp_path / "NR2D1_1_RT_4.metadata.json").read_text())
    assert bare["design"]["via_count"] is None
    assert bare["design"]["polygon_count"] == 60


def test_sidecar_mode_ignores_gds_geometry(tmp_path):
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "--mode", "sidecar", "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    meta = json.loads((tmp_path / "NR2D1_1_RT_4.metadata.json").read_text())
    assert meta["metadata_source"] == "sidecar"
    assert meta["design"]["via_count"] == 6


def test_single_file_writes_no_comparison(tmp_path):
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "comparison.json").exists()


def test_missing_file_fails_clearly(tmp_path):
    r = run_cli("data/samples/does_not_exist.gds", "--out", tmp_path)
    assert r.returncode != 0
    assert "not found" in (r.stderr + r.stdout).lower()


def test_three_files_are_analyzed_without_comparison(tmp_path):
    r = run_cli("data/samples/NR2D1_1_RT_4.gds", "data/samples/NR2D1_2_RT_4.gds",
                "data/samples/NR2D1_1_RT_4.gds", "--out", tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "comparison.json").exists()
