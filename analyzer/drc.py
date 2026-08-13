"""Design-rule checking against the GENCHIP Design Rule Manual.

Until now this tool refused every rule question, correctly: with no rule deck,
"M0 is 12 nm wide" is a measurement and "M0 violates minimum width" is an
invention. The manual changes that - but only for the rules it actually states.

The manual's character matters, and it is unusual in a useful way. Almost every
rule is **relational** rather than absolute - it equates one measurement to
another, rather than to a number. Section 3.1 relates the two diffusion widths to
each other; 3.8 relates every M0 width to the others, and M0 spacing to the routing
pitch less the width; 3.4 relates the dummy gate width to the poly width. The rule
text itself is not quoted here, because the manual forbids reproducing it: the
catalogue built by `tools/extract_drm_rules.py` holds the wording, and it is
gitignored.

A relational rule needs no numeric deck to check: the layout supplies both sides.
That is why this module can return real verdicts. What the manual does *not* give
is the absolute values - `M0 width`, `M0 routing pitch`, `via extension` are named
as parameters but never assigned numbers - so those are measured from the layout
and reported as observed, never compared against a limit that was not supplied.

Three boundaries are held throughout:

* **Only the implemented rules are checked.** `rules_checked` and
  `rules_not_checked` are both reported, and a clean result says explicitly that
  it is not a signoff DRC.
* **Technology matters.** Many rules apply only to CFET, or only to GAA/FinFET.
  The technology is inferred from the geometry (rule 3.1.1 versus 3.1.2 make that
  possible) and the inference is labelled as such.
* **A failed check cites its rule.** Every violation carries the rule id and the
  manual's own words, so it can be looked up rather than taken on trust.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "genchip_drm_rules.json"

# Tolerance for "should be equal". Coordinates are integers in database units, so
# an exact comparison is right in principle; one dbu of slack absorbs rounding in
# the µm conversion without admitting a real difference.
def _tol(dbu_um: float) -> float:
    return dbu_um * 1.5


class RulesUnavailable(FileNotFoundError):
    """The rule catalogue is not present.

    Its own message is the documentation: the catalogue is transcribed from a
    manual that may not be redistributed, so it is not in the repository and a
    fresh clone has to be told where to get it.
    """


def rules_available(path: str | Path = RULES_FILE) -> bool:
    return Path(path).exists()


def load_rules(path: str | Path = RULES_FILE) -> dict[str, Any]:
    """The rule catalogue transcribed from the manual.

    Raises RulesUnavailable with an actionable message when the file is absent,
    rather than a bare FileNotFoundError naming a path the reader has never seen.
    """
    p = Path(path)
    if not p.exists():
        raise RulesUnavailable(
            f"The design rule catalogue is not present at {p}.\n\n"
            "It is transcribed from the GENCHIP Design Rule Manual, which states it may not be "
            "reproduced or transmitted without written permission, so it is deliberately excluded "
            "from version control.\n\n"
            "To enable rule checking, place the manual at data/GENCHIP Design Rule Manual.pdf and "
            "run:  python tools/extract_drm_rules.py\n\n"
            "Everything else in the tool - geometry, connectivity, XOR comparison, classification - "
            "works without it.")
    data = json.loads(p.read_text(encoding="utf-8"))
    data["by_id"] = {r["id"]: r for r in data["rules"]}
    return data


def _layer(outlines: dict[str, Any], name: str) -> dict[str, Any] | None:
    for row in outlines["layers"]:
        if row["name"].upper() == name.upper() and row["shape_count"]:
            return row
    return None


def _widths(row: dict[str, Any] | None, axis: str) -> list[float]:
    """Shape sizes along one axis. `axis` is "x" for width, "y" for height."""
    if not row:
        return []
    key = "width_um" if axis == "x" else "height_um"
    return sorted({s[key] for s in row["shapes"]})


def _pitch(row: dict[str, Any] | None, axis: str) -> dict[str, Any]:
    """Centre-to-centre spacing between consecutive shapes along one axis."""
    if not row or row["shape_count"] < 2:
        return {"pitches": [], "uniform": None}
    index = 0 if axis == "x" else 1
    centres = sorted({round(s["centre_um"][index], 9) for s in row["shapes"]})
    gaps = [round(b - a, 9) for a, b in zip(centres, centres[1:])]
    return {"pitches": sorted(set(gaps)), "uniform": len(set(gaps)) == 1 if gaps else None,
            "tracks": centres}


def detect_technology(outlines: dict[str, Any]) -> dict[str, Any]:
    """Infer GAA/FinFET versus CFET from the diffusion geometry.

    The manual makes this possible: 3.1.1 says pdiff and ndiff are *separated* in
    GAA and FinFET, while 3.1.2 says they *exactly overlap* in CFET. FinFET is then
    distinguished from GAA by 3.6.1, "pdiff is placed in a Nwell".
    """
    ndiff, pdiff = _layer(outlines, "NDIFF"), _layer(outlines, "PDIFF")
    nwell = _layer(outlines, "NWELL")
    if not ndiff or not pdiff:
        return {"technology": None, "confidence": "none",
                "basis": "the layout has no NDIFF/PDIFF geometry to judge from"}

    def box(row):
        left = min(s["left_um"] for s in row["shapes"])
        bottom = min(s["bottom_um"] for s in row["shapes"])
        right = max(s["left_um"] + s["width_um"] for s in row["shapes"])
        top = max(s["bottom_um"] + s["height_um"] for s in row["shapes"])
        return left, bottom, right, top

    na, pa = box(ndiff), box(pdiff)
    overlap_y = min(na[3], pa[3]) - max(na[1], pa[1])
    if overlap_y > _tol(outlines["dbu_um"]):
        tech, why = "CFET", ("pdiff and ndiff overlap vertically, which rule 3.1.2 gives as the "
                             "CFET arrangement")
    elif nwell:
        tech, why = "FinFET", ("pdiff and ndiff are separated (rule 3.1.1) and an NWELL layer is "
                               "present, which rule 3.6.1 makes specific to FinFET")
    else:
        tech, why = "GAA", ("pdiff and ndiff are separated (rule 3.1.1) and no NWELL is present, "
                            "which rule 3.6.1 would require for FinFET")
    return {"technology": tech, "confidence": "inferred from geometry", "basis": why,
            "ndiff_extent_um": na, "pdiff_extent_um": pa,
            "vertical_overlap_um": round(overlap_y, 9)}


def _result(rule: dict[str, Any], status: str, detail: str,
            observed: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": rule["id"], "section": rule["section"], "rule": rule["rule"],
            "status": status, "detail": detail, "observed": observed or {}}


def check_layout(outlines: dict[str, Any], rules: dict[str, Any] | None = None,
                 technology: str | None = None) -> dict[str, Any]:
    """Check the implemented subset of the manual's rules against one layout.

    Returns an `available: False` result when the catalogue is absent, rather than
    raising: rule checking is one feature among many, and its absence must not stop
    a layout from being analysed.
    """
    if rules is None and not rules_available():
        return {"available": False,
                "reason": ("the design rule catalogue is not present, so no rule can be checked. "
                           "It is transcribed from the GENCHIP Design Rule Manual, which may not "
                           "be redistributed, so it is not in the repository - see the README."),
                "technology": detect_technology(outlines) | {"used": technology,
                                                             "supplied": technology is not None},
                "results": [], "violations": [], "rules_not_checked": [],
                "summary": {"pass": 0, "violation": 0, "not checked": 0, "not applicable": 0,
                            "rules_in_manual": 0, "rules_checked": 0, "rules_not_checked": 0},
                "caveat": "No rules were checked, so nothing here is a statement about compliance.",
                "not_derivable": {"rules": "Requires the design rule manual."}}
    cat = rules or load_rules()
    by_id = cat["by_id"]
    tol = _tol(outlines["dbu_um"])
    tech_info = detect_technology(outlines)
    tech = technology or tech_info["technology"]

    results: list[dict[str, Any]] = []
    implemented: set[str] = set()

    def applies(rule_id: str) -> bool:
        techs = by_id[rule_id]["technologies"]
        return techs == ["all"] or tech is None or tech in techs

    def add(rule_id: str, status: str, detail: str, observed=None):
        implemented.add(rule_id)
        rule = by_id.get(rule_id)
        if not rule:
            return
        if not applies(rule_id):
            status, detail = "not applicable", (
                f"this rule applies to {', '.join(rule['technologies'])}; the layout was "
                f"identified as {tech}")
        results.append(_result(rule, status, detail, observed))

    def equal_widths(rule_id: str, name_a: str, name_b: str, axis: str, what: str):
        a, b = _widths(_layer(outlines, name_a), axis), _widths(_layer(outlines, name_b), axis)
        if not a or not b:
            add(rule_id, "not checked",
                f"{name_a if not a else name_b} has no geometry in this layout")
            return
        observed = {f"{name_a}_{what}_um": a, f"{name_b}_{what}_um": b}
        if len(a) > 1 or len(b) > 1:
            add(rule_id, "violation",
                f"{what} is not single-valued: {name_a} has {a}, {name_b} has {b}", observed)
        elif abs(a[0] - b[0]) <= tol:
            add(rule_id, "pass", f"both are {a[0] * 1000:.0f} nm", observed)
        else:
            add(rule_id, "violation",
                f"{name_a} {what} is {a[0] * 1000:.0f} nm but {name_b} is {b[0] * 1000:.0f} nm",
                observed)

    def uniform_width(rule_id: str, name: str, axis: str, what: str):
        row = _layer(outlines, name)
        values = _widths(row, axis)
        if not values:
            add(rule_id, "not checked", f"{name} has no geometry in this layout")
        elif len(values) == 1:
            add(rule_id, "pass",
                f"all {row['shape_count']} {name} shapes are {values[0] * 1000:.0f} nm",
                {f"{name}_{what}_um": values})
        else:
            add(rule_id, "violation",
                f"{name} has {len(values)} different {what}s: "
                + ", ".join(f"{v * 1000:.0f} nm" for v in values),
                {f"{name}_{what}_um": values})

    def fixed_pitch(rule_id: str, name: str, axis: str):
        row = _layer(outlines, name)
        info = _pitch(row, axis)
        if not info["pitches"]:
            add(rule_id, "not checked",
                f"{name} has fewer than two shapes, so there is no pitch to measure")
        elif info["uniform"]:
            add(rule_id, "pass", f"a single pitch of {info['pitches'][0] * 1000:.0f} nm",
                {f"{name}_pitch_um": info["pitches"]})
        else:
            add(rule_id, "violation",
                f"{name} pitch is not fixed: "
                + ", ".join(f"{p * 1000:.0f} nm" for p in info["pitches"]),
                {f"{name}_pitch_um": info["pitches"]})

    def on_track_guide(rule_id: str, name: str, guide: str, axis: str):
        """Do the wires sit on their declared routing tracks?

        This replaces a naive "is the pitch uniform?" test, which produced a false
        violation. The M0 track guide in this technology is itself at 21, 42, 73 and
        94 nm - gaps of 21, 31, 21 - so demanding a uniform pitch would contradict
        the technology's own track definition. What is unambiguous, and what the
        guide layer exists for, is whether the metal lands on the tracks.
        """
        row, grow = _layer(outlines, name), _layer(outlines, guide)
        if not row:
            add(rule_id, "not checked", f"{name} is not present in this layout")
            return
        index = 0 if axis == "x" else 1
        if not grow:
            info = _pitch(row, axis)
            add(rule_id, "not checked",
                f"{guide} is absent, so the routing grid is undeclared. Observed "
                f"{name} track positions: "
                + ", ".join(f"{c * 1000:.0f} nm" for c in
                            sorted({s['centre_um'][index] for s in row["shapes"]})),
                {"observed_pitches_um": info["pitches"]})
            return
        tracks = sorted({round(s["centre_um"][index], 9) for s in grow["shapes"]})
        gaps = sorted({round(b - a, 9) for a, b in zip(tracks, tracks[1:])})
        off = [s["centre_um"] for s in row["shapes"]
               if not any(abs(s["centre_um"][index] - t) <= tol for t in tracks)]
        observed = {"guide_tracks_um": tracks, "guide_pitches_um": gaps,
                    "guide_pitch_uniform": len(gaps) == 1}
        if off:
            add(rule_id, "violation",
                f"{len(off)} {name} shape(s) are not centred on a {guide} track "
                f"(e.g. at {off[0]})", observed)
        else:
            note = ("a single pitch of %.0f nm" % (gaps[0] * 1000) if len(gaps) == 1
                    else "the guide's own pitch is not uniform ("
                         + ", ".join(f"{g * 1000:.0f} nm" for g in gaps)
                         + "), so the fixed-pitch wording is read against the guide")
            add(rule_id, "pass",
                f"all {row['shape_count']} {name} shapes are centred on a {guide} track; "
                + note, observed)

    def spacing_matches_pitch(rule_id: str, name: str, axis: str):
        """Rules 3.8.5 / 3.10.5: spacing == pitch - width.

        Width is measured along the *same* axis as the pitch: for a vertical M1 wire
        the pitch is horizontal and so is the width. Taking the perpendicular
        dimension gave the wire's length instead, and a negative expected space -
        which is what exposed the error.
        """
        row = _layer(outlines, name)
        widths = _widths(row, axis)
        info = _pitch(row, axis)
        if not info["pitches"] or not widths:
            add(rule_id, "not checked", f"{name} needs two shapes and a single width to check")
            return
        if len(info["pitches"]) > 1 or len(widths) > 1:
            add(rule_id, "not checked",
                f"{name} has more than one pitch or width, so the relation is ambiguous",
                {"pitches_um": info["pitches"], "widths_um": widths})
            return
        expected = round(info["pitches"][0] - widths[0], 9)
        index = 0 if axis == "x" else 1
        edges = sorted((s["centre_um"][index] - (s["width_um"] if axis == "x" else s["height_um"]) / 2,
                        s["centre_um"][index] + (s["width_um"] if axis == "x" else s["height_um"]) / 2)
                       for s in row["shapes"])
        gaps = sorted({round(b[0] - a[1], 9) for a, b in zip(edges, edges[1:]) if b[0] > a[1]})
        observed = {"pitch_um": info["pitches"][0], "width_um": widths[0],
                    "expected_space_um": expected, "measured_space_um": gaps}
        if not gaps:
            add(rule_id, "not checked", f"{name} shapes abut, so there is no space to measure",
                observed)
        elif len(gaps) == 1 and abs(gaps[0] - expected) <= tol:
            add(rule_id, "pass",
                f"space is {gaps[0] * 1000:.0f} nm, matching pitch − width", observed)
        else:
            add(rule_id, "violation",
                f"space is {', '.join(f'{g * 1000:.0f} nm' for g in gaps)} but pitch − width is "
                f"{expected * 1000:.0f} nm", observed)

    def via_lands_on(rule_id: str, via: str, targets: list[str]):
        """Rules 3.7.5-3.7.9 / 3.9.4: a via must sit on the layers it connects."""
        row = _layer(outlines, via)
        if not row:
            add(rule_id, "not checked", f"{via} is not present in this layout")
            return
        missing = []
        for shape in row["shapes"]:
            left, bottom = shape["left_um"], shape["bottom_um"]
            right, top = left + shape["width_um"], bottom + shape["height_um"]
            for target in targets:
                trow = _layer(outlines, target)
                if not trow:
                    continue
                if not any(not (right <= s["left_um"] or left >= s["left_um"] + s["width_um"]
                                or top <= s["bottom_um"] or bottom >= s["bottom_um"] + s["height_um"])
                           for s in trow["shapes"]):
                    missing.append((shape["centre_um"], target))
        present = [t for t in targets if _layer(outlines, t)]
        if not present:
            add(rule_id, "not checked",
                f"none of {', '.join(targets)} is present, so the landing cannot be checked")
        elif missing:
            add(rule_id, "violation",
                f"{len(missing)} {via} shape(s) do not overlap a required layer: "
                + "; ".join(f"at {c} missing {t}" for c, t in missing[:4]),
                {"checked_against": present})
        else:
            add(rule_id, "pass",
                f"every {via} shape overlaps {', '.join(present)}",
                {"checked_against": present, "shapes": row["shape_count"]})

    def within_cell(rule_id: str, name: str):
        """Rule 3.11.3: the shape must not extend beyond the cell boundary.

        The boundary is the CELL-BOUNDARY layer (1/0 in the manual's layer map), not
        the layout's bounding box. Using the bounding box is circular - it is
        computed from all geometry including the shape being tested, so anything
        placed outside simply enlarges the box and then sits inside it. That is what
        let a deliberately out-of-bounds DVB pass.
        """
        row = _layer(outlines, name)
        if not row:
            add(rule_id, "not checked", f"{name} is not present in this layout")
            return
        boundary = _layer(outlines, "CELL-BOUNDARY")
        if not boundary:
            add(rule_id, "not checked",
                "the CELL-BOUNDARY layer is absent, and the layout bounding box cannot stand in "
                "for it: the box is derived from this geometry, so nothing could ever fall outside")
            return
        left = min(s["left_um"] for s in boundary["shapes"])
        bottom = min(s["bottom_um"] for s in boundary["shapes"])
        right = max(s["left_um"] + s["width_um"] for s in boundary["shapes"])
        top = max(s["bottom_um"] + s["height_um"] for s in boundary["shapes"])
        outside = [s["centre_um"] for s in row["shapes"]
                   if s["left_um"] < left - tol or s["bottom_um"] < bottom - tol
                   or s["left_um"] + s["width_um"] > right + tol
                   or s["bottom_um"] + s["height_um"] > top + tol]
        if outside:
            add(rule_id, "violation",
                f"{len(outside)} {name} shape(s) extend beyond the cell boundary "
                f"(e.g. at {outside[0]})")
        else:
            add(rule_id, "pass", f"all {row['shape_count']} {name} shapes are inside the boundary")

    def pin_overlaps(rule_id: str, pin: str, drawing: str):
        prow, drow = _layer(outlines, pin), _layer(outlines, drawing)
        if not prow or not drow:
            add(rule_id, "not checked", f"{pin} or {drawing} is not present in this layout")
            return
        def key(row):
            return sorted((s["left_um"], s["bottom_um"], s["width_um"], s["height_um"])
                          for s in row["shapes"])
        if key(prow) == key(drow):
            add(rule_id, "pass", f"{pin} matches {drawing} shape for shape "
                                 f"({prow['shape_count']} shapes)")
        else:
            add(rule_id, "violation",
                f"{pin} has {prow['shape_count']} shape(s) and {drawing} has "
                f"{drow['shape_count']}, and they are not identical")

    # ---- section 3.1 diffusion -------------------------------------------------
    equal_widths("3.1.5", "PDIFF", "NDIFF", "y", "width")
    # ---- section 3.2 gate -----------------------------------------------------
    equal_widths("3.2.5", "PPOLY", "NPOLY", "x", "width")
    fixed_pitch("3.2.6", "NPOLY", "x")
    # ---- section 3.3 diffcon --------------------------------------------------
    equal_widths("3.3.6", "NDIFFCON", "PDIFFCON", "x", "width")
    fixed_pitch("3.3.8", "NDIFFCON", "x")
    # ---- section 3.4 dummy gate ----------------------------------------------
    equal_widths("3.4.5", "DUMMY-GATE", "NPOLY", "x", "width")
    # ---- section 3.7 contacts ------------------------------------------------
    via_lands_on("3.7.6", "N-VIAT", ["NDIFFCON"])
    via_lands_on("3.7.7", "N-VIAG", ["NPOLY"])
    # ---- section 3.8 M0 -------------------------------------------------------
    uniform_width("3.8.2", "M0", "y", "width")
    on_track_guide("3.8.4", "M0", "M0-TRACK-GUIDE", "y")
    spacing_matches_pitch("3.8.5", "M0", "y")
    # ---- section 3.9 via0 -----------------------------------------------------
    via_lands_on("3.9.4", "VIA0", ["M0", "M1"])
    # ---- section 3.10 M1 ------------------------------------------------------
    uniform_width("3.10.2", "M1", "x", "width")
    on_track_guide("3.10.4", "M1", "M1-TRACK-GUIDE", "x")
    spacing_matches_pitch("3.10.5", "M1", "x")
    # ---- section 3.11 DVB -----------------------------------------------------
    within_cell("3.11.3", "DVB")
    via_lands_on("3.11.4", "DVB", ["BM0"])
    # ---- section 3.12 BM0 -----------------------------------------------------
    uniform_width("3.12.2", "BM0", "y", "width")
    # ---- section 3.14 pins ----------------------------------------------------
    pin_overlaps("3.14.1", "BM0-PIN", "BM0")
    pin_overlaps("3.14.3", "M0-PIN", "M0")
    pin_overlaps("3.14.4", "M1-PIN", "M1")

    counts = {status: sum(1 for r in results if r["status"] == status)
              for status in ("pass", "violation", "not checked", "not applicable")}
    not_implemented = [r for r in cat["rules"] if r["id"] not in implemented]
    return {
        "source": cat["source"],
        "technology": tech_info | {"used": tech,
                                   "supplied": technology is not None},
        "results": sorted(results, key=lambda r: (r["status"] != "violation", r["id"])),
        "summary": counts | {
            "rules_in_manual": cat["rule_count"],
            "rules_checked": len(implemented),
            "rules_not_checked": len(not_implemented),
        },
        "violations": [r for r in results if r["status"] == "violation"],
        "rules_not_checked": [{"id": r["id"], "rule": r["rule"]} for r in not_implemented],
        "caveat": (f"{len(implemented)} of {cat['rule_count']} rules in the manual are checked "
                   "here. A clean result means no violation of *those* rules - it is not a "
                   "signoff DRC, and the unchecked rules are listed so the gap is visible."),
        "not_derivable": {
            "absolute_limits": ("The manual names M0 width, routing pitch and via extension as "
                                "parameters but does not give their values, so those are measured "
                                "from the layout and reported as observed rather than compared "
                                "against a limit."),
            "rules_needing_a_figure": ("Some rules are defined by a figure rather than by text "
                                       "and cannot be reduced to a geometric test here."),
        },
    }
