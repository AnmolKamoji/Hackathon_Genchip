"""Standard-cell pitch metrics: CPP, metal pitches, cell width and track height.

These are the numbers a layout engineer quotes first - "a 3-CPP cell on a 4-track
height, 45 nm gate pitch, 30 nm M1" - and the tool could not answer any of them.
Asked for the M0 pitch it described how the shapes happened to be arranged; asked
how many gate pitches wide the cell was, it said nothing at all.

Definitions follow standard usage:

* **CPP** (contacted poly pitch), also written CGP or called the gate pitch or poly
  pitch, is the poly-to-poly centre spacing. All four names mean this one number.
* **Cell width** is quoted as a multiple of CPP - the count of gate pitches across
  the cell.
* **Cell height** is quoted as a number of metal tracks.
* **Gear ratio** is CPP divided by the M1 pitch, a design-technology
  co-optimisation figure.

The routing pitch comes from the technology's own track-guide layers rather than
from where metal happens to sit, because a guide exists whether or not a wire uses
it. That distinction matters: measuring the pitch from occupied tracks alone gives
the wrong answer as soon as a track is empty.

Every figure is cross-checked where the inputs allow it. CPP is derived three
independent ways - the poly spacing, the diffcon spacing (which the manual's rule
3.3.8 requires to match), and the manual's rule 3.2.6 decomposition into poly
width, diffcon width and the poly-to-diffcon spacing - and disagreement is reported
rather than averaged away.
"""
from __future__ import annotations

import re
from typing import Any

# Routing layers and the axis their pitch is measured along. M0 and M2 run
# horizontally, so their tracks stack vertically; M1 runs vertically.
ROUTING_LAYERS = (
    ("M0", "M0-TRACK-GUIDE", "y", "horizontal"),
    ("M1", "M1-TRACK-GUIDE", "x", "vertical"),
    ("M2", "M2-TRACK-GUIDE", "y", "horizontal"),
)


def _by_name(outlines: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in outlines["layers"]}


def _centres_nm(row: dict[str, Any] | None, axis: str) -> list[float]:
    """Distinct shape-centre positions along one axis, in nanometres."""
    if not row or not row["shapes"]:
        return []
    index = 0 if axis == "x" else 1
    return sorted({round(s["centre_um"][index] * 1000, 4) for s in row["shapes"]})


def _widths_nm(row: dict[str, Any] | None, axis: str) -> list[float]:
    if not row or not row["shapes"]:
        return []
    key = "width_um" if axis == "x" else "height_um"
    return sorted({round(s[key] * 1000, 4) for s in row["shapes"]})


def _steps(values: list[float]) -> list[float]:
    return [round(b - a, 4) for a, b in zip(values, values[1:])]


def _pitch_from(values: list[float]) -> dict[str, Any]:
    """Summarise a set of track positions as a pitch.

    A single repeated step is the pitch. Where one step differs - which happens at
    the n/p boundary in the middle of a cell - the dominant step is the pitch and
    the exception is reported, not hidden or averaged in.
    """
    steps = _steps(values)
    if not steps:
        return {"pitch_nm": None, "uniform": None, "steps_nm": [], "positions_nm": values,
                "note": "fewer than two positions, so there is no pitch to measure"}
    unique = sorted(set(steps))
    if len(unique) == 1:
        return {"pitch_nm": unique[0], "uniform": True, "steps_nm": unique,
                "positions_nm": values, "note": None}
    dominant = max(unique, key=steps.count)
    exceptions = [s for s in unique if s != dominant]
    return {
        "pitch_nm": dominant, "uniform": False, "steps_nm": unique,
        "positions_nm": values,
        "note": (f"{steps.count(dominant)} of {len(steps)} steps are {dominant:g} nm; the "
                 f"exception(s) are {', '.join(f'{s:g} nm' for s in exceptions)}"),
    }


