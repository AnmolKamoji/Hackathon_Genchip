"""Device extraction and LVS.

The two tests that matter most are at the ends of this file: a netlist compared
against itself must match, and a *correct* schematic compared against a layout that
is missing a connection must not. A comparer that cannot fail is not checking
anything, and one that cannot match is not usable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.connectivity import default_stack
from analyzer.layermap import default_layermap, load_lyp
from analyzer.lvs import compare, read_schematic, schematic_summary
from analyzer.netlist import default_recipe, extract

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
AND2 = SAMPLES / "AN2D1_2_RT_4.gds"
NOR2 = SAMPLES / "NR2D1_1_RT_4.gds"

# A correct AND2: a NAND2 into an inverter. Written by hand with net and device
# names that share nothing with the extractor's, so a match can only come from
# topology.
CORRECT_AND2 = """* AND2, drive 1
.SUBCKT AN2D1 A1 A2 Z VDD VSS
MN1 mid  A2   VSS VSS NMOS L=0.015U W=0.015U
MN2 nand A1   mid VSS NMOS L=0.015U W=0.015U
MP1 nand A1   VDD VDD PMOS L=0.015U W=0.015U
MP2 nand A2   VDD VDD PMOS L=0.015U W=0.015U
MN3 Z    nand VSS VSS NMOS L=0.015U W=0.015U
MP3 Z    nand VDD VDD PMOS L=0.015U W=0.015U
.ENDS
"""


@pytest.fixture(scope="module")
def lm():
    return load_lyp(default_layermap())


@pytest.fixture(scope="module")
def stack(lm):
    return default_stack(lm)


@pytest.fixture(scope="module")
def netlist(lm, stack):
    return extract(AND2, lm, stack)


# --- the recipe -------------------------------------------------------------

def test_the_recipe_is_proposed_and_says_so(lm, stack):
    recipe = default_recipe(lm, stack)
    assert recipe["confirmed"] is False
    assert "proposed" in recipe["source"]
    names = {d["name"] for d in recipe["devices"]}
    assert names == {"NMOS", "PMOS"}
    nmos = next(d for d in recipe["devices"] if d["name"] == "NMOS")
    assert nmos["diffusion"] == "NDIFF" and nmos["gate"] == "NPOLY"
    assert nmos["contacts"] == ["NDIFFCON"]
    # A SPICE M element has four nodes, so the body tie has to be stated somewhere.
    assert nmos["bulk_net"] == "VSS"


def test_extraction_needs_the_connection_stack(lm):
    result = extract(AND2, lm, None)
    assert result["available"] is False
    assert "elevations" in result["reason"]


def test_extraction_needs_a_device_recipe(lm, stack):
    result = extract(AND2, lm, stack, recipe={"devices": []})
    assert result["available"] is False
    assert "device recipe" in result["reason"]


# --- what comes out ---------------------------------------------------------

def test_an_and2_extracts_six_transistors(netlist):
    """AND2 is a NAND2 and an inverter: three n and three p."""
    assert netlist["available"] is True
    assert netlist["summary"]["device_count"] == 6
    assert netlist["summary"]["device_classes"] == {"NMOS": 3, "PMOS": 3}


def test_the_nor2_extracts_four_transistors(lm, stack):
    result = extract(NOR2, lm, stack)
    assert result["summary"]["device_classes"] == {"NMOS": 2, "PMOS": 2}


def test_the_source_drain_conductor_is_the_diffusion_minus_the_gates(netlist):
    """Raw diffusion would short every device on a shared active area into one net.

    Three NMOS in this cell are on one diffusion strip; if the strip were the
    conductor they would all share a single source/drain node.
    """
    circuit = netlist["circuits"][0]
    nmos = [d for d in circuit["devices"] if d["class"] == "NMOS"]
    nets = {t for d in nmos for t in d["terminals"].values() if t}
    assert len(nets) >= 4, "the devices share too few nets to be separate"


def test_net_names_come_from_the_technologys_label_layers(netlist):
    names = {n["name"] for c in netlist["circuits"] for n in c["nets"]}
    assert {"A1", "A2", "Z", "VDD", "VSS"} <= names


def test_device_geometry_is_measured_not_assumed(netlist):
    for circuit in netlist["circuits"]:
        for device in circuit["devices"]:
            assert device["parameters"]["L"] == pytest.approx(0.015)
            assert device["parameters"]["W"] == pytest.approx(0.015)


def test_the_spice_output_carries_names_and_a_port_list(netlist):
    spice = netlist["spice"]
    assert ".SUBCKT AN2D1" in spice
    for pin in ("A1", "A2", "Z", "VDD", "VSS"):
        assert pin in spice.split("\n")[spice.split("\n").index(
            next(l for l in spice.split("\n") if l.startswith(".SUBCKT")))]
    # Node names, not the numeric nodes the writer defaults to.
    assert "M$1" in spice and " 1 " not in spice.split(".SUBCKT")[1][:200]


def test_a_terminal_that_reaches_nothing_is_reported(netlist):
    """The one real defect in this cell: the inverter's pull-down drain."""
    floating = netlist["diagnostics"]["floating_terminals"]
    assert len(floating) == 1
    assert floating[0]["class"] == "NMOS"
    assert floating[0]["reason"] == "nothing else is on this net"


