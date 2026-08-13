"""Cell classification: power delivery, technology, metal solution, height, tracks.

These are the questions asked of a standard cell before any geometry is discussed -
"is it backside power?", "is it a two-metal cell?", "single or multi height?", "how
many routing tracks?" - and the tool previously answered none of them. Asked
"frontside or backside", it replied that the metadata had no such field, which was
true and useless: `BM0` carries `VSS` and `VDD` labels, and that *is* the answer.

Implemented to the company's own specification, with one deliberate change: the
spec is written against the JSON sidecars in an S3 folder, and this works from
**GDS + LYP**, because that is what a user uploads. The layer map identifies the
layers by name, and the labels come from the GDS text records.

Each result carries how it was reached, because several of these are inferences
rather than recorded facts. Cell *orientation* is the honest example: a placement's
orientation is recorded in the GDS as an instance transform, but a flat single-cell
layout has no placement, so the only available evidence is the order of the power
rails - which settles R0 versus Mx and cannot settle My at all. That limit is
reported rather than papered over.
"""
from __future__ import annotations

import re
from typing import Any

# Label vocabularies from the specification.
GROUND_LABELS = {"VSS", "GND", "VGND"}
POWER_LABELS = {"VDD", "VPWR", "VCC"}

# Layers holding power labels on each side. The label datatypes are checked as well
# as the drawing layer, since a label may be carried on either.
BACKSIDE_LABEL_LAYERS = ("BM0-LABEL", "BM0-PIN-LABEL", "BM0")
FRONTSIDE_LABEL_LAYERS = ("M0-LABEL", "M0-PIN-LABEL", "M0")

# A "multi height" cell is one whose largest power-rail shape recurs at least this
# many times, per the specification.
MULTI_HEIGHT_MAX_AREA_COUNT = 3


def _by_name(outlines: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in outlines["layers"]}


