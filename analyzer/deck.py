"""Run a user-supplied design rule deck.

`drc.py` checks the rules of one manual that was transcribed into this repository.
This runs *any* deck the user supplies: a table of rules in JSON, executed with
KLayout's own region checks, with every violation carrying the place it happened so
it can be clicked in the viewer.

The deck is the input the user has to bring. Nothing here invents a limit: a rule
without a number is reported as unusable rather than assumed, and a rule naming a
layer the layout does not have is "not applicable", not "pass". Those two
distinctions are the whole difference between a checker and a rubber stamp.

Deck format (JSON):

    {"technology": "...",
     "rules": [
       {"id": "M1.W.1", "type": "width",     "layer": "M1", "min_nm": 30},
       {"id": "M1.S.1", "type": "space",     "layer": "M1", "min_nm": 30},
       {"id": "V0.A.1", "type": "area",      "layer": "VIA0", "min_nm2": 400},
       {"id": "M1.E.1", "type": "enclosure", "layer": "M1", "of": "VIA0", "min_nm": 5},
       {"id": "M1.O.1", "type": "overlap",   "layer": "M1", "with": "M0", "min_nm": 10},
       {"id": "M1.X.1", "type": "separation","layer": "M1", "from": "M0", "min_nm": 20},
       {"id": "NP.N.1", "type": "not_overlapping", "layer": "NPOLY", "with": "PDIFF"},
       {"id": "M1.I.1", "type": "inside",    "layer": "VIA0", "of": "M1"},
       {"id": "M1.D.1", "type": "density",   "layer": "M1", "min_pct": 20, "max_pct": 80,
        "window_nm": 1000},
       {"id": "M1.G.1", "type": "grid",      "layer": "M1", "grid_nm": 1}
     ]}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .density import tile_density

TYPES = ("width", "space", "notch", "area", "enclosure", "overlap", "separation",
         "inside", "not_overlapping", "density", "grid")

# How many violation locations to keep per rule. Enough to browse, not so many that
# a deck run on a full chip returns a hundred megabytes of coordinates.
MAX_LOCATIONS = 200


def load_deck(path: str | Path) -> dict[str, Any]:
    """Read and validate a deck. Raises with a usable message rather than a KeyError."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"rules": data}
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("the deck has no 'rules' list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {index} is not an object")
        if not rule.get("type"):
            raise ValueError(f"rule {rule.get('id', index)} has no 'type'")
        if str(rule["type"]) not in TYPES:
            raise ValueError(f"rule {rule.get('id', index)}: unknown type "
                             f"'{rule['type']}' (expected one of {', '.join(TYPES)})")
        if not rule.get("layer"):
            raise ValueError(f"rule {rule.get('id', index)} names no layer")
    return {"technology": data.get("technology"),
            "source": str(path),
            "rules": rules,
            "rule_count": len(rules)}


def _regions(gds_path, layermap, wanted):
    """Merged regions for the named layers, plus which names were missing."""
    import klayout.db as db

    names = {entry["technology_name"]: key
             for key, entry in ((layermap or {}).get("by_key") or {}).items()}
    layout = db.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise ValueError("GDS contains no top-level cell.")
    made: dict[str, Any] = {}
    for name in wanted:
        key = names.get(name)
        if key is None:
            continue
        index = layout.find_layer(key[0], key[1])
        if index is None:
            continue
        region = db.Region(top.begin_shapes_rec(index))
        region.merged_semantics = True
        made[name] = region
    return layout, top, made


def _edge_pairs_to_locations(pairs, dbu, limit=MAX_LOCATIONS):
    """Where each violation is, as a box, so the viewer can zoom to it."""
    out = []
    for pair in pairs.each():
        box = pair.bbox()
        out.append([round(box.left * dbu, 6), round(box.bottom * dbu, 6),
                    round(box.right * dbu, 6), round(box.top * dbu, 6)])
        if len(out) >= limit:
            break
    return out