def test_ordinary_internal_nodes_are_not_called_floating(netlist):
    """The series node of a NAND has no label and needs none."""
    assert netlist["diagnostics"]["internal_nodes"], "the series node vanished"
    for node in netlist["diagnostics"]["internal_nodes"]:
        assert node not in {f["net"] for f in netlist["diagnostics"]["floating_terminals"]}


def test_the_connections_used_are_reported(netlist):
    used = " ".join(netlist["connections_used"])
    assert "N-VIAG ↔ NPOLY" in used
    assert "NDIFF source/drain ↔ NDIFFCON" in used
    assert "named by M0-LABEL" in used
    # The body tie is a process statement and has to appear as one.
    assert "bulk ↔ global net VSS" in used


def test_what_a_netlist_cannot_say_is_stated(netlist):
    assert "device_models" in netlist["not_derivable"]
    assert "parasitics" in netlist["not_derivable"]


# --- LVS --------------------------------------------------------------------

def test_lvs_needs_a_schematic(lm, stack, tmp_path):
    result = compare(AND2, lm, stack, tmp_path / "nothing.cir")
    assert result["available"] is False
    assert "no schematic" in result["reason"]


def test_lvs_needs_the_connection_stack(lm, tmp_path):
    schematic = tmp_path / "s.cir"
    schematic.write_text(CORRECT_AND2)
    result = compare(AND2, lm, None, schematic)
    assert result["available"] is False
    assert "elevations" in result["reason"]


def test_an_unparsable_schematic_is_refused_not_guessed(lm, stack, tmp_path):
    bad = tmp_path / "bad.cir"
    bad.write_text(".SUBCKT\nM1 this is not spice\n")
    result = compare(AND2, lm, stack, bad)
    assert result["available"] is False
    assert "could not be parsed" in result["reason"]


def test_a_netlist_matches_itself(lm, stack, tmp_path, netlist):
    """The floor: the extracted netlist written out and read back must match.

    If this fails, nothing else the comparison says means anything.
    """
    schematic = tmp_path / "self.cir"
    schematic.write_text(netlist["spice"])
    result = compare(AND2, lm, stack, schematic)
    assert result["matched"] is True
    assert result["totals"]["devices"]["other"] == 0
    assert result["totals"]["nets"]["other"] == 0
    assert result["totals"]["pins"]["other"] == 0
    assert result["problem_count"] == 0


def test_a_correct_schematic_finds_the_layouts_missing_connection(lm, stack, tmp_path):
    """The point of LVS.

    This layout's inverter pull-down drain reaches nothing - the netlist extraction
    flags it as a floating terminal. A correct AND2 schematic therefore must *not*
    match, and the mismatch has to land on that net rather than somewhere vague.
    """
    schematic = tmp_path / "and2.cir"
    schematic.write_text(CORRECT_AND2)
    result = compare(AND2, lm, stack, schematic)
    assert result["matched"] is False

    # Five of six devices still pair up, by topology, across completely different
    # names - so this is a real comparison, not a name check that failed.
    assert result["totals"]["devices"]["match"] == 5
    assert result["totals"]["pins"]["other"] == 0

    problems = " ".join(result["problems"])
    assert "Z" in problems


def test_the_comparison_says_which_parameters_it_compared(lm, stack, tmp_path):
    """An extracted device has six parameters and a schematic states two."""
    schematic = tmp_path / "and2.cir"
    schematic.write_text(CORRECT_AND2)
    result = compare(AND2, lm, stack, schematic)
    notes = " ".join(result["parameter_comparison"])
    assert "comparing L, W" in notes
    assert "ignoring AD, AS, PD, PS" in notes


def test_the_schematic_is_summarised_so_a_mismatch_has_context(lm, stack, tmp_path):
    schematic = tmp_path / "and2.cir"
    schematic.write_text(CORRECT_AND2)
    result = compare(AND2, lm, stack, schematic)
    assert result["schematic"]["file"] == "and2.cir"
    circuit = result["schematic"]["circuits"][0]
    assert circuit["device_count"] == 6
    assert circuit["device_classes"] == {"NMOS": 3, "PMOS": 3}
    assert set(circuit["pins"]) == {"A1", "A2", "Z", "VDD", "VSS"}


def test_reading_a_schematic_on_its_own(tmp_path):
    schematic = tmp_path / "and2.cir"
    schematic.write_text(CORRECT_AND2)
    summary = schematic_summary(read_schematic(schematic))
    assert summary["circuit_count"] == 1
    assert summary["device_count"] == 6


def test_what_lvs_cannot_settle_is_stated(lm, stack, tmp_path):
    schematic = tmp_path / "and2.cir"
    schematic.write_text(CORRECT_AND2)
    result = compare(AND2, lm, stack, schematic)
    assert "device_models" in result["not_derivable"]
    assert "parameters" in result["not_derivable"]
