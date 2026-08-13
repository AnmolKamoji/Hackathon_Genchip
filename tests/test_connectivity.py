"""Tests for physical connectivity analysis.

The theme running through these: the analysis must not overstate what a .gds and
a .lyp can support. Several tests exist specifically to fail if someone later
"improves" the module by turning measured overlap into asserted connection.
"""
from __future__ import annotations

import json
from pathlib import Path

import klayout.db as db
import pytest

from analyzer import connectivity as C
from analyzer.layermap import load_lyp
from analyzer.sidecar_parser import analyze_sidecar
from models.metadata import SchemaError, validate_connectivity

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
DCAP = SAMPLES / "DCAP0_1_RT_4.gds"
NR2D1 = SAMPLES / "NR2D1_1_RT_4.gds"
LYP = SAMPLES / "Titan_layer_properties.lyp"
STACK = SAMPLES / "Titan_stack.json"


@pytest.fixture(scope="module")
def lm():
    return load_lyp(LYP)


@pytest.fixture(scope="module")
def stack(lm):
    return C.load_stack(STACK, lm)


# --- role classification ----------------------------------------------------

@pytest.mark.parametrize("name,role", [
    ("M0", "metal"), ("M1", "metal"), ("BM0", "metal"),
    ("VIA0", "via"), ("VIA1", "via"), ("DVB", "via"),
    ("N-VIAG", "via"), ("P-VIAT", "via"), ("BSPDN-PMOS-VIA", "via"),
    ("NDIFFCON", "contact"), ("PDIFFCON", "contact"),
    ("NPOLY", "poly"), ("PPOLY", "poly"),
    ("NDIFF", "diffusion"), ("PDIFF", "diffusion"),
    ("NWELL", "well"),
])
def test_roles_from_names(name, role):
    assert C.classify_role(name) == role


@pytest.mark.parametrize("name", [
    "NDIFF-DUPLICATE", "NPOLY-EXTENDED", "PPOLY-PATTERN-CUT", "M0-TRACK-GUIDE",
    "2D-M1-TRACK-GUIDE", "M0-LABEL", "M1-PIN", "DUMMY-GATE", "DUMMY-ISLAND",
    "CELL-BOUNDARY", "GATE-ISOLATION",
])
def test_derived_layers_are_not_conductors(name):
    """Derived variants must never enter the conductor set.

    Leaving them in made an early version report NDIFFCON as connected to
    PPOLY-PATTERN-CUT, because a cut layer happened to enclose the contact.
    """
    assert C.classify_role(name) == "derived"
    assert C.classify_role(name) not in C.CONDUCTOR_ROLES


def test_lyp_role_overrides_name(lm):
    """A layer the LYP flags as pin/label/duplicate is not a conductor."""
    roles = C.layer_roles(lm)
    assert roles[(200, 2)]["role"] == "derived"     # M0-PIN, would match ^M\d+$
    assert roles[(100, 1)]["role"] == "derived"     # NDIFF-DUPLICATE
    assert roles[(200, 0)]["role"] == "metal"       # M0 itself survives


def test_conductor_set_is_only_real_conductors(lm):
    roles = C.layer_roles(lm)
    names = {roles[k]["name"] for k in C._conductors(roles)}
    assert names == {"NDIFF", "PDIFF", "NPOLY", "PPOLY", "DIFF-INTERCONNECT",
                     "M0", "M1", "M2", "BM0"}


# --- tier 1: exact, GDS-only ------------------------------------------------

