#!/usr/bin/env python3
"""Write out the question battery and its answer key, as files you can hand over.

The battery lives in `tools/judge.py` as (question, grader, axis) triples, and the
graders are closures - readable by the judge, not by a person. This exports them:
every question, what a correct answer has to contain, which axis it tests, and the
input file each expected value was measured from.

Nothing here re-derives an expected value. The key comes from `tools/oracle.py`,
which reads the layout with `gdstk` and parses the `.lyp` itself, importing nothing
from `analyzer/` - so the key is an independent measurement, not the analyzer
agreeing with itself.

    python tools/export_question_key.py                 # -> build/question_key/
    python tools/export_question_key.py --zip out.zip
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.layermap import default_layermap, load_lyp        # noqa: E402
from analyzer.techparams import find_reference, load_reference  # noqa: E402
from tools.judge import battery, SAMPLES                        # noqa: E402
from tools.oracle import fact_sheet                             # noqa: E402

AXIS_NOTE = {
    "correctness": "Does the answer state the value the independent oracle measured?",
    "restraint": "Does the answer refuse to claim what a .gds and .lyp cannot support?",
}


def rows_for(gds: Path) -> list[dict[str, str]]:
    """Every question for one layout, with what a correct answer must contain."""
    reference = find_reference(gds)
    stated = load_reference(reference) if reference else None
    out = []
    for question, grader, axis in battery(fact_sheet(gds), stated):
        out.append({
            "file": gds.name,
            "axis": axis,
            "question": question,
            "expected": getattr(grader, "expects", "(grader has no description)"),
        })
    return out


def write(out_dir: Path, samples: list[Path]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    everything: list[dict[str, str]] = []

    for gds in samples:
        rows = rows_for(gds)
        everything.extend(rows)
        stem = gds.stem

        csv_path = out_dir / f"{stem}.questions.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["axis", "question", "expected"])
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row[k] for k in ("axis", "question", "expected")})
        written.append(csv_path)

        json_path = out_dir / f"{stem}.questions.json"
        json_path.write_text(json.dumps({
            "input_gds": gds.name,
            "layer_map": "Titan_layer_properties.lyp (bundled)",
            "answer_key_source": "tools/oracle.py - gdstk + a plain-XML .lyp parse, "
                                 "independent of analyzer/",
            "question_count": len(rows),
            "questions": [{k: r[k] for k in ("axis", "question", "expected")}
                          for r in rows],
        }, indent=2), encoding="utf-8")
        written.append(json_path)

    combined = out_dir / "ALL_QUESTIONS.csv"
    with combined.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "axis", "question", "expected"])
        writer.writeheader()
        writer.writerows(everything)
    written.append(combined)

    written.append(_write_markdown(out_dir, samples, everything))
    written.append(_write_readme(out_dir, samples, everything))
    return written


def _write_markdown(out_dir: Path, samples: list[Path], rows: list[dict]) -> Path:
    lines = ["# Question battery and answer key", "",
             f"Generated {date.today().isoformat()} from the sample layouts in "
             "`data/samples/`.", ""]
    for gds in samples:
        mine = [r for r in rows if r["file"] == gds.name]
        lines += [f"## {gds.name}", "",
                  f"{len(mine)} questions "
                  f"({sum(1 for r in mine if r['axis'] == 'correctness')} correctness, "
                  f"{sum(1 for r in mine if r['axis'] == 'restraint')} restraint).", "",
                  "| # | Axis | Question | A correct answer |",
                  "|---|---|---|---|"]
        for n, row in enumerate(mine, 1):
            lines.append(f"| {n} | {row['axis']} | {row['question']} | {row['expected']} |")
        lines.append("")
    return _write(out_dir / "ANSWER_KEY.md", "\n".join(lines))


def _write_readme(out_dir: Path, samples: list[Path], rows: list[dict]) -> Path:
    correctness = sum(1 for r in rows if r["axis"] == "correctness")
    restraint = sum(1 for r in rows if r["axis"] == "restraint")
    text = f"""# Judging the answers — questions, expected answers, and inputs

Generated {date.today().isoformat()}. {len(rows)} questions across
{len(samples)} layouts: {correctness} correctness, {restraint} restraint.

## Input files

The questions are not hand-written per file. Each one is generated from what the
layout actually contains, so the same battery runs on any `.gds`.

