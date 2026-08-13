"""Physical connectivity analysis from GDS + LYP.

The central fact that shapes this module: **GDSII has no Z axis.** A GDS file
records shapes on numbered layers; it does not record which layer sits above
which, nor which via layer bridges which two levels. That vertical stack is a
*process* fact, and a `.lyp` file carries colours and names, not elevations.

The consequence is concrete, and it was measured rather than assumed. In a dense
standard cell almost every connector layer overlaps almost every conductor layer
in plan view, because they are stacked on top of one another. Treating plan-view
overlap as connection collapses an entire cell into one false net. Requiring full
enclosure instead is too strict: a diffusion contact is wider than the fin it
straddles, so nothing encloses it, while a wide backside power rail encloses vias
it has no connection to.

So the analysis is split into three tiers, and every result records its tier:

* **Tier 1 - intra-layer connectivity (GDS-only, exact).** Shapes on the *same*
  layer that touch or overlap are one physical conductor. No stack knowledge is
  needed and no assumption is made. Yields per-layer connected components and
  fragmentation.
* **Tier 2 - connector landing measurements (GDS + LYP, exact measurement).**
  For every via/contact shape, which conductor layers it overlaps and which
  enclose it. Reported as *overlap*, explicitly not as *connection*.
* **Tier 3 - the net graph (requires a layer connection stack).** Computed only
  when a stack is supplied. `propose_stack` derives a *candidate* stack from the
  tier-2 evidence, with a confidence and the evidence attached, and marks it as
  requiring confirmation. It is never applied silently.

Even with a stack, what comes out is physical connectivity. "M0 and M1 are
physically joined through VIA0" is derivable here; "M0 and M1 are *supposed* to
be joined" is design intent and needs a netlist or schematic. A short and an open
are both defined relative to an intended netlist, so neither is reported as such;
the observable proxies are reported instead.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .gds_parser import rank_top_cells

# Layers whose names mark them as derived, auxiliary or annotation variants of
# another layer rather than independent conductors. Leaving these in the
# conductor set is what made an early version of this analysis report NDIFFCON
# as "connected to" PPOLY-PATTERN-CUT.
DERIVED_NAME_PATTERNS = (
    r"[-_]DUPLICATE\b", r"[-_]EXTENDED\b", r"[-_]PATTERN[-_]CUT\b",
    r"TRACK[-_]?GUIDE", r"[-_]LABEL\b", r"[-_]TEXT\b", r"[-_]PIN\b",
    r"^DUMMY[-_]", r"[-_]ISLAND\b", r"BOUNDARY", r"GATE[-_]ISOLATION",
)

# Role inference from LYP layer names; first match wins, so specific before
# general. These are name heuristics, and every consumer labels them as such.
_ROLE_PATTERNS: list[tuple[str, str]] = [
    ("via",       r"(^|[-_])VIA($|[-_])|(^|[-_])VIA\d|^DVB$|VIA[GT]$|BSPDN.*VIA"),
    ("contact",   r"DIFFCON|CONTACT|(^|[-_])CON($|[-_])"),
    ("metal",     r"^BM\d+$|^M\d+$|POWERRAIL|^METAL\d*$"),
    ("poly",      r"^[NP]?POLY$|^GATE$"),
    ("diffusion", r"^[NP]?DIFF$|NANOSHEET|DIFF[-_]INTERCONNECT"),
    ("well",      r"WELL$"),
]

CONDUCTOR_ROLES = ("metal", "poly", "diffusion")
CONNECTOR_ROLES = ("via", "contact")


def is_derived(name: str) -> bool:
    """True if the layer name marks it as a derived/auxiliary variant."""
    upper = (name or "").upper()
    return any(re.search(p, upper) for p in DERIVED_NAME_PATTERNS)


def classify_role(name: str) -> str:
    """Infer a layer's role from its LYP name. A heuristic, never a LYP fact."""
    upper = (name or "").upper()
    if is_derived(upper):
        return "derived"
    for role, pattern in _ROLE_PATTERNS:
        if re.search(pattern, upper):
            return role
    return "unknown"


def layer_roles(layermap: dict[str, Any] | None,
                overrides: dict[str, str] | None = None) -> dict[tuple[int, int], dict[str, Any]]:
    """Role per (layer, datatype) from the LYP, with derived layers marked.

    `overrides` maps a layer name to a role, correcting the name heuristic where it
    is wrong. It has to exist: reading a name ending in "CON" as a contact is right
    for most technologies and wrong for this one, where NDIFFCON is local
    interconnect. Only someone who knows the technology can settle that.
    """
    if not layermap:
        return {}
    by_upper = {str(k).upper(): v for k, v in (overrides or {}).items()}
    out = {}
    for key, entry in layermap["by_key"].items():
        name = entry["technology_name"]
        lyp_role = entry.get("role", "drawing")
        role = classify_role(name)
        # The LYP's own role field wins where it disagrees: a layer flagged
        # pin/label/duplicate is not a conductor whatever it happens to be called.
        if lyp_role in ("pin", "label", "duplicate"):
            role = "derived"
        overridden = by_upper.get(str(name).upper())
        if overridden and role != "derived":
            role = overridden
        out[key] = {"name": name, "role": role, "lyp_role": lyp_role,
                    "derived": role == "derived",
                    "role_source": "supplied override" if overridden else "inferred from name"}
    return out


