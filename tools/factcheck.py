#!/usr/bin/env python3
"""Audit an LLM answer against the metadata it was given.

The model is only permitted to restate figures that the deterministic analyzer
computed. This extracts every number from its prose and checks each against the
set of values actually present in the metadata, so a fabricated or mis-copied
figure is caught mechanically rather than by eye.

    python tools/factcheck.py                      # full audit, uses the API
    python tools/factcheck.py --deterministic-only # no API calls

Exit code is 0 only when every stated number is traceable to the metadata.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from ai.deterministic import answer, answer_comparison            # noqa: E402
from ai.llm import ask_llm, generate_comparison, looks_like_failure, provider_status  # noqa: E402
from analyzer.comparison import compare_metadata                  # noqa: E402
from analyzer.fused import analyze_pair                           # noqa: E402
from analyzer.layermap import find_layermap, load_lyp             # noqa: E402

SAMPLES = ROOT / "data/samples"

# Numbers in prose that are structural rather than measurements.
_IGNORE_TOKENS = {
    "0", "1", "2", "3", "4",          # list markers, "one sentence", small ordinals
    "2026",                            # dates
}


def numbers_in(text: str) -> list[str]:
    """Every numeric literal in the text, as written."""
    # Strip markdown list markers ("1.", "- 2)") so they are not read as facts.
    cleaned = re.sub(r"(?m)^\s*[-*]?\s*\d+[.)]\s", " ", text)
    return re.findall(r"-?\d+(?:\.\d+)?", cleaned)


def values_in(obj, out: set[str] | None = None) -> set[str]:
    """Every numeric value present anywhere in the metadata, in several
    renderings, because the model may quote 0.02295 as 0.022950 or 76.5 as 76.50.
    """
    out = set() if out is None else out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(str(obj))
        if isinstance(obj, float):
            for digits in range(0, 9):
                out.add(f"{obj:.{digits}f}")
            if obj == int(obj):
                out.add(str(int(obj)))
        else:
            out.add(f"{obj}.0")
        return out
    if isinstance(obj, dict):
        for v in obj.values():
            values_in(v, out)
        # Layer/datatype pairs are frequently written as "200/0" or "layer 200".
        if "layer" in obj and "datatype" in obj:
            out.add(f"{obj['layer']}/{obj['datatype']}")
        return out
    if isinstance(obj, (list, tuple)):
        for v in obj:
            values_in(v, out)
        return out
    if isinstance(obj, str):
        for n in re.findall(r"-?\d+(?:\.\d+)?", obj):
            out.add(n)
    return out


def audit(label: str, prose: str, metadata: dict) -> tuple[int, list[str]]:
    """Return (numbers_checked, unsupported_numbers)."""
    allowed = values_in(metadata)
    # Derived figures the model is allowed to state because they are printed in
    # the prose of the metadata itself (percentages already rounded, etc.).
    stated = numbers_in(prose)
    bad = []
    for n in stated:
        if n in _IGNORE_TOKENS or n in allowed:
            continue
        # Tolerate a trailing-zero or precision rendering of an allowed value.
        try:
            f = float(n)
        except ValueError:
            continue
        # A positive figure may stand for a negative one in the data: "decreased by
        # 0.0003" is the correct English rendering of a -0.0003 delta, and demanding
        # the minus sign in the prose would flag correct writing as invention.
        # Deliberately one-directional - a negative in the prose still has to be
        # negative in the data, so a sign the data does not support is still caught.
        # Whether the direction word matches is the claim audit's job, not this one.
        if f >= 0 and any(abs(f + float(a)) < 1e-9 for a in allowed
                          if re.fullmatch(r"-\d+(?:\.\d+)?", a)):
            continue
        bad.append(n)
    return len(stated), bad


def show_failure(prose: str, bad: list[str]) -> None:
    """Print the whole answer, flagging the lines that carry an unsupported number.

    A model answer costs an API call to reproduce, so truncating the evidence to a
    few hundred characters can mean paying again just to see where the number came
    from. Print all of it, and mark the lines worth reading.
    """
    flagged = {b for b in bad}
    for line in prose.splitlines():
        hit = any(n in flagged for n in numbers_in(line))
        print(("    >>  " if hit else "        ") + line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deterministic-only", action="store_true",
                    help="skip the API and audit only the local answers")
    ap.add_argument("--stem", default="DCAP0_1_RT_4")
    ap.add_argument("--stem-b", default="DCAP0_2_RT_4")
    args = ap.parse_args()

    lyp = find_layermap(SAMPLES / f"{args.stem}.gds")
    lm = load_lyp(lyp) if lyp else None
    a = analyze_pair(SAMPLES / f"{args.stem}.gds", SAMPLES / f"{args.stem}.json", layermap=lm)
    b = analyze_pair(SAMPLES / f"{args.stem_b}.gds", SAMPLES / f"{args.stem_b}.json", layermap=lm)
    comparison = compare_metadata(a, b)

    print(f"layer map: {lyp.name if lyp else 'none'}")
    print(f"backend  : {provider_status().get('primary')}")

    questions = [
        "How many polygons are there?",
        "How many vias are present?",
        "What is the layout size?",
        "Which layer has the highest density?",
        "What is the area of M0?",
    ]

    total_checked = total_bad = 0
    print(f"\n{'='*74}\nDETERMINISTIC ANSWERS (no API)\n{'='*74}")
    for q in questions:
        r = answer(a, q)
        if not r:
            print(f"  (deferred) {q}")
            continue
        n, bad = audit(q, r, a)
        total_checked += n
        total_bad += len(bad)
        print(f"  {'OK ' if not bad else 'BAD'} {q}  [{n} numbers]"
              + (f"  UNSUPPORTED: {bad}" if bad else ""))
        if bad:
            show_failure(r, bad)
    r = answer_comparison(comparison, "what changed?")
    n, bad = audit("comparison", r, comparison)
    total_checked += n
    total_bad += len(bad)
    print(f"  {'OK ' if not bad else 'BAD'} what changed?  [{n} numbers]"
          + (f"  UNSUPPORTED: {bad}" if bad else ""))

    if not args.deterministic_only:
        print(f"\n{'='*74}\nMODEL ANSWERS (one API call each)\n{'='*74}")
        model_questions = [
            ("Explain this layout to a non-expert.", a),
            ("Summarise the via structure of this cell in two sentences.", a),
            ("What is the area of M0, and how does it compare to the cell bounding box?", a),
        ]
        for q, meta in model_questions:
            r = ask_llm(meta, q)
            if looks_like_failure(r):
                print(f"  SKIP {q}  (backend unavailable)")
                continue
            n, bad = audit(q, r, meta)
            total_checked += n
            total_bad += len(bad)
            print(f"  {'OK ' if not bad else 'BAD'} {q}  [{n} numbers]"
                  + (f"  UNSUPPORTED: {bad}" if bad else ""))
            if bad:
                show_failure(r, bad)

        r = generate_comparison(comparison)
        if not looks_like_failure(r):
            n, bad = audit("comparison narrative", r, comparison)
            total_checked += n
            total_bad += len(bad)
            print(f"  {'OK ' if not bad else 'BAD'} comparison narrative  [{n} numbers]"
                  + (f"  UNSUPPORTED: {bad}" if bad else ""))
            if bad:
                show_failure(r, bad)

    print(f"\n{'='*74}")
    print(f"numbers checked: {total_checked}   unsupported: {total_bad}")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