def _independent_components(gds: Path) -> dict[tuple[int, int], tuple[int, int]]:
    """Union-find over pairwise shape interaction, written independently.

    Deliberately does not use Region.merged(), so it is a real cross-check of the
    module rather than a restatement of it.
    """
    layout = db.Layout()
    layout.read(str(gds))
    top = sorted(layout.top_cells(), key=lambda c: c.name)[0]
    out = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        polys = []
        it = top.begin_shapes_rec(li)
        while not it.at_end():
            s, t = it.shape(), it.trans()
            if s.is_box():
                polys.append(db.Polygon(s.box).transformed(t))
            elif s.is_polygon():
                polys.append(s.polygon.transformed(t))
            elif s.is_path():
                polys.append(s.path.polygon().transformed(t))
            it.next()
        if not polys:
            continue
        parent = list(range(len(polys)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(polys)):
            ri = db.Region(); ri.insert(polys[i])
            for j in range(i + 1, len(polys)):
                rj = db.Region(); rj.insert(polys[j])
                if ri.interacting(rj).count():
                    a, b = find(i), find(j)
                    if a != b:
                        parent[a] = b
        out[(info.layer, info.datatype)] = (len(polys), len({find(i) for i in range(len(polys))}))
    return out


@pytest.mark.parametrize("gds", [DCAP, NR2D1,
                                SAMPLES / "DCAP0_2_RT_4.gds",
                                SAMPLES / "NR2D1_2_RT_4.gds"])
def test_intra_layer_matches_independent_union_find(gds, lm):
    ref = _independent_components(gds)
    mine = {(r["layer"], r["datatype"]): (r["shape_count"], r["component_count"])
            for r in C.intra_layer_connectivity(gds, lm)["layers"]}
    assert mine == ref


def test_intra_layer_needs_no_layermap():
    """Tier 1 is GDS-only, so it must work with no .lyp at all."""
    result = C.intra_layer_connectivity(DCAP, None)
    assert result["availability"] == "GDS-only"
    assert result["total_shapes"] > 0
    assert all(r["role"] == "unknown" for r in result["layers"])


def test_components_never_exceed_shapes(lm):
    for gds in (DCAP, NR2D1):
        for row in C.intra_layer_connectivity(gds, lm)["layers"]:
            assert 1 <= row["component_count"] <= row["shape_count"]


def test_abutting_poly_cuts_are_detected(lm):
    """NPOLY-PATTERN-CUT has 4 shapes forming 1 conductor in both designs."""
    for gds in (DCAP, NR2D1):
        rows = {r["name"]: r for r in C.intra_layer_connectivity(gds, lm)["layers"]}
        assert rows["NPOLY-PATTERN-CUT"]["shape_count"] == 4
        assert rows["NPOLY-PATTERN-CUT"]["component_count"] == 1


# --- tier 2: measurement, not connection ------------------------------------

def test_landings_are_labelled_as_overlap_not_connection(lm):
    land = C.measure_connector_landings(DCAP, lm)
    assert land["available"]
    assert "not connection" in land["basis"]
    # No key in the output may promise a connection.
    for conn in land["connectors"]:
        assert "connects_to" not in conn
        assert set(conn["overlaps"][0]) >= {"shapes_interacting", "shapes_enclosed",
                                            "interaction_ratio", "enclosure_ratio"}


def test_via0_is_enclosed_by_m0_and_m1(lm):
    """The one case geometry does resolve cleanly, and it must stay resolved."""
    land = C.measure_connector_landings(DCAP, lm)
    via0 = next(c for c in land["connectors"] if c["name"] == "VIA0")
    assert via0["shape_count"] == 4
    fully = {o["name"] for o in via0["overlaps"] if o["enclosure_ratio"] == 1.0}
    assert {"M0", "M1"} <= fully


def test_landings_match_independent_measurement(lm):
    """Cross-check with set difference and region growing rather than
    Region.inside()/interacting()."""
    roles = C.layer_roles(lm)
    conn_keys, cond_keys = C._connectors(roles), C._conductors(roles)
    _, _, regions = C._load_regions(DCAP, conn_keys + cond_keys)
    merged = {k: v.merged() for k, v in regions.items()}
    measured = {(c["name"], o["name"]): (o["shapes_interacting"], o["shapes_enclosed"])
                for c in C.measure_connector_landings(DCAP, lm)["connectors"]
                for o in c["overlaps"]}
    checked = 0
    for ck in conn_keys:
        if ck not in merged:
            continue
        for dk in cond_keys:
            if dk not in merged:
                continue
            inter = encl = 0
            for poly in merged[ck].each():
                one = db.Region(); one.insert(poly)
                if not (one.sized(1) & merged[dk]).is_empty():
                    inter += 1
                if (one - merged[dk]).is_empty():
                    encl += 1
            key = (roles[ck]["name"], roles[dk]["name"])
            if inter == 0:
                assert key not in measured
                continue
            assert measured[key] == (inter, encl), key
            checked += 1
    assert checked > 20


def test_conductor_adjacency_finds_npoly_ppoly_abutment(lm):
    """NPOLY and PPOLY abut edge-to-edge in NR2D1 - one gate across two wells."""
    land = C.measure_connector_landings(NR2D1, lm)
    pair = next(a for a in land["conductor_adjacency"]
                if set(a["names"]) == {"NPOLY", "PPOLY"})
    assert pair["shapes_touching"] == 2
    assert pair["overlap_area_dbu2"] == 0.0      # meet at an edge, do not stack
    assert pair["abut_without_overlap"] is True
    assert pair["same_role"] is True


def test_no_conductor_adjacency_in_dcap(lm):
    """In DCAP0 the poly layers do not touch, so no same-level pair is inferred."""
    land = C.measure_connector_landings(DCAP, lm)
    assert not [a for a in land["conductor_adjacency"]
                if set(a["names"]) == {"NPOLY", "PPOLY"}]


def test_landings_unavailable_without_layermap():
    land = C.measure_connector_landings(DCAP, None)
    assert land["available"] is False
    assert "layer map" in land["reason"]


# --- tier 3a: the proposal is a proposal ------------------------------------

def test_proposal_requires_confirmation(lm):
    prop = C.propose_stack(C.measure_connector_landings(DCAP, lm))
    assert prop["requires_confirmation"] is True
    assert "PDK" in prop["availability"] or "technology" in prop["availability"]
    assert "no layer elevations" in prop["caveat"] or "records no layer" in prop["caveat"]


def test_via0_proposal_is_high_confidence_from_two_agreeing_sources(lm):
    """VIA0 -> M0 + M1: the name and the geometry agree independently."""
    prop = C.propose_stack(C.measure_connector_landings(DCAP, lm))
    via0 = next(p for p in prop["proposals"] if p["connector_name"] == "VIA0")
    assert [c["name"] for c in via0["connects"]] == ["M0", "M1"]
    assert via0["confidence"] == "high"


def test_enclosure_alone_never_yields_high_confidence(lm):
    """Enclosure was measured to be insufficient, so it must not read as certain.

    In this backside-power technology BM0 underlies the whole cell and encloses
    vias it does not connect to, so "exactly two layers enclose this via" picks
    the wrong pair. Any proposal resting on enclosure alone is capped at medium.
    """
    prop = C.propose_stack(C.measure_connector_landings(DCAP, lm))
    for p in prop["proposals"]:
        if p["confidence"] == "high":
            # High confidence requires name agreement on both chosen layers.
            named = {e["name"] for e in p["evidence"] if e.get("name_agreement")}
            assert {c["name"] for c in p["connects"]} <= named, p["connector_name"]


def test_proposal_reports_unresolved_alternatives(lm):
    """Where geometry cannot decide, the alternatives must be stated."""
    prop = C.propose_stack(C.measure_connector_landings(DCAP, lm))
    ambiguous = [p for p in prop["proposals"] if p["confidence"] in ("low", "medium")]
    assert ambiguous, "expected at least one connector geometry cannot resolve"
    assert any(p["unresolved_alternatives"] for p in ambiguous)


def test_same_level_candidates_inferred_for_nr2d1(lm):
    prop = C.propose_stack(C.measure_connector_landings(NR2D1, lm))
    assert [set(s["names"]) for s in prop["same_level"]] == [{"NPOLY", "PPOLY"}]
    assert prop["same_level"][0]["confidence"] == "medium"


# --- tier 3b: the net graph ------------------------------------------------

def test_no_stack_means_no_net_graph(lm):
    """The default must not silently apply an inferred stack."""
    result = C.analyze_connectivity(DCAP, lm)
    assert result["nets"] is None
    assert result["stack_source"] is None
    assert any("not applied" in w for w in result["warnings"])


def test_supplied_stack_builds_the_graph(lm, stack):
    result = C.analyze_connectivity(NR2D1, lm, stack=stack)
    assert result["stack_source"] == "supplied"
    nets = result["nets"]
    assert nets["available"]
    assert nets["summary"]["net_count"] == len(nets["nets"])


def test_same_level_prevents_false_floating_gate(lm, stack):
    """Without same-level poly, NPOLY gate fingers are wrongly reported floating.

    NR2D1 has no N-VIAG, so an NPOLY finger reaches M0 only through the PPOLY it
    abuts. Modelling NPOLY and PPOLY as separate levels splits each gate into two
    nets and reports the NPOLY half as floating, when it is physically part of the
    PPOLY net.
    """
    with_same = C.analyze_connectivity(NR2D1, lm, stack=stack)["nets"]["summary"]
    no_same = C.analyze_connectivity(NR2D1, lm, stack=dict(stack, same_level=[])
                                     )["nets"]["summary"]
    assert no_same["floating_net_count"] > with_same["floating_net_count"]
    assert no_same["net_count"] > with_same["net_count"]
    # The gate nets must reach both poly layers once they are modelled as one.
    gate_nets = [n for n in C.analyze_connectivity(NR2D1, lm, stack=stack)["nets"]["nets"]
                 if "NPOLY" in n["layers"]]
    assert gate_nets and all("PPOLY" in n["layers"] for n in gate_nets)


def test_net_shape_counts_are_conserved(lm, stack):
    """Every conducting shape must land in exactly one net."""
    for gds in (DCAP, NR2D1):
        nets = C.analyze_connectivity(gds, lm, stack=stack)["nets"]
        assert (sum(n["shape_count"] for n in nets["nets"])
                == nets["summary"]["conducting_shape_count"])


def test_correct_stack_does_not_trip_the_plausibility_guard(lm, stack):
    """The corrected stack gives a decap four nets, so nothing should be flagged."""
    result = C.analyze_connectivity(DCAP, lm, stack=stack)
    assert result["nets"]["summary"]["net_count"] == 4
    assert not result["nets"]["stack_plausibility_warnings"]


def test_single_net_spanning_everything_is_flagged(lm, tmp_path):
    """A whole cell collapsing to one net means the stack over-connects.

    Reporting "1 net" without saying the stack is suspect would be a confidently
    wrong answer. The stack here deliberately treats the interconnect layers as
    contacts bridging diffusion to M0, which is the mistake the .lyp names invite
    and which shorts the whole cell.
    """
    path = tmp_path / "overconnected.json"
    path.write_text(json.dumps({"connections": {
        "NDIFFCON": ["NDIFF", "M0"], "PDIFFCON": ["PDIFF", "M0"],
        "N-VIAG": ["NPOLY", "M0"], "P-VIAG": ["PPOLY", "M0"],
        "N-VIAT": ["NDIFF", "M0"], "P-VIAT": ["PDIFF", "M0"],
        "VIA0": ["M0", "M1"], "DVB": ["BM0", "M0"]}}))
    bad = C.load_stack(path, lm)
    result = C.analyze_connectivity(DCAP, lm, stack=bad)
    assert result["nets"]["summary"]["net_count"] == 1
    assert result["nets"]["stack_plausibility_warnings"]
    assert any("at least two nets" in w for w in result["warnings"])


def test_stack_is_checked_against_the_layout(lm, tmp_path):
    """A stack claiming a connection the geometry cannot support is surfaced."""
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps({"connections": {
        # VIA0 does not touch NWELL anywhere; M2 is absent from this layout.
        "VIA0": ["M0", "NWELL"], "VIA1": ["M1", "M2"]}}))
    wrong = C.load_stack(path, lm)
    issues = C.compare_stack_to_evidence(wrong, C.measure_connector_landings(DCAP, lm))
    high = [i for i in issues if i["severity"] == "high"]
    assert high, "a claimed connection with no overlap at all must be reported"
    assert "NWELL" in high[0]["issue"]
    # Layers the stack defines but the layout does not use are informational only.
    assert all(i["severity"] == "info" for i in issues if i.get("connector") == "VIA1")