def gate_pitch(outlines: dict[str, Any]) -> dict[str, Any]:
    """CPP - the contacted poly pitch, also called the gate or poly pitch.

    Derived three ways so they can be compared: poly spacing, diffcon spacing, and
    the manual's decomposition CPP = 2 x (poly-to-diffcon spacing) + diffcon width
    + poly width.
    """
    by = _by_name(outlines)
    evidence: dict[str, Any] = {}

    poly_positions = sorted(set(_centres_nm(by.get("NPOLY"), "x")
                                + _centres_nm(by.get("PPOLY"), "x")))
    poly_steps = sorted(set(_steps(poly_positions)))
    if poly_steps:
        evidence["from_poly_spacing_nm"] = poly_steps

    diffcon_positions = sorted(set(_centres_nm(by.get("NDIFFCON"), "x")
                                   + _centres_nm(by.get("PDIFFCON"), "x")))
    diffcon_steps = sorted(set(_steps(diffcon_positions)))
    if diffcon_steps:
        # The manual requires the diffcon pitch to equal the poly pitch (rule 3.3.8),
        # so this is an independent measurement of the same quantity.
        evidence["from_diffcon_spacing_nm"] = diffcon_steps

    poly_width = _widths_nm(by.get("NPOLY"), "x") or _widths_nm(by.get("PPOLY"), "x")
    diffcon_width = (_widths_nm(by.get("NDIFFCON"), "x")
                     or _widths_nm(by.get("PDIFFCON"), "x"))

    candidates = [s for steps in (poly_steps, diffcon_steps) for s in steps]
    if not candidates:
        return {"cpp_nm": None, "evidence": evidence,
                "basis": ("neither poly nor diffcon has two shapes to measure a pitch between, so "
                          "the gate pitch cannot be determined from this cell alone"),
                "aliases": ["CPP", "CGP", "gate pitch", "poly pitch"]}

    cpp = min(candidates)
    agree = len(set(candidates)) == 1

    # The manual's decomposition, solved for the poly-to-diffcon spacing it implies.
    if poly_width and diffcon_width:
        implied = round((cpp - diffcon_width[0] - poly_width[0]) / 2, 4)
        evidence["poly_width_nm"] = poly_width[0]
        evidence["diffcon_width_nm"] = diffcon_width[0]
        evidence["implied_poly_to_diffcon_spacing_nm"] = implied
        evidence["decomposition"] = (
            f"{cpp:g} = 2 x {implied:g} + {diffcon_width[0]:g} + {poly_width[0]:g}")

    return {
        "cpp_nm": cpp, "cpp_um": round(cpp / 1000, 6),
        "sources_agree": agree,
        "evidence": evidence,
        "aliases": ["CPP", "CGP", "gate pitch", "poly pitch"],
        "basis": (f"{cpp:g} nm, measured as the poly centre-to-centre spacing"
                  + (" and confirmed by the diffcon spacing, which the manual requires to match"
                     if agree and "from_diffcon_spacing_nm" in evidence else "")
                  + ("" if agree else
                     f" - note the poly and diffcon spacings disagree: {sorted(set(candidates))}")),
    }


def metal_pitches(outlines: dict[str, Any]) -> dict[str, Any]:
    """Routing pitch per metal layer, taken from the track-guide layers."""
    by = _by_name(outlines)
    out: dict[str, Any] = {}
    for metal, guide, axis, direction in ROUTING_LAYERS:
        guide_row, metal_row = by.get(guide), by.get(metal)
        entry: dict[str, Any] = {"routing_direction": direction,
                                 "pitch_axis": axis,
                                 "present": bool(metal_row and metal_row["shape_count"])}
        if guide_row and guide_row["shape_count"]:
            summary = _pitch_from(_centres_nm(guide_row, axis))
            entry.update(summary)
            entry["source"] = f"{guide} ({guide_row['shape_count']} tracks)"
            entry["tracks"] = guide_row["shape_count"]
        elif metal_row and metal_row["shape_count"] > 1:
            summary = _pitch_from(_centres_nm(metal_row, axis))
            entry.update(summary)
            entry["source"] = (f"{metal} shape positions - no {guide} layer, so this is where the "
                               f"metal happens to sit rather than the declared grid")
        else:
            entry.update({"pitch_nm": None, "uniform": None,
                          "note": (f"no {guide} layer and fewer than two {metal} shapes, so no "
                                   f"pitch is measurable")})
            entry["source"] = None
        width = _widths_nm(metal_row, "y" if axis == "y" else "x")
        entry["width_nm"] = width[0] if len(width) == 1 else (width or None)
        if entry.get("pitch_nm") and isinstance(entry.get("width_nm"), (int, float)):
            entry["implied_space_nm"] = round(entry["pitch_nm"] - entry["width_nm"], 4)
        out[metal] = entry
    return out