def _labels(by: dict[str, dict[str, Any]], layers: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for name in layers:
        for label in (by.get(name) or {}).get("labels") or []:
            out.add(label["text"].strip().upper())
    return out


def power_delivery(outlines: dict[str, Any]) -> dict[str, Any]:
    """Backside or frontside power, from the labels on each power layer.

    A side qualifies when it carries at least one ground label *and* at least one
    power label. Backside is tested first, so a layout with both is reported as
    backside - that ordering is from the specification, not an accident.
    """
    by = _by_name(outlines)
    back = _labels(by, BACKSIDE_LABEL_LAYERS)
    front = _labels(by, FRONTSIDE_LABEL_LAYERS)

    def qualifies(found: set[str]) -> bool:
        return bool(found & GROUND_LABELS) and bool(found & POWER_LABELS)

    detail = {
        "backside_labels": sorted(back), "frontside_labels": sorted(front),
        "backside_qualifies": qualifies(back), "frontside_qualifies": qualifies(front),
        "ground_vocabulary": sorted(GROUND_LABELS), "power_vocabulary": sorted(POWER_LABELS),
    }
    if qualifies(back):
        return {"power_delivery": "backside", "backside": True, "basis":
                f"the backside power layer carries {', '.join(sorted(back & (GROUND_LABELS | POWER_LABELS)))}",
                **detail}
    if qualifies(front):
        return {"power_delivery": "frontside", "backside": False, "basis":
                f"the backside layer has no power/ground pair; the frontside carries "
                f"{', '.join(sorted(front & (GROUND_LABELS | POWER_LABELS)))}",
                **detail}
    return {"power_delivery": None, "backside": None,
            "basis": ("neither power layer carries both a ground label and a power label, so the "
                      "power delivery scheme cannot be determined"),
            "failed": True, **detail}


def technology(outlines: dict[str, Any], gds_path: str | Path | None = None) -> dict[str, Any]:
    """GAA, FinFET or CFET, from the diffusion separation and the well.

    Distance zero means the n and p diffusions touch or overlap, which the
    specification and the manual's rule 3.1.2 both give as CFET.
    """
    from pathlib import Path as _Path
    by = _by_name(outlines)
    ndiff, pdiff, nwell = by.get("NDIFF"), by.get("PDIFF"), by.get("NWELL")
    if not (ndiff and ndiff["shape_count"]) or not (pdiff and pdiff["shape_count"]):
        return {"technology": "Unknown",
                "basis": "the layout has no NDIFF/PDIFF geometry, so the classification is skipped",
                "nwell_count": (nwell or {}).get("shape_count", 0)}

    touching = False
    gap = None
    if gds_path:
        # Exact test through the geometry engine: `interacting` counts a shared edge
        # as contact, which is what "distance = 0" means here.
        import klayout.db as db
        layout = db.Layout()
        layout.read(str(_Path(gds_path)))
        from .gds_parser import rank_top_cells
        top = rank_top_cells(layout)[0]

        def region(layer, datatype):
            reg = db.Region()
            index = layout.find_layer(layer, datatype)
            if index is None:
                return reg
            it = top.begin_shapes_rec(index)
            while not it.at_end():
                shape, trans = it.shape(), it.trans()
                if shape.is_box():
                    reg.insert(db.Polygon(shape.box).transformed(trans))
                elif shape.is_polygon():
                    reg.insert(shape.polygon.transformed(trans))
                elif shape.is_path():
                    reg.insert(shape.path.polygon().transformed(trans))
                it.next()
            return reg.merged()

        n_reg = region(ndiff["layer"], ndiff["datatype"])
        p_reg = region(pdiff["layer"], pdiff["datatype"])
        touching = n_reg.interacting(p_reg).count() > 0
    if not gds_path:
        # Fall back to the extents, which is enough to separate touching from apart.
        def box(row):
            return (min(s["left_um"] for s in row["shapes"]),
                    min(s["bottom_um"] for s in row["shapes"]),
                    max(s["left_um"] + s["width_um"] for s in row["shapes"]),
                    max(s["bottom_um"] + s["height_um"] for s in row["shapes"]))
        na, pa = box(ndiff), box(pdiff)
        dx = max(0.0, max(na[0] - pa[2], pa[0] - na[2]))
        dy = max(0.0, max(na[1] - pa[3], pa[1] - na[3]))
        gap = round(max(dx, dy), 9)
        touching = gap <= outlines["dbu_um"]

    wells = (nwell or {}).get("shape_count", 0)
    if touching:
        tech, why = "CFET", "the n and p diffusions touch, so their separation is zero"
    elif wells:
        tech, why = "FinFET", (f"the diffusions are separated and {wells} NWELL polygon(s) are "
                               f"present")
    else:
        tech, why = "GAA", "the diffusions are separated and there is no NWELL layer"
    return {"technology": tech, "basis": why, "diffusions_touch": touching,
            "measured_gap_um": gap, "nwell_count": wells}


def metal_solution(outlines: dict[str, Any]) -> dict[str, Any]:
    """Single, two or three metal routing, from the metal track-guide layers.

    This is a routing *capability*, so it is read from the track guides rather than
    from the drawn wires. A cell that happens to route on M0 and M1 only is still a
    three-metal cell if the technology gives it an M2 track guide - the third layer
    is available whether or not this particular cell needed it. Counting drawn metal
    instead would report the same standard cell differently depending on how busy it
    is, which is not a property of the technology.

    Drawn metal is still reported, because "M2 is available but unused" is worth
    knowing and is a different statement from "M2 is available and used".
    """
    by = _by_name(outlines)
    metals = ("M0", "M1", "M2")
    available = [name for name in metals
                 if (by.get(f"{name}-TRACK-GUIDE") or {}).get("shape_count")]
    drawn = [name for name in metals if (by.get(name) or {}).get("shape_count")]

    # Without any track guide there is nothing declaring the capability, so fall back
    # to the drawn metal and say so rather than reporting a guess as a technology fact.
    basis_layers, source = (available, "track guide") if available else (drawn, "drawn geometry")
    if {"M0", "M1", "M2"} <= set(basis_layers):
        result = "ThreeMetalSolution"
    elif {"M0", "M1"} <= set(basis_layers):
        result = "TwoMetalSolution"
    elif "M0" in basis_layers:
        result = "SingleMetalSolution"
    else:
        result = "UNKNOWN"

    if not basis_layers:
        basis = "no metal track guide and no M0 geometry were found"
    elif source == "track guide":
        unused = [m for m in available if m not in drawn]
        basis = (f"{', '.join(available)} have track guides, so the technology offers "
                 f"{len(available)} routing layer(s)"
                 + (f"; {', '.join(unused)} carry no geometry in this cell" if unused else ""))
    else:
        basis = (f"no metal track guide was found, so this counts the drawn metal "
                 f"instead: {', '.join(drawn)}")
    return {"metal_solution": result, "metals_available": available,
            "metals_drawn": drawn, "metals_present": drawn, "source": source,
            "basis": basis}


def routing_tracks(outlines: dict[str, Any]) -> dict[str, Any]:
    """Track count and occupancy, from the M0 track-guide layer.

    A track counts as used when an M0 polygon lies wholly inside it, which is the
    specification's test. The guide layer is the technology's own declaration of
    where the tracks are, so this needs no assumption about pitch.
    """
    by = _by_name(outlines)
    guides, metal = by.get("M0-TRACK-GUIDE"), by.get("M0")
    if not guides or not guides["shape_count"]:
        return {"tracks": None,
                "basis": "the M0 track-guide layer is absent, so the track count is undeclared"}
    used, empty, detail = 0, 0, []
    for track in sorted(guides["shapes"], key=lambda s: s["centre_um"][1]):
        left, bottom = track["left_um"], track["bottom_um"]
        right, top = left + track["width_um"], bottom + track["height_um"]
        occupied = any(
            s["left_um"] >= left - 1e-9 and s["bottom_um"] >= bottom - 1e-9
            and s["left_um"] + s["width_um"] <= right + 1e-9
            and s["bottom_um"] + s["height_um"] <= top + 1e-9
            for s in (metal or {}).get("shapes") or [])
        used += occupied
        empty += not occupied
        detail.append({"centre_y_um": track["centre_um"][1], "occupied": occupied})
    return {"tracks": guides["shape_count"], "tracks_used": used, "tracks_empty": empty,
            "track_detail": detail,
            "basis": ("a track counts as used when an M0 polygon lies wholly inside the guide; "
                      "the guide layer declares where the tracks are")}


def cell_height(outlines: dict[str, Any], tech: str | None = None) -> dict[str, Any]:
    """Single or multi height, by the specification's rules.

    The power rail is the measure: BM0 if present, else M0. For CFET and GAA the
    test is how often the largest rail area recurs; for FinFET it is the number of
    NWELL polygons.
    """
    by = _by_name(outlines)
    base_name = "BM0" if (by.get("BM0") or {}).get("shape_count") else (
        "M0" if (by.get("M0") or {}).get("shape_count") else None)

    # FinFET is decided by the well count alone, so it is settled before the power
    # rail is looked for. Requiring a rail first made a well-only layout return
    # "cannot be judged" while the deciding evidence was sitting right there.
    if tech == "FinFET":
        wells = (by.get("NWELL") or {}).get("shape_count", 0)
        if wells >= 2:
            return {"height": "multi", "basis": f"{wells} NWELL polygons", "base_layer": base_name}
        if wells == 1:
            return {"height": "single", "basis": "a single NWELL polygon", "base_layer": base_name}
        return {"height": None, "basis": "FinFET was identified but no NWELL polygon was found",
                "base_layer": base_name}

    if base_name is None:
        return {"height": None,
                "basis": "neither BM0 nor M0 carries geometry, so height cannot be judged"}
    base = by[base_name]

    areas = [s["area_um2"] for s in base["shapes"]]
    if not areas:
        return {"height": None, "basis": f"{base_name} has no measurable area",
                "base_layer": base_name}
    largest = max(areas)
    repeats = sum(1 for a in areas if abs(a - largest) <= 1e-12)
    height = "multi" if repeats >= MULTI_HEIGHT_MAX_AREA_COUNT else "single"
    return {"height": height, "base_layer": base_name,
            "largest_area_um2": largest, "shapes_at_largest_area": repeats,
            "basis": (f"the largest {base_name} area ({largest:g} µm²) occurs {repeats} time(s); "
                      f"{MULTI_HEIGHT_MAX_AREA_COUNT} or more means multi height")}


def half_dr(outlines: dict[str, Any]) -> dict[str, Any]:
    """Is the power rail centred on the cell boundary edge?

    True when the rail's centre y coincides with a cell-boundary y, which is the
    half-design-rule arrangement: the rail is shared with the neighbouring row.
    """
    by = _by_name(outlines)
    boundary = by.get("CELL-BOUNDARY")
    target_name = "BM0" if (by.get("BM0") or {}).get("shape_count") else "M0"
    target = by.get(target_name)
    if not boundary or not boundary["shape_count"] or not target or not target["shape_count"]:
        return {"half_dr": None,
                "basis": "the cell boundary or the power rail is missing, so this cannot be judged"}
    edges = {round(s["bottom_um"], 9) for s in boundary["shapes"]}
    edges |= {round(s["bottom_um"] + s["height_um"], 9) for s in boundary["shapes"]}
    centres = [round(s["centre_um"][1], 9) for s in target["shapes"]]
    matched = [c for c in centres if c in edges]
    if matched and len(matched) == len(centres):
        return {"half_dr": True, "target_layer": target_name,
                "boundary_y_um": sorted(edges), "rail_centres_y_um": sorted(centres),
                "basis": (f"every {target_name} centre lies on a cell-boundary edge "
                          f"({', '.join(f'{c * 1000:.0f} nm' for c in sorted(matched))}), so the "
                          f"rail is shared with the adjacent row")}
    return {"half_dr": False, "target_layer": target_name,
            "boundary_y_um": sorted(edges), "rail_centres_y_um": sorted(centres),
            "basis": (f"{len(matched)} of {len(centres)} {target_name} centres lie on a "
                      f"cell-boundary edge")}


def orientation(outlines: dict[str, Any], gds_path: str | Path | None = None) -> dict[str, Any]:
    """R0, Mx or My - and an honest account of what can and cannot be decided.

    Orientation is a property of a *placement*, recorded in the GDS as an instance
    transform. A flat single-cell layout has no placement, so there is nothing
    recorded to read. The only remaining evidence is the order of the power rails:
    ground at the bottom is the canonical R0 arrangement, and ground at the top
    means a flip about the x axis. A flip about the y axis leaves the rail order
    untouched and so cannot be detected from one cell at all.
    """
    from pathlib import Path as _Path
    placements: list[dict[str, Any]] = []
    if gds_path:
        import klayout.db as db
        layout = db.Layout()
        layout.read(str(_Path(gds_path)))
        for cell in layout.each_cell():
            for inst in cell.each_inst():
                trans = inst.trans
                code = ("M" if trans.is_mirror() else "R") + str(int(trans.angle))
                placements.append({"in_cell": cell.name,
                                   "cell": layout.cell(inst.cell_index).name,
                                   "orientation": {"R0": "R0", "M0": "Mx", "M90": "My"}.get(
                                       code, code),
                                   "raw": code, "count": inst.size()})
    if placements:
        found = sorted({p["orientation"] for p in placements})
        return {"orientation": found[0] if len(found) == 1 else None,
                "orientations_present": found, "placements": placements[:20],
                "confidence": "recorded",
                "basis": ("read from the instance transforms in the GDS, which is where a "
                          "placement's orientation is actually stored")}

    by = _by_name(outlines)
    rails = _labels(by, BACKSIDE_LABEL_LAYERS) or _labels(by, FRONTSIDE_LABEL_LAYERS)
    positions: dict[str, list[float]] = {}
    for name in BACKSIDE_LABEL_LAYERS + FRONTSIDE_LABEL_LAYERS:
        for label in (by.get(name) or {}).get("labels") or []:
            text = label["text"].strip().upper()
            if text in GROUND_LABELS | POWER_LABELS:
                positions.setdefault(text, []).append(label["at_um"][1])
    ground = [y for t, ys in positions.items() if t in GROUND_LABELS for y in ys]
    power = [y for t, ys in positions.items() if t in POWER_LABELS for y in ys]
    if not ground or not power:
        return {"orientation": None, "confidence": "none",
                "basis": ("this layout has no instance placements, and without both a ground and a "
                          "power label there is no rail order to judge from either"),
                "not_derivable": "orientation is a property of a placement, and none exists here"}

    flipped = min(ground) > max(power)
    return {
        "orientation": "Mx" if flipped else "R0",
        "confidence": "inferred from the power rail order",
        "ground_label_y_um": sorted(ground), "power_label_y_um": sorted(power),
        "basis": (f"ground sits at {'the top' if flipped else 'the bottom'} "
                  f"({min(ground) * 1000:.0f} nm) and power at "
                  f"{'the bottom' if flipped else 'the top'} ({max(power) * 1000:.0f} nm), "
                  f"which is {'a flip about the x axis' if flipped else 'the canonical R0 order'}"),
        "not_derivable": ("My cannot be determined from a single cell: a flip about the y axis "
                          "leaves the power rail order unchanged. It would need the placement "
                          "transform or a reference orientation to compare against."),
    }


def min_rt_number(filenames: list[str]) -> dict[str, Any]:
    """The smallest RT number among `*_RT_<n>.gds` filenames."""
    found: dict[str, int] = {}
    for name in filenames:
        match = re.search(r"_RT_(\d+)\.gds$", name, re.I)
        if match:
            found[name] = int(match.group(1))
    if not found:
        return {"min_rt": None, "rt_numbers": {},
                "basis": "no filename matches the _RT_<number>.gds pattern"}
    return {"min_rt": min(found.values()), "rt_numbers": found,
            "basis": f"taken from the filename(s): {', '.join(f'{k} -> {v}' for k, v in found.items())}"}


def classify(outlines: dict[str, Any], gds_path: str | Path | None = None,
             filenames: list[str] | None = None) -> dict[str, Any]:
    """Every classification for one layout, each with the basis for its answer."""
    power = power_delivery(outlines)
    tech = technology(outlines, gds_path)
    height = cell_height(outlines, tech["technology"])
    tracks = routing_tracks(outlines)
    metals = metal_solution(outlines)
    orient = orientation(outlines, gds_path)
    result = {
        "power_delivery": power,
        "technology": tech,
        "metal_solution": metals,
        "routing_tracks": tracks,
        "cell_height": height,
        "half_dr": half_dr(outlines),
        "orientation": orient,
        "availability": "GDS + LYP",
        "basis": ("classified from the geometry and the GDS text labels, with layers identified by "
                  "the layer map"),
    }
    if filenames:
        result["min_rt_number"] = min_rt_number(filenames)

    # The one-line answer to "what kind of cell is this?".
    parts = []
    if height.get("height") and tech.get("technology") != "Unknown":
        parts.append(f"{height['height']}-height {tech['technology']}")
    if power.get("power_delivery"):
        parts.append(f"{power['power_delivery']} power")
    if metals.get("metal_solution") not in (None, "UNKNOWN"):
        readable = {"SingleMetalSolution": "single-metal", "TwoMetalSolution": "two-metal",
                    "ThreeMetalSolution": "three-metal"}[metals["metal_solution"]]
        parts.append(f"{readable} routing")
    if tracks.get("tracks"):
        parts.append(f"{tracks['tracks']} M0 tracks ({tracks['tracks_used']} used)")
    result["headline"] = ", ".join(parts) if parts else "not enough information to classify"
    return result
