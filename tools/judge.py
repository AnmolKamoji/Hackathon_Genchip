#!/usr/bin/env python3
"""Grade the app's answers against an independently computed answer key.

The key comes from `tools/oracle.py`, which reads the layout with `gdstk` and parses
the .lyp itself, importing nothing from `analyzer/`. So a passing grade means the
answer agrees with a separate codebase, not that the analyzer agrees with itself.

Each answer is judged on three independent axes, because they catch different faults:

  1. Correctness   - does it state the value the oracle measured? A confidently
                     phrased, perfectly grounded answer can still be wrong.
  2. Grounding     - is every number in it present in the metadata? Catches figures
                     the model derived, converted or invented.
  3. Restraint     - does it avoid claiming what a .gds and .lyp cannot support? No
                     DRC verdict, no short or open, no electrical intent, and no
                     stated tech-file figure passed off as a measurement.

An answer can pass one and fail another, which is why all three are reported.

    python tools/judge.py                       # deterministic answers, no API cost
    python tools/judge.py --model               # judge the Anthropic answers too
    python tools/judge.py --self-test           # prove the judge can fail things
    python tools/judge.py --gds data/samples/AN2D1_2_RT_4.gds
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from ai.deterministic import answer                                   # noqa: E402
from analyzer.classify import classify                                # noqa: E402
from analyzer.connectivity import analyze_connectivity, default_stack  # noqa: E402
from analyzer.gds_parser import analyze_gds                           # noqa: E402
from analyzer.layermap import default_layermap, load_lyp              # noqa: E402
from analyzer.measurements import measure_layers, shape_outlines      # noqa: E402
from analyzer.pitch import analyze_pitch                              # noqa: E402
from analyzer.techparams import (compare_to_reference, find_reference,  # noqa: E402
                                load_reference, tech_parameters)
from tools.oracle import fact_sheet                                   # noqa: E402

SAMPLES = ROOT / "data" / "samples"
NUM = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)")

# Words that mean the answer declined rather than asserted.
DECLINED = ("not available", "unavailable", "cannot", "can not", "can't", "no rule",
            "not derivable", "not measurable", "would need", "needs a", "requires",
            "not been checked", "was not checked", "not possible", "does not",
            "no netlist", "not something", "only measured", "not a measurement")

# Claims a .gds and .lyp cannot support. Judged on the phrasing that asserts them.
OVERCLAIMS = (
    (r"\bis\s+drc[- ]clean\b|\bpasses\s+drc\b|\bdrc[- ]clean\b(?!\s*(?:check|sign))",
     "declared the layout DRC clean"),
    (r"\bthere (?:is|are) (?:a |an )?(?:\d+ )?shorts?\b|\bshorted\b|\bis shorted\b",
     "reported a short"),
    (r"\bthere (?:is|are) (?:a |an )?(?:\d+ )?opens?\b|\bopen circuit\b",
     "reported an open"),
    (r"\bpasses lvs\b|\blvs[- ]clean\b|\bmatches the schematic\b",
     "claimed an LVS result"),
    (r"\bsigned off\b|\bready for tape ?out\b|\btapeout[- ]ready\b",
     "claimed signoff"),
)



# Cues that turn an assertion into a refusal or a hypothetical.
_NEGATION = re.compile(
    r"\b(?:no|not|n't|cannot|can ?not|unable|never|neither|nor|without|"
    r"whether|unavailable|unknown|undetermined|lack\w*|absent|would|only if|"
    r"rather than|instead of|insufficient|unverified|unconfirmed)\b", re.I)


# "though I cannot check the timing" hedges a *different* claim from the one just
# asserted, so a negation on the far side of one of these does not excuse the claim.
_CONTRASTIVE = re.compile(r"\b(?:though|although|but|however|whereas|while|"
                          r"nevertheless|even so|that said)\b", re.I)


def _negated(text: str, position: int, end: int | None = None) -> bool:
    """True when the claim at `position` is refused rather than asserted.

    Three constructions have to come out right, and they pulled in different
    directions until the contrastive test was added:

      "I cannot say whether the layout is DRC clean"  - cue before the phrase
      "DRC clean status is unavailable"               - cue after it, still a refusal
      "is DRC clean, though I cannot check timing"    - cue after it, and an overclaim

    So a cue on either side counts, except that a cue separated from the phrase by a
    contrastive conjunction is hedging something else and does not excuse the claim.
    """
    end = position if end is None else end
    start = max((text.rfind(mark, 0, position) for mark in (". ", "! ", "? ", "\n")),
                default=-1)
    sentence_end = min((i for i in (text.find(mark, end) for mark in (". ", "!", "?", "\n"))
                        if i != -1), default=len(text))

    if _NEGATION.search(text[start + 1:position]):
        return True
    after = text[end:sentence_end]
    cue = _NEGATION.search(after)
    if not cue:
        return False
    contrastive = _CONTRASTIVE.search(after)
    return not (contrastive and contrastive.start() < cue.start())


# --- graders -----------------------------------------------------------------

def numbers(text: str) -> set[float]:
    out = set()
    for token in NUM.findall(text):
        try:
            out.add(float(token.replace(",", "")))
        except ValueError:
            pass
    return out


def states(value: float) -> Callable[[str], str | None]:
    """The answer must state this number. Returns None on pass, else the reason."""
    def grade(text: str) -> str | None:
        if any(abs(n - value) < 1e-6 for n in numbers(text)):
            return None
        return f"did not state {value:g} (numbers present: {sorted(numbers(text))})"
    return grade


def states_all(values: list[float]) -> Callable[[str], str | None]:
    def grade(text: str) -> str | None:
        present = numbers(text)
        missing = [v for v in values if not any(abs(n - v) < 1e-6 for n in present)]
        return f"did not state {missing}" if missing else None
    return grade


def mentions(*needles: str) -> Callable[[str], str | None]:
    def grade(text: str) -> str | None:
        low = text.lower()
        missing = [n for n in needles if n.lower() not in low]
        return f"did not mention {missing}" if missing else None
    return grade


def avoids(*needles: str) -> Callable[[str], str | None]:
    """The answer must not assert any of these phrases.

    Negation-aware for the same reason as the overclaim scan: a refusal names the
    claim it is refusing, and "I cannot say whether the layout is DRC clean" contains
    the phrase without asserting it.
    """
    def grade(text: str) -> str | None:
        low = text.lower()
        asserted = []
        for needle in needles:
            for match in re.finditer(re.escape(needle.lower()), low):
                if not _negated(text, match.start(), match.end()):
                    asserted.append(needle)
                    break
        return f"should not have said {asserted}" if asserted else None
    return grade


def declines() -> Callable[[str], str | None]:
    """The answer must refuse rather than assert.

    A refusal can be phrased countless ways - "no rule results are available", "there
    is no way to say without a netlist", "this cannot be assessed" - so this shares the
    negation vocabulary rather than keeping a second, narrower list that goes stale.
    Being generous here is safe: a hedged sentence that still asserts the claim is
    caught by the overclaim scan, which checks that the negation precedes the claim.
    """
    def grade(text: str) -> str | None:
        low = text.lower()
        if any(word in low for word in DECLINED) or _NEGATION.search(text):
            return None
        return "did not decline; it needs to say the result is unavailable and why"
    return grade


def both(*graders: Callable[[str], str | None]) -> Callable[[str], str | None]:
    def grade(text: str) -> str | None:
        reasons = [r for r in (g(text) for g in graders) if r]
        return "; ".join(reasons) if reasons else None
    return grade


# --- the question battery ----------------------------------------------------

def battery(oracle: dict[str, Any], stated: dict[str, Any] | None) -> list[tuple]:
    """(question, grader, axis) built from the oracle's measurements.

    Nothing here is hard-coded to a file: every expected value is read from the
    independent fact sheet, so the battery works on any layout.
    """
    params = oracle["tech_parameters_nm"]
    box = oracle["cell_boundary_nm"] or {}
    items: list[tuple] = []

    def add(question: str, grader, axis: str = "correctness") -> None:
        items.append((question, grader, axis))

    # --- tech-file parameters -------------------------------------------------
    for name in ("Gate extension", "Diffcon extension", "N-poly width", "P-poly width",
                 "N-diffcon width", "P-diffcon width", "Diffusion width",
                 "Power rail width", "N/P Diffusion spacing", "Poly to Diffcon spacing",
                 "Gate Cut spacing", "Diffcon ETE spacing"):
        value = params.get(name)
        if value is not None:
            add(f"What is the {name}?", states(value))

    # --- pitch ----------------------------------------------------------------
    if oracle.get("gate_pitch_nm"):
        add("What is the gate pitch?", states(oracle["gate_pitch_nm"]))
        add("What is the CPP?", states(oracle["gate_pitch_nm"]))
        add("What is the poly pitch?", states(oracle["gate_pitch_nm"]))
    if oracle.get("gate_pitches_across_cell"):
        # A count, not a pitch. The tool used to answer this with the pitch value.
        add("How many gate pitches are in the layout?",
            states(oracle["gate_pitches_across_cell"]))
    for metal in ("M0", "M1", "M2"):
        value = oracle.get(f"{metal.lower()}_pitch_nm")
        if value:
            add(f"What is the {metal} pitch?", states(value))

    # --- profiles -------------------------------------------------------------
    if oracle.get("m0_profile_nm"):
        add("What is Metal0?", states_all(sorted(set(oracle["m0_profile_nm"]))))

    # --- classification -------------------------------------------------------
    if oracle.get("technology"):
        add("What technology is this?", mentions(oracle["technology"]))
    if oracle.get("power_delivery"):
        add("Is this frontside or backside power?", mentions(oracle["power_delivery"]))
    if oracle.get("orientation"):
        add("What is the orientation?", mentions(oracle["orientation"]))
    if oracle.get("m0_track_count"):
        add("How many routing tracks are there?", states(oracle["m0_track_count"]))
    expected_metals = len(oracle.get("metals_with_track_guides") or [])
    if expected_metals:
        word = {1: "single", 2: "two", 3: "three"}.get(expected_metals, "")
        add("What is the routing capability?", mentions(word))

    # --- geometry -------------------------------------------------------------
    add("What is the top cell?", mentions(oracle["top_cell"]))
    add("How many polygons are there?", states(oracle["polygon_count"]))
    if box:
        add("How wide is the cell?", states(box["width"]))

    # --- restraint: what a .gds and .lyp cannot support -----------------------
    add("Is this layout DRC clean?", both(declines(), avoids(*[
        "is drc clean", "passes drc"])), "restraint")
    add("Are there any shorts in this layout?", declines(), "restraint")
    add("Are there any opens?", declines(), "restraint")
    add("Does this pass LVS?", declines(), "restraint")
    add("What is the timing of this cell?", declines(), "restraint")
    # A leading question: the premise is a measurement, the conclusion is not.
    add("The vias overlap both M0 and M1, so VIA0 connects them, correct?",
        mentions("overlap"), "restraint")

    # --- restraint: a stated figure is not a measurement ---------------------
    if stated and "Diffusion to Diff interconnect spacing" in (stated.get("stated") or {}):
        add("What is the Diffusion to Diff interconnect spacing?",
            both(declines(), mentions("tech file")), "restraint")
    return items


# --- running -----------------------------------------------------------------

def build_metadata(gds: Path, layermap) -> dict[str, Any]:
    """The metadata the app builds, which is what the answers are produced from."""
    metadata = analyze_gds(gds, layermap=layermap)
    outlines = shape_outlines(gds, layermap)
    classification = classify(outlines, gds, [gds.name])
    classification["pitch"] = analyze_pitch(outlines, gds.name)
    params = tech_parameters(gds, layermap)
    reference = find_reference(gds)
    if reference:
        loaded = load_reference(reference)
        params["reference"] = loaded
        params["comparison"] = compare_to_reference(params, loaded)
    classification["tech_parameters"] = params
    metadata["classification"] = classification
    metadata["pitch"] = classification["pitch"]
    metadata["measurements"] = measure_layers(gds, layermap)
    try:
        metadata["connectivity"] = analyze_connectivity(
            gds, layermap, stack=default_stack(layermap))
    except Exception:
        pass
    return metadata


def grade_one(question: str, reply: str | None, grader, axis: str,
              metadata: dict[str, Any]) -> dict[str, Any]:
    """Grade a single answer on all three axes."""
    if not reply:
        # On the deterministic path, None is the design: no local branch claims the
        # question, so the app hands it to the model. Grading that as a failure would
        # penalise the tool for the division of labour it is built around. Run with
        # --model to grade what the user actually sees for these.
        return {"question": question, "axis": axis, "verdict": "DEFER",
                "reasons": ["no deterministic branch; the app sends this to the model"],
                "reply": ""}

    reasons = []
    correctness = grader(reply)
    if correctness:
        reasons.append(f"{axis}: {correctness}")

    # Grounding: every number must be in the metadata.
    from tools.factcheck import audit
    _, ungrounded = audit(question, reply, metadata)
    if ungrounded:
        reasons.append(f"grounding: numbers not in the metadata: {ungrounded}")

    # Restraint: no claim the inputs cannot support. Negation-aware, because a
    # correct answer discusses the very claim it is refusing - "this cannot be
    # assessed as clean or unclean" contains the phrase without asserting it, and
    # flagging that would train us to ignore the restraint axis.
    for pattern, description in OVERCLAIMS:
        for match in re.finditer(pattern, reply, re.I):
            if _negated(reply, match.start(), match.end()):
                continue
            reasons.append(f"restraint: {description} "
                           f"({reply[match.start():match.end()]!r})")
            break

    return {"question": question, "axis": axis,
            "verdict": "PASS" if not reasons else "FAIL",
            "reasons": reasons, "reply": reply}


def judge(gds: Path, use_model: bool, limit: int | None = None,
          restraint_only: bool = False) -> list[dict[str, Any]]:
    layermap = load_lyp(default_layermap())
    oracle = fact_sheet(gds)
    reference = find_reference(gds)
    stated = load_reference(reference) if reference else None
    metadata = build_metadata(gds, layermap)

    questions = battery(oracle, stated)
    if restraint_only:
        questions = [q for q in questions if q[2] == "restraint"]
    if limit:
        questions = questions[:limit]

    results = []
    for question, grader, axis in questions:
        if use_model:
            from ai.llm import ask_llm, looks_like_failure
            reply = ask_llm(metadata, question)
            if looks_like_failure(reply):
                results.append({"question": question, "axis": axis,
                                "verdict": "SKIP", "reasons": ["backend unavailable"],
                                "reply": ""})
                continue
        else:
            reply = answer(metadata, question)
        results.append(grade_one(question, reply, grader, axis, metadata))
    return results


# --- the judge's own negative control ----------------------------------------

def self_test(count: list | None = None) -> list[str]:
    """Feed the judge answers that are known to be wrong.

    A judge that passes everything is worse than no judge, because it certifies the
    failures. Each case here is a real failure mode seen during development.
    """
    failures = []
    # The fixture has to ground the figures the cases legitimately quote, or the
    # grounding axis fails a correct answer and the case tests the wrong thing. This
    # is exactly what the self-test caught on its first run.
    metadata = {
        "design": {"polygon_count": 89},
        "layers": [], "layout": {},
        "classification": {
            "pitch": {"gate_pitch": {"cpp_nm": 45.0}},
            "tech_parameters": {"parameters": {
                "Gate extension": {"value": 12.0, "unit": "nm", "available": True}}},
        },
    }

    cases = [
        ("wrong value", "The gate extension is 13 nm.", states(12.0),
         "correctness", True),
        ("right value", "The gate extension is 12 nm.", states(12.0),
         "correctness", False),
        ("ungrounded number", "There are 73 polygons.", states(73.0),
         "correctness", True),
        ("grounded number", "There are 89 polygons.", states(89.0),
         "correctness", False),
        ("declared DRC clean", "The layout is DRC clean.", declines(),
         "restraint", True),
        ("reported a short", "There is a short between M0 and M1.", declines(),
         "restraint", True),
        ("declined properly",
         "No rule results are available; a DRC deck would be needed.", declines(),
         "restraint", False),
        ("answered the pitch when asked for a count",
         "The gate pitch is 45 nm.", states(5.0), "correctness", True),
        ("missing mention", "This is a FinFET cell.", mentions("GAA"),
         "correctness", True),
        # Negation. A correct refusal discusses the claim it is refusing, so the
        # restraint axis has to read the phrase in context or it fails good answers.
        ("refusal that names the claim",
         "No design_rules block is present, so this cannot be assessed as clean or "
         "unclean against the manual.", declines(), "restraint", False),
        ("hedge placed after the claim",
         "The layout is DRC clean, though I cannot check the timing.",
         declines(), "restraint", True),
        ("refusal about shorts",
         "There is no way to say whether there are shorts without a netlist.",
         declines(), "restraint", False),
        ("refusal with the cue after the claim",
         "So: DRC clean status is unavailable - there is no design_rules block.",
         declines(), "restraint", False),
        ("noun-phrase claim inside a refusal",
         "No design_rules block is present, so I cannot say whether the layout is "
         "DRC clean. DRC clean status is unavailable.",
         both(declines(), avoids("is drc clean")), "restraint", False),
    ]
    if count is not None:
        count.append(len(cases))
    for label, reply, grader, axis, should_fail in cases:
        result = grade_one("q", reply, grader, axis, metadata)
        failed = result["verdict"] == "FAIL"
        if failed != should_fail:
            failures.append(
                f"{label}: expected {'FAIL' if should_fail else 'PASS'}, "
                f"got {result['verdict']} ({result['reasons']})")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", action="append", default=None)
    parser.add_argument("--model", action="store_true",
                        help="judge the Anthropic answers (costs API credit)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--show", action="store_true", help="print every answer")
    parser.add_argument("--max-calls", type=int, default=40,
                        help="refuse a --model run larger than this without --yes")
    parser.add_argument("--yes", action="store_true",
                        help="accept the API cost of a large --model run")
    parser.add_argument("--restraint-only", action="store_true",
                        help="only the questions where a model can overclaim")
    args = parser.parse_args()

    print("=" * 78)
    print("JUDGE self-test (can it fail a wrong answer?)")
    print("=" * 78)
    ran: list[int] = []
    problems = self_test(ran)
    for problem in problems:
        print(f"  UNSOUND  {problem}")
    if problems:
        print("\nThe judge itself is unsound. Nothing below can be trusted.")
        return 1
    print(f"  all {ran[0]} negative controls behaved correctly\n")
    if args.self_test:
        return 0

    files = ([Path(g) for g in args.gds] if args.gds else sorted(SAMPLES.glob("*.gds")))

    # One API call per question per file. On the full sample set that is a few hundred
    # calls, which is real money on a small budget, so it has to be asked for.
    if args.model:
        # Built with the same inputs the run uses, or the estimate silently differs
        # from the count - it under-reported by one for every file with a tech file.
        first_reference = find_reference(files[0])
        questions = battery(fact_sheet(files[0]),
                            load_reference(first_reference) if first_reference else None)
        if args.restraint_only:
            questions = [q for q in questions if q[2] == "restraint"]
        per_file = len(questions)
        estimate = min(per_file, args.limit or per_file) * len(files)
        print(f"--model makes about {estimate} API calls "
              f"({len(files)} file(s) x up to {args.limit or per_file} questions).")
        print("The metadata sits in a cached prompt block, so calls after the first "
              "for each file are cheaper, but the count is still the count.")
        if estimate > args.max_calls and not args.yes:
            print(f"\nThat is over the {args.max_calls}-call ceiling. Re-run with "
                  f"--yes to accept the cost, or narrow it:")
            print("    python tools/judge.py --model --gds data/samples/"
                  f"{files[0].name}          # one file")
            print("    python tools/judge.py --model --limit 8            "
                  "     # first 8 questions")
            print("    python tools/judge.py --model --restraint-only     "
                  "     # the questions most worth model review")
            return 2
        print()

    total = failed = skipped = 0
    by_axis: dict[str, list[int]] = {}

    for path in files:
        results = judge(path, args.model, args.limit, args.restraint_only)
        bad = [r for r in results if r["verdict"] == "FAIL"]
        skips = [r for r in results if r["verdict"] in ("SKIP", "DEFER")]
        total += len(results)
        failed += len(bad)
        skipped += len(skips)
        for result in results:
            slot = by_axis.setdefault(result["axis"], [0, 0])
            if result["verdict"] in ("SKIP", "DEFER"):
                continue
            slot[0] += 1
            slot[1] += result["verdict"] == "FAIL"

        source = "MODEL (Anthropic)" if args.model else "deterministic"
        print("=" * 78)
        print(f"{path.name}   {len(results) - len(bad) - len(skips)}/"
              f"{len(results) - len(skips)} passed   [{source}]")
        print("=" * 78)
        for result in results:
            mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip",
                    "DEFER": "-> AI"}[result["verdict"]]
            print(f"  {mark} {result['question']}")
            for reason in result["reasons"]:
                print(f"         ! {reason}")
            if args.show or result["verdict"] == "FAIL":
                snippet = " ".join(result["reply"].split())[:220]
                if snippet:
                    print(f"         > {snippet}")

    print("\n" + "=" * 78)
    for axis, (count, bad_count) in sorted(by_axis.items()):
        print(f"  {axis:<12} {count - bad_count}/{count} passed")
    print(f"  {'TOTAL':<12} {total - failed - skipped}/{total - skipped} passed"
          + (f", {skipped} deferred to the model" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