| Input | Role |
|---|---|
{chr(10).join(f"| `data/samples/{g.name}` | layout under test |" for g in samples)}
| `data/samples/Titan_layer_properties.lyp` | layer map — turns layer numbers into names and roles |
| `data/samples/AN2D1_2_RT_4.techparams.json` | a *stated* tech-file table, used only to test restraint: a stated figure must never be passed off as a measurement |

## Where the expected answers come from

`tools/oracle.py` reads the layout with **gdstk** and parses the `.lyp` as plain
XML. It imports nothing from `analyzer/`. So an answer that matches the key agrees
with a separately written program, not with the analyzer's own output.

## The three axes

| Axis | Question it asks |
|---|---|
| Correctness | {AXIS_NOTE['correctness']} |
| Grounding | Is every number in the answer present in the metadata it was given? Catches figures that were derived, converted or invented. Applied to every answer, so it has no rows of its own here. |
| Restraint | {AXIS_NOTE['restraint']} |

Restraint questions are the ones with no right numeric answer: DRC cleanliness,
shorts, opens, LVS, timing. A `.gds` and a `.lyp` cannot support any of them, and
an answer that produces a verdict anyway has failed even if it sounds careful.
One is a leading question — the premise is a measurement, the conclusion is not.

## Files in this bundle

- `ALL_QUESTIONS.csv` — every question, one row each, with its file and axis.
- `ANSWER_KEY.md` — the same, as readable tables per layout.
- `<layout>.questions.csv` / `.json` — one file per layout.
- `transcripts/` — real graded answers, if any had been recorded: the question,
  the model, the reply, the verdict and the reason. `python tools/judge.py
  --regrade <file>` re-grades them for free.

## Reproducing

    python tools/judge.py                    # deterministic answers, no API cost
    python tools/judge.py --model            # judge the Anthropic answers too
    python tools/judge.py --self-test        # prove the judge can fail things
    python tools/export_question_key.py      # regenerate this bundle

## A note on the design rule manual

No text from the GENCHIP Design Rule Manual is reproduced here. The expected values
are measurements taken from the layouts listed above; the parameter names are the
ones in the tech-file table supplied with the project.
"""
    return _write(out_dir / "README.md", text)


def _slim(log: Path, target: Path) -> Path:
    """One row per graded answer: question, model, verdict, reason, reply."""
    fields = ["file", "model", "axis", "question", "verdict", "reasons", "reply"]
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            writer.writerow({
                "file": record.get("file", ""),
                "model": record.get("model", ""),
                "axis": record.get("axis", ""),
                "question": record.get("question", ""),
                "verdict": record.get("verdict", ""),
                "reasons": "; ".join(record.get("reasons") or []),
                "reply": " ".join((record.get("reply") or "").split()),
            })
    return target


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(ROOT / "build" / "question_key"),
                        help="directory to write into")
    parser.add_argument("--zip", dest="zip_path", default=None,
                        help="also write a .zip of the directory")
    parser.add_argument("--gds", nargs="*", default=None,
                        help="layouts to export for (default: every sample)")
    parser.add_argument("--transcripts", default=str(ROOT / "build" / "judge"),
                        help="graded-answer logs to include, if present")
    args = parser.parse_args()

    samples = ([Path(g) for g in args.gds] if args.gds
               else sorted(SAMPLES.glob("*.gds")))
    missing = [g for g in samples if not g.exists()]
    if missing:
        print("not found: " + ", ".join(str(m) for m in missing))
        return 1

    out_dir = Path(args.out)
    written = write(out_dir, samples)

    # The recorded answers matter as much as the questions: a key nobody ran against
    # is a claim, not a result.
    logs = sorted(Path(args.transcripts).glob("*.jsonl")) if args.transcripts else []
    for log in logs:
        target = out_dir / "transcripts" / log.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)
        # The .jsonl is what `--regrade` reads, but most of its bulk is the metadata
        # digest repeated on every record. This is the readable form: what was asked,
        # what came back, and how it was graded.
        written.append(_slim(log, target.with_suffix(".answers.csv")))

    for path in written:
        print(f"  wrote {path.relative_to(ROOT)}")

    if args.zip_path:
        zip_path = Path(args.zip_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(out_dir.parent))
        print(f"\n  zipped -> {zip_path} "
              f"({zip_path.stat().st_size / 1024:.1f} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