def test_bad_stack_layer_names_are_reported(lm, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"connections": {"NOSUCHVIA": ["M0", "M1"],
                                                "VIA0": ["M0", "NOSUCHMETAL"],
                                                "DVB": ["BM0"]},
                                "same_level": [["NPOLY", "NOPE"]]}))
    loaded = C.load_stack(path, lm)
    assert loaded["usable_count"] == 0
    joined = " ".join(loaded["problems"])
    assert "NOSUCHVIA" in joined and "NOSUCHMETAL" in joined
    assert "at least two" in joined         # DVB with only one target
    assert "NOPE" in joined


def test_supplied_stack_is_not_marked_as_needing_confirmation(stack):
    assert stack["requires_confirmation"] is False
    assert stack["confidence_summary"] == {"supplied": stack["usable_count"]}


# --- the sidecar names the stack -------------------------------------------

def _sidecar(gds: Path):
    return analyze_sidecar(gds.with_suffix(".json"))


def test_sidecar_via_names_yield_the_stack(lm):
    """`VIA_M0_M1` states its endpoints, so no geometric guessing is needed."""
    stack = C.stack_from_sidecar(_sidecar(DCAP), lm)
    assert not stack["problems"]
    by_via = {p["connector_name"]: sorted({c["name"] for c in p["connects"]})
              for p in stack["proposals"]}
    assert by_via["VIA_M0_M1"] == ["M0", "M1"]
    assert by_via["VIA_M0_PMOSGate"] == ["M0", "PMOSGate"]
    assert by_via["VIA_M0_NMOSInterconnect"] == ["M0", "NMOSInterconnect"]