def _polys_to_locations(region, dbu, limit=MAX_LOCATIONS):
    out = []
    for polygon in region.each():
        box = polygon.bbox()
        out.append([round(box.left * dbu, 6), round(box.bottom * dbu, 6),
                    round(box.right * dbu, 6), round(box.top * dbu, 6)])
        if len(out) >= limit:
            break
    return out


def _worst(pairs, dbu):
    """The smallest measured distance among the failures, in nanometres."""
    worst = None
    for pair in pairs.each():
        distance = pair.distance() * dbu * 1000
        if worst is None or distance < worst:
            worst = distance
    return None if worst is None else round(worst, 4)


def _run_rule(rule, regions, dbu, layout, top):
    """One rule. Returns (status, detail, count, locations, observed)."""
    import klayout.db as db

    kind = str(rule["type"])
    layer = rule["layer"]
    region = regions.get(layer)
    if region is None:
        return ("not applicable", f"{layer} is not in this layout", 0, [], {})

    def nm(value):
        return int(round(float(value) / 1000.0 / dbu))

    if kind in ("width", "space", "notch"):
        limit = rule.get("min_nm")
        if limit is None:
            return ("unusable", "no min_nm given", 0, [], {})
        check = {"width": region.width_check, "space": region.space_check,
                 "notch": region.notch_check}[kind]
        pairs = check(nm(limit))
        count = pairs.count()
        return (("violation" if count else "pass"),
                f"{count} place(s) below {limit} nm" if count
                else f"nothing below {limit} nm",
                count, _edge_pairs_to_locations(pairs, dbu),
                {"limit_nm": limit, "worst_nm": _worst(pairs, dbu)})

    if kind == "area":
        minimum = rule.get("min_nm2")
        maximum = rule.get("max_nm2")
        bad = db.Region()
        worst = None
        for polygon in region.each():
            area_nm2 = float(polygon.area()) * dbu * dbu * 1e6
            if (minimum is not None and area_nm2 < float(minimum)) or \
               (maximum is not None and area_nm2 > float(maximum)):
                bad.insert(polygon)
                if worst is None or area_nm2 < worst:
                    worst = area_nm2
        count = bad.count()
        return (("violation" if count else "pass"),
                f"{count} shape(s) outside the area limits" if count
                else "every shape is within the area limits",
                count, _polys_to_locations(bad, dbu),
                {"min_nm2": minimum, "max_nm2": maximum,
                 "worst_nm2": None if worst is None else round(worst, 4)})

    if kind in ("enclosure", "overlap", "separation"):
        other_name = rule.get("of") or rule.get("with") or rule.get("from")
        other = regions.get(other_name)
        if other is None:
            return ("not applicable", f"{other_name} is not in this layout", 0, [], {})
        limit = rule.get("min_nm")
        if limit is None:
            return ("unusable", "no min_nm given", 0, [], {})
        check = {"enclosure": region.enclosing_check,
                 "overlap": region.overlap_check,
                 "separation": region.separation_check}[kind]
        pairs = check(other, nm(limit))
        count = pairs.count()
        return (("violation" if count else "pass"),
                f"{count} place(s) below {limit} nm to {other_name}" if count
                else f"nothing below {limit} nm to {other_name}",
                count, _edge_pairs_to_locations(pairs, dbu),
                {"limit_nm": limit, "worst_nm": _worst(pairs, dbu), "against": other_name})

    if kind == "inside":
        other_name = rule.get("of") or rule.get("with")
        other = regions.get(other_name)
        if other is None:
            return ("not applicable", f"{other_name} is not in this layout", 0, [], {})
        outside = region - other
        count = outside.count()
        return (("violation" if count else "pass"),
                f"{count} shape(s) reach outside {other_name}" if count
                else f"every shape is inside {other_name}",
                count, _polys_to_locations(outside, dbu), {"against": other_name})

    if kind == "not_overlapping":
        other_name = rule.get("with") or rule.get("of")
        other = regions.get(other_name)
        if other is None:
            return ("not applicable", f"{other_name} is not in this layout", 0, [], {})
        both = region & other
        count = both.count()
        return (("violation" if count else "pass"),
                f"{count} place(s) overlap {other_name}" if count
                else f"nothing overlaps {other_name}",
                count, _polys_to_locations(both, dbu), {"against": other_name})

    if kind == "density":
        window = float(rule.get("window_nm") or 1000)
        result = tile_density(region, top.bbox(), dbu, window)
        if not result.get("available", True):
            return ("unusable", result["reason"], 0, [], {})
        low, high = rule.get("min_pct"), rule.get("max_pct")
        bad = [tile for tile in result["tiles"]
               if (low is not None and tile["pct"] < float(low))
               or (high is not None and tile["pct"] > float(high))]
        return (("violation" if bad else "pass"),
                f"{len(bad)} window(s) outside {low}–{high}%" if bad
                else f"every window is within {low}–{high}%",
                len(bad), [t["box"] for t in bad[:MAX_LOCATIONS]],
                {"min_pct": low, "max_pct": high, "window_nm": window,
                 "measured_min_pct": result["min_pct"],
                 "measured_max_pct": result["max_pct"]})

    if kind == "grid":
        step_nm = float(rule.get("grid_nm") or 1)
        step = step_nm / 1000.0 / dbu
        bad = db.Region()
        if step > 1.0000001:
            for polygon in region.each():
                for point in polygon.each_point_hull():
                    if point.x % step or point.y % step:
                        bad.insert(polygon)
                        break
        count = bad.count()
        return (("violation" if count else "pass"),
                f"{count} shape(s) have a vertex off the {step_nm:g} nm grid" if count
                else f"every vertex is on the {step_nm:g} nm grid",
                count, _polys_to_locations(bad, dbu), {"grid_nm": step_nm})

    return ("unusable", f"unknown rule type '{kind}'", 0, [], {})


