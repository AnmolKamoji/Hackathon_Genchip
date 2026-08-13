from __future__ import annotations

import re
from typing import Any

VIA_UNAVAILABLE = (
    "Via counts are unavailable for this file. A raw GDSII stream does not label which "
    "geometry is a via, so the analyzer reports this as unknown rather than 0. Two things fix "
    "it: the KLayout layer map (.lyp), whose via layer names make the count derivable, or the "
    "semantic JSON sidecar, which flags vias explicitly."
)


def _fmt(value: Any, unit: str = "", digits: int = 3) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.{digits}f}{unit}"
    return f"{value}{unit}"


def _layer_names(layers: list[dict[str, Any]]) -> list[str]:
    """Every name a layer can be referred to, in both vocabularies.

    A design can carry two naming schemes at once: the sidecar's functional name
    (`BSPowerRail`) and the .lyp's mask name (`BM0`). A question may use either,
    so both are searchable.
    """
    seen, out = set(), []
    for x in layers:
        for key in ("name", "technology_name"):
            n = str(x.get(key) or "")
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _mentioned_layer(question: str, layers: list[dict[str, Any]]) -> str | None:
    """Find a layer name from the metadata that appears in the question.

    Matching against the real layer list is far more reliable than guessing with
    a regex, and longest-first prevents `M0` from shadowing `M0_pin`.
    """
    q = question.lower()
    for name in sorted(_layer_names(layers), key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", q):
            return name
    return None


def _rows_for(name: str, layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows matching `name` under either naming scheme."""
    low = name.lower()
    return [x for x in layers
            if str(x.get("name") or "").lower() == low
            or str(x.get("technology_name") or "").lower() == low]


_NOT_A_LAYER = {
    "any", "vias", "via", "layers", "layer", "cells", "cell", "text", "texts", "label", "labels",
    "polygons", "polygon", "shapes", "shape", "drc", "this", "it", "the", "a", "an", "there",
    "design", "layout", "gds", "total", "all", "each", "top", "metal",
}


def _looks_like_layer_name(token: str) -> bool:
    """Does this token plausibly name a layer, as opposed to being an English word?

    Real layer names in these technologies look like `M1`, `M0`, `VIA_M0_M1`,
    `BSPowerRail` or `NmosNanoSheet` - they carry a digit, an underscore, or
    internal capitalisation. Plain lowercase words like "in", "good" or "any" do
    not, and must never be reported as a missing layer.
    """
    if token.lower() in _NOT_A_LAYER:
        return False
    if len(token) < 2:
        return False
    has_digit = any(c.isdigit() for c in token)
    has_underscore = "_" in token
    internal_caps = any(c.isupper() for c in token[1:])
    all_caps = token.isupper()
    return has_digit or has_underscore or internal_caps or all_caps


def _unknown_layer_token(question: str, layers: list[dict[str, Any]]) -> str | None:
    """Detect a layer-like name in the question that is absent from the metadata.

    Without this, "how many polygons are on M0?" against a file that has no M0
    silently answers with the whole-design total, which reads as if M0 held every
    polygon in the layout.
    """
    m = re.search(r"\b(?:on|in|for|of)\s+(?:layer\s+|the\s+)*([A-Za-z][A-Za-z0-9_]*)\b", question)
    if not m:
        return None
    token = m.group(1)
    if not _looks_like_layer_name(token):
        return None
    if any(token.lower() == n.lower() for n in _layer_names(layers)):
        return None
    return token


def _group_for(name: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    """The unioned geometry group for a layer name, if the analyzer produced one."""
    for g in metadata.get("layer_groups", []):
        if str(g.get("label", "")).lower() == name.lower():
            return g
    return None


def _absent(token: str, layers: list[dict[str, Any]]) -> str:
    names = _layer_names(layers)
    listing = ", ".join(f"`{n}`" for n in names) if names else "none"
    return (f"There is no layer named `{token}` in this design, so no count is available for it. "
            f"Layers present: {listing}.")


def _summary(metadata: dict[str, Any]) -> str:
    d, l = metadata["design"], metadata["layout"]
    src = {
        "gds": "raw GDSII geometry only",
        "sidecar": "semantic JSON sidecar only",
        "fused": "GDSII geometry fused with the semantic JSON sidecar",
    }.get(metadata.get("metadata_source", ""), metadata.get("metadata_source", "unknown"))

    lines = [
        f"**{metadata['source']['file']}** — analyzed from {src}.",
        "",
        f"- Top cell: `{d['top_cell']}`",
        f"- Cells: {d['cell_count']}",
        f"- Layer entries in use: {d['layer_count']}",
        f"- Polygons: {d['polygon_count']}",
        f"- Text labels: {d['text_count']}",
        f"- Vias: {_fmt(d.get('via_count'))}",
        f"- Bounding box: {_fmt(l.get('width_um'))} × {_fmt(l.get('height_um'))} µm"
        f" ({_fmt(l.get('bbox_area_um2'))} µm²)",
    ]
    dense = [x for x in metadata.get("layers", []) if x.get("density_percent") is not None]
    if dense:
        top = max(dense, key=lambda z: z["density_percent"])
        lines.append(f"- Densest layer: `{top['name']}` at {top['density_percent']:.2f}% of the bounding box")
    busiest = [x for x in metadata.get("layers", []) if x.get("polygon_count")]
    if busiest:
        top = max(busiest, key=lambda z: z["polygon_count"])
        lines.append(f"- Most populated layer: `{top['name']}` with {top['polygon_count']} polygons")
    if d.get("via_count") is None:
        lines += ["", "_Via counts require the semantic JSON sidecar._"]
    cons = metadata.get("consistency")
    if cons and not cons.get("agrees"):
        lines += ["", "⚠️ The GDS and the sidecar disagree; see the consistency block in the metadata."]
    for w in metadata.get("warnings", []):
        lines += ["", f"⚠️ {w}"]
    return "\n".join(lines)


# Metrics this tool does not compute. Each entry is (pattern, what was asked for,
# what would be needed). Checked before the broad branches further down, which
# match on words like "area" and "size" and would otherwise answer a different
# question with a plausible-looking number.
UNCOMPUTED_METRICS: list[tuple[str, str, str]] = [
    (r"\bsymmetr\w*|\bmirror\w*",
     "layout symmetry",
     "symmetry detection is not implemented. The geometry needed for it is present, so this is a "
     "missing feature rather than a missing input"),
    (r"\brepeat\w*(?!.*\barray)|\bpattern\w*|\bregular structure",
     "repeated structures and layout patterns",
     "pattern and repetition detection is not implemented. Array *instances* are counted "
     "separately (instance placements versus instance records); what is missing is detection of "
     "repetition in raw geometry"),
    (r"\banomal\w*|\boutlier\w*|\bunusual\b|\bsuspicious\b",
     "geometry anomaly and outlier detection",
     "statistical anomaly detection is not implemented. Per-layer measurements are available for "
     "comparison, but no outlier scoring is computed"),
    (r"\bspatial distribution\b|\bhow are .*distributed\b|\bcluster\w*|\bquadrant\w*|"
     r"\bwindow\w* density|\bdensity map\b",
     "spatial distribution and windowed density",
     "only whole-cell density per layer is computed, not a windowed density map or a spatial "
     "distribution analysis"),
    (r"\bhealth score\b|\bhealth\b.{0,20}\bscor|\brisk score\b|\brisk\b.{0,20}\bscor|"
     r"\bscore\b.{0,15}\bout of\b|\bgrade this\b",
     "a layout health or risk score",
     "no composite score is produced, deliberately. A single number implies a pass/fail threshold, "
     "and thresholds come from a rule deck that was not supplied. Individual measurements are "
     "reported instead"),
]


def _uncomputed(what: str, needs: str, metadata: dict[str, Any]) -> str:
    """Say plainly that a metric is not computed, and what would be needed."""
    return (f"{what.capitalize()} is not available: {needs}. "
            "Nothing here should be read as evidence that the layout is free of it — it simply was "
            "not measured.")


CONNECTIVITY_TRIGGER = re.compile(
    # "disconnected" needs the optional dis- prefix: \bconnect\w* does not match it,
    # because there is no word boundary in the middle of the word.
    r"\b(?:dis)?connect\w*|\bnet\b|\bnets\b|\bnetlist\b|\bshort\w*|\bopen\w*|\bfloat\w*|"
    r"\bisolat\w*|\bdangl\w*|\bstack\b|\breach\w*|\btouch\w*|\bcomponent\w*|\bisland\w*|"
    r"\bconductor\w*|\bfragment\w*|\babut\w*|\bgraph\b|\binteract\w*|\bintersect\w*|"
    # "overlap" only when it is about layers relating to each other; a question
    # about overlapping datatypes on one layer belongs to the polygon branch.
    r"\blayers?\s+overlap|\boverlap\w*\s+(?:with|between|each)")

# A via question only belongs to connectivity when it asks what the via *reaches*.
# "How many vias are there?" is a count question and must keep its existing
# answer, which reports the sidecar figure or says the count is unavailable.
VIA_CONNECTIVITY_TRIGGER = re.compile(
    r"\b(?:via|vias|contact|contacts)\b(?:(?!\?).)*\b(?:land|lands|landing|overlap\w*|enclos\w*|"
    r"sit|sits|reach\w*|connect\w*|touch\w*|go|goes|attach\w*)"
    r"|\b(?:land|lands|landing|overlap\w*|enclos\w*|reach\w*|connect\w*|touch\w*)"
    r"(?:(?!\?).)*\b(?:via|vias|contact|contacts)\b")


def _connectivity_answer(conn: dict[str, Any] | None, q: str) -> str | None:
    """Answer connectivity questions, refusing to overstate what is derivable.

    The hard boundary enforced here: a *short* and an *open* are defined relative
    to an intended netlist, and a net graph at all requires the vertical stack,
    which GDSII does not record. Those questions get the limitation, never a guess.
    """
    if conn is None:
        return ("No connectivity analysis is available for this file. Physical connectivity needs "
                "the layer map (.lyp) to tell which layers are vias, contacts and metals.")
    if conn.get("error"):
        return f"Connectivity analysis failed for this file: {conn['error']}"

    # Shorts and opens first: these are the claims that must never be guessed.
    if re.search(r"\bshort\w*", q):
        return ("Physical shorts are not determinable here. A short means two nets that should be "
                "separate are joined, which is defined relative to an intended netlist — and no "
                "netlist, schematic or LVS reference was supplied. What is measurable is physical "
                "adjacency: which shapes touch, and which conductor layers each via overlaps.")
    if re.search(r"\bopen\w*\b", q) and not re.search(r"\bopen (the|this|a) ", q):
        land = conn.get("landings") or {}
        stranded = sum(c.get("shapes_overlapping_no_conductor", 0)
                       for c in land.get("connectors", []))
        lead = ("Physical opens are not determinable here. An open means a net that should be "
                "continuous is broken, which is defined relative to design intent, and no netlist "
                "was supplied. The observable proxy is a via or contact that overlaps no conductor "
                "at all: ")
        return lead + (f"{stranded} such shape(s) were found." if stranded else
                       "none were found in this layout.")

    t1 = conn["intra_layer"]
    # Intra-layer components: exact, and the safest thing to lead with. A question
    # naming nets is about the net graph instead, so it falls through to that.
    asks_about_nets = re.search(r"\bnet\b|\bnets\b|\bnetlist\b", q)
    if not asks_about_nets and (
            re.search(r"\bcomponent\w*|\bisland\w*|\bfragment\w*|\bconductor\w*", q)
            or re.search(r"\btouch\w*|\babut\w*", q)):
        abutting = [r for r in t1["layers"] if r["component_count"] < r["shape_count"]]
        head = (f"Within layers, {t1['total_shapes']} conducting shapes form "
                f"{t1['total_components']} separate physical conductors "
                f"(shapes on one layer that touch or overlap are one conductor). "
                f"This is measured from the geometry alone and needs no process-stack data.")
        if not abutting:
            return head + " No layer has shapes that touch, so every shape is its own conductor."
        detail = "; ".join(f"{r['name']} — {r['shape_count']} shapes forming "
                           f"{r['component_count']} conductor(s)" for r in abutting)
        return f"{head} Layers where shapes abut: {detail}."

    nets = conn.get("nets")
    if re.search(r"\bnet\b|\bnets\b|\bnetlist\b|\bfloat\w*", q):
        if not nets or not nets.get("available"):
            reason = (nets or {}).get("reason") if nets else None
            return ("The net graph was not built, so there is no net count to report. "
                    + (f"Reason: {reason}. " if reason else "")
                    + "Building it needs the vertical connection stack — which via joins which two "
                    "conductor layers. GDSII records no layer elevations and a .lyp records colours "
                    "and names, so that stack has to come from the PDK or be supplied explicitly. "
                    "What is available without it: "
                    f"{t1['total_shapes']} shapes forming {t1['total_components']} within-layer "
                    "conductors, and measured via-to-conductor overlaps.")
        s = nets["summary"]
        out = (f"Under the connection stack in use, the layout resolves to {s['net_count']} "
               f"physical net(s): {s['multi_layer_net_count']} spanning more than one layer and "
               f"{s['single_layer_net_count']} confined to a single layer. "
               f"{s['floating_net_count']} net(s) use no via or contact at all. "
               f"This is physical connectivity — which shapes are joined — not electrical intent; "
               f"whether they are *meant* to be joined needs a netlist.")
        # Three sources with three different standings; a sidecar-derived stack is
        # not a geometric guess and must not be described as one.
        source = conn.get("stack_source") or ""
        if "sidecar" in source:
            out += (" The stack came from the semantic sidecar's own via layer names, which state "
                    "each via's endpoints. That is a naming convention rather than verified "
                    "technology data, but it is not a geometric guess.")
        elif source != "supplied":
            out += (" The stack was inferred from layer naming and measured overlap rather than "
                    "supplied from technology data, so these net counts are provisional.")
        for w in nets.get("stack_plausibility_warnings", []):
            out += " " + w
        return out

    if re.search(r"\bstack\b", q):
        prop = conn.get("proposed_stack")
        if conn.get("stack_source") == "supplied":
            used = conn.get("stack_used", {})
            rules = "; ".join(f"{p['connector_name']} joins "
                              f"{' and '.join(x['name'] for x in p['connects'])}"
                              for p in used.get("proposals", [])[:12])
            return f"The connection stack was supplied explicitly: {rules}."
        if not prop:
            return ("No connection stack is available, and none could be inferred without a layer "
                    "map identifying the via and metal layers.")
        conf = prop["confidence_summary"]
        lines = "; ".join(f"{p['connector_name']} → "
                          f"{' + '.join(x['name'] for x in p['connects']) or 'undetermined'} "
                          f"[{p['confidence']}]" for p in prop["proposals"])
        return (f"No stack was supplied, so one was inferred and is offered for review only: "
                f"{lines}. Confidence: {conf}. {prop['caveat']}")

    if re.search(r"\bvia\w*|\bcontact\w*|\bland\w*|\breach\w*", q):
        land = conn.get("landings") or {}
        if not land.get("available"):
            return (f"Via and contact landings could not be measured: {land.get('reason')}.")
        parts = []
        for c in land["connectors"]:
            full = [o["name"] for o in c["overlaps"] if o["enclosure_ratio"] == 1.0]
            parts.append(f"{c['name']} ({c['shape_count']} shape(s)) is enclosed by "
                         + (", ".join(full) if full else "no conductor layer"))
        return ("Measured via and contact landings — this is plan-view overlap, which is not the "
                "same as connection, because GDSII has no Z axis: " + "; ".join(parts) + ".")

    if re.search(r"\binteract\w*|\boverlap\w*|\bintersect\w*|\bcross\w*", q):
        land = conn.get("landings") or {}
        if not land.get("available"):
            return (f"Layer interaction could not be measured: {land.get('reason')}.")
        pieces = []
        for c in land.get("connectors", []):
            hit = [o["name"] for o in c["overlaps"] if o["interaction_ratio"] == 1.0]
            if hit:
                pieces.append(f"every `{c['name']}` shape overlaps {', '.join(hit)}")
        abut = [a for a in land.get("conductor_adjacency", []) if a["shapes_touching"]]
        out = []
        if pieces:
            out.append("Measured overlaps between connector and conductor layers: "
                       + "; ".join(pieces) + ".")
        if abut:
            out.append("Conductor layers whose shapes touch each other: " + "; ".join(
                f"{' + '.join(a['names'])} at {a['shapes_touching']} place(s)"
                + (" (edge to edge, no overlapping area)" if a["abut_without_overlap"] else "")
                for a in abut) + ".")
        out.append("These are plan-view measurements. Two layers overlapping does not mean they are "
                   "connected — that depends on the process stack, which GDSII does not record.")
        return " ".join(out)

    # Catch-all for a general request ("analyse the connectivity", "give me the
    # connectivity graph", "is any geometry disconnected?"). Without this these fell
    # through to the model, which then had only the digest to work from.
    land = conn.get("landings") or {}
    parts = [
        f"Within layers, {t1['total_shapes']} conducting shapes form {t1['total_components']} "
        f"separate physical conductors. This is exact and needs no process-stack data."
    ]
    if land.get("available"):
        stranded = sum(c.get("shapes_overlapping_no_conductor", 0)
                       for c in land.get("connectors", []))
        parts.append(
            f"{len(land['connectors'])} via/contact layer(s) were measured against "
            f"{len(land['conductor_layers'])} conductor layer(s); {stranded} connector shape(s) "
            f"overlap no conductor at all. Overlap is a measurement, not a connection — GDSII "
            f"stores no layer elevations.")
    if nets and nets.get("available"):
        s = nets["summary"]
        parts.append(
            f"Under the connection stack in use the layout resolves to {s['net_count']} physical "
            f"net(s) across {s['conducting_shape_count']} conducting shapes, "
            f"{s['floating_net_count']} of which use no via or contact. The graph has "
            f"{t1['total_components']} within-layer conductor nodes.")
    else:
        parts.append(
            "No net graph was built, because that needs the vertical connection stack — which via "
            "joins which two conductor layers — and neither the .gds nor the .lyp records it.")
    parts.append(
        "Physical shorts and opens are not reported: both are defined relative to an intended "
        "netlist, and none was supplied.")
    return " ".join(parts)


MEASUREMENT_TRIGGER = re.compile(
    r"\btotal\s+(?:metal|via|contact|poly|gate|diffusion)\w*|"
    r"\b(?:metal|via|contact|poly|gate|diffusion)\w*\s+area\b|"
    r"\bperimeter\w*|\bvertic\w*|\bvertex\b|\bpoint count|\bcomplexity\b|"
    r"\bmin\w*\s+(?:metal\s+)?width|\bwidth\b|\bmin\w*\s+spac\w*|\bspac\w*|\bnarrow\w*|"
    r"\bpath\w*|\brectangular\b|\bshape type|\bsize\w*|\bdimension\w*|\bhow (?:big|wide|thick)\b|"
    r"\barray\w*|\bpitch\w*|\bgrid\b|\barrang\w*|\bevenly spaced\b")

HIERARCHY_TRIGGER = re.compile(
    r"\bhierarch\w*|\bdepth\b|\bdeep\b|\bnest\w*|\bempty cell|\borphan\w*|\brecursi\w*|"
    r"\bcell reference|\breference\w* valid|\bunreferenced\b|\bflat\b|\blevel\w*|\bsub-?cell\w*|"
    r"\binstance\w*")


def _fmt_um(value, unit="µm"):
    return "unavailable" if value is None else f"{value:g} {unit}"


def _measurement_answer(meas: dict[str, Any] | None, q: str, layers: list[dict[str, Any]]) -> str | None:
    """Answer from the geometric measurements, or None to fall through."""
    if not meas:
        return None
    rows = meas.get("layers") or []
    agg = meas.get("role_aggregates") or {}
    named = _mentioned_layer(q, layers) if layers else None
    subject = [r for r in rows if named and r["name"].lower() == named.lower()] if named else []
    disclaimer = (" These are measured values, not rule compliance — no rule deck was supplied.")

    # Which role is being asked about, so "metal width" uses the metal aggregate.
    role = None
    for word, key in (("metal", "metal"), ("via", "via"), ("contact", "contact"),
                      ("poly", "poly"), ("gate", "poly"), ("diffusion", "diffusion")):
        if re.search(rf"\b{word}\w*", q):
            role = key
            break

    # Role aggregates first: "total metal area" must not fall through to a branch
    # that answers with the cell bounding box.
    if role and role in agg and re.search(r"\barea\b|\btotal\b", q):
        a = agg[role]
        return (f"Total {role} area is {a['total_area_um2']:g} µm², summed over "
                f"{a['layer_count']} {role} layer(s) ({', '.join(a['layers'])}) across "
                f"{a['shape_count']} shapes. Each layer's own area is a merged region, so overlaps "
                f"within a layer are not double counted.")

    if re.search(r"\bperimeter\w*", q):
        if subject:
            r = subject[0]
            same = r["perimeter_um"] == r["merged_perimeter_um"]
            tail = (" The two agree, so no shapes on this layer abut."
                    if same else " They differ because some shapes abut and merge.")
            return (f"`{r['name']}`: the shapes as drawn total {_fmt_um(r['perimeter_um'])} of "
                    f"perimeter; the merged outline measures "
                    f"{_fmt_um(r['merged_perimeter_um'])}." + tail)
        top = sorted((r for r in rows if r.get("perimeter_um")),
                     key=lambda r: -r["perimeter_um"])[:6]
        if not top:
            return None
        return ("Perimeter per layer (shapes as drawn, then the merged outline): "
                + "; ".join(f"`{r['name']}` {_fmt_um(r['perimeter_um'])} / "
                            f"{_fmt_um(r['merged_perimeter_um'])}" for r in top)
                + f". {len(rows)} layers were measured in total.")

    if re.search(r"\bvertic\w*|\bvertex\b|\bpoint count|\bcomplexity\b|\brectangular\b", q):
        if subject:
            r = subject[0]
            return (f"`{r['name']}` has {r['vertex_count']} vertices across {r['shape_count']} "
                    f"shape(s), at most {r['max_vertices_in_one_polygon']} in one shape "
                    f"({r['non_rectangular_shape_count']} shape(s) have more than 4 vertices, so "
                    f"are not plain rectangles).")
        total = sum(r["vertex_count"] for r in rows if r.get("vertex_count"))
        worst = max((r for r in rows if r.get("max_vertices_in_one_polygon")),
                    key=lambda r: r["max_vertices_in_one_polygon"], default=None)
        nonrect = sum(r.get("non_rectangular_shape_count") or 0 for r in rows)
        if worst is None:
            return None
        return (f"The layout has {total} polygon vertices in total. The most complex single shape is "
                f"on `{worst['name']}` with {worst['max_vertices_in_one_polygon']} vertices. "
                f"{nonrect} shape(s) in the design have more than 4 vertices; every other shape is "
                f"a rectangle.")

    if re.search(r"\bpath\w*", q):
        paths = [r for r in rows if (r.get("shape_types") or {}).get("path")]
        if not paths:
            return ("No PATH elements are present — every shape in this layout is stored as a "
                    "BOUNDARY or BOX record, so there are no path widths to report.")
        return ("PATH elements: " + "; ".join(
            f"`{r['name']}` has {r['shape_types']['path']} path(s), widths "
            f"{r.get('path_widths_um')}" for r in paths) + ".")

    if re.search(r"\bmin\w*\s+spac\w*|\bspac\w*|\bgap\b", q):
        if subject:
            r = subject[0]
            if r.get("observed_min_space_um") is None:
                return (f"`{r['name']}` has {r['shape_count']} shape(s) with no measurable gap "
                        f"between separate shapes, so there is no spacing to report."
                        + disclaimer)
            return (f"The smallest observed gap between separate `{r['name']}` shapes is "
                    f"{_fmt_um(r['observed_min_space_um'])}." + disclaimer)
        if role and role in agg and agg[role].get("observed_min_space_um") is not None:
            a = agg[role]
            return (f"Across the {a['layer_count']} {role} layer(s) ({', '.join(a['layers'])}), the "
                    f"smallest observed spacing is {_fmt_um(a['observed_min_space_um'])}."
                    + disclaimer)
        cands = [r for r in rows if r.get("observed_min_space_um") is not None]
        if not cands:
            return None
        tight = min(cands, key=lambda r: r["observed_min_space_um"])
        return (f"The smallest observed spacing anywhere in the layout is "
                f"{_fmt_um(tight['observed_min_space_um'])}, on `{tight['name']}`." + disclaimer)

    if re.search(r"\bwidth\b|\bnarrow\w*|\bhow (?:wide|thick)\b", q):
        if subject:
            r = subject[0]
            return (f"`{r['name']}`: narrowest observed width {_fmt_um(r['observed_min_width_um'])}, "
                    f"widest {_fmt_um(r.get('observed_max_width_um'))}." + disclaimer)
        if role and role in agg and agg[role].get("observed_min_width_um") is not None:
            a = agg[role]
            return (f"Across the {a['layer_count']} {role} layer(s) ({', '.join(a['layers'])}), the "
                    f"narrowest observed width is {_fmt_um(a['observed_min_width_um'])}."
                    + disclaimer)
        cands = [r for r in rows if r.get("observed_min_width_um") is not None]
        if not cands:
            return None
        thin = min(cands, key=lambda r: r["observed_min_width_um"])
        return (f"The narrowest observed width anywhere in the layout is "
                f"{_fmt_um(thin['observed_min_width_um'])}, on `{thin['name']}`." + disclaimer)

    if re.search(r"\barray\w*|\bpitch\w*|\bgrid\b|\barrang\w*|\bspaced regularly\b|"
                 r"\bevenly spaced\b", q):
        members = [r for r in rows if r.get("arrangement")
                   and (role is None or r["role"] == role)]
        if not members:
            return None
        regular = [r for r in members if r["arrangement"]["regular"]]
        head = (f"{len(regular)} of {len(members)} measured layer(s) place their shapes on a "
                f"regular pitch. ")
        detail = "; ".join(f"`{r['name']}` ({r['shape_count']} shapes): "
                           f"{r['arrangement']['description']}"
                           for r in members[:8])
        return head + detail + "."

    if re.search(r"\bsize\w*|\bdimension\w*|\bhow big\b", q):
        if subject:
            r = subject[0]
            e = r.get("shape_extents_um") or {}
            if e.get("uniform"):
                return (f"All {r['shape_count']} `{r['name']}` shapes measure "
                        f"{e['min_width']:g} × {e['min_height']:g} µm.")
            return (f"`{r['name']}` has {r['shape_count']} shape(s) of differing size: widths "
                    f"{e.get('min_width')}–{e.get('max_width')} µm, heights "
                    f"{e.get('min_height')}–{e.get('max_height')} µm.")
        if role in ("via", "contact"):
            members = [r for r in rows if r["role"] == role and r.get("shape_extents_um")]
            if members:
                return (f"{role.capitalize()} sizes: " + "; ".join(
                    f"`{r['name']}` ×{r['shape_count']} "
                    + (f"{r['shape_extents_um']['min_width']:g} × "
                       f"{r['shape_extents_um']['min_height']:g} µm"
                       if r["shape_extents_um"]["uniform"] else
                       f"varying, {r['shape_extents_um']['min_width']:g}–"
                       f"{r['shape_extents_um']['max_width']:g} µm wide")
                    for r in members) + ".")
        return None

    # "Total metal area" and friends: a real sum over the layers of that role.
    if role and re.search(r"\barea\b|\btotal\b", q) and role in agg:
        a = agg[role]
        return (f"Total {role} area is {a['total_area_um2']:g} µm², summed over "
                f"{a['layer_count']} {role} layer(s) ({', '.join(a['layers'])}) across "
                f"{a['shape_count']} shapes. Each layer's own area is a merged region, so overlaps "
                f"within a layer are not double counted.")
    return None


def _hierarchy_answer(hier: dict[str, Any] | None, q: str) -> str | None:
    """Answer from the cell-hierarchy analysis, or None to fall through."""
    if not hier:
        return None

    if re.search(r"\brecursi\w*", q):
        if hier["recursive_cells"]:
            return (f"Recursive cell references were found in: "
                    f"{', '.join(hier['recursive_cells'])}. That is not legal GDSII.")
        return ("No recursive cell references. GDSII cannot legally express one and none is "
                f"present: all {hier['cell_count_total']} cell(s) form an acyclic hierarchy.")

    if re.search(r"\bempty\b.{0,12}\borphan|\borphan\w*.{0,12}\bempty\b", q):
        empty = hier["empty_cells"]
        orphan = hier["orphan_cells"]
        parts = [f"{len(empty)} empty cell(s)"
                 + (f": {', '.join(empty)}" if empty else " — every cell holds shapes or instances"),
                 f"{len(orphan)} cell(s) unreachable from `{hier['top_cell']}`"
                 + (f": {', '.join(orphan)}" if orphan else "")]
        return ("; ".join(parts)
                + f". {hier['cell_count_total']} cell(s) are in the file in total.")

    if re.search(r"\bempty cell|\bempty\b.*\bcell", q):
        if not hier["empty_cells"]:
            return (f"No empty cells. All {hier['cell_count_total']} cell(s) contain shapes or "
                    f"instances.")
        return (f"{len(hier['empty_cells'])} empty cell(s) — no shapes and no instances: "
                f"{', '.join('`%s`' % c for c in hier['empty_cells'])}.")

    if re.search(r"\borphan\w*|\bunreferenced\b|\bunreachable\b", q):
        if not hier["orphan_cells"]:
            return (f"No orphan cells. Every one of the {hier['cell_count_total']} cell(s) is "
                    f"reachable from the top cell `{hier['top_cell']}`.")
        return (f"{len(hier['orphan_cells'])} cell(s) are not reachable from `{hier['top_cell']}`, "
                f"so their geometry is excluded from every figure reported: "
                f"{', '.join('`%s`' % c for c in hier['orphan_cells'])}.")

    if re.search(r"\bcell reference|\breference\w* valid|\bproxy\b|\bunresolved\b", q):
        if hier["unresolved_reference_cells"]:
            return (f"{len(hier['unresolved_reference_cells'])} cell reference(s) could not be "
                    f"resolved to a definition: "
                    f"{', '.join(hier['unresolved_reference_cells'])}.")
        return (f"All cell references resolve. KLayout read the file with "
                f"{hier['cell_count_total']} cell(s) and no unresolved or proxy cells.")

    if re.search(r"\bhierarch\w*|\bdepth\b|\bdeep\b|\bnest\w*|\bflat\b|\blevel\w*|\bsub-?cell\w*", q):
        parts = [f"The hierarchy is {hier['depth_description']}.",
                 f"Top cell `{hier['top_cell']}`"]
        if hier["top_cell_count"] > 1:
            parts[-1] += (f" (one of {hier['top_cell_count']} top-level cells: "
                          f"{', '.join(hier['top_cells'])})")
        parts[-1] += (f"; {hier['cell_count_total']} cell(s) in the file, "
                      f"{hier['cell_count_in_scope']} reachable from it.")
        deep = sorted(hier["cells"], key=lambda c: -c["levels_below"])[:4]
        if any(c["levels_below"] for c in deep):
            parts.append("Levels below each cell: " + ", ".join(
                f"`{c['name']}` {c['levels_below']}" for c in deep) + ".")
        if hier["empty_cells"]:
            parts.append(f"{len(hier['empty_cells'])} empty cell(s).")
        if hier["orphan_cells"]:
            parts.append(f"{len(hier['orphan_cells'])} unreachable cell(s).")
        return " ".join(parts)

    if re.search(r"\binstance\w*|\bplacement\w*", q):
        placed = sum(c["child_instance_placements"] for c in hier["cells"])
        records = sum(c["child_instance_records"] for c in hier["cells"])
        if placed == 0:
            return (f"No cell instances are placed. `{hier['top_cell']}` is flat — all its geometry "
                    f"is drawn directly, with no sub-cell references.")
        return (f"{placed} instance placement(s) from {records} instance record(s) "
                f"(an array record places several). Per cell: " + ", ".join(
                    f"`{c['name']}` places {c['child_instance_placements']}"
                    for c in hier["cells"] if c["child_instance_placements"]) + ".")
    return None


PITCH_TRIGGER = re.compile(
    r"\bcpp\b|\bcgp\b|\bgate pitch\w*|\bpoly pitch\w*|\bcontacted poly\b|"
    r"\bmetal ?[012]\b.*\bpitch|\bpitch\b.*\bmetal ?[012]\b|\bm[012]\b.*\bpitch|"
    r"\bpitch\b.*\bm[012]\b|\brouting pitch\w*|\bgear ratio\b|\bhow many (?:gate|poly) "
    r"pitch|\bcell width\b|\bhow wide\b|\btrack height\b|\bpitch(?:es)?\b")


def _pitch_answer(pitch: dict[str, Any] | None, q: str) -> str | None:
    """Answer the pitch questions a layout engineer asks first.

    These used to be answered with a description of how the shapes happened to be
    arranged, which is not the same thing: a routing pitch is a property of the
    track grid, and the grid exists whether or not a wire occupies it.
    """
    if not pitch:
        return ("No pitch metrics are available for this file. They need the layer map, which "
                "identifies the poly, diffcon and track-guide layers.")
    if pitch.get("error"):
        return f"Pitch analysis failed for this file: {pitch['error']}"

    gp, metals, dims = pitch["gate_pitch"], pitch["metal_pitches"], pitch["cell_dimensions"]

    # "How many gate pitches / poly pitches" is a count across the cell, not a value.
    if re.search(r"\bhow many\b|\bcount\b|\bwide\b|\bcell width\b", q) and \
            re.search(r"\bgate pitch\w*|\bpoly pitch\w*|\bcpp\b|\bwide\b|\bcell width\b", q):
        if not dims.get("gate_pitches"):
            return (f"The number of gate pitches could not be determined: "
                    f"{dims.get('basis') or gp['basis']}")
        note = "" if dims.get("width_is_whole_cpp") else \
            " That is not a whole number, which is unusual for a standard cell."
        return (f"**{dims['gate_pitches']} gate pitches.** {dims['width_basis']}."
                f"{note} The gate pitch (CPP) itself is {gp['cpp_nm']:g} nm, so the cell is "
                f"{dims['width_nm']:g} nm wide.")

    if re.search(r"\bgear ratio\b", q):
        gear = pitch.get("gear_ratio")
        if not gear:
            return ("The gear ratio needs both the gate pitch and the M1 pitch, and one of them "
                    "could not be measured in this cell.")
        return f"**Gear ratio {gear['gear_ratio']:g}.** {gear['basis']}."

    # A question naming a metal layer wants the routing pitch of those layers.
    named = [m for m in ("M0", "M1", "M2")
             if re.search(rf"\b(?:metal ?{m[1]}|{m})\b", q, re.I)]
    if named or re.search(r"\brouting pitch\w*|\bmetal pitch\w*", q):
        wanted = named or ["M0", "M1", "M2"]
        lines = []
        for metal in wanted:
            entry = metals.get(metal) or {}
            if not entry.get("pitch_nm"):
                lines.append(f"**{metal}**: no pitch measurable — {entry.get('note')}")
                continue
            piece = (f"**{metal}**: {entry['pitch_nm']:g} nm pitch "
                     f"({entry['routing_direction']} routing, so measured along "
                     f"{entry['pitch_axis']})")
            if isinstance(entry.get("width_nm"), (int, float)):
                piece += (f", {entry['width_nm']:g} nm wide leaving "
                          f"{entry['implied_space_nm']:g} nm of space")
            if entry.get("tracks"):
                piece += f", {entry['tracks']} tracks"
            if entry.get("note"):
                piece += f". {_sentence(entry['note'])}"
            lines.append(piece)
        # The note ends without punctuation, so the sentence that follows needs its
        # own full stop or the two run together.
        joined = " · ".join(lines)
        return (joined + ("" if joined.endswith(".") else ".")
                + " Taken from the track-guide layers, which declare the grid whether "
                  "or not a wire uses it.")

    # Otherwise: the gate pitch, which is what CPP/CGP/poly pitch all mean.
    if not gp.get("cpp_nm"):
        return f"The gate pitch could not be determined: {gp['basis']}"
    out = [f"**Gate pitch (CPP) = {gp['cpp_nm']:g} nm.** {_sentence(gp['basis'])}."]
    ev = gp["evidence"]
    if ev.get("decomposition"):
        out.append(f"It decomposes as {ev['decomposition']} nm "
                   f"(2 × poly-to-diffcon spacing + diffcon width + poly width), which is the "
                   f"manual's rule 3.2.6.")
    if dims.get("gate_pitches"):
        out.append(f"The cell is {dims['gate_pitches']} gate pitches wide "
                   f"({dims['width_nm']:g} nm).")
    out.append("CPP, CGP, gate pitch and poly pitch all name this one number.")
    return " ".join(out)


def _sentence(text: str) -> str:
    """Capitalise the first character only.

    str.capitalize() lowercases everything after it, which turns "VDD, VSS" into
    "vdd, vss" and "an M0 polygon" into "an m0 polygon" - every layer and label name
    in the sentence is a proper noun here.
    """
    return text[:1].upper() + text[1:] if text else text


# Dimensional tech-file parameters. Deliberately narrow, and tested before the
# classification trigger, because "power rail width" would otherwise be swallowed by
# the classifier's "power rail" pattern and answered with the power-delivery scheme.
TECHPARAM_TRIGGER = re.compile(
    r"\bgate extension\b|\bdiffcon extension\b|\bvia extension\b|\bgate cut\b|"
    r"\b[np][-\s]?poly width\b|\b[np][-\s]?diffcon width\b|\bpoly width\b|"
    r"\bdiffcon width\b|\bdiffusion width\b|\bpower rail width\b|"
    r"\bdiffusion spacing\b|\bpoly to diffcon\b|\bdiffcon ete\b|\bete spacing\b|"
    r"\bdiff(?:usion)? to diff interconnect\b|\bdiff interconnect\b|"
    r"\benclosure\b|\bvia (?:size|offset)\b|\b[pn]?via[gt]\b|\bvia[01]\b|"
    r"\bmetal ?[012]\b|\btech(?:nology)? file\b|\btech param\w*|\bparameter table\b|"
    r"\bdiffcon profile\b|\btrack profile\b")


def _fmt_param(value: Any) -> str:
    """Render a tech-file parameter value the way a tech file prints it.

    Deliberately not named `_fmt`: this module already has one with a different
    signature, and shadowing it broke every caller of the original.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(_fmt_param(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k} {_fmt_param(v)}" for k, v in value.items())
    return str(value)


def _techparam_answer(params: dict[str, Any] | None, q: str) -> str | None:
    """Answer a question about a tech-file parameter by quoting the measurement.

    The parameter is looked up by name, so "calculate the gate extension" is answered
    with the figure measured from this layout rather than with a description of how
    gate extensions work. Where the layout cannot express the parameter, the reason is
    given and any stated value is attributed to the tech file - a stated figure is
    never presented as a measurement.
    """
    if not params or not params.get("parameters"):
        return ("No tech-file parameters were measured for this file. They need the "
                "layer map, because the parameters are defined against named layers "
                "(NPOLY, NDIFFCON, BM0) and a raw GDS only has layer numbers.")

    from analyzer.techparams import parameter

    # Strip the question framing so the parameter name is what gets matched.
    needle = re.sub(r"\b(?:what|is|the|of|in|for|calculate|compute|measure|find|"
                    r"give|me|show|please|tell|value|are|and|a|an|this|that|cell|"
                    r"layout|gds|file|nm|how|much|many)\b", " ", q)
    needle = re.sub(r"[?.,;:]|\b\w+\.gds\b", " ", needle).strip()
    record = parameter(params, needle) if needle else None
    if record is None:
        names = ", ".join(list(params["parameters"])[:8])
        return (f"That parameter is not one of the {len(params['parameters'])} measured "
                f"here. The measured ones include: {names}.")

    name, unit = record["parameter"], record.get("unit") or ""
    suffix = f" {unit}" if unit else ""

    if not record.get("available"):
        stated = ((params.get("comparison") or {}).get("stated_only") or [])
        quoted = next((row for row in stated if row["parameter"] == name), None)
        text = (f"**{name}** cannot be measured from this layout: {record['basis']}.")
        if quoted and quoted.get("stated") is not None:
            text += (f" The supplied tech file states {_fmt_param(quoted['stated'])}{suffix}, "
                     f"which is that file's figure and not a measurement of this cell.")
        return text

    value = record["value"]
    text = f"**{name}: {_fmt_param(value)}{suffix}**"
    if record.get("compact_nm") and isinstance(value, list):
        text += (f" — as a repeating unit that is "
                 f"{_fmt_param(record['compact_nm'])}{suffix} (margin, width, gap)")
    text += f". {_sentence(record['basis'])}."

    # A stated tech file is the strongest confirmation available, so say when the
    # measurement agrees with it.
    comparison = params.get("comparison") or {}
    for row in comparison.get("agree") or []:
        if row["parameter"] == name:
            text += (f" This matches the {_fmt_param(row['stated'])}{suffix} stated in "
                     f"`{comparison.get('reference_file')}`.")
            break
    else:
        for row in comparison.get("disagree") or []:
            if row["parameter"] == name:
                text += (f" **The supplied tech file states "
                         f"{_fmt_param(row['stated'])}{suffix}, which the geometry does not "
                         f"agree with.**")
                break
    return text


CLASSIFY_TRIGGER = re.compile(
    r"\bfront ?side\b|\bback ?side\b|\bbspdn\b|\bpower (?:delivery|distribution|scheme|rail)|"
    r"\bpower type\b|\bmetal solution\b|\bhow many metal|\brouting capabilit\w*|"
    r"\b(?:single|two|three)[- ]metal\b|\bsingle[- ]?height\b|\bmulti[- ]?height\b|"
    r"\bcell height\b|\borientation\b|\bflip\w*|\bmirror\w*|\br0\b|\bmx\b|\bmy\b|"
    r"\brouting track\w*|\btrack\w*|\bempty track\w*|\bgaa\b|\bcfet\b|\bfinfet\b|"
    r"\btechnolog\w*|\bhalf[- ]?dr\b|\bwhat kind of cell|\bclassif\w*|\brt number\b")


def _classify_answer(cls: dict[str, Any] | None, q: str) -> str | None:
    """Answer the standard-cell classification questions.

    These were the questions the tool used to fail on. Asked "frontside or
    backside" it said the metadata had no such field - which was literally true and
    entirely useless, because BM0 carries VSS and VDD labels and that is the answer.
    """
    if not cls:
        return ("No cell classification is available for this file. It needs the layer map, which "
                "identifies the power, metal and track-guide layers by name.")
    if cls.get("error"):
        return f"Cell classification failed for this file: {cls['error']}"

    if re.search(r"\bfront ?side\b|\bback ?side\b|\bbspdn\b|\bpower (?:delivery|distribution|"
                 r"scheme|rail)|\bpower type\b", q):
        p = cls["power_delivery"]
        if not p.get("power_delivery"):
            return (f"The power delivery scheme could not be determined: {p['basis']}. "
                    f"Backside labels found: {p['backside_labels'] or 'none'}; frontside: "
                    f"{p['frontside_labels'] or 'none'}. A side qualifies only if it carries both a "
                    f"ground label ({', '.join(p['ground_vocabulary'])}) and a power label "
                    f"({', '.join(p['power_vocabulary'])}).")
        other = "frontside" if p["power_delivery"] == "backside" else "backside"
        return (f"**{_sentence(p['power_delivery'])} power.** {_sentence(p['basis'])}. "
                f"Backside labels: {p['backside_labels'] or 'none'}; frontside labels: "
                f"{p['frontside_labels'] or 'none'}. A side counts only when it carries both a "
                f"ground and a power label, and the backside is tested first — so a layout with "
                f"both reads as backside.")

    if re.search(r"\bmetal solution\b|\bhow many metal|\brouting capabilit\w*|"
                 r"\b(?:single|two|three)[- ]metal\b", q):
        m = cls["metal_solution"]
        readable = {"SingleMetalSolution": "single-metal", "TwoMetalSolution": "two-metal",
                    "ThreeMetalSolution": "three-metal",
                    "UNKNOWN": "undetermined"}[m["metal_solution"]]
        if m["metal_solution"] == "UNKNOWN":
            return f"The routing capability is undetermined: {m['basis']}."
        available, drawn = m.get("metals_available") or [], m.get("metals_drawn") or []
        unused = [layer for layer in available if layer not in drawn]
        # Capability and usage are different answers, and conflating them is how this
        # once reported a three-metal cell as two-metal for routing on two layers.
        detail = (f"{', '.join(available)} have track guides, so the technology gives "
                  f"this cell {len(available)} routing layer(s)."
                  if m.get("source") == "track guide"
                  else f"No metal track guide was found, so this counts the drawn "
                       f"metal instead: {', '.join(drawn) or 'none'}.")
        usage = (f" {', '.join(drawn)} "
                 f"{'carries' if len(drawn) == 1 else 'carry'} geometry here"
                 + (f"; {', '.join(unused)} "
                    f"{'is' if len(unused) == 1 else 'are'} available but unused."
                    if unused else ".")) if drawn else ""
        return f"**{m['metal_solution']}** — a {readable} cell. {detail}{usage}"

    if re.search(r"\bsingle[- ]?height\b|\bmulti[- ]?height\b|\bcell height\b", q):
        h, t = cls["cell_height"], cls["technology"]
        if not h.get("height"):
            return f"The cell height could not be determined: {h['basis']}."
        return (f"**{_sentence(h['height'])}-Height GDS — {t['technology']}.** {h['basis']}. "
                f"The power rail used for the measurement is {h['base_layer']} "
                f"(BM0 takes priority over M0).")

    if re.search(r"\borientation\b|\bflip\w*|\bmirror\w*|\br0\b|\bmx\b|\bmy\b", q):
        o = cls["orientation"]
        if not o.get("orientation"):
            return (f"The orientation could not be determined: {o['basis']}. "
                    + str(o.get("not_derivable", "")))
        line = (f"**{o['orientation']}** — {o['basis']}. Confidence: {o['confidence']}.")
        if o.get("not_derivable"):
            line += f" {o['not_derivable']}"
        return line

    if re.search(r"\brouting track\w*|\btrack\w*|\bempty track\w*", q):
        t = cls["routing_tracks"]
        if not t.get("tracks"):
            return f"The routing track count is unavailable: {t['basis']}."
        return (f"**{t['tracks']} M0 routing tracks**, of which {t['tracks_used']} carry metal and "
                f"{t['tracks_empty']} are empty. {_sentence(t['basis'])}.")

    if re.search(r"\bgaa\b|\bcfet\b|\bfinfet\b|\btechnolog\w*", q):
        t = cls["technology"]
        return (f"**{t['technology']}** — {t['basis']}. "
                f"The diffusions {'touch' if t['diffusions_touch'] else 'are separated'} and there "
                f"{'is' if t['nwell_count'] == 1 else 'are'} {t['nwell_count']} NWELL polygon(s). "
                f"CFET is diffusions touching; FinFET is separated with an NWELL; GAA is separated "
                f"without one.")

    if re.search(r"\bhalf[- ]?dr\b", q):
        h = cls["half_dr"]
        if h.get("half_dr") is None:
            return f"The half-DR arrangement could not be judged: {h['basis']}."
        return (f"**Half-DR: {h['half_dr']}.** {_sentence(h['basis'])}. Boundary edges at "
                f"{', '.join(f'{y * 1000:.0f} nm' for y in h['boundary_y_um'])}; "
                f"{h['target_layer']} centres at "
                f"{', '.join(f'{y * 1000:.0f} nm' for y in h['rail_centres_y_um'])}.")

    if re.search(r"\brt number\b", q):
        rt = cls.get("min_rt_number")
        if not rt or rt.get("min_rt") is None:
            return "No RT number could be read from the filenames (expected `..._RT_<number>.gds`)."
        return f"The minimum RT number is **{rt['min_rt']}**. {rt['basis']}."

    # "What kind of cell is this?" and anything else the trigger caught.
    return (f"**{cls['headline']}.** Technology {cls['technology']['technology']} "
            f"({cls['technology']['basis']}); power delivery "
            f"{cls['power_delivery'].get('power_delivery') or 'undetermined'}; "
            f"{cls['metal_solution']['metal_solution']}; "
            f"{cls['cell_height'].get('height') or 'unknown'}-height; orientation "
            f"{cls['orientation'].get('orientation') or 'undetermined'} "
            f"({cls['orientation'].get('confidence')}).")


def _drc_answer(drc: dict[str, Any] | None, q: str) -> str | None:
    """Answer a design-rule question from the manual's checked rules.

    The standing boundary is kept in a narrower form: a clean result means no
    violation of the rules that were *checked*, and the count of unchecked rules is
    always stated so it cannot be read as a signoff.
    """
    if not drc:
        return ("No design rule results are available for this file. The GENCHIP Design Rule "
                "Manual supplies the rules; without it this tool measures geometry but cannot "
                "say whether a measurement is legal.")
    if drc.get("error"):
        return f"The design rule check failed for this file: {drc['error']}"
    if drc.get("available") is False:
        return (f"No design rules were checked: {drc.get('reason')} "
                "Everything else — geometry, connectivity, the XOR comparison and the cell "
                "classification — is unaffected.")

    s = drc["summary"]
    tech = drc["technology"]
    scope = (f"{s['rules_checked']} of {s['rules_in_manual']} rules from the manual were "
             f"checked, against the layout as {tech['used']} "
             f"({'supplied' if tech.get('supplied') else tech['confidence']}).")

    if re.search(r"\bwhich rule|\bwhat rule|\bnot checked|\bunchecked|\bcoverage\b", q):
        missing = drc["rules_not_checked"]
        return (scope + f" The {len(missing)} not checked include: "
                + "; ".join(f"{r['id']} {r['rule'][:70]}" for r in missing[:5])
                + ". " + drc["caveat"])

    violations = drc["violations"]
    if not violations:
        return (f"No violations of the checked rules. {scope} {s['pass']} rule(s) passed, "
                f"{s['not checked']} could not be evaluated on this layout and "
                f"{s['not applicable']} apply to a different technology. "
                f"This is not a signoff DRC — the {s['rules_not_checked']} unchecked rules are "
                f"listed in the results.")

    lines = [f"{len(violations)} violation(s) of the checked rules. {scope}"]
    for v in violations[:6]:
        lines.append(f"**{v['id']}** ({v['section']}): {v['detail']}. The manual says: "
                     f"\"{v['rule']}\"")
    if len(violations) > 6:
        lines.append(f"...and {len(violations) - 6} more.")
    lines.append(drc["caveat"])
    return " ".join(lines)


def answer(metadata: dict[str, Any], question: str) -> str | None:
    """Answer from metadata alone, or return None to defer to the LLM."""
    q = question.lower().strip()
    d = metadata["design"]
    layers = metadata.get("layers", [])
    cells = metadata.get("cells", [])
    layout = metadata["layout"]
    connectivity = metadata.get("connectivity")

    # --- rule questions, deliberately first ---
    # This branch used to refuse outright, which was right while no rule deck
    # existed. With the GENCHIP Design Rule Manual loaded, the relational rules it
    # states can be checked and answered - but only those, and LVS still cannot be,
    # so the refusal survives for anything the manual does not cover.
    if re.search(r"\blvs\b|\berc\b|\bschematic\b.*\bmatch|\bnetlist.*\bmatch", q):
        return ("LVS and ERC are not available. Those compare a layout against a schematic or a "
                "netlist, and neither was supplied. The design rule manual supports geometric "
                "rule checks only.")
    if re.search(r"\bdrc\b|\bviolation\w*|\brule check|\bdesign rule|\bcompliant\w*|"
                 r"\bpasses\b|\blegal\b|\brules?\b", q):
        reply = _drc_answer(metadata.get("drc"), q)
        if reply:
            return reply

    # --- overall summary ---
    # "explain ... to a non-expert" is deliberately NOT caught here: rephrasing
    # for a human audience is the LLM's job, not the fact table's.
    if re.search(r"\bsummar\w*|\boverview\b|\btell me about\b|\bwhat is this\b", q) and "non-expert" not in q:
        return _summary(metadata)

    # --- uncomputed-metric guard, before any of the loose branches below ---
    # The branches further down match on broad words ("area", "size", "each
    # layer"), so a question about a metric this tool does not compute used to fall
    # into whichever branch matched first and come back with a different metric
    # entirely: "how many vertices?" was answered with a polygon count, and "total
    # metal area?" with the cell bounding-box area. A plausible number answering
    # the wrong question is the worst failure this tool can produce, so anything
    # not computed is refused here by name.
    for pattern, what, needs in UNCOMPUTED_METRICS:
        if re.search(pattern, q):
            return _uncomputed(what, needs, metadata)

    # --- top cell, before the hierarchy branch claims "top-level" ---
    if re.search(r"\btop[- ]?(?:level\s+)?cell\b", q) and not re.search(
            r"\bhierarch\w*|\bdepth\b|\bhow many\b|\blist\b", q):
        d_ = metadata["design"]
        extra = ""
        if d_.get("top_cell_count", 1) > 1:
            extra = (f" This file has {d_['top_cell_count']} top-level cells "
                     f"({', '.join(d_['top_cells'])}); only `{d_['top_cell']}` was analyzed.")
        return f"The top cell is `{d_['top_cell']}`.{extra}"

    # --- pitch metrics, before the measurement branch claims "pitch" ---
    if PITCH_TRIGGER.search(q):
        reply = _pitch_answer(metadata.get("pitch"), q)
        if reply:
            return reply

    # --- tech-file parameters, before the classification branch ---
    # "power rail width" is a dimension; the classifier's "power rail" pattern would
    # otherwise answer it with the power-delivery scheme.
    if TECHPARAM_TRIGGER.search(q):
        reply = _techparam_answer(
            (metadata.get("classification") or {}).get("tech_parameters")
            or metadata.get("tech_parameters"), q)
        if reply:
            return reply

    # --- cell classification, before the loose branches ---
    # "frontside or backside" and "how many tracks" must not fall through to a layer
    # listing or a connectivity answer.
    if CLASSIFY_TRIGGER.search(q):
        reply = _classify_answer(metadata.get("classification"), q)
        if reply:
            return reply

    # --- hierarchy and geometric measurements, also before the loose branches ---
    if HIERARCHY_TRIGGER.search(q):
        reply = _hierarchy_answer(metadata.get("hierarchy"), q)
        if reply:
            return reply
    if MEASUREMENT_TRIGGER.search(q):
        reply = _measurement_answer(metadata.get("measurements"), q, layers)
        if reply:
            return reply

    # --- connectivity ---
    # Placed before the generic count and layer branches, which would otherwise
    # swallow "how many nets does this design have?" and answer it with a polygon
    # count. Shorts and opens are handled inside, and always refused.
    if CONNECTIVITY_TRIGGER.search(q) or VIA_CONNECTIVITY_TRIGGER.search(q):
        reply = _connectivity_answer(connectivity, q)
        if reply:
            return reply

    # --- top cell ---
    if "top cell" in q or "top-level cell" in q or "top level cell" in q:
        return f"The top cell is `{d['top_cell']}`."

    # --- largest / smallest cell ---
    if re.search(r"\b(largest|biggest)\b.*\bcell\b", q) or re.search(r"\bcell\b.*\b(largest|biggest)\b", q):
        usable = [c for c in cells if c.get("area_um2") is not None]
        if usable:
            c = max(usable, key=lambda x: x["area_um2"])
            return (f"The largest cell by bounding-box area is `{c['name']}` "
                    f"({c['area_um2']:.6f} µm², {_fmt(c.get('width_um'))} × {_fmt(c.get('height_um'))} µm).")
        return "Cell area is unavailable in the current metadata."
    if re.search(r"\bsmallest\b.*\bcell\b", q):
        usable = [c for c in cells if c.get("area_um2") is not None]
        if usable:
            c = min(usable, key=lambda x: x["area_um2"])
            return f"The smallest cell by bounding-box area is `{c['name']}` ({c['area_um2']:.6f} µm²)."
        return "Cell area is unavailable in the current metadata."

    # --- layer presence: "does this design contain M1?" / "is there an M1 layer?"
    #     Skipped for counting questions, which the count branch handles. ---
    is_counting = bool(re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", q))
    if not is_counting and re.search(r"\b(contain|contains|including|include|includes|have|has|is there|are there|use|uses|present)\b", q):
        named = _mentioned_layer(question, layers)
        if named:
            rows = _rows_for(named, layers)
            polys = sum(r.get("polygon_count") or 0 for r in rows)
            vias = sum(r.get("via_count") or 0 for r in rows)
            extra = f", {vias} of them vias" if vias else ""
            return (f"Yes. `{named}` is present with {polys} polygon(s){extra}, across "
                    f"{len(rows)} layer/datatype entr{'y' if len(rows) == 1 else 'ies'}.")
        # An explicitly-named layer that is absent is still a deterministic answer,
        # but only when the token actually looks like a layer identifier. Grabbing
        # any following word answered "...have in total?" with "`in` is not a layer".
        m = re.search(
            r"\b(?:contain|contains|include|includes|have|has|use|uses|is there|are there)\s+"
            r"(?:a\s+|an\s+|the\s+|any\s+)*([A-Za-z][A-Za-z0-9_]{0,30})\b",
            question, re.IGNORECASE,
        )
        if m and _layer_names(layers) and _looks_like_layer_name(m.group(1)):
            return (f"No. `{m.group(1)}` does not appear in the layer list for this design. "
                    f"Layers present: {', '.join(f'`{n}`' for n in _layer_names(layers))}.")
        # "does this design contain any vias?" is a presence question about a
        # category rather than a named layer.
        if "via" in q:
            if d.get("via_count") is None:
                return VIA_UNAVAILABLE
            if d["via_count"]:
                via_layers = [x for x in layers if x.get("via_count")
                              and x.get("via_role", "via") == "via"]
                return (f"Yes. The metadata reports {d['via_count']} vias on "
                        + ", ".join(f"`{x['name']}` ({x['via_count']})" for x in via_layers) + ".")
            return "No vias are reported in this design."
        if "text" in q or "label" in q:
            return f"{'Yes' if d['text_count'] else 'No'}. The design contains {d['text_count']} text labels."

    # --- density (checked before the layer listing, so "which layer has the
    #     highest density?" is not swallowed by "which layers are used?") ---
    if "densit" in q or re.search(r"\b(highest|lowest|densest)\b.*\blayer\b", q) or re.search(r"\blayer\b.*\b(highest|lowest|densest)\b", q):
        usable = [x for x in layers if x.get("density_percent") is not None]
        if not usable:
            return ("Layer density is unavailable in the current metadata. Density requires measured "
                    "geometry; analyze the .gds file (optionally fused with its sidecar) to obtain it.")
        named = _mentioned_layer(question, layers)
        if named:
            rows = [r for r in _rows_for(named, layers) if r.get("density_percent") is not None]
            if rows:
                return f"`{named}` has a measured density of {max(r['density_percent'] for r in rows):.2f}%."
        if re.search(r"\b(lowest|least|sparsest)\b", q):
            x = min(usable, key=lambda z: z["density_percent"])
            return f"`{x['name']}` has the lowest measured density at {x['density_percent']:.2f}%."
        x = max(usable, key=lambda z: z["density_percent"])
        return (f"`{x['name']}` has the highest measured density at {x['density_percent']:.2f}% "
                f"of the layout bounding box ({x['area_um2']:.6f} µm²).")

    # --- most / least populated layer (before the layer listing, and before the
    #     generic polygon count, both of which would otherwise swallow it) ---
    if re.search(r"\b(most|busiest|fullest|fewest|least)\b", q) and "layer" in q:
        populated = [x for x in layers if x.get("polygon_count")]
        if populated:
            if re.search(r"\b(fewest|least)\b", q):
                x = min(populated, key=lambda z: z["polygon_count"])
                return f"`{x['name']}` holds the fewest polygons of the populated layers ({x['polygon_count']})."
            x = max(populated, key=lambda z: z["polygon_count"])
            return (f"`{x['name']}` holds the most polygons ({x['polygon_count']} of "
                    f"{d['polygon_count']} in the design).")

    # --- via layers, before the generic layer listing claims the question ---
    if re.search(r"\bvia\s+layers?\b|\blayers?\s+(?:are\s+)?vias?\b", q):
        if d.get("via_layer_count") is None:
            return VIA_UNAVAILABLE
        names = ", ".join(f"`{n}`" for n in (d.get("via_layer_names") or []))
        contacts = d.get("contact_layer_names") or []
        tail = (f" A further {len(contacts)} contact layer(s) "
                + ", ".join(f"`{n}`" for n in contacts) + " are counted separately."
                if contacts else "")
        return (f"There are {d['via_layer_count']} via layer(s): {names}, carrying "
                f"{d['via_count']} via shapes in total.{tail}")

    # --- layer utilization: share of geometry and coverage per layer ---
    if re.search(r"\butiliz\w*|\butilis\w*", q):
        rows = [r for r in layers if r.get("polygon_count")]
        if rows:
            total = sum(r["polygon_count"] for r in rows)
            top = sorted(rows, key=lambda r: -r["polygon_count"])[:8]
            parts = [f"`{r['name']}` {r['polygon_count']} polygon(s)"
                     + (f", {r['density_percent']:.2f}% of the bounding box"
                        if r.get("density_percent") is not None else "")
                     for r in top]
            return (f"{len(rows)} of {d['layer_count']} layer entries carry geometry, "
                    f"{total} polygons in total. Busiest: " + "; ".join(parts) + ".")

    # --- which layers are used ---
    if re.search(r"\b(which|what|list|show|name)\b.*\blayers?\b", q) or re.search(r"\blayers?\b.*\b(used|present|exist|are there)\b", q):
        names = _layer_names(layers)
        if names:
            detail = ", ".join(f"`{n}`" for n in names)
            return (f"{d['layer_count']} layer entries are in use across {len(names)} distinct layer names: {detail}.")

    # --- counts ---
    if "how many" in q or re.search(r"\b(count|number of|total)\b", q):
        if "polygon" in q:
            named = _mentioned_layer(question, layers)
            if named:
                rows = _rows_for(named, layers)
                total = sum(r.get("polygon_count") or 0 for r in rows)
                if len(rows) > 1:
                    breakdown = ", ".join(f"datatype {r.get('datatype')}: {r.get('polygon_count')}" for r in rows)
                    group = _group_for(named, metadata)
                    dup = ""
                    if group and group.get("geometry_duplicated_across_datatypes"):
                        dup = (f" These overlap: after merging there are {group['unique_polygons']} "
                               f"distinct shapes, the same geometry repeated across datatypes.")
                    return f"`{named}` contains {total} polygon records ({breakdown}).{dup}"
                return f"`{named}` contains {total} polygons."
            missing = _unknown_layer_token(question, layers)
            if missing:
                return _absent(missing, layers)
            return f"The design contains {d['polygon_count']} polygons in total."
        if "via" in q:
            named = _mentioned_layer(question, layers)
            if named:
                rows = _rows_for(named, layers)
                vals = [r.get("via_count") for r in rows]
                if all(v is None for v in vals):
                    return VIA_UNAVAILABLE
                return f"`{named}` contains {sum(v or 0 for v in vals)} vias."
            if d.get("via_count") is None:
                return VIA_UNAVAILABLE
            missing = _unknown_layer_token(question, layers)
            if missing:
                return _absent(missing, layers)
            # Only via-role rows may be listed here. Listing contacts alongside
            # made the breakdown sum to 12 under a headline count of 6.
            via_layers = [x for x in layers
                          if x.get("via_count") and x.get("via_role", "via") == "via"]
            extra = ""
            if via_layers:
                extra = (" Via layers: "
                         + ", ".join(f"`{x['name']}` ({x['via_count']})" for x in via_layers) + ".")
            contacts = [x for x in layers if x.get("via_role") == "contact" and x.get("via_count")]
            if contacts:
                extra += (f" Contacts are counted separately: {d.get('contact_count')} on "
                          + ", ".join(f"`{x['name']}` ({x['via_count']})" for x in contacts) + ".")
            if d.get("via_count_source"):
                extra += f" Derived from {d['via_count_source']}."
            return f"The metadata reports {d['via_count']} vias.{extra}"
        if "layer" in q:
            return f"The analyzer reports {d['layer_count']} used layer entries ({len(_layer_names(layers))} distinct names)."
        if "cell" in q or "structure" in q:
            return f"The design contains {d['cell_count']} cell(s). The top cell is `{d['top_cell']}`."
        if "text" in q or "label" in q:
            return f"The design contains {d['text_count']} text labels."
        if "shape" in q:
            return f"The design contains {d['shape_count']} shapes in total (polygons plus text)."

    # --- area / size ---
    if "area" in q:
        # A named layer takes priority over the whole-layout bounding box.
        named = _mentioned_layer(question, layers)
        if named:
            # Prefer the unioned group area. Summing the per-datatype areas would
            # double count technologies that duplicate shapes across datatypes.
            group = _group_for(named, metadata)
            if group:
                notes = []
                if group.get("geometry_duplicated_across_datatypes"):
                    notes.append(
                        f"The same geometry appears on more than one datatype, so the per-datatype "
                        f"areas sum to {group['sum_of_datatype_areas_um2']:.6f} µm²; "
                        f"{group['union_area_um2']:.6f} µm² is the physical coverage.")
                if len(group.get("layer_numbers") or []) > 1:
                    notes.append(
                        f"`{named}` spans {len(group['layer_numbers'])} distinct layer numbers "
                        f"({', '.join(str(n) for n in group['layer_numbers'])}); the figure is the sum "
                        f"of each layer's own merged coverage, since separate mask layers may overlap.")
                if group.get("area_is_exclusive_to_this_name") is False:
                    shared = ", ".join(f"`{n}`" for n in group["area_shared_with_other_layer_names"])
                    notes.append(
                        f"This is an upper bound: some of that geometry sits on layer/datatype pairs "
                        f"shared with {shared}, and the sidecar does not say which shape belongs to "
                        f"which name.")
                density = group.get("union_density_percent")
                head = (f"`{named}` covers {group['union_area_um2']:.6f} µm²"
                        + (f" ({density:.2f}% of the bounding box)." if density is not None else "."))
                return head + ("" if not notes else " " + " ".join(notes))
            rows = [r for r in _rows_for(named, layers) if r.get("area_um2") is not None]
            if rows:
                total = sum(r["area_um2"] for r in rows)
                method = rows[0].get("geometry_source") or metadata.get("technology", {}).get("area_method", "")
                note = " (unmerged polygon sum)" if "unmerged" in str(method) else ""
                return f"`{named}` covers {total:.6f} µm²{note}."
            return f"Area is unavailable for `{named}` in the current metadata."
        missing = _unknown_layer_token(question, layers)
        if missing:
            return _absent(missing, layers)
    if "area" in q and ("layout" in q or "design" in q or "total" in q or "bounding" in q or "cell" in q):
        if layout.get("bbox_area_um2") is not None:
            return f"The layout bounding-box area is {layout['bbox_area_um2']:.6f} µm²."
    if re.search(r"\b(dimension|size|how big|bounding box|bbox|width and height)\b", q) or ("width" in q and "height" in q):
        if layout.get("width_um") is not None:
            return (f"The layout bounding box is {layout['width_um']:.6f} µm × {layout['height_um']:.6f} µm "
                    f"({_fmt(layout.get('bbox_area_um2'), digits=6)} µm²).")

    return None


# --- comparison questions ----------------------------------------------------

def answer_xor(xor: dict[str, Any] | None, question: str) -> str | None:
    """Answer from a layout-versus-layout XOR result.

    The questions a reviewer actually asks of a revision diff: what changed, where,
    how much, and is it confined to the interconnect.
    """
    if not xor:
        return None
    if not xor.get("comparable"):
        return f"These two layouts cannot be compared: {xor.get('reason')}."
    q = question.lower().strip()
    s = xor["summary"]
    a, b = xor["file_a"], xor["file_b"]

    if s["identical"]:
        return (f"`{a}` and `{b}` are geometrically identical on all "
                f"{s['layers_compared']} layers — the XOR is empty everywhere.")

    changed = [r for r in xor["layers"] if not r["identical"]]

    if re.search(r"\bwhere\b|\blocation\w*|\bcoordinat\w*|\bwhich part|\bnavigat\w*", q):
        # An explicit key: sorting the tuples directly falls through to comparing
        # the location dicts whenever area and layer name tie, which raises.
        ranked = sorted(((loc["area_um2"], r["name"], loc) for r in changed
                         for loc in r["xor"]["locations"]),
                        key=lambda t: (-t[0], t[1]))[:6]
        return ("The largest differences, biggest first: "
                + "; ".join(f"`{name}` at {loc['centre_um']} µm "
                            f"({loc['width_um']} × {loc['height_um']} µm, {area:g} µm²)"
                            for area, name, loc in ranked)
                + f". In total {s['difference_regions']} difference region(s) across "
                  f"{s['layers_changed']} layer(s).")

    if re.search(r"\bmetal[- ]only\b|\bmask\b|\bre-?spin\b|\beco\b|\bbase layer", q):
        impact = xor["mask_impact"]
        return f"{impact['observation']} {impact['caveat']}"

    if re.search(r"\bhow much\b|\barea\b|\bmagnitude\b|\bsize of the (?:change|diff)", q):
        return (f"The total XOR area is {s['total_xor_area_um2']:g} µm² across "
                f"{s['difference_regions']} region(s): {s['total_area_removed_um2']:g} µm² present "
                f"in `{a}` and gone from `{b}`, and {s['total_area_added_um2']:g} µm² new in `{b}`. "
                f"The largest single difference is {s['largest_single_difference_um2']:g} µm² on "
                f"`{s['largest_difference_on_layer']}`.")

    if re.search(r"\blabel\w*|\btext\b|\bpin name", q):
        rows = [r for r in xor["layers"] if r.get("texts_added") or r.get("texts_removed")]
        if not rows:
            return "No labels differ between the two layouts."
        return ("Label changes: " + "; ".join(
            f"`{r['name']}` adds {r.get('texts_added') or 'none'}, removes "
            f"{r.get('texts_removed') or 'none'}" for r in rows) + ".")

    # Default: what changed.
    parts = [f"{s['layers_changed']} of {s['layers_compared']} layers differ between `{a}` and "
             f"`{b}`, in {s['difference_regions']} region(s) totalling "
             f"{s['total_xor_area_um2']:g} µm² of XOR area."]
    parts.append("Per layer: " + "; ".join(
        f"`{r['name']}` {r['xor']['count']} region(s), {r['xor']['area_um2']:g} µm²"
        + (" (new in B)" if not r["present_in_a"] else
           " (gone from B)" if not r["present_in_b"] else "")
        for r in sorted(changed, key=lambda r: -r["xor"]["area_um2"])[:8]) + ".")
    parts.append(xor["mask_impact"]["observation"])
    parts.append("An XOR difference is a difference, not an error — whether each was intended "
                 "needs the design intent.")
    return " ".join(parts)


COMPARISON_TRIGGER = re.compile(
    r"\b(compare|comparison|changed?|change|differ|difference|differences|delta|deltas|versus|\bvs\b|between the two)\b"
)


def is_comparison_question(question: str) -> bool:
    return bool(COMPARISON_TRIGGER.search(question.lower()))


def answer_comparison(comparison: dict[str, Any], question: str) -> str | None:
    """Answer "what changed?" deterministically from the comparison JSON."""
    if not is_comparison_question(question):
        return None
    s = comparison["summary"]
    lines = [f"**{comparison['file_a']} → {comparison['file_b']}**", ""]
    if not comparison.get("comparable", True):
        lines += ["⚠️ " + comparison["warnings"][0], ""]
        return "\n".join(lines)

    def d(label: str, key: str, unit: str = "") -> str:
        v = s.get(key)
        if v is None:
            return f"- {label}: unavailable"
        sign = "+" if (isinstance(v, (int, float)) and v > 0) else ""
        shown = f"{v:.6f}" if isinstance(v, float) else str(v)
        return f"- {label}: {sign}{shown}{unit}"

    lines += [
        d("Polygons", "polygon_delta"),
        d("Vias", "via_delta"),
        d("Text labels", "text_delta"),
        d("Cells", "cell_delta"),
        d("Layer entries", "layer_delta"),
        d("Bounding-box width", "width_delta_um", " µm"),
        d("Bounding-box height", "height_delta_um", " µm"),
    ]
    added = comparison.get("layers_added", [])
    removed = comparison.get("layers_removed", [])
    modified = comparison.get("layers_modified", [])
    if added:
        lines += ["", "**Layers only in " + comparison["file_b"] + ":**"]
        lines += [f"- `{x['name']}` (layer {x['layer']}/{x['datatype']}), {x.get('polygon_count') or 0} polygons" for x in added]
    if removed:
        lines += ["", "**Layers only in " + comparison["file_a"] + ":**"]
        lines += [f"- `{x['name']}` (layer {x['layer']}/{x['datatype']}), {x.get('polygon_count') or 0} polygons" for x in removed]
    # Every changed row must land in exactly one bucket below. An earlier version
    # reported only count changes and pure moves, so a layer whose shapes changed
    # size (different area, same counts) appeared in neither and a real change
    # went unreported.
    def _label(x):
        return f"`{x['name']}` (layer {x['layer']}/{x['datatype']})"

    counts = [x for x in modified
              if x.get("polygon_delta") or x.get("via_delta") or x.get("text_delta")]
    counted = {id(x) for x in counts}
    resized = [x for x in modified if id(x) not in counted and x.get("area_delta_um2")]
    resized_ids = {id(x) for x in resized}
    moved = [x for x in modified if id(x) not in counted and id(x) not in resized_ids
             and x.get("geometry_changed")]

    if counts:
        lines += ["", "**Layers with changed counts:**"]
        for x in counts:
            bits = []
            if x.get("polygon_delta"):
                bits.append(f"{x['polygon_delta']:+d} polygons")
            if x.get("via_delta"):
                bits.append(f"{x['via_delta']:+d} vias")
            if x.get("text_delta"):
                bits.append(f"{x['text_delta']:+d} text labels")
            if x.get("area_delta_um2"):
                bits.append(f"{x['area_delta_um2']:+.6f} µm² area")
            lines.append(f"- {_label(x)}: " + ", ".join(bits))

    if resized:
        lines += ["", "**Layers with the same counts but a different area** "
                      "(shapes changed size or position):"]
        for x in resized:
            lines.append(f"- {_label(x)}: {x['area_delta_um2']:+.6f} µm² "
                         f"({x['area_um2_a']} → {x['area_um2_b']})")

    # Shapes can move with every count and area unchanged. Reporting "no
    # differences" for that case is a wrong answer, not a missing one.
    if moved:
        lines += ["", "**Layers whose shapes moved** (same counts and same total area, "
                      "but the geometry is not identical):"]
        for x in moved:
            lines.append(f"- {_label(x)}")

    if not added and not removed and not counts and not resized and not moved:
        geometry_compared = any(x.get("geometry_changed") is not None
                                for x in comparison.get("layer_changes", []))
        if geometry_compared:
            lines += ["", "No differences were detected: counts, areas and the measured "
                          "geometry of every layer are identical."]
        else:
            lines += ["", "No count or area differences were detected. Geometry was not "
                          "compared for these inputs, so shapes could still differ in "
                          "position — analyze the .gds files to compare geometry."]
    for w in comparison.get("warnings", []):
        lines += ["", f"_{w}_"]
    return "\n".join(lines)