def test_sidecar_resolves_the_misspelled_endpoint(lm):
    """The sample data spells it "Inteconnect"; both interconnect layers match."""
    stack = C.stack_from_sidecar(_sidecar(DCAP), lm)
    dvb = next(p for p in stack["proposals"]
               if p["connector_name"] == "VIA_Inteconnect_BSPowerRail")
    names = sorted({c["name"] for c in dvb["connects"]})
    assert names == ["BSPowerRail", "NMOSInterconnect", "PMOSInterconnect"]


def test_sidecar_stack_excludes_pin_and_duplicate_copies(lm):
    """A sidecar reuses one name across datatypes; the pin copies must be dropped.

    Including them doubled every shape count and turned the -EXTENDED poly
    variants into invented floating nets.
    """
    stack = C.stack_from_sidecar(_sidecar(DCAP), lm)
    keys = {tuple(c["layer"]) for p in stack["proposals"] for c in p["connects"]}
    assert (200, 0) in keys           # M0 drawing
    assert (200, 2) not in keys       # M0-PIN
    assert (300, 2) not in keys       # BM0-PIN
    roles = C.layer_roles(lm)
    assert all(not roles[k]["derived"] for k in keys if k in roles)


def test_sidecar_stack_is_still_marked_as_needing_confirmation(lm):
    stack = C.stack_from_sidecar(_sidecar(DCAP), lm)
    assert stack["requires_confirmation"] is True
    assert "naming convention" in stack["availability"]


