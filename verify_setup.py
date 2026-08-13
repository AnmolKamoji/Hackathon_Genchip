#!/usr/bin/env python3
"""One-command health check for the AI GDS Design Reviewer.

Run this after installing, after changing the AI configuration, or right before a
demo. It verifies the environment, checks the analyzer against known-good numbers
for the bundled reference files, and reports the state of the AI backend.

    python verify_setup.py            # environment + analyzer + backend status
    python verify_setup.py --live-ai  # also send one real request to the model

Exit code is 0 only when every required check passes. The AI backend is optional:
its absence is reported but does not fail the run, because every question in the
demo script is answered deterministically.
"""
from __future__ import annotations

import argparse
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "data/samples"

# Load .env exactly as app.py does, or this script reports a different backend
# than the dashboard will actually use.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

results: list[tuple[str, bool, str]] = []
optional_notes: list[str] = []


def check(label: str, fn):
    """Run one check. fn returns a detail string, or raises to fail."""
    try:
        detail = fn() or ""
        results.append((label, True, detail))
        print(f"  {GREEN}PASS{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    except Exception as exc:
        results.append((label, False, f"{type(exc).__name__}: {exc}"))
        print(f"  {RED}FAIL{RESET}  {label}\n        {RED}{type(exc).__name__}: {exc}{RESET}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------- environment
print("\n=== Environment ===")


def _python():
    v = sys.version_info
    expect(v >= (3, 10), f"Python 3.10+ required, found {platform.python_version()}")
    return f"Python {platform.python_version()} on {platform.system()}"


def _klayout():
    import klayout.db as db
    return f"KLayout {db.__version__}"


def _deps():
    import pandas, plotly, streamlit          # noqa: F401
    return f"streamlit {streamlit.__version__}, pandas {pandas.__version__}"


def _samples():
    missing = [f.name for f in [
        SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json",
        SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json",
    ] if not f.exists()]
    expect(not missing, f"missing sample files: {missing}")
    return "4 reference files present"


check("Python version", _python)
check("KLayout importable", _klayout)
check("Dashboard dependencies", _deps)
check("Reference samples", _samples)

if any(not ok for _, ok, _ in results):
    print(f"\n{RED}Environment is incomplete; fix the above before continuing.{RESET}")
    print("  pip install -r requirements.txt")
    raise SystemExit(1)

from ai.deterministic import answer, answer_comparison            # noqa: E402
from ai.llm import ollama_model, provider_chain, provider_status  # noqa: E402
from analyzer.comparison import compare_metadata                  # noqa: E402
from analyzer.fused import analyze_pair                           # noqa: E402
from analyzer.gds_parser import analyze_gds                       # noqa: E402
from analyzer.layermap import find_layermap, load_lyp             # noqa: E402
from analyzer.sidecar_parser import analyze_sidecar               # noqa: E402
from models.metadata import validate_connectivity                 # noqa: E402

# ------------------------------------------------------------------ analyzer
print("\n=== Analyzer (known-good numbers for the reference files) ===")

RAW = {}
FUSED = {}


def _raw_gds():
    m = RAW.setdefault(1, analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds"))
    expect(m["design"]["polygon_count"] == 60, f'polygons {m["design"]["polygon_count"]} != 60')
    expect(m["design"]["text_count"] == 10, f'texts {m["design"]["text_count"]} != 10')
    expect(m["design"]["via_count"] is None, "raw GDS must report via_count as unavailable, not 0")
    return "60 polygons, 10 texts, vias unavailable"


def _sidecar():
    m = analyze_sidecar(SAMPLES / "NR2D1_1_RT_4.json", "NR2D1_1_RT_4.gds")
    expect(m["design"]["polygon_count"] == 60, "sidecar polygons != 60")
    expect(m["design"]["via_count"] == 6, "sidecar vias != 6")
    expect(m["warnings"] == [], f"unexpected sidecar warnings: {m['warnings']}")
    return "60 polygons, 6 vias, no warnings"


def _fused():
    m = FUSED.setdefault(1, analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json"))
    d = m["design"]
    expect(d["polygon_count"] == 60 and d["via_count"] == 6, f"fused counts wrong: {d}")
    expect(m["consistency"]["agrees"], f'GDS and sidecar disagree: {m["consistency"]}')
    expect(m["layout"]["bbox_area_um2"] == 0.03, f'bbox area {m["layout"]["bbox_area_um2"]} != 0.03')
    return "geometry + semantics agree exactly (60 polygons, 6 vias, 0.03 um2)"


def _dup_detection():
    m = FUSED[1]
    g = next(x for x in m["layer_groups"] if x["label"] == "BSPowerRail")
    expect(g["geometry_duplicated_across_datatypes"], "datatype duplication not detected")
    expect(abs(g["union_area_um2"] - 0.02295) < 1e-9, f'union area {g["union_area_um2"]}')
    return "BSPowerRail: 0.02295 um2 union vs 0.0459 um2 naive sum"


def _layermap():
    lyp = find_layermap(SAMPLES / "NR2D1_1_RT_4.gds")
    expect(lyp, "no .lyp layer map found next to the samples")
    lm = load_lyp(lyp)
    expect(lm["entry_count"] >= 40, f"only {lm['entry_count']} layer-map entries")
    named = analyze_gds(SAMPLES / "NR2D1_1_RT_4.gds", lm)
    by_key = {(r["layer"], r["datatype"]): r for r in named["layers"]}
    expect(by_key[(300, 0)]["name"] == "BM0", "layer map did not name 300/0 as BM0")
    # Renaming must not move a number.
    expect(named["design"]["polygon_count"] == 60, "layer map changed the polygon count")
    return f"{lyp.name}: {lm['entry_count']} technology names, geometry unchanged"


def _comparison():
    a = FUSED[1]
    b = analyze_pair(SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    FUSED[2] = b
    c = compare_metadata(a, b)
    expect(c["comparable"], "reference files should be comparable")
    s = c["summary"]
    expect(s["polygon_delta"] == 7, f'polygon delta {s["polygon_delta"]} != 7')
    expect(s["via_delta"] == 3, f'via delta {s["via_delta"]} != 3')
    added = {x["name"] for x in c["layers_added"]}
    expect({"M1", "VIA_M0_M1"} <= added, f"expected M1 and VIA_M0_M1 in added layers, got {added}")
    return "+7 polygons, +3 vias, adds M1 and VIA_M0_M1"


def _mismatch_guard():
    m = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    expect(not m["consistency"]["agrees"], "wrong sidecar pairing was not detected")
    expect(m["design"]["via_count"] is None, "another revision's via count leaked through")
    return "wrong sidecar rejected, via counts dropped to unavailable"


def _connectivity():
    from analyzer import connectivity as conn_mod

    gds = SAMPLES / "NR2D1_1_RT_4.gds"
    lm = load_lyp(find_layermap(gds))

    # Tier 1 must work with no layer map at all - it is GDS-only.
    bare = conn_mod.intra_layer_connectivity(gds, None)
    expect(bare["total_shapes"] == 60, f'tier 1 saw {bare["total_shapes"]} shapes, expected 60')
    expect(bare["total_components"] == 54,
           f'tier 1 found {bare["total_components"]} conductors, expected 54')

    # Tier 2 must measure, and must not promise connection.
    land = conn_mod.measure_connector_landings(gds, lm)
    expect(land["available"], "tier 2 landings unavailable with a layer map present")
    expect("not connection" in land["basis"], "tier 2 basis does not disclaim connection")

    # Without a stack there must be no net graph.
    no_stack = conn_mod.analyze_connectivity(gds, lm)
    expect(no_stack["nets"] is None, "a net graph was built without a connection stack")
    expect(no_stack["stack_source"] is None, "stack_source set with no stack supplied")

    stack_file = SAMPLES / "Titan_stack.json"
    expect(stack_file.exists(), "connection stack sample missing")
    stack = conn_mod.load_stack(stack_file, lm)
    expect(not stack["problems"], f'stack did not load cleanly: {stack["problems"]}')
    with_stack = conn_mod.analyze_connectivity(gds, lm, stack=stack)
    s = with_stack["nets"]["summary"]
    expect(s["net_count"] == 7, f'expected 7 nets with the supplied stack, got {s["net_count"]}')

    # A decap must come out as two capacitor terminals plus two power taps.
    dcap = conn_mod.analyze_connectivity(SAMPLES / "DCAP0_1_RT_4.gds", lm, stack=stack)
    expect(dcap["nets"]["summary"]["net_count"] == 4,
           f'expected 4 nets for the decap, got {dcap["nets"]["summary"]["net_count"]}')
    expect(not dcap["nets"]["stack_plausibility_warnings"],
           f'the corrected stack should not look implausible: '
           f'{dcap["nets"]["stack_plausibility_warnings"]}')

    # The sidecar names its vias after their endpoints, so it is an independent
    # source for the same stack. The two must agree.
    side = analyze_sidecar(SAMPLES / "NR2D1_1_RT_4.json")
    derived = conn_mod.stack_from_sidecar(side, lm)
    expect(derived["usable_count"] > 0, "no stack could be derived from the sidecar via names")
    d = conn_mod.analyze_connectivity(gds, lm, stack=derived)["nets"]["summary"]
    expect(d["net_count"] == s["net_count"],
           f'sidecar-derived stack gives {d["net_count"]} nets, supplied gives {s["net_count"]}')

    validate_connectivity(with_stack)
    return ("tier 1: 60 shapes -> 54 conductors (no .lyp needed); tier 3: 7 nets, and the "
            "sidecar-derived stack agrees with the supplied one")


check("Raw GDS parsing", _raw_gds)
check("Sidecar parsing", _sidecar)
check("Fused analysis + consistency", _fused)
check("Datatype duplication detection", _dup_detection)
check("Layer map (.lyp)", _layermap)
check("Physical connectivity", _connectivity)
check("Two-file comparison", _comparison)
check("Mismatched sidecar guard", _mismatch_guard)

# ------------------------------------------------------------- demo questions
print("\n=== Demo questions (must be answered with no AI backend) ===")

DEMO = [
    ("Give me a summary of this GDS.", None),
    ("How many polygons are there?", "60"),
    ("Which layers are used?", "BSPowerRail"),
    ("How many vias are present?", "6"),
    ("What is the largest cell?", "NR2D1"),
    ("Which layer has the highest density?", "76.50"),
    ("What is the layout size?", "0.15"),
    ("How many polygons are on M0?", "M0"),
    ("Does this design contain M1?", "No."),
    ("What is the area of M0?", "0.002460"),
    # The blanket refusal is gone: with the GENCHIP manual loaded the geometric
    # rules are checked. This metadata carries no drc block, so the honest answer is
    # that the results are absent - never a guess.
    ("Does this design contain any DRC violations?", "No design rule results are available"),
    ("Is the design LVS clean?", "LVS and ERC are not available"),
]


def _demo():
    m = FUSED[1]
    bad = []
    for q, must_contain in DEMO:
        r = answer(m, q)
        if not r:
            bad.append(f"{q!r} -> no answer")
        elif must_contain and must_contain not in r:
            bad.append(f"{q!r} -> missing {must_contain!r}")
    expect(not bad, "; ".join(bad))
    return f"all {len(DEMO)} answered deterministically"


def _demo_comparison():
    c = compare_metadata(FUSED[1], FUSED[2])
    r = answer_comparison(c, "What changed between the two layouts?")
    expect(r and "M1" in r and "+7" in r, f"comparison answer inadequate: {r}")
    return "'what changed' names M1 and reports +7 polygons"


def _no_wrong_answers():
    """Questions that must return None rather than a confident guess."""
    m = FUSED[1]
    bad = []
    for q in ["", "?", "asdfgh", "Explain this layout to a non-expert."]:
        r = answer(m, q)
        if r is not None:
            bad.append(f"{q!r} -> {r[:60]!r}")
    expect(not bad, "; ".join(bad))
    return "nonsense and narrative questions correctly deferred"


check("Deterministic demo answers", _demo)
check("Deterministic comparison answer", _demo_comparison)
check("Questions correctly deferred", _no_wrong_answers)

# ------------------------------------------------------------------ AI backend
print("\n=== AI backend (optional) ===")
status = provider_status()
print(f"  {DIM}provider chain: {' -> '.join(status['chain']) or 'none'}{RESET}")
if status["ready"]:
    print(f"  {GREEN}READY{RESET} {status['detail']}")
else:
    print(f"  {YELLOW}NOT READY{RESET} {status['detail']}")
    optional_notes.append(status["detail"])
    print(f"        {DIM}Deterministic Q&A is unaffected. To enable narrative answers, either:{RESET}")
    print(f"        {DIM}  - set ANTHROPIC_API_KEY in .env (console.anthropic.com -> API keys), or{RESET}")
    print(f"        {DIM}  - start Ollama and run: ollama pull {ollama_model()}{RESET}")
    if "ollama" in provider_chain():
        print(f"        {DIM}    (on Windows, not inside WSL2, if your GPU is AMD){RESET}")

if status["ready"]:
    args = argparse.ArgumentParser(add_help=False)
    args.add_argument("--live-ai", action="store_true")
    if args.parse_known_args()[0].live_ai:
        from ai.llm import ask_llm, looks_like_failure
        print(f"  {DIM}sending one real request via {status.get('primary')}...{RESET}")
        t0 = time.time()
        reply = ask_llm(FUSED[1], "In one sentence, what kind of layout is this?")
        elapsed = time.time() - t0
        # Not startswith("**"): a real answer may legitimately open with bold text.
        failed = looks_like_failure(reply)
        print(f"  {'%sFAIL%s' % (RED, RESET) if failed else '%sPASS%s' % (GREEN, RESET)}"
              f"  live model round-trip  {DIM}{elapsed:.1f}s{RESET}")
        print(f"        {DIM}{reply[:300]}{RESET}")
        results.append(("live model round-trip", not failed, reply[:120]))
    else:
        print(f"  {DIM}re-run with --live-ai to send one real request{RESET}")

# ----------------------------------------------------------------- conclusion
failed = [label for label, ok, _ in results if not ok]
print("\n" + "=" * 68)
if failed:
    print(f"{RED}{len(failed)} CHECK(S) FAILED{RESET}: " + ", ".join(failed))
    raise SystemExit(1)
print(f"{GREEN}All {len(results)} checks passed.{RESET}")
if optional_notes:
    print(f"{YELLOW}AI narrative is not configured{RESET} - the dashboard and every demo "
          f"question still work.")
print("\nNext:  streamlit run app.py")
