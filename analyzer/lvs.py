"""LVS: the extracted layout netlist against a schematic netlist.

Layout versus schematic needs both halves. The layout half is extracted here the same
way `netlist.py` does it - same stack, same device recipe - and the schematic half has
to be supplied by the user as a SPICE or CDL netlist. There is no way around that: a
`.gds` contains no schematic, and a tool that produced an LVS verdict without one
would be inventing the thing it claims to check.

What comes back is a cross-reference, not a verdict alone: which circuits, devices,
nets and pins matched, and which did not. A bare "LVS failed" is not a review.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .netlist import build, default_recipe, join_same_named_nets

# KLayout's cross-reference statuses, in words a reader can act on.
_STATUS = {
    "Match": "match",
    "MatchWithWarning": "match with warning",
    "NoMatch": "no match",
    "Mismatch": "mismatch",
    "Skipped": "skipped",
    "None": "not compared",
    "None_": "not compared",
}


def _status_name(value) -> str:
    text = str(value).split(".")[-1].strip()
    return _STATUS.get(text, text.lower())


def _call(owner, attribute):
    """Read a KLayout accessor that may be a method or a property.

    The cross-reference data classes expose `first`, `second` and `status` as
    methods; most of the netlist classes expose their names the same way. Guessing
    wrong prints `<built-in method first of ...>` into a report, which is how a
    comparison ends up looking like it ran and saying nothing.
    """
    value = getattr(owner, attribute, None)
    if callable(value):
        try:
            return value()
        except Exception:                            # pragma: no cover - defensive
            return None
    return value


def _pair_name(item) -> str:
    if item is None:
        return "—"
    for attribute in ("expanded_name", "name"):
        value = _call(item, attribute)
        if isinstance(value, str) and value:
            return value
    return str(item)


def relax_parameters(netlist, relative_tolerance: float = 0.01,
                     compared: tuple[str, ...] = ("L", "W")) -> list[str]:
    """Decide which device parameters the comparison is allowed to care about.

    An extracted transistor carries six parameters; a schematic states two. AS, AD,
    PS and PD are areas and perimeters measured off the layout, and no schematic has
    them - comparing them makes every device mismatch and the whole comparison
    useless. Real rule decks say the same thing: compare L and W with a tolerance,
    ignore the geometry-derived rest.
    """
    import klayout.db as db

    notes = []
    for device_class in netlist.each_device_class():
        ids = {p.name: device_class.parameter_id(p.name)
               for p in device_class.parameter_definitions()}
        comparer = None
        ignored, checked = [], []
        for name, pid in ids.items():
            if name in compared:
                part = db.EqualDeviceParameters(pid, 0.0, relative_tolerance)
                checked.append(name)
            else:
                part = db.EqualDeviceParameters.ignore(pid)
                ignored.append(name)
            comparer = part if comparer is None else comparer + part
        if comparer is not None:
            device_class.equal_parameters = comparer
            notes.append(f"{device_class.name}: comparing {', '.join(sorted(checked)) or 'nothing'}"
                         f" to {relative_tolerance:.0%}, ignoring "
                         f"{', '.join(sorted(ignored)) or 'nothing'}")
    return notes


def read_schematic(path: str | Path) -> Any:
    """Parse a SPICE or CDL netlist into a KLayout netlist object."""
    import klayout.db as db

    netlist = db.Netlist()
    netlist.read(str(path), db.NetlistSpiceReader())
    return netlist


def schematic_summary(netlist) -> dict[str, Any]:
    """What the supplied schematic contains, so a mismatch can be read in context."""
    circuits = []
    for circuit in netlist.each_circuit():
        devices = list(circuit.each_device())
        by_class: dict[str, int] = {}
        for device in devices:
            name = device.device_class().name
            by_class[name] = by_class.get(name, 0) + 1
        circuits.append({
            "name": circuit.name,
            "device_count": len(devices),
            "device_classes": by_class,
            "net_count": len(list(circuit.each_net())),
            "pins": [pin.name() or f"pin{pin.id()}" for pin in circuit.each_pin()],
        })
    return {"circuits": circuits,
            "circuit_count": len(circuits),
            "device_count": sum(c["device_count"] for c in circuits)}


def compare(gds_path: str | Path, layermap: dict[str, Any] | None,
            stack: dict[str, Any] | None, schematic: str | Path,
            recipe: dict[str, Any] | None = None,
            max_depth: int | None = None,
            max_branch_complexity: int | None = None,
            tolerance: float = 0.01) -> dict[str, Any]:
    """Run LVS and return the cross-reference.

    The comparison is KLayout's own `NetlistComparer`, driven through
    `LayoutVsSchematic`, so this is the same engine the standalone tool uses rather
    than a lookalike written here.
    """
    import klayout.db as db

    if not stack or not (stack.get("proposals") or stack.get("rules")):
        return {"available": False,
                "reason": ("LVS needs the connection stack, which a .gds and a .lyp "
                           "cannot supply - GDSII stores no layer elevations")}
    recipe = recipe or default_recipe(layermap, stack)
    if not (recipe.get("devices") or []):
        return {"available": False,
                "reason": ("LVS needs a device recipe naming the diffusion and gate "
                           "layers; the layer map alone does not settle which is which")}

    schematic_path = Path(schematic)
    if not schematic_path.exists():
        return {"available": False, "reason": f"no schematic netlist at {schematic_path}"}

    try:
        reference = read_schematic(schematic_path)
    except Exception as exc:
        return {"available": False,
                "reason": f"the schematic netlist could not be parsed: {exc}"}

    engine, layout, regions, devices, used, recipe = build(
        gds_path, layermap, stack, recipe, lvs=True)
    engine.extract_netlist()
    extracted = engine.netlist()
    # Ports first: a comparison anchors on pins, and an extracted netlist has none
    # until the named nets are turned into them.
    joined = join_same_named_nets(extracted)
    join_same_named_nets(reference)
    extracted.make_top_level_pins()
    extracted.combine_devices()
    extracted.purge()
    reference.make_top_level_pins()
    reference.combine_devices()
    reference.purge()
    parameter_notes = relax_parameters(extracted, tolerance)
    parameter_notes += relax_parameters(reference, tolerance)
    engine.reference = reference

    comparer = db.NetlistComparer()
    if max_depth:
        comparer.max_depth = int(max_depth)
    if max_branch_complexity:
        comparer.max_branch_complexity = int(max_branch_complexity)

    matched = bool(engine.compare(comparer))
    # `xref` is a method on LayoutVsSchematic, not a property.
    xref = engine.xref()

    circuits: list[dict[str, Any]] = []
    totals = {"devices": {"match": 0, "other": 0}, "nets": {"match": 0, "other": 0},
              "pins": {"match": 0, "other": 0}}

    if xref is not None:
        for circuit_pair in xref.each_circuit_pair():
            status = _status_name(_call(circuit_pair, 'status'))
            entry = {
                "layout": _pair_name(_call(circuit_pair, 'first')),
                "schematic": _pair_name(_call(circuit_pair, 'second')),
                "status": status,
                "devices": [], "nets": [], "pins": [],
            }
            for device_pair in xref.each_device_pair(circuit_pair):
                state = _status_name(_call(device_pair, 'status'))
                entry["devices"].append({
                    "layout": _pair_name(_call(device_pair, 'first')),
                    "schematic": _pair_name(_call(device_pair, 'second')),
                    "status": state,
                })
                totals["devices"]["match" if state.startswith("match") else "other"] += 1
            for net_pair in xref.each_net_pair(circuit_pair):
                state = _status_name(_call(net_pair, 'status'))
                entry["nets"].append({
                    "layout": _pair_name(_call(net_pair, 'first')),
                    "schematic": _pair_name(_call(net_pair, 'second')),
                    "status": state,
                })
                totals["nets"]["match" if state.startswith("match") else "other"] += 1
            for pin_pair in xref.each_pin_pair(circuit_pair):
                state = _status_name(_call(pin_pair, 'status'))
                entry["pins"].append({
                    "layout": _pair_name(_call(pin_pair, 'first')),
                    "schematic": _pair_name(_call(pin_pair, 'second')),
                    "status": state,
                })
                totals["pins"]["match" if state.startswith("match") else "other"] += 1
            circuits.append(entry)

    problems = []
    for circuit in circuits:
        if not circuit["status"].startswith("match"):
            problems.append(f"circuit {circuit['layout']} / {circuit['schematic']}: "
                            f"{circuit['status']}")
        for kind in ("devices", "nets", "pins"):
            for row in circuit[kind]:
                if not row["status"].startswith("match"):
                    problems.append(
                        f"{kind[:-1]} {row['layout']} / {row['schematic']}: {row['status']}")

    return {
        "available": True,
        "matched": matched,
        "headline": ("the layout matches the schematic" if matched else
                     "the layout does not match the schematic"),
        "schematic": {"file": schematic_path.name, **schematic_summary(reference)},
        "recipe": recipe,
        "connections_used": used + joined,
        "parameter_comparison": parameter_notes,
        "circuits": circuits,
        "totals": totals,
        "problems": problems[:200],
        "problem_count": len(problems),
        "basis": ("KLayout's own netlist comparer, on a netlist extracted with the "
                  "stated connection stack and device recipe"),
        "not_derivable": {
            "device_models": ("A match here is topological and geometric. Whether the "
                              "transistor is the right *model* needs the model cards, "
                              "which are not in a GDSII or in a plain SPICE netlist."),
            "parameters": ("Parameter tolerances are not checked unless the schematic "
                           "states them; a W/L difference inside the comparer's "
                           "tolerance is reported as a match."),
        },
    }