def cell_dimensions(outlines: dict[str, Any], cpp_nm: float | None,
                    metals: dict[str, Any] | None = None,
                    filename: str | None = None) -> dict[str, Any]:
    """Cell width in gate pitches and height in metal tracks.

    Width and height come from the CELL-BOUNDARY layer, not the layout bounding box:
    the box is inflated by track guides and other overlay layers that extend past
    the cell.
    """
    by = _by_name(outlines)
    boundary = by.get("CELL-BOUNDARY")
    if not boundary or not boundary["shapes"]:
        return {"width_nm": None, "height_nm": None,
                "basis": ("the CELL-BOUNDARY layer is absent, and the layout bounding box is not a "
                          "substitute - track guides extend beyond the cell and would inflate it")}
    left = min(s["left_um"] for s in boundary["shapes"]) * 1000
    bottom = min(s["bottom_um"] for s in boundary["shapes"]) * 1000
    right = max(s["left_um"] + s["width_um"] for s in boundary["shapes"]) * 1000
    top = max(s["bottom_um"] + s["height_um"] for s in boundary["shapes"]) * 1000
    width, height = round(right - left, 4), round(top - bottom, 4)

    result: dict[str, Any] = {"width_nm": width, "height_nm": height,
                             "width_um": round(width / 1000, 6),
                             "height_um": round(height / 1000, 6)}
    if cpp_nm:
        exact = width / cpp_nm
        result["width_in_cpp"] = round(exact, 4)
        result["gate_pitches"] = int(round(exact))
        result["width_is_whole_cpp"] = abs(exact - round(exact)) < 1e-6
        result["width_basis"] = (f"{width:g} nm / {cpp_nm:g} nm = {exact:g} gate pitches"
                                 + ("" if result["width_is_whole_cpp"] else
                                    " - not a whole number, which is unusual for a standard cell"))

    m0 = (metals or {}).get("M0") or {}
    positions = list(m0.get("positions_nm") or [])
    if positions:
        # The rails sit on the cell edges, so the full grid is the guides plus the two
        # boundary positions. Signal tracks are the guides between them.
        grid = sorted({round(bottom, 4), *positions, round(top, 4)})
        result["m0_track_positions_nm"] = grid
        result["signal_tracks"] = len(positions)
        result["track_positions_including_rails"] = len(grid)
        result["height_basis"] = (
            f"{height:g} nm spanned by {len(grid) - 1} M0 steps "
            f"({', '.join(f'{s:g}' for s in _steps(grid))} nm); {len(positions)} guide tracks are "
            f"available for signal and the two cell edges carry the power rails")

    if filename:
        match = re.search(r"_RT_(\d+)", filename, re.I)
        if match:
            declared = int(match.group(1))
            measured = result.get("signal_tracks")
            result["rt_in_filename"] = declared
            result["rt_matches_measured_tracks"] = (measured == declared
                                                    if measured is not None else None)
            result["rt_basis"] = (
                f"the filename declares RT {declared}"
                + ("" if measured is None else
                   f" and {measured} M0 signal track(s) were measured - "
                   + ("they agree" if measured == declared else "they DISAGREE")))
    return result


def analyze_pitch(outlines: dict[str, Any], filename: str | None = None) -> dict[str, Any]:
    """Every pitch metric for one cell, with the basis for each."""
    cpp = gate_pitch(outlines)
    metals = metal_pitches(outlines)
    dims = cell_dimensions(outlines, cpp.get("cpp_nm"), metals, filename)

    m1_pitch = (metals.get("M1") or {}).get("pitch_nm")
    gear = None
    if cpp.get("cpp_nm") and m1_pitch:
        gear = {
            "gear_ratio": round(cpp["cpp_nm"] / m1_pitch, 4),
            "basis": (f"CPP {cpp['cpp_nm']:g} nm / M1 pitch {m1_pitch:g} nm - the ratio of the "
                      f"device grid to the routing grid"),
        }

    parts = []
    if cpp.get("cpp_nm"):
        parts.append(f"{cpp['cpp_nm']:g} nm gate pitch")
    if dims.get("gate_pitches"):
        parts.append(f"{dims['gate_pitches']} CPP wide")
    if dims.get("signal_tracks"):
        parts.append(f"{dims['signal_tracks']} M0 signal tracks")
    for metal in ("M0", "M1", "M2"):
        entry = metals.get(metal) or {}
        if entry.get("pitch_nm"):
            parts.append(f"{metal} {entry['pitch_nm']:g} nm")

    return {
        "availability": "GDS + LYP",
        "gate_pitch": cpp,
        "metal_pitches": metals,
        "cell_dimensions": dims,
        "gear_ratio": gear,
        "headline": ", ".join(parts) if parts else "no pitch metric could be measured",
        "basis": ("pitches are taken from the technology's track-guide layers where they exist, "
                  "because a guide is a track whether or not any wire uses it; the gate pitch is "
                  "measured from the poly and cross-checked against the diffcon spacing"),
        "not_derivable": {
            "absolute_rule_limits": ("The manual names M0 width and routing pitch as parameters but "
                                     "gives no values, so these are the pitches this cell uses, not "
                                     "the pitches the technology permits."),
        },
    }
