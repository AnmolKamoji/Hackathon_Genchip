"""Answer the questions a reviewer asks of two layouts, from measurements.

The comparison chat has one failure mode that matters more than the rest: answering a
specific question with a general one. "Did the transistor count change?" answered with
an XOR area summary reads like an answer, is not one, and is the kind of thing a
reviewer only catches after acting on it.

So every question here is routed to the data that settles it, and anything not routed
is declined by name. The three sources are the XOR, the two metadata blocks, and
whatever analyses the page has already run (rule results, netlists, connectivity) -
never a guess, and never one file's numbers passed off as the pair's.

Judgement questions - is B better, will it pass timing, is it safe to tape out - are
refused. They need a schematic, a library or a sign-off flow, and this tool has two
files.
"""
from __future__ import annotations

import re
from typing import Any

# Questions this module refuses on purpose, with the reason. A confident answer to
# any of these would be invented: nothing in two GDSII files settles them.
JUDGEMENT = (
    (r"\b(better|worse|prefer|which (?:one )?(?:should|do) i|recommend)\b",
     "which layout is better",
     "Better needs a criterion this tool cannot measure - timing, power, area target "
     "or yield. What it can give you is what differs, how much, and on which layers."),
    (r"\b(timing|slack|delay|frequency|speed|faster|slower)\b",
     "timing",
     "Timing needs a netlist, parasitics and a library. A GDSII holds geometry; "
     "nothing here can be turned into a delay."),
    # "sign this off" and "sign it off" put words between the two halves, and a
    # refusal that only catches the hyphenated spelling is not a refusal.
    (r"\b(safe|sign[- ]?off|tape[- ]?out|ship|release|approve)\b|"
     r"\bsign\b[^.?]*\boff\b|\btape\b[^.?]*\bout\b",
     "whether this is safe to release",
     "That is a sign-off decision resting on DRC, LVS, timing, power and the project's "
     "own rules. This tool can tell you what changed and whether the checks it can run "
     "still pass - not whether to ship it."),
    (r"\b(leakage|power consumption|dynamic power|energy)\b",
     "power",
     "Power needs models and switching activity, neither of which is in a layout."),
    (r"\b(yield|defect|hotspot risk|lithograph\w*|printab\w*)\b",
     "manufacturability",
     "Yield and printability need the process: OPC decks, litho models and defect data. "
     "The geometry is here; the process is not."),
)


# Phrasings that are about the *pair* without containing a comparison word. Without
# these, "did any pin move?" and "can this be an ECO?" would be answered from one
# file's metadata - which is a different question with a plausible-looking answer.
#
# The mirror of this matters just as much: a question that carries none of these and
# no comparison word is about one layout, and answering it with "unchanged at 56"
# does not answer it. `is_pair_question` is what the chat routes on.
_PAIR_ONLY = re.compile(
    # "both" has to be about the files. "the vias overlap both M0 and M1" is a
    # question about one layout, and reading it as a comparison answers it with a
    # via count instead of with the overlap it asked about.
    r"\b(both (?:files?|layouts?|revisions?|versions?|designs?|gds|of (?:them|these|the))|"
    r"either|neither|each (?:file|layout|revision|version)|"
    r"which (?:one|layout|file|revision|version|masks?|of (?:the )?(?:two|them))|"
    r"mov(?:e|ed|ement)|shift(?:ed)?|still|already|"
    r"metal[- ]?only|base[- ]?layer|respin|\beco\b|mask (?:set|change|impact)|"
    r"identical|the same as|match(?:es)?\b|"
    r"a (?:and|vs\.?|versus) b|\bb (?:and|vs\.?|versus) a)\b"
)


# Kept here rather than imported from ai.deterministic so this module has no
# dependency on it; the two lists say the same thing and are tested against each
# other.
_COMPARISON = re.compile(
    r"\b(compare|comparison|changed?|change|differ|difference|differences|delta|"
    r"deltas|versus|\bvs\b|between the two)\b"
)