def test_sidecar_stack_gives_a_physically_sensible_decap(lm):
    """DCAP0 is a decoupling capacitor: two terminals plus two power taps."""
    stack = C.stack_from_sidecar(_sidecar(DCAP), lm)
    nets = C.analyze_connectivity(DCAP, lm, stack=stack)["nets"]
    assert nets["summary"]["net_count"] == 4
    assert nets["summary"]["floating_net_count"] == 0
    # Two mirrored capacitor terminals, each reaching gate, interconnect, M0, M1.
    big = [n for n in nets["nets"] if n["shape_count"] == 9]
    assert len(big) == 2
    gates = {g for n in big for g in n["layers"] if g in ("NPOLY", "PPOLY")}
    assert gates == {"NPOLY", "PPOLY"}


def test_two_independent_stack_sources_agree(lm, stack):
    """The hand-written stack file and the sidecar-derived stack must match.

    They come from different places - one transcribed by hand against the .lyp
    names, one parsed from the sidecar's own via names - so agreement is real
    cross-validation rather than a restatement.
    """
    for gds in (DCAP, NR2D1, SAMPLES / "DCAP0_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.gds"):
        supplied = C.analyze_connectivity(gds, lm, stack=stack)["nets"]["summary"]
        derived = C.analyze_connectivity(
            gds, lm, stack=C.stack_from_sidecar(_sidecar(gds), lm))["nets"]["summary"]
        assert supplied["net_count"] == derived["net_count"], gds.name
        assert supplied["floating_net_count"] == derived["floating_net_count"], gds.name


def test_added_via_connects_a_previously_floating_stub(lm, stack):
    """A real finding: revision 2 adds a via that lands on a floating stub.

    NR2D1_1 has PMOSInterconnect shapes no via reaches. NR2D1_2 adds
    VIA_M0_PMOSInterconnect, and one of them stops floating.
    """
    a = C.analyze_connectivity(NR2D1, lm, stack=stack)["nets"]["summary"]
    b = C.analyze_connectivity(SAMPLES / "NR2D1_2_RT_4.gds", lm,
                               stack=stack)["nets"]["summary"]
    assert a["floating_net_count"] == 2
    assert b["floating_net_count"] == 1


