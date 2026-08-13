"""Layout-versus-layout XOR comparison - what changed, how much, and where.

The existing comparison answered "did this layer change?" with a fingerprint. That
is not what a physical-design engineer reviewing a revision needs. The industry
check after a metal spin or an ECO is a **layer-by-layer XOR** of the two
databases, reviewed as a marker database: every difference has a location so it can
be navigated to, and differences are binned by size so that a one-grid-unit edge
shift is not confused with a real edit.

So this module reports, per layer:

* the XOR regions - the geometry that is in one layout and not the other;
* their **count, total area, and largest single difference**;
* **where** they are: a bounding box, and the centre of each of the largest few;
* the split between **removed** (in A only) and **added** (in B only), because
  those mean different things to a reviewer;
* a size **binning** against a tolerance, following KLayout's approach of
  undersizing after the boolean, so sub-tolerance edge shifts can be set aside.

It also answers the question that actually decides cost: **which mask layers are
affected.** A change confined to metal and via layers is consistent with a metal
ECO; a change touching diffusion, poly or contact means the base layers move too.
That is reported as an observation about which layers differ - it is not a
manufacturing or cost verdict, which depends on the mask set and the foundry.

Nothing here is a DRC result. An XOR difference is a difference, not an error: the
whole point of the review is that a human decides whether each one was intended.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .connectivity import CONDUCTOR_ROLES, layer_roles
from .gds_parser import rank_top_cells

# Roles whose change implies the base masks move, not just the interconnect.
BASE_ROLES = ("diffusion", "poly", "contact", "well")
INTERCONNECT_ROLES = ("metal", "via")

# How many individual differences to record per layer. A reviewer works through the
# largest ones first, but the difference *map* needs them all, so this is generous:
# the presentation layer trims for the findings table, and under-drawing the map
# would misrepresent the change.
TOP_DIFFERENCES = 250


def _flatten(path: Path):
    """Merged region and text list per (layer, datatype), flattened into the top cell."""
    import klayout.db as db
    layout = db.Layout()
    layout.read(str(path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError(f"{path.name} contains no top-level cell.")
    top = tops[0]
    regions: dict[tuple[int, int], Any] = {}
    texts: dict[tuple[int, int], list[str]] = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        region = db.Region()
        strings: list[str] = []
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            if shape.is_text():
                strings.append(shape.text.string)
            elif shape.is_box():
                region.insert(db.Polygon(shape.box).transformed(trans))
            elif shape.is_polygon():
                region.insert(shape.polygon.transformed(trans))
            elif shape.is_path():
                region.insert(shape.path.polygon().transformed(trans))
            it.next()
        if not region.is_empty():
            regions[key] = region.merged()
        if strings:
            texts[key] = sorted(strings)
    return layout, top, regions, texts


def _describe(region, dbu: float, limit: int = TOP_DIFFERENCES) -> dict[str, Any]:
    """Count, area, bounding box and the largest few locations of a difference set."""
    polys = list(region.each())
    if not polys:
        return {"count": 0, "area_um2": 0.0, "largest_area_um2": None,
                "bbox_um": None, "locations": []}
    sized = sorted(((float(p.area()) * dbu * dbu, p) for p in polys),
                   key=lambda t: -t[0])
    bbox = region.bbox()
    return {
        "count": len(polys),
        "area_um2": round(sum(a for a, _ in sized), 12),
        "largest_area_um2": round(sized[0][0], 12),
        "bbox_um": [round(bbox.left * dbu, 6), round(bbox.bottom * dbu, 6),
                    round(bbox.right * dbu, 6), round(bbox.top * dbu, 6)],
        "locations": [
            {"centre_um": [round((p.bbox().left + p.bbox().right) / 2 * dbu, 6),
                           round((p.bbox().bottom + p.bbox().top) / 2 * dbu, 6)],
             "area_um2": round(area, 12),
             "width_um": round(p.bbox().width() * dbu, 6),
             "height_um": round(p.bbox().height() * dbu, 6),
             # The outline, so the difference can be drawn rather than only listed.
             # A reviewer navigates visually; a coordinate in a table is a poor
             # substitute for seeing the shape in place.
             "outline_um": [[round(pt.x * dbu, 6), round(pt.y * dbu, 6)]
                            for pt in p.each_point_hull()]}
            for area, p in sized[:limit]],
    }


def xor_compare(path_a: str | Path, path_b: str | Path,
                layermap: dict[str, Any] | None = None,
                tolerance_um: float = 0.0,
                role_overrides: dict[str, str] | None = None,
                detail_limit: int = TOP_DIFFERENCES) -> dict[str, Any]:
    """XOR two layouts layer by layer.

    `tolerance_um` sets a size below which a difference is binned as an edge shift
    rather than an edit. It is applied the way KLayout's XOR does it - by
    undersizing the difference - so a thin sliver along an edge drops out while a
    genuine added or removed shape survives.
    """
    import klayout.db as db

    path_a, path_b = Path(path_a), Path(path_b)
    layout_a, top_a, regions_a, texts_a = _flatten(path_a)
    layout_b, top_b, regions_b, texts_b = _flatten(path_b)
    dbu_a, dbu_b = float(layout_a.dbu), float(layout_b.dbu)

    warnings: list[str] = []
    if abs(dbu_a - dbu_b) > 1e-15:
        return {
            "comparable": False,
            "reason": (f"the two layouts use different database units "
                       f"({dbu_a} µm vs {dbu_b} µm), so their coordinates are not on a "
                       f"common grid and an XOR would be meaningless"),
            "file_a": path_a.name, "file_b": path_b.name, "warnings": warnings,
        }
    dbu = dbu_a
    if top_a.name != top_b.name:
        warnings.append(
            f"The analysed top cells have different names (`{top_a.name}` vs `{top_b.name}`). "
            "They are compared as whole layouts; if these are unrelated designs the differences "
            "below are not a revision diff.")

    roles = layer_roles(layermap, role_overrides)
    tol_dbu = int(round(tolerance_um / dbu / 2)) if tolerance_um else 0

    layers: list[dict[str, Any]] = []
    for key in sorted(set(regions_a) | set(regions_b) | set(texts_a) | set(texts_b)):
        ra = regions_a.get(key, db.Region())
        rb = regions_b.get(key, db.Region())
        meta = roles.get(key, {})
        name = meta.get("name") or f"layer_{key[0]}_{key[1]}"

        xor = ra ^ rb
        removed = ra - rb          # in A, gone from B
        added = rb - ra            # new in B

        row: dict[str, Any] = {
            "layer": key[0], "datatype": key[1], "name": name,
            "role": meta.get("role", "unknown"),
            "present_in_a": key in regions_a, "present_in_b": key in regions_b,
            "shapes_a": ra.count(), "shapes_b": rb.count(),
            "area_a_um2": round(float(ra.area()) * dbu * dbu, 12),
            "area_b_um2": round(float(rb.area()) * dbu * dbu, 12),
            "identical": xor.is_empty(),
            "xor": _describe(xor, dbu, detail_limit),
            "removed": _describe(removed, dbu, detail_limit),
            "added": _describe(added, dbu, detail_limit),
        }
        row["area_delta_um2"] = round(row["area_b_um2"] - row["area_a_um2"], 12)

        # Size binning. `significant` survives an undersize by half the tolerance
        # from each edge; the remainder is an edge shift at or below tolerance.
        if tol_dbu and not xor.is_empty():
            significant = xor.sized(-tol_dbu).sized(tol_dbu)
            row["above_tolerance"] = _describe(significant, dbu)
            row["at_or_below_tolerance_count"] = xor.count() - significant.count()
            row["tolerance_um"] = tolerance_um
        elif not xor.is_empty():
            row["above_tolerance"] = row["xor"]
            row["at_or_below_tolerance_count"] = 0
            row["tolerance_um"] = 0.0

        # Text is compared separately: a renamed or moved label changes no geometry
        # but does change what an LVS run will match.
        ta, tb = texts_a.get(key, []), texts_b.get(key, [])
        if ta or tb:
            row["texts_a"], row["texts_b"] = len(ta), len(tb)
            row["texts_added"] = sorted(set(tb) - set(ta))
            row["texts_removed"] = sorted(set(ta) - set(tb))
        layers.append(row)

    changed = [r for r in layers if not r["identical"]]
    return {
        "comparable": True,
        "file_a": path_a.name, "file_b": path_b.name,
        "top_cell_a": top_a.name, "top_cell_b": top_b.name,
        "dbu_um": dbu, "tolerance_um": tolerance_um,
        "basis": ("layer-by-layer XOR of the two layouts, flattened into the analysed top cell. "
                  "A difference is a difference, not an error - whether each one was intended is "
                  "a judgement this tool cannot make."),
        "layers": layers,
        "changed_layers": [r["name"] for r in changed],
        "summary": _summarise(changed, layers, dbu),
        "mask_impact": _mask_impact(changed, roles),
        "warnings": warnings,
        "not_derivable": {
            "intent": "Whether a difference is intentional requires the design intent or an ECO "
                      "description. Requires netlist/design intent.",
            "rule_compliance": "Whether the changed geometry is legal requires a rule deck. "
                               "Requires PDK/DRC rules.",
            "cost": "Whether a change needs a new mask set depends on the mask plan and the "
                    "foundry, not on the layout alone.",
        },
    }


def _summarise(changed: list[dict], layers: list[dict], dbu: float) -> dict[str, Any]:
    total_area = round(sum(r["xor"]["area_um2"] for r in changed), 12)
    regions = sum(r["xor"]["count"] for r in changed)
    biggest = max(changed, key=lambda r: r["xor"]["largest_area_um2"] or 0, default=None)
    added_only = [r["name"] for r in changed if not r["present_in_a"]]
    removed_only = [r["name"] for r in changed if not r["present_in_b"]]
    text_changed = [r["name"] for r in layers
                    if r.get("texts_added") or r.get("texts_removed")]
    return {
        "identical": not changed,
        "layers_compared": len(layers),
        "layers_changed": len(changed),
        "difference_regions": regions,
        "total_xor_area_um2": total_area,
        "largest_single_difference_um2": (biggest["xor"]["largest_area_um2"] if biggest else None),
        "largest_difference_on_layer": biggest["name"] if biggest else None,
        "largest_difference_at_um": (biggest["xor"]["locations"][0]["centre_um"]
                                     if biggest and biggest["xor"]["locations"] else None),
        "layers_only_in_b": added_only,
        "layers_only_in_a": removed_only,
        "layers_with_text_changes": text_changed,
        "total_area_removed_um2": round(sum(r["removed"]["area_um2"] for r in changed), 12),
        "total_area_added_um2": round(sum(r["added"]["area_um2"] for r in changed), 12),
    }


def _mask_impact(changed: list[dict], roles: dict) -> dict[str, Any]:
    """Which kinds of layer changed - the question that drives re-spin scope."""
    by_role: dict[str, list[str]] = {}
    for row in changed:
        by_role.setdefault(row["role"], []).append(row["name"])
    base = sorted(n for r in BASE_ROLES for n in by_role.get(r, []))
    interconnect = sorted(n for r in INTERCONNECT_ROLES for n in by_role.get(r, []))
    other = sorted(n for r, names in by_role.items()
                   if r not in BASE_ROLES + INTERCONNECT_ROLES for n in names)

    if not changed:
        verdict = "The two layouts are geometrically identical on every layer."
    elif base:
        verdict = (f"Base layers changed ({', '.join(base)}), so this is not confined to the "
                   f"interconnect. A change here affects the transistor-level masks.")
    elif interconnect:
        verdict = (f"Changes are confined to interconnect layers ({', '.join(interconnect)}), "
                   f"which is the pattern of a metal/via ECO rather than a base-layer edit.")
    else:
        verdict = (f"Only auxiliary layers changed ({', '.join(other)}). These carry no "
                   f"conducting geometry, so no conductor was altered.")
    # Which bucket a layer lands in depends on its role, and roles are inferred from
    # layer names unless corrected. That matters here: NDIFFCON reads as a contact
    # (a base layer) by name, but in this technology it is local interconnect, which
    # would move it to the interconnect bucket and change the verdict. Say so rather
    # than presenting the verdict as if the classification were certain.
    inferred = sorted({row["name"] for row in changed
                       if (roles.get((row["layer"], row["datatype"])) or {})
                       .get("role_source") == "inferred from name"})
    return {
        "base_layers_changed": base,
        "interconnect_layers_changed": interconnect,
        "other_layers_changed": other,
        "observation": verdict,
        "roles_inferred_from_names": inferred,
        "caveat": ("This describes which layers differ, grouped by their inferred role. Roles come "
                   "from layer names unless a stack file corrects them, and the grouping changes if "
                   "a role is wrong - NDIFFCON reads as a contact but is local interconnect in this "
                   "technology. Whether a new mask set is required is a mask-plan and foundry "
                   "question, not a layout one."),
    }


def compare_many(paths: list[str | Path], layermap: dict[str, Any] | None = None,
                 tolerance_um: float = 0.0,
                 role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Pairwise XOR across several layouts.

    Reviewing a family of revisions is the normal case - which of these five are
    the same, and which pair differs least? A pairwise matrix answers that in one
    view instead of ten separate comparisons.
    """
    files = [Path(p) for p in paths]
    if len(files) < 2:
        raise ValueError("comparing needs at least two layouts")

    pairs: list[dict[str, Any]] = []
    matrix: dict[str, dict[str, Any]] = {f.name: {} for f in files}
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            result = xor_compare(a, b, layermap, tolerance_um, role_overrides)
            if not result["comparable"]:
                cell = {"comparable": False, "reason": result["reason"]}
            else:
                s = result["summary"]
                cell = {"comparable": True, "identical": s["identical"],
                        "layers_changed": s["layers_changed"],
                        "difference_regions": s["difference_regions"],
                        "total_xor_area_um2": s["total_xor_area_um2"],
                        "mask_impact": result["mask_impact"]["observation"]}
            matrix[a.name][b.name] = cell
            matrix[b.name][a.name] = cell
            pairs.append({"a": a.name, "b": b.name, **cell,
                          "detail": result if result["comparable"] else None})

    comparable = [p for p in pairs if p.get("comparable")]
    identical = [(p["a"], p["b"]) for p in comparable if p["identical"]]
    ranked = sorted(comparable, key=lambda p: p["total_xor_area_um2"])
    return {
        "files": [f.name for f in files],
        "pair_count": len(pairs),
        "matrix": matrix,
        "pairs": pairs,
        "identical_pairs": identical,
        "most_similar_pair": (ranked[0]["a"], ranked[0]["b"]) if ranked else None,
        "most_different_pair": (ranked[-1]["a"], ranked[-1]["b"]) if ranked else None,
        "tolerance_um": tolerance_um,
    }