def _conductors(roles) -> list[tuple[int, int]]:
    return sorted(k for k, v in roles.items() if v["role"] in CONDUCTOR_ROLES)


def _connectors(roles) -> list[tuple[int, int]]:
    return sorted(k for k, v in roles.items() if v["role"] in CONNECTOR_ROLES)


def _load_regions(gds_path: str | Path, keys):
    """Merged region per (layer, datatype), flattened into the top cell."""
    import klayout.db as db
    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    out: dict[tuple[int, int], Any] = {}
    for layer, datatype in keys:
        li = layout.find_layer(layer, datatype)
        if li is None:
            continue
        region = db.Region()
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            if shape.is_box():
                region.insert(db.Polygon(shape.box).transformed(trans))
            elif shape.is_polygon():
                region.insert(shape.polygon.transformed(trans))
            elif shape.is_path():
                region.insert(shape.path.polygon().transformed(trans))
            it.next()
        if not region.is_empty():
            out[(layer, datatype)] = region
    return layout, top, out


# ---------------------------------------------------------------------------
# Tier 1: intra-layer connectivity. Exact, GDS-only, no stack needed.
# ---------------------------------------------------------------------------

def intra_layer_connectivity(gds_path: str | Path,
                             layermap: dict[str, Any] | None = None,
                             role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Connected components within each layer.

    Two shapes on one layer that touch or overlap are one physical conductor, so
    the count of merged polygons is the count of distinct conductors on that
    layer. This needs no knowledge of the process stack, so it is exact.

    Shapes meeting only at a corner count as connected, following KLayout's merge
    semantics - the same semantics its netlist extractor uses.
    """
    roles = layer_roles(layermap, role_overrides)
    import klayout.db as db
    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    top = tops[0]
    dbu = float(layout.dbu)

    rows: list[dict[str, Any]] = []
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        region = db.Region()
        raw = 0
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            shape, trans = it.shape(), it.trans()
            if shape.is_box():
                region.insert(db.Polygon(shape.box).transformed(trans)); raw += 1
            elif shape.is_polygon():
                region.insert(shape.polygon.transformed(trans)); raw += 1
            elif shape.is_path():
                region.insert(shape.path.polygon().transformed(trans)); raw += 1
            it.next()
        if raw == 0:
            continue
        merged = region.merged()
        components = merged.count()
        sizes = sorted((float(p.area()) * dbu * dbu for p in merged.each()), reverse=True)
        meta = roles.get(key, {})
        rows.append({
            "layer": key[0], "datatype": key[1],
            "name": meta.get("name") or f"layer_{key[0]}_{key[1]}",
            "role": meta.get("role", "unknown"),
            "shape_count": raw,
            "component_count": components,
            # More shapes than components means some shapes abut and form one
            # physical conductor.
            "shapes_per_component": round(raw / components, 4) if components else None,
            "largest_component_area_um2": round(sizes[0], 9) if sizes else None,
            "smallest_component_area_um2": round(sizes[-1], 9) if sizes else None,
        })
    rows.sort(key=lambda r: (r["layer"], r["datatype"]))
    return {
        "tier": 1,
        "availability": "GDS-only",
        "basis": "shapes on the same layer that touch or overlap form one physical conductor",
        "layers": rows,
        "total_shapes": sum(r["shape_count"] for r in rows),
        "total_components": sum(r["component_count"] for r in rows),
        "layers_with_abutting_shapes": sum(1 for r in rows
                                           if r["component_count"] < r["shape_count"]),
    }


# ---------------------------------------------------------------------------
# Tier 2: connector landing measurements. Exact measurement, GDS + LYP.
# ---------------------------------------------------------------------------

def measure_connector_landings(gds_path: str | Path,
                               layermap: dict[str, Any] | None,
                               role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Measure, per connector layer, its overlap with every conductor layer.

    Two distinct measurements are reported, because they behave very differently:

    * ``interacting`` - the connector shape touches or overlaps the conductor.
      Permissive: in a stacked cell most pairs interact.
    * ``enclosed`` - the connector shape lies wholly inside the conductor. Strong
      evidence of a real landing for vias, but structurally impossible for a
      contact wider than the diffusion it straddles.

    Neither is a connection. Deciding which overlaps are connections needs the
    process stack.
    """
    roles = layer_roles(layermap, role_overrides)
    if not roles:
        return {"tier": 2, "available": False,
                "reason": "no layer map supplied, so layer roles are unknown",
                "warnings": ["Provide a .lyp file to identify via, contact and metal layers."],
                "connectors": []}

    connectors, conductors = _connectors(roles), _conductors(roles)
    if not connectors:
        return {"tier": 2, "available": False,
                "reason": "the layer map identifies no via or contact layer",
                "warnings": [], "connectors": []}

    import klayout.db as db
    layout, _, regions = _load_regions(gds_path, connectors + conductors)
    dbu = float(layout.dbu)
    merged_cache = {k: v.merged() for k, v in regions.items()}

    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ck in connectors:
        merged = merged_cache.get(ck)
        if merged is None:
            continue
        total = merged.count()
        overlaps = []
        for dk in conductors:
            dmerged = merged_cache.get(dk)
            if dmerged is None:
                continue
            inter = merged.interacting(dmerged).count()
            if not inter:
                continue
            overlaps.append({
                "layer": list(dk), "name": roles[dk]["name"], "role": roles[dk]["role"],
                "shapes_interacting": inter,
                "shapes_enclosed": merged.inside(dmerged).count(),
                "interaction_ratio": round(inter / total, 4) if total else None,
                "enclosure_ratio": round(merged.inside(dmerged).count() / total, 4) if total else None,
            })
        overlaps.sort(key=lambda o: (-o["enclosure_ratio"], -o["interaction_ratio"], o["name"]))

        # A connector overlapping no conductor connects nothing, whatever the
        # stack turns out to be. That conclusion is stack-independent, so it is
        # safe to draw here.
        no_overlap = []
        for poly in merged.each():
            one = db.Region(); one.insert(poly)
            if not any(dk in merged_cache and not (one & merged_cache[dk]).is_empty()
                       for dk in conductors):
                bbox = poly.bbox()
                no_overlap.append({
                    "centre_um": [round((bbox.left + bbox.right) / 2 * dbu, 6),
                                  round((bbox.bottom + bbox.top) / 2 * dbu, 6)],
                    "area_um2": round(float(poly.area()) * dbu * dbu, 9)})

        out.append({
            "layer": ck[0], "datatype": ck[1], "name": roles[ck]["name"],
            "role": roles[ck]["role"], "shape_count": total,
            "overlaps": overlaps,
            "shapes_overlapping_no_conductor": len(no_overlap),
            "no_conductor_examples": no_overlap[:5],
        })
        if no_overlap:
            warnings.append(
                f"{len(no_overlap)} {roles[ck]['name']} shape(s) overlap no conductor layer at all "
                f"(e.g. at {no_overlap[0]['centre_um']} µm). Whatever the process stack is, such a "
                "shape connects nothing. Whether that is a defect depends on design intent.")

    # Conductor layers that abut each other are very likely one physical level
    # drawn across two names (e.g. NPOLY/PPOLY for a gate crossing both wells).
    same_level = []
    for i, ka in enumerate(conductors):
        for kb in conductors[i + 1:]:
            ra, rb = merged_cache.get(ka), merged_cache.get(kb)
            if ra is None or rb is None:
                continue
            touching = ra.interacting(rb).count()
            if not touching:
                continue
            overlap_area = float((ra & rb).area())
            same_level.append({
                "layers": [list(ka), list(kb)], "names": [roles[ka]["name"], roles[kb]["name"]],
                "same_role": roles[ka]["role"] == roles[kb]["role"],
                "shapes_touching": touching,
                "overlap_area_dbu2": overlap_area,
                # Zero overlap area means they meet edge to edge rather than
                # stacking, which is what a single conductor split across two
                # named layers looks like.
                "abut_without_overlap": overlap_area == 0.0,
            })

    return {"tier": 2, "available": True, "availability": "GDS + LYP",
            "basis": "measured plan-view overlap and enclosure; overlap is not connection",
            "connectors": out, "warnings": warnings,
            "conductor_adjacency": same_level,
            "conductor_layers": [{"layer": list(k), "name": roles[k]["name"],
                                  "role": roles[k]["role"]} for k in conductors]}


# ---------------------------------------------------------------------------
# Tier 3a: propose a stack from the tier-2 evidence. Requires confirmation.
# ---------------------------------------------------------------------------

def propose_stack(landings: dict[str, Any]) -> dict[str, Any]:
    """Derive a *candidate* connection stack from measured landings.

    Evidence used, in order of strength:

    1. Name agreement, e.g. ``VIA0`` against ``M0``/``M1``, ``N-VIAG`` against
       ``NPOLY``. Naming is independent of the geometry, so when a name and the
       measured overlap agree, two unrelated sources of evidence agree.
    2. A conductor enclosing every shape of the connector (enclosure_ratio 1.0).
    3. A conductor every connector shape touches (interaction_ratio 1.0).

    Geometry alone is deliberately *not* trusted to produce a high confidence,
    because it was measured not to be sufficient on this technology. Three
    discriminators were tried and each fails:

    * interaction - in a stacked cell nearly every pair interacts;
    * enclosure - a cell-spanning rail encloses vias it does not connect to,
      while a contact wider than its diffusion is enclosed by nothing;
    * ubiquity (demoting layers that are candidates for every connector) - the
      genuine local-interconnect layer is also a candidate for every connector,
      so demoting ubiquitous layers demotes the right answer.

    The concrete failure: in a backside-power technology, backside metal underlies
    the whole cell and encloses vias that reach only frontside metal. "Exactly two
    layers enclose this via" therefore picks the wrong pair, so enclosure evidence
    alone is capped at medium confidence and the ambiguity is reported.

    The result is a proposal carrying a confidence and the unresolved alternatives
    per connector. It is never applied automatically - `analyze_connectivity`
    requires it to be passed back explicitly.
    """
    proposals: list[dict[str, Any]] = []
    for conn in landings.get("connectors", []):
        name = conn["name"].upper()
        cands = [o for o in conn["overlaps"] if o["interaction_ratio"] == 1.0]
        enclosing = [o for o in cands if o["enclosure_ratio"] == 1.0]

        # Name evidence: a metal index in the connector name points at M<k> and
        # M<k+1>; a trailing G points at poly (gate); DIFFCON points at diffusion.
        name_hits: set[str] = set()
        m = re.search(r"VIA(\d+)", name)
        if m:
            k = int(m.group(1))
            name_hits |= {f"M{k}", f"M{k + 1}"}
        if re.search(r"VIAG$", name):
            name_hits |= {"NPOLY", "PPOLY", "POLY"}
        if "DIFFCON" in name or "CONTACT" in name:
            name_hits |= {"NDIFF", "PDIFF", "DIFF"}
        for o in conn["overlaps"]:
            o["name_agreement"] = o["name"].upper() in name_hits

        named = [o for o in cands if o.get("name_agreement")]
        # Prefer a candidate matching the connector's own N/P prefix, so N-VIAG
        # resolves to NPOLY rather than PPOLY.
        prefix = name[0] if name[:1] in ("N", "P") else None
        if prefix and len(named) > 1:
            named = ([o for o in named if o["name"].upper().startswith(prefix)]
                     + [o for o in named if not o["name"].upper().startswith(prefix)])

        if len(named) >= 2:
            chosen, confidence, why = named[:2], "high", \
                ("the connector's name names both layers and every shape touches both, so naming "
                 "and geometry agree independently")
        elif named and enclosing:
            pick = named[:1] + [o for o in enclosing if o["name"] != named[0]["name"]][:1]
            chosen, confidence, why = pick, "medium", \
                ("one layer is named by the connector; the other is the layer enclosing every "
                 "shape, which geometry alone cannot confirm")
            if len(pick) < 2:
                chosen, confidence, why = named[:1], "none", \
                    "only the layer named by the connector could be identified"
        elif len(enclosing) == 2:
            chosen, confidence, why = enclosing, "medium", \
                ("exactly two conductor layers enclose every shape, but nothing in the .gds or "
                 ".lyp confirms these are the two levels the connector bridges")
        elif len(enclosing) > 2:
            chosen, confidence, why = enclosing[:2], "low", \
                (f"{len(enclosing)} conductor layers enclose every shape, so the true pair cannot "
                 "be distinguished by geometry alone")
        elif len(cands) >= 2:
            chosen, confidence, why = cands[:2], "low", \
                "no layer encloses every shape; ranked by overlap only, which is weak evidence"
        else:
            chosen, confidence, why = [], "none", \
                (f"only {len(cands)} conductor layer is touched by every shape, which is not enough "
                 "to bridge two levels")

        # Any other layer with equally strong evidence is an unresolved
        # alternative, and saying so is more useful than a confident guess.
        chosen_names = {o["name"] for o in chosen}
        alternatives = [o["name"] for o in cands if o["name"] not in chosen_names
                        and (o["enclosure_ratio"] == 1.0 or not enclosing)]
        proposals.append({
            "connector_layer": [conn["layer"], conn["datatype"]],
            "connector_name": conn["name"], "connector_role": conn["role"],
            "connector_shape_count": conn["shape_count"],
            "connects": [{"layer": o["layer"], "name": o["name"]} for o in chosen],
            "confidence": confidence, "reason": why,
            "unresolved_alternatives": alternatives,
            "evidence": [{k: o[k] for k in ("name", "shapes_interacting", "shapes_enclosed",
                                            "interaction_ratio", "enclosure_ratio",
                                            "name_agreement")}
                         for o in conn["overlaps"][:6]],
        })

    # Same-level candidates: two conductor layers of the same role that meet edge
    # to edge are most likely one conductor drawn across two names. They connect
    # directly where they touch, with no connector involved.
    same_level = [
        {"layers": a["layers"], "names": a["names"],
         "confidence": "medium" if a["same_role"] and a["abut_without_overlap"] else "low",
         "reason": ("both layers have the same role and their shapes meet edge to edge without "
                    "overlapping, which is how one conductor split across two named layers appears"
                    if a["same_role"] and a["abut_without_overlap"] else
                    "the layers touch, but not in the edge-to-edge pattern of a single conductor"),
         "shapes_touching": a["shapes_touching"]}
        for a in landings.get("conductor_adjacency", [])
        if a["same_role"] and a["abut_without_overlap"]
    ]

    usable = [p for p in proposals if len(p["connects"]) == 2]
    return {
        "proposals": proposals,
        "same_level": same_level,
        "usable_count": len(usable),
        "requires_confirmation": True,
        "availability": "inferred from GDS + LYP; the true stack requires PDK/technology data",
        "caveat": ("GDSII records no layer elevations, so this stack is inferred from measured "
                   "overlap and layer naming. Confirm it against the technology's process stack "
                   "before relying on the net graph built from it."),
        "confidence_summary": {c: sum(1 for p in proposals if p["confidence"] == c)
                               for c in ("high", "medium", "low", "none")},
    }


def stack_from_sidecar(sidecar_meta: dict[str, Any],
                       layermap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derive the connection stack from semantic-sidecar layer names.

    When a sidecar is present it names its via layers after their endpoints -
    ``VIA_M0_M1``, ``VIA_M0_PMOSGate``, ``VIA_Inteconnect_BSPowerRail``. That
    *states* which levels each via joins, rather than leaving it to be guessed
    from geometry, so it is a far stronger source than the .lyp.

    It corrected three of the guesses a .lyp-only reading produced on this
    technology, and one of the corrections mattered a lot: the layers a .lyp calls
    ``NDIFFCON``/``PDIFFCON`` are named ``NMOSInterconnect``/``PMOSInterconnect``
    here. They are local interconnect *conductors*, not contacts. Treating them as
    contacts bridging diffusion to M0 shorted an entire cell into one net.

    Still a naming convention, so it is labelled as sidecar-derived rather than as
    technology data - but the name asserts both endpoints explicitly, which is a
    different kind of evidence from measured overlap.
    """
    import difflib

    rows = sidecar_meta.get("layers") or []
    # A sidecar reuses one name across datatypes, so "M0" covers both 200/0 and
    # the pin copy at 200/2. Including the copies does not change the topology but
    # doubles every shape count and invents floating nets out of the -EXTENDED
    # variants, so drop the keys the .lyp marks as derived.
    lyp_roles = layer_roles(layermap)
    derived_keys = {k for k, v in lyp_roles.items() if v["derived"]}

    # name -> the (layer, datatype) keys carrying it
    by_name: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        name, layer, datatype = row.get("name"), row.get("layer"), row.get("datatype")
        if not name or layer is None or str(name).startswith("layer_"):
            continue
        key = (int(layer), int(datatype or 0))
        if key in derived_keys:
            continue
        by_name.setdefault(str(name), []).append(key)
    by_name = {n: keys for n, keys in by_name.items() if keys}

    via_names = {n for n in by_name if re.match(r"VIA[_-]", n, re.I)}
    conductors = {n: keys for n, keys in by_name.items()
                  if n not in via_names and not is_derived(n)
                  and n.lower() not in ("boundary", "lib")
                  and not re.search(r"TRACKS?$|TRACKS?_", n, re.I)}
    if not via_names:
        return {"proposals": [], "same_level": [], "usable_count": 0,
                "requires_confirmation": True,
                "availability": "sidecar present but no via layer names follow the VIA_<a>_<b> form",
                "problems": [], "confidence_summary": {}}

    lower = {n.lower(): n for n in conductors}

    def resolve(token: str) -> list[str]:
        """Map one endpoint token to conductor layer name(s)."""
        t = token.strip().lower()
        if t in lower:
            return [lower[t]]
        # Substring both ways catches "Gate" -> "NMOSGate"/"PMOSGate".
        subs = [n for n in conductors if t in n.lower() or n.lower() in t]
        if subs:
            return subs
        # Fuzzy, for the misspelling in this data ("Inteconnect").
        close = difflib.get_close_matches(t, list(lower), n=4, cutoff=0.75)
        if close:
            return [lower[c] for c in close]
        # Last resort: a shared long prefix, which links "Inteconnect" to the
        # "...Interconnect" layers when the ratio falls just below the cutoff.
        pref = [n for n in conductors
                if difflib.SequenceMatcher(None, t, n.lower()).find_longest_match(
                    0, len(t), 0, len(n)).size >= max(6, len(t) - 3)]
        return pref

    proposals, problems = [], []
    for via in sorted(via_names):
        parts = re.split(r"[_-]", via)[1:]          # drop the leading "VIA"
        if len(parts) < 2:
            problems.append(f"via layer {via!r} does not name two endpoints")
            continue
        # Endpoints are the trailing tokens; a name has exactly two of them.
        halves = [parts[0], "_".join(parts[1:])] if len(parts) > 2 else parts
        resolved: list[str] = []
        for half in halves:
            hits = resolve(half)
            if not hits:
                problems.append(f"via layer {via!r}: endpoint {half!r} matches no conductor layer")
            resolved.extend(hits)
        resolved = list(dict.fromkeys(resolved))
        if len(resolved) < 2:
            continue
        connects = [{"layer": list(k), "name": n}
                    for n in resolved for k in conductors[n]]
        for key in by_name[via]:
            proposals.append({
                "connector_layer": list(key), "connector_name": via,
                "connector_role": "via", "connects": connects,
                "confidence": "sidecar-named",
                "reason": f"the sidecar names this layer {via!r}, stating its two endpoints",
                "unresolved_alternatives": [], "evidence": [],
            })

    return {
        "proposals": proposals, "same_level": [], "usable_count": len(proposals),
        "requires_confirmation": True,
        "availability": ("derived from semantic-sidecar via layer names, which state each via's "
                         "endpoints; a naming convention, not verified technology data"),
        "problems": problems,
        "confidence_summary": {"sidecar-named": len(proposals)},
    }


# The connection stack for the bundled technology. Shipped as a default for the
# same reason the .lyp is: without it the net graph never builds, and the common
# case is a user uploading a .gds on its own. It is transcribed from the sidecar's
# via layer names and cross-checked against the .lyp numbering - not verified
# against a PDK, which every consumer states.
BUNDLED_STACK = Path(__file__).resolve().parent.parent / "data" / "samples" / "Titan_stack.json"


def default_stack(layermap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The bundled connection stack, if it loads cleanly against this layer map.

    Returns None when the layer names do not resolve, which is what happens if the
    uploaded layout uses a different technology - better to build no net graph than
    one from another technology's stack.
    """
    if not layermap or not BUNDLED_STACK.exists():
        return None
    try:
        stack = load_stack(BUNDLED_STACK, layermap)
    except (ValueError, KeyError, OSError):
        return None
    if not stack["usable_count"] or stack["problems"]:
        return None
    stack["is_bundled_default"] = True
    return stack


def load_stack(path: str | Path, layermap: dict[str, Any] | None) -> dict[str, Any]:
    """Load a connection stack supplied by someone who knows the technology.

    This is the escape hatch from the tier-3 limitation: the vertical stack is
    the one piece of information the .gds and .lyp cannot provide, and it is
    small enough to state by hand. Expected JSON::

        {"technology": "...",
         "connections": {"VIA0": ["M0", "M1"], "N-VIAG": ["NPOLY", "M0"]},
         "same_level": [["NPOLY", "PPOLY"]]}

    ``same_level`` lists pairs of layers that are one physical conductor level
    drawn under two names - a gate crossing both wells is drawn as NPOLY over the
    n-well and PPOLY over the p-well, and the two abut to form one continuous
    conductor. They connect directly where they touch, with no via involved.

    Layer names are resolved against the LYP, so a typo is reported rather than
    silently dropping a connection.
    """
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    role_overrides = data.get("roles") or {}
    if not isinstance(role_overrides, dict):
        raise ValueError("stack file: 'roles' must be an object mapping layer name -> role")
    roles = layer_roles(layermap, role_overrides)
    by_name = {v["name"].upper(): k for k, v in roles.items()}
    connections = data.get("connections") or {}
    if not isinstance(connections, dict):
        raise ValueError("stack file: 'connections' must be an object mapping connector -> [a, b]")

    proposals, problems = [], []
    for connector, targets in connections.items():
        ck = by_name.get(connector.upper())
        if ck is None:
            problems.append(f"connector layer {connector!r} is not in the layer map")
            continue
        # At least two, not exactly two: a via may be able to land on either of
        # several layers (DVB reaches backside metal from either interconnect
        # layer), and it is only connected to the ones it actually touches.
        if not isinstance(targets, list) or len(targets) < 2:
            problems.append(f"{connector!r} must list at least two conductor layers")
            continue
        resolved = []
        for target in targets:
            tk = by_name.get(str(target).upper())
            if tk is None:
                problems.append(f"conductor layer {target!r} (for {connector!r}) is not in the layer map")
            else:
                resolved.append({"layer": list(tk), "name": roles[tk]["name"]})
        if len(resolved) < 2:
            continue
        proposals.append({
            "connector_layer": list(ck), "connector_name": roles[ck]["name"],
            "connector_role": roles[ck]["role"], "connects": resolved,
            "confidence": "supplied", "reason": "stated in the supplied stack file",
            "unresolved_alternatives": [], "evidence": [],
        })
    same_level = []
    for pair in data.get("same_level") or []:
        if not isinstance(pair, list) or len(pair) != 2:
            problems.append(f"same_level entry {pair!r} must list exactly two layers")
            continue
        keys = [by_name.get(str(p).upper()) for p in pair]
        if None in keys:
            problems.append(f"same_level entry {pair!r} names a layer that is not in the layer map")
            continue
        same_level.append({"layers": [list(k) for k in keys],
                           "names": [roles[k]["name"] for k in keys],
                           "confidence": "supplied",
                           "reason": "stated in the supplied stack file"})

    unknown_roles = [n for n in role_overrides if n.upper() not in by_name]
    problems.extend(f"role override for {n!r} names a layer that is not in the layer map"
                    for n in unknown_roles)
    return {
        "proposals": proposals, "same_level": same_level, "usable_count": len(proposals),
        "requires_confirmation": False,
        "availability": "supplied by the user from technology data",
        "source": str(path), "technology": data.get("technology"),
        "role_overrides": role_overrides,
        "problems": problems,
        "confidence_summary": {"supplied": len(proposals)},
    }


def compare_stack_to_evidence(stack: dict[str, Any],
                              landings: dict[str, Any]) -> list[dict[str, Any]]:
    """Check a supplied stack against the measured overlaps.

    A supplied stack is authoritative, but if it claims a connector joins a layer
    that the connector never touches, either the stack or the layout is wrong -
    and that is worth surfacing rather than quietly extracting a wrong net graph.
    """
    measured = {c["name"]: {o["name"]: o for o in c["overlaps"]}
                for c in landings.get("connectors", [])}
    counts = {c["name"]: c["shape_count"] for c in landings.get("connectors", [])}
    issues = []
    for rule in stack.get("proposals", []):
        name = rule["connector_name"]
        if name not in measured:
            issues.append({"connector": name, "severity": "info",
                           "issue": f"the stack defines {name}, which this layout does not use"})
            continue
        total = counts.get(name)
        targets = {t["name"] for t in rule["connects"]}
        # A rule listing more than two layers is offering alternatives - DVB reaches
        # backside metal from either interconnect layer - so partial coverage of any
        # one of them is expected, not a disagreement. What matters there is whether
        # each shape reaches at least two of the listed layers.
        alternatives = len(targets) > 2
        reached = 0
        for target in sorted(targets):
            overlap = measured[name].get(target)
            if overlap is not None:
                reached += 1
            if overlap is None and not alternatives:
                issues.append({
                    "connector": name, "conductor": target, "severity": "high",
                    "issue": (f"the stack says {name} connects to {target}, but no {name} "
                              f"shape overlaps {target} anywhere in this layout")})
            elif overlap is not None and overlap["interaction_ratio"] < 1.0 and not alternatives:
                issues.append({
                    "connector": name, "conductor": target, "severity": "medium",
                    "issue": (f"only {overlap['shapes_interacting']} of {total} {name} shape(s) "
                              f"overlap {target} ({overlap['interaction_ratio']:.0%}); the "
                              f"rest do not reach {target} at all")})
        if alternatives and reached < 2:
            issues.append({
                "connector": name, "severity": "high",
                "issue": (f"the stack lists {len(targets)} candidate layers for {name}, but its "
                          f"shapes overlap only {reached} of them, so it cannot bridge two levels")})
    return issues


# ---------------------------------------------------------------------------
# Tier 3b: the net graph, given a stack.
# ---------------------------------------------------------------------------

def extract_nets(gds_path: str | Path, layermap: dict[str, Any] | None,
                 stack: dict[str, Any], max_shapes: int = 200_000,
                 role_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the physical net graph using a supplied connection stack."""
    import klayout.db as db
    roles = layer_roles(layermap, role_overrides)
    # Two or more, not exactly two: a via named for one endpoint pair can still
    # resolve to several candidate layers (VIA_Inteconnect_BSPowerRail lands on
    # either interconnect layer), and KLayout connects it only to what it touches.
    rules = [p for p in stack.get("proposals", stack.get("rules", []))
             if len(p.get("connects", [])) >= 2]
    if not rules:
        return {"tier": 3, "available": False,
                "reason": "the supplied stack contains no connector bridging two conductor layers"}

    same_level = stack.get("same_level") or []
    connector_keys = [tuple(r["connector_layer"]) for r in rules]
    conductor_keys = sorted({tuple(c["layer"]) for r in rules for c in r["connects"]}
                            | {tuple(k) for s in same_level for k in s["layers"]})
    layout, top, regions = _load_regions(gds_path, connector_keys + conductor_keys)
    dbu = float(layout.dbu)

    total = sum(r.merged().count() for r in regions.values())
    if total > max_shapes:
        return {"tier": 3, "available": False,
                "reason": f"{total} conducting shapes exceeds the {max_shapes} analysis limit"}

    l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(layout, top, []))
    made: dict[tuple[int, int], Any] = {}
    for key in conductor_keys + connector_keys:
        if regions.get(key) is None:
            continue
        li = layout.find_layer(key[0], key[1])
        if li is None:
            continue
        safe = re.sub(r"\W", "_", roles.get(key, {}).get("name", f"L{key[0]}"))
        made[key] = l2n.make_polygon_layer(li, f"{safe}_{key[0]}_{key[1]}")
        l2n.connect(made[key])                       # intra-layer: touching = connected
    for rule in rules:
        ck = tuple(rule["connector_layer"])
        if ck not in made:
            continue
        for target in rule["connects"]:
            tk = tuple(target["layer"])
            if tk in made:
                l2n.connect(made[ck], made[tk])      # connector bridges two levels
    for pair in same_level:
        ka, kb = tuple(pair["layers"][0]), tuple(pair["layers"][1])
        if ka in made and kb in made:
            l2n.connect(made[ka], made[kb])          # one level under two names
    l2n.extract_netlist()

    connector_names = {roles.get(k, {}).get("name") for k in connector_keys}
    nets: list[dict[str, Any]] = []
    for circuit in l2n.netlist().each_circuit():
        for net in circuit.each_net():
            per_layer: dict[str, int] = {}
            area = 0.0
            for key, lay in made.items():
                shapes = l2n.shapes_of_net(net, lay, True)
                count = shapes.count() if shapes else 0
                if count:
                    nm = roles.get(key, {}).get("name", f"layer_{key[0]}")
                    per_layer[nm] = per_layer.get(nm, 0) + count
                    area += float(shapes.area()) * dbu * dbu
            if not per_layer:
                continue
            names = sorted(per_layer)
            nets.append({
                "net": net.expanded_name(), "circuit": circuit.name,
                "shape_count": sum(per_layer.values()),
                "layers": names, "layer_count": len(names),
                "shapes_per_layer": per_layer, "area_um2": round(area, 9),
                "spans_multiple_layers": len(names) > 1,
                "uses_connector": bool(set(names) & connector_names),
            })
    nets.sort(key=lambda n: (-n["shape_count"], n["net"]))

    floating = [n for n in nets if not n["uses_connector"]]

    # A sanity check on the *stack*, not on the design. Any real cell has at
    # least two nets (power and ground), so a layout collapsing into a single net
    # that spans the whole stack means a connection rule is joining levels that
    # are not actually joined. Saying so is more useful than reporting "1 net".
    stack_warnings = []
    if len(nets) == 1 and nets[0]["layer_count"] >= 3:
        stack_warnings.append(
            f"Every conducting shape in this layout resolves to a single net spanning "
            f"{nets[0]['layer_count']} layers. Any real cell has at least two nets (power and "
            f"ground), so the connection stack in use is almost certainly joining levels that are "
            f"not connected in the real process. Treat this net count as unreliable and check the "
            f"stack against the PDK.")

    return {
        "tier": 3, "available": True,
        "stack_plausibility_warnings": stack_warnings,
        "availability": "requires a layer connection stack (supplied)",
        "stack_confidence": stack.get("confidence_summary"),
        "stack_requires_confirmation": stack.get("requires_confirmation", True),
        "summary": {
            "net_count": len(nets),
            "largest_net_shape_count": nets[0]["shape_count"] if nets else 0,
            "multi_layer_net_count": sum(1 for n in nets if n["spans_multiple_layers"]),
            "single_layer_net_count": sum(1 for n in nets if not n["spans_multiple_layers"]),
            "floating_net_count": len(floating),
            "conducting_shape_count": total,
        },
        "nets": nets,
        "floating_nets": floating,
        "basis": ("physical adjacency under the supplied stack: shapes touching within a layer are "
                  "connected, and a connector joins the two conductor layers the stack assigns it"),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_connectivity(gds_path: str | Path, layermap: dict[str, Any] | None,
                         stack: dict[str, Any] | None = None,
                         accept_proposed_stack: bool = False) -> dict[str, Any]:
    """Run the connectivity analysis.

    Tiers 1 and 2 always run - they need no stack and make no assumptions. Tier 3
    runs only when `stack` is supplied, or when `accept_proposed_stack` is set,
    which is an explicit acknowledgement that an inferred stack is in use. The
    returned object always records which of those happened.

    A supplied stack may also carry role corrections, which apply to tiers 1 and 2
    as well - if the stack says NDIFFCON is interconnect rather than a contact, the
    landing measurements must not go on calling it a connector.
    """
    role_overrides = (stack or {}).get("role_overrides") or None
    tier1 = intra_layer_connectivity(gds_path, layermap, role_overrides)
    tier2 = measure_connector_landings(gds_path, layermap, role_overrides)
    proposed = propose_stack(tier2) if tier2.get("available") else None

    warnings = list(tier2.get("warnings", []))
    result: dict[str, Any] = {
        "intra_layer": tier1,
        "landings": tier2,
        "proposed_stack": proposed,
        "nets": None,
        "warnings": warnings,
        "limitations": {
            "vertical_stack": ("GDSII records no layer elevations. Which via joins which two "
                               "conductor layers is a process fact and is present in neither the "
                               ".gds nor the .lyp. Requires PDK/technology data."),
            "physical_shorts": ("A short is defined relative to an intended netlist, so it is not "
                                "determinable here. Requires netlist/design intent."),
            "physical_opens": ("An open is likewise defined relative to an intended net. Connector "
                               "shapes overlapping no conductor are reported as the observable "
                               "proxy. Requires netlist/design intent to confirm."),
            "electrical_intent": "Requires a netlist, schematic or LVS reference.",
        },
    }

    use = stack or (proposed if accept_proposed_stack else None)
    if use:
        result["nets"] = extract_nets(gds_path, layermap, use, role_overrides=role_overrides)
        result["stack_source"] = ("supplied" if stack else
                                  "inferred proposal, explicitly accepted")
        if stack and stack.get("confidence_summary", {}).get("sidecar-named"):
            result["stack_source"] = "derived from semantic-sidecar via layer names"
        elif stack and stack.get("is_bundled_default"):
            result["stack_source"] = "the bundled technology stack (not PDK-verified)"
        result["stack_used"] = use
        warnings.extend(result["nets"].get("stack_plausibility_warnings", []))
        if stack and tier2.get("available"):
            issues = compare_stack_to_evidence(stack, tier2)
            result["stack_vs_evidence"] = issues
            for issue in issues:
                if issue["severity"] in ("high", "medium"):
                    warnings.append(f"Supplied stack disagrees with the layout: {issue['issue']}.")
        if not stack:
            unreliable = [p["connector_name"] for p in proposed["proposals"]
                          if p["confidence"] in ("low", "medium") and len(p["connects"]) == 2]
            warnings.append(
                "The net graph was built from an inferred connection stack, not from technology "
                "data. Nets are therefore provisional.")
            if unreliable:
                warnings.append(
                    f"{len(unreliable)} connector layer(s) could not be resolved confidently "
                    f"({', '.join(unreliable[:6])}), so the nets they take part in may be merged or "
                    "split incorrectly. Supply a stack file to make this exact.")
    else:
        result["stack_source"] = None
        if proposed and proposed["usable_count"]:
            warnings.append(
                f"A candidate connection stack was inferred for {proposed['usable_count']} "
                "connector layer(s) but was not applied. Confirm it to build the net graph.")
    return result
