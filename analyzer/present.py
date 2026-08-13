"""Presentation helpers: units, scale, severity ordering and layer filtering.

The analysis was correct but read badly, in four specific ways that a layout
engineer notices immediately:

1. **Wrong unit.** Features here are 12-25 nm. Writing `0.012 µm` makes the reader
   do arithmetic to recognise a minimum-width wire. Lengths are shown in nm;
   areas stay in µm² because that is what area is quoted in.
2. **No sense of scale.** "0.005308 µm²" is unanswerable until you know the cell
   is 0.03 µm², making it 17.7%. Every absolute figure gets a relative one.
3. **Duplicate rows.** Of 28 layer rows in the reference cell, 17 are `-PIN`,
   `-LABEL`, `-DUPLICATE`, `-EXTENDED` or `-TRACK-GUIDE` copies. Showing `NDIFF`
   and `NDIFF-DUPLICATE` with the same 0.0018 µm² reads like double counting.
   They are hidden by default and available behind a toggle.
4. **Arbitrary order.** Sorting by layer number buries the biggest change. Findings
   are ordered by magnitude, so the first row is the one to look at first.
"""
from __future__ import annotations

from typing import Any

from .connectivity import layer_roles

# A difference at or below this fraction of the cell area is unlikely to be the
# point of a revision. Used only to order and phrase, never to hide.
MINOR_FRACTION = 0.001


def nm(value_um: float | None, digits: int = 1) -> str:
    """Format a µm length in nanometres, which is how this node is discussed."""
    if value_um is None:
        return "n/a"
    value = value_um * 1000.0
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))} nm"
    return f"{value:.{digits}f} nm"


def um2(value: float | None, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}g} µm²"


def pct_of(value: float | None, total: float | None, digits: int = 2) -> str:
    """`value` as a percentage of `total`, or n/a when the base is unknown."""
    if value is None or not total:
        return "n/a"
    return f"{value / total * 100:.{digits}f}%"


def scale_note(value_um2: float | None, cell_area_um2: float | None) -> str:
    """A one-phrase sense of how big a difference is, relative to the cell."""
    if value_um2 is None or not cell_area_um2:
        return ""
    frac = value_um2 / cell_area_um2
    if frac >= 0.10:
        return f"{frac * 100:.0f}% of the cell — a substantial edit"
    if frac >= 0.01:
        return f"{frac * 100:.1f}% of the cell"
    if frac >= MINOR_FRACTION:
        return f"{frac * 100:.2f}% of the cell — a small edit"
    return f"under {MINOR_FRACTION * 100:.1f}% of the cell — very localised"


def is_derived_row(row: dict[str, Any], roles: dict) -> bool:
    """True for a pin/label/duplicate/guide copy of another layer."""
    meta = roles.get((row.get("layer"), row.get("datatype"))) or {}
    return bool(meta.get("derived"))


def split_primary(rows: list[dict[str, Any]],
                  layermap: dict[str, Any] | None,
                  role_overrides: dict[str, str] | None = None):
    """Split layer rows into the ones worth showing and the duplicate copies.

    Returns (primary, derived). With no layer map nothing can be identified as a
    copy, so everything is primary - guessing would hide real layers.
    """
    roles = layer_roles(layermap, role_overrides)
    if not roles:
        return list(rows), []
    primary = [r for r in rows if not is_derived_row(r, roles)]
    derived = [r for r in rows if is_derived_row(r, roles)]
    return primary, derived


def headline(xor: dict[str, Any] | None, cell_area_um2: float | None) -> dict[str, Any]:
    """One-line verdict for a comparison, plus the two numbers behind it.

    A reviewer's first question is "are these the same?" and the second is "is the
    change what I expected?". Both should be answerable without scrolling.
    """
    if not xor:
        return {"state": "none", "headline": "Upload two or more layouts to compare them.",
                "detail": ""}
    if not xor.get("comparable"):
        return {"state": "blocked", "headline": "These layouts cannot be compared.",
                "detail": xor.get("reason", "")}
    s = xor["summary"]
    if s["identical"]:
        return {"state": "identical",
                "headline": f"Identical — no geometric difference on any of "
                            f"{s['layers_compared']} layers.",
                "detail": "The XOR is empty everywhere, including labels."}

    impact = xor["mask_impact"]
    base = bool(impact["base_layers_changed"])
    scale = scale_note(s["total_xor_area_um2"], cell_area_um2)
    where = (f" The largest is {um2(s['largest_single_difference_um2'])} on "
             f"{s['largest_difference_on_layer']}"
             f" at {s['largest_difference_at_um']} µm."
             if s["largest_difference_at_um"] else "")
    return {
        "state": "base-layers" if base else "interconnect-only",
        "headline": (f"{s['layers_changed']} of {s['layers_compared']} layers differ — "
                     f"{s['difference_regions']} regions, {um2(s['total_xor_area_um2'])}"
                     + (f" ({scale})" if scale else "") + "."),
        "detail": impact["observation"] + where,
        "scale": scale,
    }


def _short_file(name: str) -> str:
    return name[:-4] if name.lower().endswith(".gds") else name


def findings(xor: dict[str, Any], cell_area_um2: float | None,
             limit: int = 6) -> list[dict[str, Any]]:
    """The differences worth looking at, largest first.

    One row per difference region rather than per layer, because that is the unit a
    reviewer works through - and it carries the location so it can be found.
    """
    if not xor.get("comparable") or xor["summary"]["identical"]:
        return []
    # Say which file a difference is in rather than "added"/"removed", which begs
    # the question "relative to what?" and forces the reader to remember which file
    # is the baseline.
    a, b = _short_file(xor["file_a"]), _short_file(xor["file_b"])
    out: list[dict[str, Any]] = []
    for row in xor["layers"]:
        if row["identical"]:
            continue
        for block, where in (("removed", f"only in {a}"), ("added", f"only in {b}")):
            for loc in (row.get(block) or {}).get("locations") or []:
                out.append({
                    "layer": row["name"], "role": row["role"], "change": where,
                    "area_um2": loc["area_um2"],
                    "share_of_cell": pct_of(loc["area_um2"], cell_area_um2),
                    "size": f"{nm(loc['width_um'])} × {nm(loc['height_um'])}",
                    "at_um": loc["centre_um"],
                    "outline_um": loc.get("outline_um"),
                })
    out.sort(key=lambda r: -r["area_um2"])
    return out[:limit]