def test_sidecar_stack_needs_via_named_layers():
    """A sidecar whose layers are not named VIA_<a>_<b> yields no stack."""
    fake = {"layers": [{"name": "M0", "layer": 200, "datatype": 0},
                       {"name": "M1", "layer": 202, "datatype": 0}]}
    stack = C.stack_from_sidecar(fake, None)
    assert stack["usable_count"] == 0
    assert "no via layer names" in stack["availability"]


# --- role overrides ---------------------------------------------------------

def test_role_override_corrects_the_name_heuristic(lm):
    """"NDIFFCON" reads as a contact by name but is local interconnect here."""
    assert C.classify_role("NDIFFCON") == "contact"
    roles = C.layer_roles(lm, {"NDIFFCON": "metal"})
    assert roles[(104, 0)]["role"] == "metal"
    assert roles[(104, 0)]["role_source"] == "supplied override"
    assert roles[(105, 0)]["role_source"] == "inferred from name"


def test_role_override_cannot_resurrect_a_derived_layer(lm):
    """A pin or label copy must stay out of the conductor set."""
    roles = C.layer_roles(lm, {"M0-PIN": "metal"})
    assert roles[(200, 2)]["role"] == "derived"


def test_stack_file_role_overrides_reach_the_landing_measurement(lm, stack):
    """With NDIFFCON reclassified, it must stop being measured as a connector."""
    result = C.analyze_connectivity(NR2D1, lm, stack=stack)
    connectors = {c["name"] for c in result["landings"]["connectors"]}
    assert "NDIFFCON" not in connectors
    assert "PDIFFCON" not in connectors
    conductors = {c["name"] for c in result["landings"]["conductor_layers"]}
    assert {"NDIFFCON", "PDIFFCON"} <= conductors


def test_unknown_role_override_is_reported(lm, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"roles": {"NOSUCHLAYER": "metal"},
                                "connections": {"VIA0": ["M0", "M1"]}}))
    loaded = C.load_stack(path, lm)
    assert loaded["usable_count"] == 1
    assert any("NOSUCHLAYER" in p for p in loaded["problems"])


def test_alternative_targets_are_allowed_and_not_flagged(lm, stack):
    """DVB lists three candidate layers; partial coverage is expected there."""
    dvb = next(p for p in stack["proposals"] if p["connector_name"] == "DVB")
    assert len(dvb["connects"]) == 3
    issues = C.compare_stack_to_evidence(stack, C.measure_connector_landings(
        NR2D1, lm, stack["role_overrides"]))
    assert not [i for i in issues if i.get("connector") == "DVB"], \
        "alternative targets must not be reported as a disagreement"


# --- boundaries -------------------------------------------------------------

def test_limitations_are_always_present(lm):
    for kwargs in ({}, {"accept_proposed_stack": True}):
        result = C.analyze_connectivity(DCAP, lm, **kwargs)
        lim = result["limitations"]
        assert "no layer elevations" in lim["vertical_stack"]
        for key in ("physical_shorts", "physical_opens", "electrical_intent"):
            assert "netlist" in lim[key] or "intent" in lim[key]


def test_accepting_the_inferred_stack_marks_nets_provisional(lm):
    result = C.analyze_connectivity(DCAP, lm, accept_proposed_stack=True)
    assert result["stack_source"] == "inferred proposal, explicitly accepted"
    assert any("provisional" in w for w in result["warnings"])


def test_connectivity_passes_schema_validation(lm, stack):
    for kwargs in ({}, {"stack": stack}, {"accept_proposed_stack": True}):
        validate_connectivity(C.analyze_connectivity(NR2D1, lm, **kwargs))


def test_schema_rejects_net_graph_without_a_stack_source(lm, stack):
    result = C.analyze_connectivity(NR2D1, lm, stack=stack)
    result["stack_source"] = None
    with pytest.raises(SchemaError, match="stack_source"):
        validate_connectivity(result)


def test_schema_rejects_more_components_than_shapes(lm):
    result = C.analyze_connectivity(DCAP, lm)
    result["intra_layer"]["layers"][0]["component_count"] = 99
    with pytest.raises(SchemaError, match="merging shapes can only reduce"):
        validate_connectivity(result)


def test_schema_rejects_a_proposal_presented_as_fact(lm):
    result = C.analyze_connectivity(DCAP, lm)
    result["proposed_stack"]["requires_confirmation"] = False
    with pytest.raises(SchemaError, match="requires_confirmation"):
        validate_connectivity(result)
