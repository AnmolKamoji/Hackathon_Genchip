"""Measure the technology-file parameters from the layout geometry.

A tech file states figures like "Gate extension = 12 nm". This module recovers each
of them from the .gds and the .lyp, so the question "what is the gate extension in
this cell?" is answered by measuring the cell rather than by quoting a file the tool
may not have been given.

Every parameter is defined by a rule in the GENCHIP Design Rule Manual, and each
measurement here implements the manual's own wording. That matters because several
parameters have a plausible-looking wrong reading:

  * "Gate extension" is the *minimum* extension of poly beyond diffusion (3.2.2).
    Measured on the uncut gates it looks like 20.5 nm, because the poly runs on to
    meet the poly of the opposite device; only the cut column shows the real 12 nm.
    Taking the minimum over every poly/diffusion pair gets it right either way.
  * "Gate cut spacing" is a poly-to-poly end-to-end distance (3.2.3), which exists
    only where the gate is actually cut. Averaging over all columns would report
    zero, since an uncut gate has no gap at all.
  * Widths are measured orthogonal to the poly direction and extensions parallel to
    it (3.2.1, 3.2.2, 3.3.1, 3.3.3), so the poly direction has to be derived from
    the geometry before anything else can be measured.

The parameters the layout cannot express are reported as unavailable with the reason,
never as zero. `Diffusion to Diff interconnect spacing` is the clearest case: rule
3.13.5 defines it for CFET only, so in a GAA or FinFET cell there is no geometry that
could carry it, and a number would have to come from somewhere other than this file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import klayout.db as db

from analyzer.gds_parser import rank_top_cells

# Values are reported in nanometres, which is how a tech file states them.
UNIT_NM = "nm"

# Where a measured parameter is undefined, the reason is one of these. Reporting the
# reason rather than a bare None is what lets an answer say why the figure is absent.
NO_LAYER = "the layer this parameter is measured on carries no geometry in this cell"
CFET_ONLY = ("rule 3.13.5 defines this for CFET technology only, and the Diff "
             "Interconnect layer it is measured against is empty here")


def _shapes(layout, top, layermap, name: str) -> list[db.Box]:
    """Every shape on a named layer, as boxes in database units.

    Named lookup rather than hard-coded numbers: the .lyp is the only thing that says
    which layer number is NPOLY, and it is allowed to differ between technologies.
    """
    keys = [k for k, v in (layermap or {}).get("by_key", {}).items()
            if v.get("technology_name") == name]
    boxes: list[db.Box] = []
    for layer, datatype in keys:
        index = layout.find_layer(layer, datatype)
        if index is None:
            continue
        it = top.begin_shapes_rec(index)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            poly = (db.Polygon(shape.box) if shape.is_box()
                    else shape.polygon if shape.is_polygon()
                    else shape.path.polygon() if shape.is_path() else None)
            if poly is not None:
                boxes.append(poly.transformed(trans).bbox())
            it.next()
    return boxes


def _extent(box: db.Box, axis: str) -> int:
    return box.width() if axis == "x" else box.height()


def _lo(box: db.Box, axis: str) -> int:
    return box.left if axis == "x" else box.bottom


def _hi(box: db.Box, axis: str) -> int:
    return box.right if axis == "x" else box.top


def _overlaps(a: db.Box, b: db.Box, axis: str) -> bool:
    """True when the two boxes share coordinates along `axis`.

    A spacing is only meaningful between shapes that face each other. Two shapes on
    opposite corners of the cell have a distance but not a spacing, and including
    them would report the diagonal as if it were a design rule.
    """
    return _lo(a, axis) < _hi(b, axis) and _lo(b, axis) < _hi(a, axis)


def _poly_direction(layout, top, layermap) -> str | None:
    """Which axis the gates run along, derived rather than assumed.

    The manual measures widths orthogonal to the poly direction and extensions
    parallel to it, so every other measurement depends on this. A gate is longer than
    it is wide, which is what decides it.
    """
    boxes = (_shapes(layout, top, layermap, "NPOLY")
             + _shapes(layout, top, layermap, "PPOLY"))
    if not boxes:
        return None
    taller = sum(1 for b in boxes if b.height() > b.width())
    wider = sum(1 for b in boxes if b.width() > b.height())
    if taller == wider:
        return None
    return "y" if taller > wider else "x"


def _min_width(boxes: list[db.Box], axis: str) -> int | None:
    """The narrowest extent along `axis`. Minimum, because a tech file states the
    minimum and a cell may legally draw some shapes wider."""
    return min((_extent(b, axis) for b in boxes), default=None) or None


def _min_gap(a: list[db.Box], b: list[db.Box], axis: str) -> int | None:
    """Smallest clear gap along `axis` between two sets of shapes.

    Only pairs that face each other across the axis are considered, and overlapping
    pairs are skipped rather than counted as a zero gap - an overlap is not a
    spacing, and admitting it would drag every minimum down to nothing.
    """
    other = "y" if axis == "x" else "x"
    gaps = []
    for box_a in a:
        for box_b in b:
            if not _overlaps(box_a, box_b, other):
                continue
            gap = max(_lo(box_b, axis) - _hi(box_a, axis),
                      _lo(box_a, axis) - _hi(box_b, axis))
            if gap > 0:
                gaps.append(gap)
    return min(gaps, default=None)


def _min_extension(inner: list[db.Box], outer: list[db.Box], axis: str) -> int | None:
    """How far `outer` reaches beyond `inner` along `axis`, minimised over pairs.

    Both ends of every overlapping pair are measured. The minimum is what the manual
    specifies, and it is also the only reading that survives a gate running on to
    meet its opposite number: that end measures wide, the other end measures true.
    """
    other = "y" if axis == "x" else "x"
    extensions = []
    for box_in in inner:
        for box_out in outer:
            if not _overlaps(box_in, box_out, other):
                continue
            low = _lo(box_in, axis) - _lo(box_out, axis)
            high = _hi(box_out, axis) - _hi(box_in, axis)
            extensions.extend(e for e in (low, high) if e > 0)
    return min(extensions, default=None)


def _merged_spans(boxes: list[db.Box], axis: str) -> list[tuple[int, int]]:
    """Distinct occupied intervals along `axis`, merged where they touch or overlap."""
    intervals = sorted((_lo(b, axis), _hi(b, axis)) for b in boxes)
    spans: list[list[int]] = []
    for lo, hi in intervals:
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return [(lo, hi) for lo, hi in spans]


def _track_profile(boxes: list[db.Box], axis: str, cell_lo: int, cell_hi: int,
                   dbu: float) -> dict[str, Any] | None:
    """The cell cross-section as alternating margin, width, gap, ..., margin.

    This is how a tech file states Metal0/Metal1/Metal2: walk the cell from one edge
    to the other and write down each distance in turn. The sequence therefore sums to
    the cell dimension exactly, which is a useful self-check - if it does not, a track
    was missed or a shape was counted twice.

    A compact form is also returned. Where every track is the same width and every
    interior gap the same, [margin, width, gap] carries the whole profile, and that is
    the form a tech file usually prints.
    """
    spans = _merged_spans(boxes, axis)
    if not spans:
        return None
    to_nm = lambda v: round(v * dbu * 1000, 4)  # noqa: E731

    sequence, position = [], cell_lo
    for lo, hi in spans:
        sequence.append(to_nm(lo - position))
        sequence.append(to_nm(hi - lo))
        position = hi
    sequence.append(to_nm(cell_hi - position))

    widths = [to_nm(hi - lo) for lo, hi in spans]
    gaps = [to_nm(spans[i + 1][0] - spans[i][1]) for i in range(len(spans) - 1)]
    uniform = len(set(widths)) == 1 and len(set(gaps)) <= 1
    compact = ([sequence[0], widths[0]] + ([gaps[0]] if gaps else [])) if uniform else None

    return {
        "sequence_nm": sequence,
        "compact_nm": compact,
        "track_count": len(spans),
        "widths_nm": widths,
        "gaps_nm": gaps,
        "sums_to_nm": round(sum(sequence), 4),
        "cell_extent_nm": to_nm(cell_hi - cell_lo),
        "uniform": uniform,
    }


def _row_profile(lower: list[db.Box], upper: list[db.Box], axis: str, cell_lo: int,
                 cell_hi: int, dbu: float) -> dict[str, Any] | None:
    """The n and p row cross-section, from the row each layer mostly occupies.

    Merging the two layers the way the metal profile does fails here. A cell that
    connects an n device to a p device draws one diffcon running the whole way up, and
    that single column bridges the two rows into one 91 nm block - which is true of
    that column and false of the cell.

    So take the row each layer occupies in most of its columns, and report the columns
    that depart from it as exceptions rather than letting them redefine the row.
    """
    if not lower and not upper:
        return None
    to_nm = lambda v: round(v * dbu * 1000, 4)  # noqa: E731

    rows, exceptions = [], []
    for boxes in (lower, upper):
        if not boxes:
            continue
        spans = [(_lo(b, axis), _hi(b, axis)) for b in boxes]
        common = max(set(spans), key=spans.count)
        rows.append(common)
        for span in set(spans):
            if span != common:
                exceptions.append(
                    f"{spans.count(span)} shape(s) span {to_nm(span[0]):g}-"
                    f"{to_nm(span[1]):g} nm instead of {to_nm(common[0]):g}-"
                    f"{to_nm(common[1]):g} nm")
    rows.sort()

    sequence, position = [], cell_lo
    for lo, hi in rows:
        sequence.append(to_nm(lo - position))
        sequence.append(to_nm(hi - lo))
        position = hi
    sequence.append(to_nm(cell_hi - position))

    basis = ("the diffcon rows as margin, row, gap, ... margin, parallel to the poly "
             "direction, taking the row each layer occupies in most of its columns")
    if exceptions:
        basis += "; " + "; ".join(exceptions)
    return {"sequence_nm": sequence,
            "rows_nm": [[to_nm(lo), to_nm(hi)] for lo, hi in rows],
            "exceptions": exceptions, "basis": basis}


def _via_geometry(boxes: list[db.Box], axis_width: str, metal_boxes: list[db.Box],
                  m0_width_nm: float | None, guide_boxes: list[db.Box],
                  dbu: float) -> dict[str, Any]:
    """Size, offset from the track, enclosure by the metal, and via extension.

    Rules 3.7.2 and 3.9.2 define the via height as the M0 width plus twice the via
    extension, so the extension is recovered by rearranging that: it is not measured
    directly and it can legitimately be zero, meaning the via is exactly as tall as
    the wire it lands on.

    Enclosure is how far the enclosing metal overhangs the via on each side, and the
    offset is how far the via centre sits off the track-guide centre. Zero for both
    means a via drawn exactly on grid at exactly the wire width, which is the
    common case in a compiled standard cell rather than a sign of missing data.
    """
    if not boxes:
        return {"available": False, "reason": NO_LAYER}
    to_nm = lambda v: round(v * dbu * 1000, 4)  # noqa: E731
    axis_height = "y" if axis_width == "x" else "x"

    widths = sorted({to_nm(_extent(b, axis_width)) for b in boxes})
    heights = sorted({to_nm(_extent(b, axis_height)) for b in boxes})

    extension = None
    if m0_width_nm is not None and len(heights) == 1:
        extension = round((heights[0] - m0_width_nm) / 2.0, 4)

    enclosure = None
    if metal_boxes:
        # Measured against the shape the via actually lands on, which is the one it
        # overlaps on both axes. Overlap on one axis alone is not enough: every gate
        # in the row shares the via's rows, and pairing a via with the gate three
        # columns away reports the column pitch as a negative enclosure.
        #
        # Full containment is not required either. A via drawn as tall as the M0 track
        # it feeds can overhang the diffcon beneath it by a nanometre, and that is a
        # real overhang to report rather than a reason to abandon the measurement on
        # the axis where the enclosure is defined.
        overhangs = []
        for via in boxes:
            for metal in metal_boxes:
                if not (_overlaps(via, metal, axis_height)
                        and _overlaps(via, metal, axis_width)):
                    continue
                overhangs.append(min(_lo(via, axis_width) - _lo(metal, axis_width),
                                     _hi(metal, axis_width) - _hi(via, axis_width)))
        if overhangs:
            enclosure = to_nm(min(overhangs))

    offset = None
    if guide_boxes:
        offsets = []
        for via in boxes:
            centre = (_lo(via, axis_height) + _hi(via, axis_height)) / 2.0
            nearest = min(guide_boxes,
                          key=lambda g: abs((_lo(g, axis_height) + _hi(g, axis_height))
                                            / 2.0 - centre))
            guide_centre = (_lo(nearest, axis_height) + _hi(nearest, axis_height)) / 2.0
            offsets.append(abs(centre - guide_centre))
        offset = to_nm(max(offsets))

    return {
        "available": True,
        "size_nm": [widths[0], heights[0]] if len(widths) == 1 and len(heights) == 1
                   else {"widths_nm": widths, "heights_nm": heights},
        "offset_nm": offset,
        "enclosure_nm": enclosure,
        "extension_nm": extension,
        "shape_count": len(boxes),
        "basis": ("via extension from rule 3.7.2/3.9.2 (via height = M0 width + "
                  "2 x via extension); enclosure is the metal overhang on each side; "
                  "offset is the via centre against the track-guide centre"),
    }


def _add_categorical(params: dict[str, dict[str, Any]], path: Path,
                     layermap: dict[str, Any] | None, guide0: list[db.Box],
                     boundary: list[db.Box], bm0: list[db.Box], along: str,
                     dbu: float) -> None:
    """Add the tech file's non-dimensional rows: technology, power, orientation, ...

    These come from the cell classifier, which reads the same geometry. Rather than
    reimplement the reasoning, this maps its results onto the tech-file parameter
    names so that both vocabularies reach the same answer.
    """
    from analyzer.classify import classify
    from analyzer.measurements import shape_outlines

    to_nm = lambda v: round(v * dbu * 1000, 4)  # noqa: E731
    result = classify(shape_outlines(path, layermap), str(path))

    def put(name: str, value: Any, unit: str | None, basis: str, rule: str) -> None:
        params[name] = {"parameter": name, "value": value, "unit": unit,
                        "available": value is not None, "drm_rule": rule,
                        "basis": basis}

    tech = result.get("technology") or {}
    put("Technology", (tech.get("technology") or "").lower() or None, None,
        tech.get("basis", ""), "3.1.1/3.6.2")

    power = result.get("power_delivery") or {}
    delivery = power.get("power_delivery")
    put("Power Distribution", power.get("backside") if delivery else None, None,
        (f"{delivery} power delivery - {power.get('basis', '')}" if delivery
         else "no power layer carried a recognised supply label"), "3.12.1")

    metal = result.get("metal_solution") or {}
    solution = metal.get("metal_solution")
    readable = {"ThreeMetalSolution": "Three Metal Solution",
                "TwoMetalSolution": "Two Metal Solution",
                "SingleMetalSolution": "Single Metal Solution"}.get(solution)
    put("Routing Capability", readable, None, metal.get("basis", ""), "3.14")

    orientation = result.get("orientation") or {}
    put("Orientation", orientation.get("orientation"), None,
        orientation.get("basis", ""), "3.15")

    tracks = result.get("routing_tracks") or {}
    put("Number of routing tracks", tracks.get("tracks"), None,
        tracks.get("basis", ""), "3.14")

    # Multiheight counts row heights, so it needs the row pitch. Backside rail centres
    # give it directly: rule 3.12.1 puts one rail on each cell edge, so the distance
    # between them is one row.
    height = result.get("cell_height") or {}
    multiheight = None
    basis = height.get("basis", "")
    if boundary and bm0:
        cell = _lo(boundary[0], along), _hi(boundary[0], along)
        centres = sorted({(_lo(b, along) + _hi(b, along)) / 2.0 for b in bm0})
        if len(centres) >= 2:
            row = centres[1] - centres[0]
            span = cell[1] - cell[0]
            if row > 0 and abs(span / row - round(span / row)) < 1e-9:
                multiheight = int(round(span / row))
                basis = (f"the cell spans {to_nm(span):g} nm and the power rails are "
                         f"{to_nm(row):g} nm apart, so it is {multiheight} row(s) high")
    if multiheight is None and height.get("height") == "single":
        multiheight = 1
    put("Multiheight", multiheight, None, basis, "3.12.1")


def tech_parameters(gds: str | Path, layermap: dict[str, Any] | None) -> dict[str, Any]:
    """Measure every tech-file parameter the layout can express.

    Returns one record per parameter: the value, the unit, how it was measured, and
    the manual rule that defines it. Parameters the layout cannot express carry
    `available: False` and the reason instead of a value.
    """
    path = Path(gds)
    layout = db.Layout()
    layout.read(str(path))
    top = rank_top_cells(layout)[0]
    dbu = float(layout.dbu)
    to_nm = lambda v: round(v * dbu * 1000, 4)  # noqa: E731

    get = lambda name: _shapes(layout, top, layermap, name)  # noqa: E731
    npoly, ppoly = get("NPOLY"), get("PPOLY")
    ndiff, pdiff = get("NDIFF"), get("PDIFF")
    ndiffcon, pdiffcon = get("NDIFFCON"), get("PDIFFCON")
    boundary, bm0 = get("CELL-BOUNDARY"), get("BM0")
    m0, m1 = get("M0"), get("M1")
    guide0, guide1, guide2 = (get("M0-TRACK-GUIDE"), get("M1-TRACK-GUIDE"),
                              get("M2-TRACK-GUIDE"))

    along = _poly_direction(layout, top, layermap)          # parallel to poly
    across = ("y" if along == "x" else "x") if along else None  # orthogonal to poly

    params: dict[str, dict[str, Any]] = {}

    def record(name: str, value: Any, unit: str | None, rule: str, basis: str,
               **extra: Any) -> None:
        params[name] = {"parameter": name, "value": value, "unit": unit,
                        "available": value is not None, "drm_rule": rule,
                        "basis": basis, **extra}

    def unavailable(name: str, unit: str | None, rule: str, reason: str) -> None:
        params[name] = {"parameter": name, "value": None, "unit": unit,
                        "available": False, "drm_rule": rule, "basis": reason}

    if across is None:
        unavailable("Poly direction", None, "3.2.1",
                    "no NPOLY or PPOLY geometry, so the poly direction is undefined "
                    "and no width or extension can be measured")
        return {"file": path.name, "top_cell": top.name, "dbu_um": dbu,
                "poly_direction": None, "parameters": params,
                "measured_count": 0, "unavailable_count": len(params)}

    # --- widths, orthogonal to the poly direction (3.2.1, 3.3.1) ---------------
    for label, boxes, rule in (("N-poly width", npoly, "3.2.1"),
                               ("P-poly width", ppoly, "3.2.1"),
                               ("N-diffcon width", ndiffcon, "3.3.1"),
                               ("P-diffcon width", pdiffcon, "3.3.1")):
        width = _min_width(boxes, across)
        if width is None:
            unavailable(label, UNIT_NM, rule, NO_LAYER)
        else:
            record(label, to_nm(width), UNIT_NM, rule,
                   f"narrowest extent orthogonal to the poly direction over "
                   f"{len(boxes)} shape(s)", shape_count=len(boxes))

    # --- diffusion width, parallel to poly (3.1) ------------------------------
    diff_width = _min_width(ndiff + pdiff, along)
    if diff_width is None:
        unavailable("Diffusion width", UNIT_NM, "3.1.1", NO_LAYER)
    else:
        record("Diffusion width", to_nm(diff_width), UNIT_NM, "3.1.1",
               "narrowest diffusion extent parallel to the poly direction",
               shape_count=len(ndiff + pdiff))

    # --- spacings -------------------------------------------------------------
    np_gap = _min_gap(ndiff, pdiff, along)
    if np_gap is None:
        unavailable("N/P Diffusion spacing", UNIT_NM, "3.1.1",
                    "ndiff and pdiff do not face each other across the poly "
                    "direction, so there is no spacing to measure")
    else:
        record("N/P Diffusion spacing", to_nm(np_gap), UNIT_NM, "3.1.1",
               "smallest clear gap between ndiff and pdiff, parallel to the poly "
               "direction, over facing pairs only")

    poly_diffcon = _min_gap(npoly + ppoly, ndiffcon + pdiffcon, across)
    if poly_diffcon is None:
        unavailable("Poly to Diffcon spacing", UNIT_NM, "3.3.2", NO_LAYER)
    else:
        record("Poly to Diffcon spacing", to_nm(poly_diffcon), UNIT_NM, "3.3.2",
               "smallest clear gap between poly and diffcon, orthogonal to the poly "
               "direction")

    gate_cut = _min_gap(npoly, ppoly, along)
    if gate_cut is None:
        unavailable("Gate Cut spacing", UNIT_NM, "3.2.3",
                    "no npoly/ppoly pair is cut - every gate runs through, so there "
                    "is no end-to-end gap to measure")
    else:
        record("Gate Cut spacing", to_nm(gate_cut), UNIT_NM, "3.2.3",
               "npoly-to-ppoly end-to-end gap parallel to the poly direction, at the "
               "cut gate; uncut gates have no gap and are excluded")

    diffcon_ete = _min_gap(ndiffcon, pdiffcon, along)
    if diffcon_ete is None:
        unavailable("Diffcon ETE spacing", UNIT_NM, "3.3.4", NO_LAYER)
    else:
        record("Diffcon ETE spacing", to_nm(diffcon_ete), UNIT_NM, "3.3.4",
               "end-to-end gap between ndiffcon and pdiffcon, parallel to the poly "
               "direction")

    # --- extensions, parallel to poly (3.2.2, 3.3.3) --------------------------
    gate_ext = min((v for v in (_min_extension(ndiff, npoly, along),
                                _min_extension(pdiff, ppoly, along))
                    if v is not None), default=None)
    if gate_ext is None:
        unavailable("Gate extension", UNIT_NM, "3.2.2", NO_LAYER)
    else:
        record("Gate extension", to_nm(gate_ext), UNIT_NM, "3.2.2",
               "smallest distance the poly reaches beyond its diffusion, parallel to "
               "the poly direction, over every overlapping poly/diffusion pair")

    diffcon_ext = min((v for v in (_min_extension(ndiff, ndiffcon, along),
                                   _min_extension(pdiff, pdiffcon, along))
                       if v is not None), default=None)
    if diffcon_ext is None:
        unavailable("Diffcon extension", UNIT_NM, "3.3.3", NO_LAYER)
    else:
        record("Diffcon extension", to_nm(diffcon_ext), UNIT_NM, "3.3.3",
               "smallest distance the diffcon reaches beyond its diffusion, parallel "
               "to the poly direction")

    # --- power rail (3.12.1) --------------------------------------------------
    rail = _min_width(bm0, along)
    if rail is None:
        unavailable("Power rail width", UNIT_NM, "3.12.1",
                    "no BM0 geometry, so the power rail width is not in this layout")
    else:
        record("Power rail width", to_nm(rail), UNIT_NM, "3.12.1",
               "BM0 extent parallel to the poly direction; rule 3.12.1 makes the BM0 "
               "width equal to the power rail width", shape_count=len(bm0))

    # --- diff interconnect: CFET only (3.13.5) -------------------------------
    diff_ic = get("DIFF-INTERCONNECT")
    if diff_ic:
        gap = _min_gap(diff_ic, ndiff + pdiff, along)
        if gap is None:
            unavailable("Diffusion to Diff interconnect spacing", UNIT_NM, "3.13.5",
                        "the Diff Interconnect and the diffusions do not face each "
                        "other, so there is no spacing to measure")
        else:
            record("Diffusion to Diff interconnect spacing", to_nm(gap), UNIT_NM,
                   "3.13.5", "smallest gap between Diff Interconnect and diffusion")
    else:
        unavailable("Diffusion to Diff interconnect spacing", UNIT_NM, "3.13.5",
                    CFET_ONLY)

    # --- metal track profiles ------------------------------------------------
    if boundary:
        cell = boundary[0]
        for label, guides, axis in (("Metal0", guide0, along),
                                    ("Metal1", guide1, across),
                                    ("Metal2", guide2, along)):
            profile = _track_profile(guides, axis, _lo(cell, axis), _hi(cell, axis), dbu)
            if profile is None:
                unavailable(label, UNIT_NM, "3.14",
                            f"the {label} track-guide layer carries no geometry")
                continue
            # The full sequence is the value, because it is unambiguous and it sums to
            # the cell dimension. The compact form is carried alongside for the
            # uniform layers, since that is how a tech file usually prints them, and
            # an answer can then give whichever the question asked for.
            record(label, profile["sequence_nm"], UNIT_NM, "3.14",
                   f"the cell cross-section along {axis}, written as margin, track "
                   f"width, gap, ... margin; the sequence sums to "
                   f"{profile['sums_to_nm']:g} nm, the cell "
                   f"{'height' if axis == 'y' else 'width'}",
                   sequence_nm=profile["sequence_nm"],
                   compact_nm=profile["compact_nm"],
                   track_count=profile["track_count"],
                   widths_nm=profile["widths_nm"], gaps_nm=profile["gaps_nm"],
                   uniform=profile["uniform"],
                   closes_on_cell=profile["sums_to_nm"] == profile["cell_extent_nm"])

        # The diffcon rows have the same shape of answer, and a tech file lists them
        # alongside the metals even where it leaves the entry blank.
        diffcon_profile = _row_profile(ndiffcon, pdiffcon, along,
                                       _lo(cell, along), _hi(cell, along), dbu)
        if diffcon_profile is None:
            unavailable("diffcon", UNIT_NM, "3.3.8", NO_LAYER)
        else:
            record("diffcon", diffcon_profile["sequence_nm"], UNIT_NM, "3.3.8",
                   diffcon_profile["basis"],
                   sequence_nm=diffcon_profile["sequence_nm"],
                   rows_nm=diffcon_profile["rows_nm"],
                   exceptions=diffcon_profile["exceptions"])
    else:
        for label in ("Metal0", "Metal1", "Metal2", "diffcon"):
            unavailable(label, UNIT_NM, "3.14",
                        "no CELL-BOUNDARY layer, and the profile is measured from the "
                        "cell edge; the layout bounding box is not a substitute "
                        "because it is derived from the same shapes")

    # --- vias ----------------------------------------------------------------
    m0_width = _min_width(m0, along)
    m0_width_nm = to_nm(m0_width) if m0_width is not None else None
    for label, layer, enclosing, guides in (
            ("pviag", "P-VIAG", ppoly, guide0),
            ("nviag", "N-VIAG", npoly, guide0),
            ("pviat", "P-VIAT", pdiffcon, guide0),
            ("nviat", "N-VIAT", ndiffcon, guide0),
            ("via0", "VIA0", m1, guide0),
            ("via1", "VIA1", get("M2"), guide2),
            ("Diff Interconnect", "DIFF-INTERCONNECT", ndiff + pdiff, guide0)):
        geometry = _via_geometry(get(layer), across, enclosing, m0_width_nm, guides, dbu)
        params[label] = {"parameter": label, "unit": UNIT_NM,
                         "drm_rule": "3.7.2/3.9.2", **geometry}
        if not geometry.get("available"):
            params[label]["value"] = None
            params[label]["basis"] = geometry["reason"]
        else:
            params[label]["value"] = {
                "size": geometry["size_nm"], "offset": geometry["offset_nm"],
                "enclosure": geometry["enclosure_nm"],
                "extension": geometry["extension_nm"]}

    # --- the categorical rows of a tech file ---------------------------------
    # Technology, power delivery, orientation and the rest are already classified from
    # the same geometry; a tech file lists them beside the dimensions, so a question
    # about "the tech file parameters" should reach them from one place.
    _add_categorical(params, path, layermap, guide0, boundary, bm0, along, dbu)

    measured = sum(1 for p in params.values() if p.get("available"))
    return {
        "file": path.name,
        "top_cell": top.name,
        "dbu_um": dbu,
        "poly_direction": along,
        "orthogonal_direction": across,
        "parameters": params,
        "measured_count": measured,
        "unavailable_count": len(params) - measured,
        "basis": ("measured from the .gds geometry with layers identified by name from "
                  "the .lyp; each parameter follows the definition in the GENCHIP "
                  "Design Rule Manual rule cited against it"),
    }


BUNDLED_SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def find_reference(gds: str | Path,
                   search_dirs: list[Path] | None = None) -> Path | None:
    """Locate a `<stem>.techparams.json` for a layout.

    Looked for beside the layout first, then in the bundled samples. An uploaded file
    is written to a temporary directory, so the bundled lookup is what lets a sample's
    stated tech file be found by name.
    """
    path = Path(gds)
    for directory in [path.parent, *(search_dirs or []), BUNDLED_SAMPLES]:
        candidate = directory / f"{path.stem}.techparams.json"
        if candidate.exists():
            return candidate
    return None


def load_reference(path: str | Path) -> dict[str, Any]:
    """Read a stated tech-file parameter table into {name: {value, unit}}."""
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("parameters") if isinstance(raw, dict) else raw
    stated: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        name = row.get("Parameter1") or row.get("parameter") or row.get("name")
        if name:
            stated[str(name)] = {"value": row.get("Value", row.get("value")),
                                 "unit": row.get("unit")}
    return {"file": Path(path).name,
            "source": (raw.get("source") if isinstance(raw, dict) else None)
                      or "supplied tech file",
            "stated": stated, "parameter_count": len(stated)}


def _comparable(measured: Any, stated: Any) -> bool | None:
    """Whether a measured value agrees with a stated one, or None if not comparable.

    A stated via record of all zeros is a deviation table, not a size: the layout draws
    an 18 x 12 nm via0 and the tech file says its offset, enclosure and via extension
    are all zero, which the measurement confirms. So the two are compared field by
    field, and `size` is skipped rather than declared a mismatch against [0, 0].
    """
    if measured is None or stated is None:
        return None
    if isinstance(stated, bool) or isinstance(measured, bool):
        return bool(measured) == bool(stated)
    if isinstance(stated, str):
        return str(measured).strip().lower() == stated.strip().lower()
    if isinstance(stated, dict):
        if not isinstance(measured, dict):
            return None
        checks = []
        for field in ("offset", "enclosure", "extension"):
            want, got = stated.get(field), measured.get(field)
            if want is None or got is None:
                continue
            want_value = want[0] if isinstance(want, list) and want else want
            if isinstance(want_value, (int, float)) and isinstance(got, (int, float)):
                checks.append(abs(float(got) - float(want_value)) < 1e-6)
        return all(checks) if checks else None
    if isinstance(stated, list):
        if not stated or not isinstance(measured, list):
            return None
        as_float = lambda seq: [float(v) for v in seq]  # noqa: E731
        if as_float(measured) == as_float(stated):
            return True
        # A uniform profile may be stated as its repeating unit [margin, width, gap].
        if len(stated) == 3:
            margin, width, gap = as_float(stated)
            body = as_float(measured)
            return (len(body) >= 3 and body[0] == margin and body[-1] == margin
                    and all(v == width for v in body[1:-1:2])
                    and all(v == gap for v in body[2:-1:2]))
        return False
    if isinstance(stated, (int, float)) and isinstance(measured, (int, float)):
        return abs(float(measured) - float(stated)) < 1e-6
    return None


def compare_to_reference(result: dict[str, Any],
                         reference: dict[str, Any]) -> dict[str, Any]:
    """Check the measured parameters against a stated tech file.

    This is the layout-versus-tech-file check: the tech file says what the cell should
    be, the geometry says what it is, and the interesting output is where they part
    company. Parameters the layout cannot express are carried through as stated-only,
    labelled, so an answer can quote them without implying they were measured.
    """
    stated = reference.get("stated") or {}
    agree, disagree, stated_only, measured_only, incomparable = [], [], [], [], []

    for name, record in (result.get("parameters") or {}).items():
        want = stated.get(name)
        measured = record.get("value")
        if want is None:
            if record.get("available"):
                measured_only.append({"parameter": name, "measured": measured})
            continue
        verdict = _comparable(measured, want["value"])
        row = {"parameter": name, "measured": measured, "stated": want["value"],
               "unit": record.get("unit") or want.get("unit"),
               "basis": record.get("basis")}
        if verdict is True:
            agree.append(row)
        elif verdict is False:
            disagree.append(row)
        elif not record.get("available"):
            row["reason"] = record.get("basis")
            stated_only.append(row)
        else:
            incomparable.append(row)

    for name, want in stated.items():
        if name not in (result.get("parameters") or {}):
            stated_only.append({"parameter": name, "measured": None,
                                "stated": want["value"], "unit": want.get("unit"),
                                "reason": "the tool does not measure this parameter"})

    return {
        "reference_file": reference.get("file"),
        "reference_source": reference.get("source"),
        "agree": agree, "disagree": disagree, "stated_only": stated_only,
        "measured_only": measured_only, "incomparable": incomparable,
        "agree_count": len(agree), "disagree_count": len(disagree),
        "headline": (f"{len(agree)} of {len(agree) + len(disagree)} comparable "
                     f"parameters match the tech file"
                     + (f", {len(disagree)} disagree" if disagree else "")
                     + (f"; {len(stated_only)} stated but not measurable in this cell"
                        if stated_only else "")),
    }


def parameter(result: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Look a parameter up forgivingly, so a question can phrase it loosely.

    "gate extension", "Gate Extension" and "what is the gate ext" should all reach
    the same record; a tech-file parameter name is not something a user should have
    to type exactly.
    """
    params = result.get("parameters") or {}
    needle = name.strip().lower()
    if not needle:
        return None
    for key, value in params.items():
        if key.lower() == needle:
            return value
    # Longest match first, so "N-poly width" is not shadowed by "poly width".
    for key in sorted(params, key=len, reverse=True):
        if needle in key.lower() or key.lower() in needle:
            return params[key]
    return None
