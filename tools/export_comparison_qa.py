#!/usr/bin/env python3
"""Write out the comparison questions and the answers the tool gives.

Two batteries, both drawn from what a physical-design engineer asks when handed two
revisions of a layout:

* **Review** - what changed, where, how much, is it metal-only, did a pin move, did
  the counts change, does B break a rule A did not.
* **Parasitics** - which layout is worse for R and C, and why. The geometry that
  drives them is in the file; the constants that turn it into ohms and farads are
  not, so the battery is run twice: without a process file and with one.

The answers are generated, not transcribed: this runs the same `answer_pair` the chat
uses, on the same analyses, so what it prints is what a user gets.

    python tools/export_comparison_qa.py
    python tools/export_comparison_qa.py --a data/samples/X.gds --b data/samples/Y.gds
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.compare import answer_pair                                    # noqa: E402
from analyzer.classify import classify                                # noqa: E402
from analyzer.connectivity import analyze_connectivity, default_stack  # noqa: E402
from analyzer.drc import check_layout                                 # noqa: E402
from analyzer.edit import grid_audit                                  # noqa: E402
from analyzer.gds_parser import analyze_gds                           # noqa: E402
from analyzer.layermap import default_layermap, load_lyp              # noqa: E402
from analyzer.measurements import (measure_layers, measure_vias,      # noqa: E402
                                   shape_outlines)
from analyzer.netlist import extract as extract_netlist               # noqa: E402
from analyzer.parasitics import (estimate_rc, load_process,           # noqa: E402
                                 wire_geometry)
from analyzer.pitch import analyze_pitch                              # noqa: E402
from analyzer.xor_diff import xor_compare                             # noqa: E402

SAMPLES = ROOT / "data" / "samples"

# What a reviewer asks of two revisions. Grouped the way the review happens: the
# change first, then its blast radius, then whether anything broke.
REVIEW = [
    ("What changed", [
        "What changed between these two layouts?",
        "Which layers changed?",
        "Where are the differences?",
        "What is the largest difference?",
        "How much area changed?",
    ]),
    ("Mask and ECO impact", [
        "Is this a metal-only change?",
        "Do I need a base layer respin?",
        "Which masks are affected?",
        "Can this be an ECO?",
    ]),
    ("Pins and labels", [
        "Did any pin move?",
        "Did the pin names change?",
    ]),
    ("Counts and dimensions", [
        "Did the cell size change?",
        "Did the number of polygons change?",
        "Did the via count change?",
        "Did the transistor count change?",
        "Did the gate pitch change?",
        "Did metal density change?",
    ]),
    ("Rules, grid, technology", [
        "Does B introduce any DRC violations that A did not have?",
        "Is B still on grid?",
        "Are both layouts the same technology?",
        "Does B use any layer that A does not?",
        "Did connectivity change?",
    ]),
    ("Refused on purpose", [
        "Is B better than A?",
        "Which one should I use?",
        "Will B pass timing?",
        "Is this change safe to tape out?",
        "Did the leakage change?",
        "Will this hurt yield?",
    ]),
]

# The parasitic battery. These are answered from the geometry that drives R and C,
# and only become ohms and farads when a process file supplies the constants.
PARASITICS = [
    "Which layout has more capacitance?",
    "Which has higher resistance?",
    "Is the RC worse in B?",
    "Which has more coupling?",
    "Is there more parasitic loading in B?",
    "Which layout would have worse crosstalk?",
    "Any IR drop concern?",
    "Did the wire length change?",
    "Which one is better for capacitance?",
    "Will the extra resistance hurt timing?",
]


def build_context(a: Path, b: Path, process: Path | None = None) -> dict:
    """Everything the chat has, assembled the same way the page assembles it."""
    layermap = load_lyp(default_layermap())
    stack = default_stack(layermap)
    overrides = stack.get("role_overrides")
    constants = load_process(process) if process else None

    def side(path: Path) -> dict:
        outlines = shape_outlines(path, layermap)
        metadata = analyze_gds(path, layermap=layermap)
        classification = classify(outlines, path, [path.name])
        classification["pitch"] = analyze_pitch(outlines, path.name)
        metadata["classification"] = classification
        metadata["pitch"] = classification["pitch"]
        metadata["outlines"] = outlines
        measurements = measure_layers(path, layermap)
        measurements["vias"] = measure_vias(measurements)
        metadata["measurements"] = measurements
        geometry = wire_geometry(path, layermap, role_overrides=overrides)
        return {
            "file": path.name,
            "metadata": metadata,
            "drc": check_layout(outlines),
            "connectivity": analyze_connectivity(path, layermap, stack=stack),
            "netlist": extract_netlist(path, layermap, stack),
            "grid": grid_audit(path, 1.0),
            "parasitics": geometry,
            "rc": estimate_rc(geometry, constants) if constants else None,
        }

    return {"xor": xor_compare(a, b, layermap), "a": side(a), "b": side(b)}


def collect(context: dict) -> list[dict]:
    rows = []
    for group, questions in REVIEW:
        for question in questions:
            rows.append({"battery": "review", "group": group, "question": question,
                         "answer": answer_pair(context, question) or "(handed to the model)"})
    for question in PARASITICS:
        rows.append({"battery": "parasitics", "group": "Resistance and capacitance",
                     "question": question,
                     "answer": answer_pair(context, question) or "(handed to the model)"})
    return rows


def write(out_dir: Path, a: Path, b: Path, rows: list[dict],
          rows_with_process: list[dict] | None, process: Path | None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    csv_path = out_dir / "comparison_questions.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["battery", "group", "question",
                                                    "answer"])
        writer.writeheader()
        writer.writerows(rows)
    written.append(csv_path)

    lines = [f"# Comparison questions and answers", "",
             f"`{a.name}` → `{b.name}`, generated {date.today().isoformat()}.", "",
             "Every answer below is produced by the same code the chat uses, from the "
             "same measurements. Nothing is transcribed by hand.", ""]
    group = None
    for row in rows:
        if row["group"] != group:
            group = row["group"]
            lines += [f"## {group}", ""]
        lines += [f"**{row['question']}**", "", row["answer"], ""]

    if rows_with_process and process:
        lines += ["---", "",
                  f"## The same parasitic questions, with `{process.name}` loaded", "",
                  "The geometry is unchanged; the constants turn it into ohms and "
                  "farads.", ""]
        for row in rows_with_process:
            if row["battery"] != "parasitics":
                continue
            lines += [f"**{row['question']}**", "", row["answer"], ""]

    markdown = out_dir / "COMPARISON_ANSWERS.md"
    markdown.write_text("\n".join(lines), encoding="utf-8")
    written.append(markdown)

    payload = {"a": a.name, "b": b.name, "generated": date.today().isoformat(),
               "questions": rows,
               "with_process": rows_with_process or []}
    json_path = out_dir / "comparison_questions.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    written.append(json_path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a", default=str(SAMPLES / "DCAP0_1_RT_4.gds"))
    parser.add_argument("--b", default=str(SAMPLES / "DCAP0_2_RT_4.gds"))
    parser.add_argument("--process", default=str(SAMPLES / "example_process.json"),
                        help="process constants, to also answer in ohms and farads")
    parser.add_argument("--out", default=str(ROOT / "build" / "comparison_qa"))
    args = parser.parse_args()

    a, b = Path(args.a), Path(args.b)
    process = Path(args.process) if args.process else None
    if process and not process.exists():
        process = None

    rows = collect(build_context(a, b))
    with_process = collect(build_context(a, b, process)) if process else None
    for path in write(Path(args.out), a, b, rows, with_process, process):
        print(f"  wrote {path.relative_to(ROOT)}")

    answered = sum(1 for row in rows if not row["answer"].startswith("(handed"))
    refused = sum(1 for row in rows if row["answer"].startswith("I cannot tell you"))
    print(f"\n  {answered}/{len(rows)} answered from measurements, "
          f"{refused} refused on purpose")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