def is_pair_question(question: str, name_a: str | None = None,
                     name_b: str | None = None) -> bool:
    """Is this question about the two layouts rather than about one of them?

    Naming a file is the strongest signal there is - "is B still on grid?" is a pair
    question however it is phrased - so the two file names are checked when known.
    """
    q = (question or "").lower()
    for name in (name_a, name_b):
        if name and name.lower() in q:
            return True
    # "does B introduce a violation A did not have" - the bare letters, as words.
    if re.search(r"\ba\b", q) and re.search(r"\bb\b", q):
        return True
    return bool(_PAIR_ONLY.search(q))


def _fmt_um2(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:g} µm²"


def _delta(a: float | None, b: float | None, unit: str = "", digits: int = 6) -> str:
    """A → B with the difference, or a plain statement that neither moved."""
    if a is None or b is None:
        return "unavailable"
    if a == b:
        return f"unchanged at {a:g}{unit}"
    change = round(b - a, digits)
    return f"{a:g}{unit} → {b:g}{unit} ({change:+g}{unit})"


def _layer_rows(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-layer rows, whichever block carries them.

    `analyze_gds` puts them at the top level as a list; the measurement pass returns
    its own richer rows under `measurements`. Both are used, so this reads either
    rather than assuming one and silently finding nothing.
    """
    rows = metadata.get("layers")
    if isinstance(rows, list):
        return rows
    if isinstance(rows, dict):
        return rows.get("layers") or []
    return (metadata.get("measurements") or {}).get("layers") or []


def _layers_of(metadata: dict[str, Any]) -> dict[str, tuple[int, int]]:
    out = {}
    for row in _layer_rows(metadata):
        name = row.get("name") or f"layer_{row.get('layer')}_{row.get('datatype')}"
        out[name] = (row.get("layer"), row.get("datatype"))
    return out


def _labels_of(metadata: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    """Every text and where it is, from the geometry the page already read."""
    out: dict[str, list[tuple[float, float]]] = {}
    for row in (metadata.get("outlines") or {}).get("layers", []) or []:
        for label in row.get("labels") or []:
            out.setdefault(label["text"], []).append(tuple(label["at_um"]))
    return out


def _via_count(metadata: dict[str, Any]) -> int | None:
    """Vias, from the via layers the layer map identified - contacts excluded.

    The parser counts the two separately on purpose, and folding them together
    disagreed with every sample file's own via count. `measurements.vias` lists
    both under one key, so reading that here is what made this answer say 14 where
    the rest of the page says 10 and 4.
    """
    design = metadata.get("design") or {}
    if design.get("via_count") is not None:
        return int(design["via_count"])
    vias = (metadata.get("measurements") or {}).get("vias") or {}
    layers = vias.get("via_layers")
    if layers is not None:
        return sum(int(row.get("count") or 0) for row in layers
                   if (row.get("role") or "via") == "via")
    rows = _layer_rows(metadata)
    if not rows:
        return None
    counted = [row.get("via_count") for row in rows if row.get("via_count") is not None]
    return sum(counted) if counted else None


def _contact_count(metadata: dict[str, Any]) -> int | None:
    design = metadata.get("design") or {}
    if design.get("contact_count") is not None:
        return int(design["contact_count"])
    vias = (metadata.get("measurements") or {}).get("vias") or {}
    layers = vias.get("via_layers")
    if layers is None:
        return None
    return sum(int(row.get("count") or 0) for row in layers
               if (row.get("role") or "") == "contact")


def _cell_box(metadata: dict[str, Any]) -> dict[str, float | None]:
    layout = metadata.get("layout") or {}
    return {"width": layout.get("width_um"), "height": layout.get("height_um"),
            "area": layout.get("bbox_area_um2")}


def _violations(drc: dict[str, Any] | None) -> set[str]:
    if not drc or drc.get("available") is False:
        return set()
    return {row["id"] for row in drc.get("results") or [] if row["status"] == "violation"}


def _devices(netlist: dict[str, Any] | None) -> dict[str, int] | None:
    if not netlist or not netlist.get("available"):
        return None
    return dict((netlist.get("summary") or {}).get("device_classes") or {})


def _nets(connectivity: dict[str, Any] | None) -> int | None:
    nets = (connectivity or {}).get("nets") or {}
    if not nets.get("available"):
        return None
    return (nets.get("summary") or {}).get("net_count")


def _fmt_pair(values, unit: str = "", digits: int = 4) -> str:
    first, second = values
    if first is None or second is None:
        return "unavailable"
    if first == second:
        return f"identical at {first:g}{unit}"
    change = second - first
    share = (change / first * 100) if first else 0.0
    return (f"{first:g}{unit} → {second:g}{unit} "
            f"({change:+.{digits}g}{unit}, {share:+.1f}%)")


def _parasitic_answer(context, question: str, name_a: str, name_b: str) -> str | None:
    """R and C: the measured drivers, and the constants that are missing.

    The question behind "which has more capacitance?" is answerable from geometry
    whenever the drivers agree, and that is what an engineer reading a layout is
    doing. What they cannot do by eye - and what this will not do either - is turn it
    into farads without the process file.
    """
    geometry_a = (context.get("a") or {}).get("parasitics")
    geometry_b = (context.get("b") or {}).get("parasitics")
    if not geometry_a or not geometry_b:
        return None

    from analyzer.parasitics import compare_geometry

    verdict = compare_geometry(geometry_a, geometry_b)
    drivers = verdict["drivers"]
    lines = [
        f"Measured drivers, `{name_a}` → `{name_b}`:",
        f"- total wire length {_fmt_pair(drivers['wire_length_um'], ' µm')}",
        f"- metal area {_fmt_pair(drivers['metal_area_um2'], ' µm²')}",
        f"- coupling run within "
        f"{geometry_a.get('coupling_window_nm', 100):g} nm "
        f"{_fmt_pair(drivers['coupling_run_um'], ' µm')}",
        f"- vias {_fmt_pair(drivers['via_count'])}",
        "",
        # Already sentence-cased by the analyzer. `str.capitalize()` would lowercase
        # everything after the first letter and turn the file names into noise.
        verdict["resistance"],
        verdict["capacitance"],
    ]

    estimate_a = (context.get("a") or {}).get("rc")
    estimate_b = (context.get("b") or {}).get("rc")
    if estimate_a and estimate_b and estimate_a.get("available"):
        totals_a, totals_b = estimate_a["totals"], estimate_b["totals"]
        lines += [
            "",
            f"With the supplied process file: resistance "
            f"{_fmt_pair([totals_a['resistance_ohm'], totals_b['resistance_ohm']], ' Ω')}, "
            f"capacitance "
            f"{_fmt_pair([totals_a['capacitance_fF'], totals_b['capacitance_fF']], ' fF')}.",
            "Lumped per layer, not a distributed network - a delay needs an extractor "
            "and a timer, not a layout.",
        ]
        if estimate_a.get("unpriced_layers"):
            lines.append("Not priced by that file, so not counted: "
                         + ", ".join(estimate_a["unpriced_layers"]) + ".")
    else:
        lines += [
            "",
            "Ohms and farads need the process constants, and none were supplied: "
            "R = ρ·L/(W·T) needs resistivity and thickness, C needs permittivity and "
            "the dielectric height. Those live in an ITF or technology file, not in a "
            "GDSII. Load one under More tools → Parasitics and the same geometry "
            "becomes ohms and farads.",
        ]
    return "\n".join(lines)


def _label_positions(meta_a, meta_b, name_a: str, name_b: str) -> str:
    """Which labels moved, appeared or disappeared, by exact coordinate.

    Labels are where a pin's name lives, so this is the closest a geometry tool gets
    to "did a pin move" without a LEF. It says which it is, so the answer is not
    mistaken for a statement about the pin *shapes*.
    """
    labels_a, labels_b = _labels_of(meta_a), _labels_of(meta_b)
    if not labels_a and not labels_b:
        return ("No labels were read for either layout, so pin positions cannot be "
                "compared. Pin positions come from the label layers.")
    moved, gone, added = [], [], []
    for text, places in labels_a.items():
        if text not in labels_b:
            gone.append(text)
        elif sorted(places) != sorted(labels_b[text]):
            moved.append(f"{text} {sorted(places)} → {sorted(labels_b[text])}")
    for text in labels_b:
        if text not in labels_a:
            added.append(text)
    if not moved and not gone and not added:
        return (f"No pin moved: every label in `{name_a}` appears in `{name_b}` at the "
                "same coordinates. Whether a *pin shape* moved is a separate question - "
                "ask about the pin layers.")
    parts = []
    if moved:
        parts.append("moved: " + "; ".join(moved))
    if gone:
        parts.append(f"only in {name_a}: {', '.join(gone)}")
    if added:
        parts.append(f"only in {name_b}: {', '.join(added)}")
    return ("Label positions — " + "; ".join(parts) +
            ". These are the texts; whether a pin *shape* moved is a question about "
            "the pin layers.")


def answer_pair(context: dict[str, Any], question: str,
                about_the_pair: bool = True) -> str | None:
    """Answer one question about the pair, or return None to let the model try.

    `context` carries what the page has already computed for both files: the XOR, the
    two metadata blocks and, when they were run, the rule results, netlists and
    connectivity. Nothing here reads a file.

    `about_the_pair` is what the caller decided the question was asking. It is only
    consulted where the phrasing of an answer depends on it - a refusal offers the
    measured difference when the question was comparative, and stops at the reason
    when it was about one layout.
    """
    q = (question or "").lower().strip()
    if not q:
        return None

    xor = context.get("xor") or {}
    a, b = context.get("a") or {}, context.get("b") or {}
    name_a = a.get("file") or "A"
    name_b = b.get("file") or "B"
    meta_a, meta_b = a.get("metadata") or {}, b.get("metadata") or {}

    # --- resistance and capacitance -------------------------------------------
    # Answered before the refusals, because "which has more capacitance?" is not the
    # same question as "which is better": the drivers of R and C are in the file even
    # though the constants are not. What must not happen is an invented ohm.
    if re.search(r"\b(resistan\w*|capacitan\w*|\brc\b|parasitic\w*|coupling|"
                 r"cross[- ]?talk|ir[- ]?drop|electromigration|\bem\b|impedance|"
                 r"wire (?:length|load)|net length|loading)\b", q):
        reply = _parasitic_answer(context, q, name_a, name_b)
        if reply:
            return reply

    # --- refusals first, so a judgement question never picks up a factual branch ---
    # A refusal still offers what *is* measured, or it is just a wall. The exception
    # is a question about one layout that reached here only because nothing else
    # claimed it: "what is the timing of this cell?" gets the reason and nothing
    # else, because the pair's XOR area is not among that cell's measurements.
    for pattern, subject, reason in JUDGEMENT:
        if re.search(pattern, q):
            head = f"I cannot tell you {subject} from these two files. {reason}"
            if about_the_pair and xor.get("comparable") \
                    and not (xor.get("summary") or {}).get("identical"):
                s = xor["summary"]
                head += (f" What is measured: {s['layers_changed']} of "
                         f"{s['layers_compared']} layers differ, "
                         f"{_fmt_um2(s['total_xor_area_um2'])} of XOR area across "
                         f"{s['difference_regions']} region(s).")
            return head

    if not xor.get("comparable", True):
        return (f"These two layouts cannot be compared: {xor.get('reason', 'unknown')}. "
                "Nothing below would mean anything until that is resolved.")

    summary = xor.get("summary") or {}
    changed = [r for r in xor.get("layers") or [] if not r["identical"]]

    # --- cell size ------------------------------------------------------------
    if re.search(r"\b(cell (?:size|area|height|width)|size of the cell|bounding box|"
                 r"boundary|footprint|outline of the cell)\b|"
                 r"\bcell\b[^.?]*\b(bigger|smaller|larger|grew|grown|shrunk|shrank|"
                 r"taller|shorter|wider|narrower)\b", q):
        box_a, box_b = _cell_box(meta_a), _cell_box(meta_b)
        if box_a["width"] is None or box_b["width"] is None:
            return None
        same = (box_a == box_b)
        lead = ("The cell outline is identical in both." if same
                else "The cell outline differs.")
        return (f"{lead} Width {_delta(box_a['width'], box_b['width'], ' µm')}; "
                f"height {_delta(box_a['height'], box_b['height'], ' µm')}; "
                f"bounding-box area {_delta(box_a['area'], box_b['area'], ' µm²', 9)}. "
                "This is the drawn extent, not a placement site: the site a placer uses "
                "comes from the library, not from the GDSII.")

    # --- counts ---------------------------------------------------------------
    if re.search(r"\b(polygons?|shapes?) count\b|how many (?:polygons|shapes)|"
                 r"\b(polygons?|shapes?)\b[^.?]*\b(change|changed|differ|more|fewer|"
                 r"added|removed|same)\b|\b(more|fewer)\b[^.?]*\b(polygons?|shapes?)\b", q):
        pa = (meta_a.get("design") or {}).get("polygon_count")
        pb = (meta_b.get("design") or {}).get("polygon_count")
        return (f"Polygons: {_delta(pa, pb)}. A polygon count is a drawing-style "
                "measure, not a design one - the same shape split in two counts twice.")

    if re.search(r"\bvias?\b|\bcontacts?\b", q):
        va, vb = _via_count(meta_a), _via_count(meta_b)
        if va is None or vb is None:
            return None
        via_layers = [r["name"] for r in changed
                      if (r.get("role") or "") in ("via", "contact")]
        tail = (f" The via layers that differ: {', '.join(via_layers)}."
                if via_layers else " No via layer differs.")
        # Contacts are a separate count everywhere else on the page, so they are a
        # separate count here. One number covering both would not match any table.
        ca, cb = _contact_count(meta_a), _contact_count(meta_b)
        if ca is not None and cb is not None:
            tail += f" Contacts, counted separately: {_delta(ca, cb)}."
        return f"Vias: {_delta(va, vb)}.{tail}"

    if re.search(r"\b(labels?|texts?|pin names?|net names?)\b[^.?]*"
                 r"\b(change|changed|differ|same|move|renam\w*|new|added|removed|gone)\b|"
                 r"\b(change|changed|differ|renam\w*|new)\w*\b[^.?]*"
                 r"\b(labels?|texts?|pin names?|net names?)\b", q):
        rows = [r for r in xor.get("layers") or []
                if r.get("texts_added") or r.get("texts_removed")]
        if not rows:
            return ("No label differs between the two layouts: every text, on every "
                    "layer, appears in both.")
        return ("Label changes: " + "; ".join(
            f"`{r['name']}` adds {r.get('texts_added') or 'none'}, removes "
            f"{r.get('texts_removed') or 'none'}" for r in rows) + ".")

    # --- pins ------------------------------------------------------------------
    if (re.search(r"\b(pins?|ports?|terminals?)\b", q)
            and re.search(r"\bmove\w*|\bshift\w*|\brelocat\w*|position|location|place\w*\b", q)):
        return _label_positions(meta_a, meta_b, name_a, name_b)

    # --- layer sets ------------------------------------------------------------
    if re.search(r"\b(layer)s?\b.*\b(only in|new|extra|missing|added|removed|same set)\b|"
                 r"\bdoes b use\b|\bnew layer", q):
        set_a, set_b = _layers_of(meta_a), _layers_of(meta_b)
        only_a = sorted(set(set_a) - set(set_b))
        only_b = sorted(set(set_b) - set(set_a))
        if not only_a and not only_b:
            return (f"Both layouts use the same {len(set_a)} layers. "
                    f"{summary.get('layers_changed', 0)} of them differ in geometry.")
        return ("Layer sets differ. "
                + (f"Only in `{name_a}`: {', '.join(only_a)}. " if only_a else "")
                + (f"Only in `{name_b}`: {', '.join(only_b)}." if only_b else ""))

    # --- devices ---------------------------------------------------------------
    if re.search(r"\b(transistors?|devices?|mosfets?|nmos|pmos|fets?)\b", q):
        dev_a, dev_b = _devices(a.get("netlist")), _devices(b.get("netlist"))
        if dev_a is None or dev_b is None:
            return ("Device counts need the netlist extraction, which needs the "
                    "connection stack and a device recipe. Open the Netlist tool on "
                    "each layout to run it; without it this tool sees polygons, not "
                    "transistors.")
        if dev_a == dev_b:
            total = sum(dev_a.values())
            return (f"The device count is unchanged: {total} "
                    f"({', '.join(f'{k} × {v}' for k, v in sorted(dev_a.items()))}) in "
                    "both. Same count is not same circuit - that is what LVS settles.")
        kinds = sorted(set(dev_a) | set(dev_b))
        return ("Device counts differ: " + "; ".join(
            f"{kind} {dev_a.get(kind, 0)} → {dev_b.get(kind, 0)}" for kind in kinds) + ".")

    # --- nets ------------------------------------------------------------------
    if re.search(r"\b(net|connectivit|short|open|island|electrically)\w*\b", q):
        nets_a, nets_b = _nets(a.get("connectivity")), _nets(b.get("connectivity"))
        if nets_a is None or nets_b is None:
            return ("The net graph needs a connection stack - GDSII stores no layer "
                    "elevations, so which via bridges which layers has to be stated. "
                    "Without it neither layout has a net count to compare.")
        lead = (f"Both extract {nets_a} physical nets." if nets_a == nets_b
                else f"Net counts differ: {nets_a} → {nets_b}.")
        return (lead + " That is physical connectivity from the stated stack. Whether "
                "the two are the *same* circuit - nets matched one to one - is LVS, and "
                "needs a schematic for each.")

    # --- rule results ----------------------------------------------------------
    if re.search(r"\b(drc|rules?|violations?|clean|legal|design rules?)\b", q):
        drc_a, drc_b = a.get("drc"), b.get("drc")
        if not drc_a or not drc_b or drc_a.get("available") is False:
            return ("No rule results are available for both layouts. The design rule "
                    "catalogue supplies the rules; without it this tool measures "
                    "geometry but cannot say whether a measurement is legal.")
        va, vb = _violations(drc_a), _violations(drc_b)
        introduced = sorted(vb - va)
        fixed = sorted(va - vb)
        both = sorted(va & vb)
        parts = [f"`{name_a}` fails {len(va)} of the {drc_a['summary']['rules_checked']} "
                 f"checked rules, `{name_b}` fails {len(vb)}."]
        if introduced:
            parts.append(f"New in `{name_b}`: {', '.join(introduced)}.")
        if fixed:
            parts.append(f"Fixed in `{name_b}`: {', '.join(fixed)}.")
        if both:
            parts.append(f"In both: {', '.join(both)}.")
        if not introduced and not fixed:
            parts.append("The same rules pass and fail in both.")
        parts.append(f"Only the {drc_a['summary']['rules_checked']} checkable rules were "
                     f"run, out of {drc_a['summary']['rules_in_manual']} in the manual.")
        return " ".join(parts)

    # --- grid ------------------------------------------------------------------
    if re.search(r"\b(on[- ]grid|off[- ]grid|grid|snapped|manufacturing grid)\b", q):
        grid_a = (a.get("grid") or {})
        grid_b = (b.get("grid") or {})
        if not grid_a or not grid_b:
            return ("Nothing has measured the grid for both layouts yet. The database "
                    "unit is in each file; whether a vertex sits on the *design* grid "
                    "depends on which grid the process uses, so it has to be stated.")
        return (f"Off the {grid_a.get('grid_nm')} nm grid: `{name_a}` "
                f"{grid_a.get('shapes', 0)} shape(s), `{name_b}` "
                f"{grid_b.get('shapes', 0)}.")

    # --- pitch and tracks ------------------------------------------------------
    if re.search(r"\b(pitch|cpp|cgp|track|row height)\b", q):
        pitch_a = (meta_a.get("pitch") or {})
        pitch_b = (meta_b.get("pitch") or {})
        if not pitch_a or not pitch_b:
            return None
        cpp_a = (pitch_a.get("gate_pitch") or {}).get("cpp_nm")
        cpp_b = (pitch_b.get("gate_pitch") or {}).get("cpp_nm")
        rows = [f"gate pitch (CPP) {_delta(cpp_a, cpp_b, ' nm')}"]
        for metal in ("M0", "M1", "M2"):
            ma = (pitch_a.get("metal_pitches") or {}).get(metal) or {}
            mb = (pitch_b.get("metal_pitches") or {}).get(metal) or {}
            if ma.get("pitch_nm") or mb.get("pitch_nm"):
                rows.append(f"{metal} pitch {_delta(ma.get('pitch_nm'), mb.get('pitch_nm'), ' nm')}")
        return "Pitch: " + "; ".join(rows) + "."

    # --- density ---------------------------------------------------------------
    if re.search(r"\b(dens\w*|coverage|utilisation|utilization|fill)\b", q):
        rows = []
        by_name_b = {r.get("name"): r for r in _layer_rows(meta_b)}
        for row_a in _layer_rows(meta_a):
            name = row_a.get("name")
            row_b = by_name_b.get(name)
            if not row_b:
                continue
            da = row_a.get("density_percent")
            db = row_b.get("density_percent")
            if da is None:
                da, db = row_a.get("area_um2"), (row_b or {}).get("area_um2")
            if da is None or db is None or da == db:
                continue
            unit = "%" if row_a.get("density_percent") is not None else " µm²"
            rows.append(f"`{name}` {da:g}{unit} → {db:g}{unit}")
        if not rows:
            return ("No layer's density differs between the two, on the densities "
                    "measured here (drawn area over the cell's bounding box).")
        return ("Density differs on " + "; ".join(rows[:8]) +
                ". These are drawn-area densities over the cell box, not the windowed "
                "densities a fill rule uses - the Density map tool does those.")

    # --- technology ------------------------------------------------------------
    if re.search(r"\b(technolog|process|node|gaa|finfet|cfet|same tech)\w*\b", q):
        tech_a = ((meta_a.get("classification") or {}).get("technology") or {}).get("technology")
        tech_b = ((meta_b.get("classification") or {}).get("technology") or {}).get("technology")
        if not tech_a or not tech_b:
            return None
        if tech_a == tech_b:
            return (f"Both read as {tech_a}, inferred from the geometry - the diffusion "
                    "arrangement and the absence of a well layer. A GDSII does not state "
                    "its technology; this is a reading of what is drawn.")
        return (f"They read differently: `{name_a}` as {tech_a}, `{name_b}` as {tech_b}. "
                "Both are inferred from geometry, so check the technology data before "
                "acting on it.")

    # --- masks, ECO, respin ----------------------------------------------------
    if re.search(r"\b(masks?|respin|re-?spin|eco|metal[- ]only|base layers?|"
                 r"front[- ]?end|back[- ]?end|which layers changed|affected)\b", q):
        impact = xor.get("mask_impact") or {}
        keys = ", ".join(f"{r['name']} ({r['layer']}/{r['datatype']})" for r in changed) \
            or "none"
        return (f"{impact.get('observation', '')} The layers that differ, with their "
                f"numbers: {keys}. Each of those is a mask that would have to change. "
                f"{impact.get('caveat', '')}")

    # --- where -----------------------------------------------------------------
    # This locates the *differences*, so it only claims the question when the
    # question is about them. "Where is the widest metal?" is about one layout, and
    # answering it with a ranked list of XOR regions is a different question's answer.
    if re.search(r"\bwhere\b|\blocation\w*|\bcoordinat\w*|\bwhich part|\bnavigat\w*|"
                 r"\bhot ?spot|\bcorner\b|\bwhereabouts\b|\bshow me\b", q) \
            and (about_the_pair or re.search(r"\bdiff\w*|\bchang\w*|\bxor\b", q)):
        ranked = sorted(((loc["area_um2"], r["name"], loc) for r in changed
                         for loc in r["xor"]["locations"]),
                        key=lambda t: (-t[0], t[1]))[:6]
        if not ranked:
            return "There are no differences to locate: the two layouts are identical."
        return ("The largest differences, biggest first: "
                + "; ".join(f"`{name}` at {loc['centre_um']} µm "
                            f"({loc['width_um']} × {loc['height_um']} µm, {area:g} µm²)"
                            for area, name, loc in ranked)
                + f". In total {summary['difference_regions']} difference region(s) "
                  f"across {summary['layers_changed']} layer(s).")

    # --- magnitude -------------------------------------------------------------
    if re.search(r"\bhow much\b|\barea\b|\bmagnitude\b|\bsize of the (?:change|diff)|"
                 r"\bhow big\b|\bhow many (?:regions|differences)|\blargest\b|"
                 r"\bbiggest\b|\bworst\b", q):
        return (f"The total XOR area is {_fmt_um2(summary['total_xor_area_um2'])} across "
                f"{summary['difference_regions']} region(s): "
                f"{_fmt_um2(summary['total_area_removed_um2'])} present in `{name_a}` and "
                f"gone from `{name_b}`, and {_fmt_um2(summary['total_area_added_um2'])} "
                f"new in `{name_b}`. The largest single difference is "
                f"{_fmt_um2(summary['largest_single_difference_um2'])} on "
                f"`{summary['largest_difference_on_layer']}`.")

    if re.search(r"\bmov(?:e|ed|ing)\b|\bshift(?:ed|s)?\b", q):
        return _label_positions(meta_a, meta_b, name_a, name_b)

    # --- the general question --------------------------------------------------
    if re.search(r"\bwhat (?:changed|is different|differs)\b|\bcompare\b|\bcomparison\b|"
                 r"\bsummar\w*|\bdifferences?\b|\boverview\b|\breview\b", q):
        if summary.get("identical"):
            return (f"`{name_a}` and `{name_b}` are geometrically identical on all "
                    f"{summary['layers_compared']} layers - the XOR is empty everywhere.")
        parts = [f"{summary['layers_changed']} of {summary['layers_compared']} layers "
                 f"differ, in {summary['difference_regions']} region(s) totalling "
                 f"{_fmt_um2(summary['total_xor_area_um2'])}."]
        parts.append("Per layer: " + "; ".join(
            f"`{r['name']}` {r['xor']['count']} region(s), {_fmt_um2(r['xor']['area_um2'])}"
            + (" (new in B)" if not r["present_in_a"] else
               " (gone from B)" if not r["present_in_b"] else "")
            for r in sorted(changed, key=lambda r: -r["xor"]["area_um2"])[:8]) + ".")
        parts.append((xor.get("mask_impact") or {}).get("observation", ""))
        parts.append("An XOR difference is a difference, not an error - whether each was "
                     "intended needs the design intent.")
        return " ".join(p for p in parts if p)

    # Nothing here answers it. Saying so is the point: the alternative is handing back
    # the general summary and letting it read as an answer to whatever was asked.
    return None
