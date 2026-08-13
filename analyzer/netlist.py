"""Device-level netlist extraction: transistors, terminals, nets, SPICE.

This is the step above the net graph in `connectivity.py`. That one answers "which
shapes are electrically joined?"; this one answers "what devices does this layout
contain, and what are they connected to?" - which is what a netlist browser shows
and what LVS compares against a schematic.

Two things make it possible at all, and both come from outside the GDSII:

* **The connection stack.** GDSII stores no elevations, so which via bridges which
  two layers has to be stated. It is read from the same stack file the net graph
  uses, so a netlist and a net count can never disagree about what is connected.
* **The device recipe.** Which layer is the gate and which is the diffusion is a
  process fact, not a geometric one. A recipe is proposed from the layer roles and
  can be replaced wholesale by the user; the result always says which was used.

One detail matters more than it looks. The source/drain conductor is the diffusion
*with the gates removed* - `NDIFF - NPOLY`. Using raw diffusion shorts every device
on a shared active area into one net, which is exactly the failure the bundled stack
file warns about in its own comments.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from .connectivity import layer_roles

# Roles the layer map assigns that can act as one half of a MOS device.
_DIFFUSION_ROLES = ("diffusion", "active", "nanosheet", "fin")
_GATE_ROLES = ("gate", "poly")


def _safe(name: str) -> str:
    return re.sub(r"\W", "_", name)


def _name_index(layermap: dict[str, Any] | None) -> dict[str, tuple[int, int]]:
    return {entry["technology_name"]: key
            for key, entry in ((layermap or {}).get("by_key") or {}).items()
            if entry.get("technology_name")}


def default_recipe(layermap: dict[str, Any] | None,
                   stack: dict[str, Any] | None = None) -> dict[str, Any]:
    """Propose a device recipe from the layer map's roles.

    A proposal, never a fact: which diffusion belongs to which gate is a process
    statement. The pairing here is by the n/p prefix the technology already uses in
    its layer names, and every device it proposes says so.
    """
    roles = layer_roles(layermap, (stack or {}).get("role_overrides"))
    diffusion, gate = {}, {}
    for key, meta in roles.items():
        name = meta.get("name") or ""
        role = (meta.get("role") or "").lower()
        polarity = "n" if name.upper().startswith("N") else "p" if name.upper().startswith("P") else None
        if not polarity:
            continue
        if role in _DIFFUSION_ROLES:
            diffusion.setdefault(polarity, name)
        elif role in _GATE_ROLES:
            gate.setdefault(polarity, name)

    # What contacts the source and drain. The stack states which via bridges which
    # metals but says nothing about diffusion, because a net graph built without
    # device recognition never reaches it - so this is proposed here, named in the
    # recipe, and shown to the user rather than applied invisibly.
    contacts: dict[str, list[str]] = {"n": [], "p": []}
    for key, meta in roles.items():
        name = meta.get("name") or ""
        upper = name.upper()
        if "CON" not in upper or upper.endswith("-LABEL"):
            continue
        if upper.startswith("N"):
            contacts["n"].append(name)
        elif upper.startswith("P"):
            contacts["p"].append(name)

    devices = []
    for polarity, kind in (("n", "nmos"), ("p", "pmos")):
        if polarity in diffusion and polarity in gate:
            devices.append({
                "name": kind.upper(),
                "type": kind,
                "diffusion": diffusion[polarity],
                "gate": gate[polarity],
                "poly": gate[polarity],
                "contacts": sorted(contacts[polarity]),
                # A SPICE `M` element always has four nodes, so an extracted device
                # has to have a bulk terminal or it can never be compared against a
                # schematic. Which net the body ties to is a process statement, not
                # a geometric one - stated here, in the recipe, so it can be changed.
                "bulk_net": "VSS" if polarity == "n" else "VDD",
            })
    return {
        "devices": devices,
        "source": "proposed from the layer map roles",
        "basis": ("layers whose role is diffusion or gate, paired by the n/p prefix "
                  "in their technology names; the source/drain contact is the layer "
                  "of the same polarity whose name contains CON"),
        "confirmed": False,
    }


def _regions(l2n, layout, names: dict[str, tuple[int, int]], wanted: list[str]):
    """Hierarchical regions for the named layers, skipping ones the file lacks."""
    made: dict[str, Any] = {}
    for name in wanted:
        key = names.get(name)
        if key is None:
            continue
        index = layout.find_layer(key[0], key[1])
        if index is None:
            continue
        made[name] = l2n.make_polygon_layer(index, _safe(name))
    return made


def _text_regions(l2n, layout, names: dict[str, tuple[int, int]], wanted: list[str]):
    made: dict[str, Any] = {}
    for name in wanted:
        key = names.get(name)
        if key is None:
            continue
        index = layout.find_layer(key[0], key[1])
        if index is None:
            continue
        made[name] = l2n.make_text_layer(index, _safe(name) + "_text")
    return made


def _label_layers(layermap: dict[str, Any] | None) -> dict[str, str]:
    """Which label layer names a net on which conductor.

    `M0-LABEL` names nets on `M0`. The convention is the technology's own, so it is
    read off the layer names rather than configured: a label layer whose name is a
    conductor's name plus a LABEL suffix belongs to that conductor.
    """
    names = list(_name_index(layermap))
    out = {}
    for name in names:
        upper = name.upper()
        for suffix in ("-LABEL", "_LABEL", ".LABEL"):
            if upper.endswith(suffix):
                base = name[: -len(suffix)]
                if base in names:
                    out[name] = base
                break
    return out


def build(gds_path: str | Path, layermap: dict[str, Any] | None,
          stack: dict[str, Any], recipe: dict[str, Any] | None = None,
          lvs: bool = False):
    """Wire up KLayout's extractor: layers, devices, connections, labels.

    Shared by `extract` and by LVS, so a netlist and a comparison can never be built
    from two different sets of connections.
    """
    import klayout.db as db

    names = _name_index(layermap)
    layout = db.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        raise ValueError("GDS contains no top-level cell.")

    engine = (db.LayoutVsSchematic(db.RecursiveShapeIterator(layout, top, []))
              if lvs else db.LayoutToNetlist(db.RecursiveShapeIterator(layout, top, [])))

    recipe = recipe or default_recipe(layermap, stack)
    rules = [p for p in stack.get("proposals", stack.get("rules", []))
             if len(p.get("connects", [])) >= 2]
    same_level = stack.get("same_level") or []
    direct = stack.get("direct") or []          # stated overlap connections, if any

    wanted = set()
    for device in recipe.get("devices") or []:
        wanted.update({device["diffusion"], device["gate"], device.get("poly") or device["gate"]})
        wanted.update(device.get("contacts") or [])
    for rule in rules:
        wanted.add(rule.get("connector_name") or "")
        wanted.update(c.get("name") for c in rule["connects"])
    for pair in same_level:
        wanted.update(pair.get("names") or [])
    for pair in direct:
        wanted.update(pair if isinstance(pair, (list, tuple)) else [])
    wanted.discard("")
    wanted.discard(None)

    regions = _regions(engine, layout, names, sorted(wanted))
    labels = _text_regions(engine, layout, names, sorted(_label_layers(layermap)))

    # --- devices ---------------------------------------------------------------
    extracted = []
    globals_used: list[str] = []
    for device in recipe.get("devices") or []:
        diff = regions.get(device["diffusion"])
        poly = regions.get(device.get("poly") or device["gate"])
        gate_layer = regions.get(device["gate"])
        if diff is None or poly is None or gate_layer is None:
            continue
        gate = gate_layer & diff
        source_drain = diff - gate_layer
        if gate.is_empty():
            continue
        kind = str(device.get("type", "nmos")).lower()
        bulk_net = device.get("bulk_net")
        if bulk_net:
            # Four-terminal, because that is what a SPICE netlist has. The bulk layer
            # is empty and the terminal is tied to a global net: the body connection
            # is not drawn in this layout, it is a property of the process.
            bulk = engine.make_layer()
            engine.extract_devices(
                db.DeviceExtractorMOS4Transistor(device["name"]),
                # The extractor calls the bulk input layer "W", not "B" as its own
                # description says. Passing "B" raises "missing input layer W".
                {"SD": source_drain, "G": gate, "P": poly, "W": bulk})
            engine.connect_global(bulk, str(bulk_net))
            globals_used.append(f"{device['name']} bulk ↔ global net {bulk_net}")
        else:
            engine.extract_devices(db.DeviceExtractorMOS3Transistor(device["name"]),
                                   {"SD": source_drain, "G": gate, "P": poly})
        # The source/drain islands are the conductor, not the whole diffusion.
        regions[f"_sd_{device['name']}"] = source_drain
        extracted.append({"name": device["name"], "type": kind,
                          "diffusion": device["diffusion"], "gate": device["gate"],
                          "contacts": list(device.get("contacts") or []),
                          "bulk_net": bulk_net,
                          "gate_shapes": gate.count()})

    # --- connections -----------------------------------------------------------
    used: list[str] = list(globals_used)
    for name, region in regions.items():
        engine.connect(region)                       # intra-layer: touching is joined
    for device in extracted:
        sd = regions.get(f"_sd_{device['name']}")
        for contact_name in device.get("contacts") or []:
            contact = regions.get(contact_name)
            if sd is not None and contact is not None:
                engine.connect(sd, contact)
                used.append(f"{device['diffusion']} source/drain ↔ {contact_name}")

    for rule in rules:
        connector = regions.get(rule.get("connector_name"))
        if connector is None:
            continue
        for target in rule["connects"]:
            other = regions.get(target.get("name"))
            if other is not None:
                engine.connect(connector, other)
                used.append(f"{rule['connector_name']} ↔ {target['name']}")
    for pair in same_level:
        first, second = (pair.get("names") or [None, None])[:2]
        if regions.get(first) is not None and regions.get(second) is not None:
            engine.connect(regions[first], regions[second])
            used.append(f"{first} ↔ {second} (one level under two names)")
    for pair in direct:
        first, second = (list(pair) + [None, None])[:2]
        if regions.get(first) is not None and regions.get(second) is not None:
            engine.connect(regions[first], regions[second])
            used.append(f"{first} ↔ {second} (stated direct contact)")

    for label_layer, conductor in _label_layers(layermap).items():
        if labels.get(label_layer) is not None and regions.get(conductor) is not None:
            engine.connect(regions[conductor], labels[label_layer])
            used.append(f"{conductor} named by {label_layer}")

    return engine, layout, regions, extracted, used, recipe


def join_same_named_nets(netlist) -> list[str]:
    """Merge nets that carry the same name inside one circuit.

    The body terminal is tied to a global net called VSS, and the power rail is a
    conductor labelled VSS. They are the same net - that is what the label means -
    but the extractor has no way to know it and produces `VSS` and `VSS$1`. Left
    alone, the layout then has two power nets and no schematic will ever match it.
    """
    joined = []
    for circuit in netlist.each_circuit():
        by_name: dict[str, Any] = {}
        for net in list(circuit.each_net()):
            name = net.name
            if not name:
                continue
            first = by_name.get(name)
            if first is None:
                by_name[name] = net
            else:
                circuit.join_nets(first, net)
                joined.append(f"{circuit.name}: joined two nets named {name}")
    return joined


def _device_row(device) -> dict[str, Any]:
    klass = device.device_class()
    terminals = {}
    for terminal in klass.terminal_definitions():
        net = device.net_for_terminal(terminal.id())
        terminals[terminal.name] = net.expanded_name() if net else None
    parameters = {}
    for parameter in klass.parameter_definitions():
        value = device.parameter(parameter.name)
        if value:
            parameters[parameter.name] = round(float(value), 6)
    return {"name": device.expanded_name(), "class": klass.name,
            "terminals": terminals, "parameters": parameters}


def extract(gds_path: str | Path, layermap: dict[str, Any] | None,
            stack: dict[str, Any] | None, recipe: dict[str, Any] | None = None,
            spice: bool = True) -> dict[str, Any]:
    """The layout's devices and nets, with the diagnostics that say how far to trust it."""
    import klayout.db as db

    if not stack or not (stack.get("proposals") or stack.get("rules")):
        return {"available": False,
                "reason": ("a netlist needs the connection stack, which a .gds and a "
                           ".lyp cannot supply - GDSII stores no layer elevations")}
    recipe = recipe or default_recipe(layermap, stack)
    if not (recipe.get("devices") or []):
        return {"available": False, "recipe": recipe,
                "reason": ("no device recipe: the layer map has no pair of diffusion "
                           "and gate layers to build a transistor from. Supply a "
                           "device recipe naming them.")}

    engine, layout, regions, devices, used, recipe = build(
        gds_path, layermap, stack, recipe)
    engine.extract_netlist()
    netlist = engine.netlist()
    # The three steps every KLayout LVS flow performs before comparing. Without the
    # first, a netlist has no ports and nothing anchors a comparison to a schematic:
    # every net is then "some net", and the comparer has to guess.
    joined = join_same_named_nets(netlist)
    netlist.make_top_level_pins()
    netlist.combine_devices()
    netlist.purge()

    circuits = []
    floating: list[dict[str, Any]] = []
    internal: list[str] = []
    unnamed_nets = 0
    device_count = 0
    by_class: dict[str, int] = {}

    for circuit in netlist.each_circuit():
        # An unnamed net joining two or more terminals is an ordinary internal node -
        # the series node of a NAND has no label and needs none. An unnamed net with
        # one terminal on it is a terminal that reaches nothing, which is worth
        # saying out loud.
        reach = {}
        for net in circuit.each_net():
            reach[net.expanded_name()] = (net.terminal_count(), net.pin_count())
        rows = []
        for device in circuit.each_device():
            row = _device_row(device)
            rows.append(row)
            device_count += 1
            by_class[row["class"]] = by_class.get(row["class"], 0) + 1
            for terminal, net in row["terminals"].items():
                if net is None:
                    floating.append({"device": row["name"], "class": row["class"],
                                     "terminal": terminal, "net": None,
                                     "reason": "the terminal is on no net at all"})
                elif net.startswith("$"):
                    terminals, pins = reach.get(net, (0, 0))
                    if terminals <= 1 and pins == 0:
                        floating.append({"device": row["name"], "class": row["class"],
                                         "terminal": terminal, "net": net,
                                         "reason": "nothing else is on this net"})
                    elif net not in internal:
                        internal.append(net)
        nets = []
        for net in circuit.each_net():
            name = net.expanded_name()
            named = not name.startswith("$")
            if not named:
                unnamed_nets += 1
            nets.append({"name": name, "named": named,
                         "terminals": net.terminal_count(), "pins": net.pin_count(),
                         "subcircuit_pins": net.subcircuit_pin_count()})
        circuits.append({
            "name": circuit.name,
            "devices": rows,
            "nets": nets,
            "pins": [pin.name() or f"pin{pin.id()}" for pin in circuit.each_pin()],
        })

    spice_text = None
    if spice:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "extracted.cir"
            writer = db.NetlistSpiceWriter()
            # Node numbers with the names in comments are unreadable and unusable as
            # a reference: a schematic written this way cannot be matched by name.
            writer.use_net_names = True
            writer.with_comments = True
            netlist.write(str(out), writer,
                          f"extracted from {Path(gds_path).name}")
            spice_text = out.read_text(encoding="utf-8")

    return {
        "available": True,
        "top_cell": layout.top_cell().name,
        "recipe": recipe,
        "devices_extracted": devices,
        "connections_used": used + joined,
        "circuits": circuits,
        "summary": {
            "circuit_count": len(circuits),
            "device_count": device_count,
            "device_classes": by_class,
            "net_count": sum(len(c["nets"]) for c in circuits),
            "named_net_count": sum(1 for c in circuits for n in c["nets"] if n["named"]),
            "unnamed_net_count": unnamed_nets,
        },
        "diagnostics": {
            # A terminal alone on its net is either an incomplete connection recipe
            # or an incomplete layout, and the tool cannot tell which - so it says
            # what it found and leaves the judgement where it belongs.
            "floating_terminals": floating,
            "internal_nodes": internal,
            "note": ("A floating terminal reaches nothing else in the layout. That is "
                     "either a connection the stack does not state or a connection "
                     "the layout does not make; deciding which needs the process "
                     "stack, not the file. Unnamed nets joining two or more terminals "
                     "are ordinary internal nodes and are listed separately."),
        },
        "spice": spice_text,
        "basis": ("devices from the recipe, connectivity from the stated stack, net "
                  "names from the technology's label layers"),
        "not_derivable": {
            "device_models": ("Which SPICE model a transistor uses is not in a GDSII. "
                              "The extracted device carries geometry (L, W, areas and "
                              "perimeters), not a model card."),
            "parasitics": ("Resistance and capacitance need a process stack with "
                           "sheet resistances and dielectric constants."),
        },
    }