def run(gds_path: str | Path, layermap: dict[str, Any] | None,
        deck: dict[str, Any]) -> dict[str, Any]:
    """Run every rule in the deck against the layout."""
    rules = deck.get("rules") or []
    wanted = set()
    for rule in rules:
        wanted.add(rule.get("layer"))
        for key in ("of", "with", "from"):
            if rule.get(key):
                wanted.add(rule[key])
    wanted.discard(None)

    layout, top, regions = _regions(gds_path, layermap, sorted(wanted))
    dbu = float(layout.dbu)

    results = []
    counts: dict[str, int] = {}
    for rule in rules:
        try:
            status, detail, count, locations, observed = _run_rule(
                rule, regions, dbu, layout, top)
        except Exception as exc:                     # a bad rule must not stop the deck
            status, detail, count, locations, observed = (
                "error", f"{type(exc).__name__}: {exc}", 0, [], {})
        counts[status] = counts.get(status, 0) + 1
        results.append({
            "id": rule.get("id") or f"rule{len(results) + 1}",
            "type": rule["type"],
            "text": rule.get("text") or rule.get("description") or "",
            "layers": [n for n in (rule.get("layer"), rule.get("of"), rule.get("with"),
                                   rule.get("from")) if n],
            "status": status,
            "detail": detail,
            "count": count,
            "locations": locations,
            "observed": observed,
        })

    order = {"violation": 0, "error": 1, "unusable": 2, "not applicable": 3, "pass": 4}
    results.sort(key=lambda r: (order.get(r["status"], 9), str(r["id"])))
    return {
        "available": True,
        "technology": deck.get("technology"),
        "deck_source": deck.get("source"),
        "top_cell": top.name,
        "dbu_um": dbu,
        "results": results,
        "summary": {
            "rules": len(results),
            "violation": counts.get("violation", 0),
            "pass": counts.get("pass", 0),
            "not applicable": counts.get("not applicable", 0),
            "unusable": counts.get("unusable", 0),
            "error": counts.get("error", 0),
            "violations_found": sum(r["count"] for r in results
                                    if r["status"] == "violation"),
        },
        "basis": "the supplied deck, run with KLayout's own region checks",
        "not_derivable": {
            "rules_not_in_the_deck": ("Only what the deck states is checked. A clean "
                                      "run means the layout passes these rules, not "
                                      "that it is DRC clean."),
        },
    }
