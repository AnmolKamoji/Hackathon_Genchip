#!/usr/bin/env python3
"""Build the design-rule catalogue from the GENCHIP Design Rule Manual.

The catalogue is not in version control. It is transcribed from a manual whose own
copyright notice forbids reproducing or transmitting it without written permission,
so publishing the transcription would publish the manual's text. This script
regenerates it locally from a copy of the PDF you already have.

    pip install pypdf
    python tools/extract_drm_rules.py                       # expects data/*.pdf
    python tools/extract_drm_rules.py --pdf /path/to.pdf    # or point at it

Everything else in the tool works without this: geometry, connectivity, the XOR
comparison and the cell classification need only the .gds and the .lyp. Rule
checking is the one feature that requires the manual.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "genchip_drm_rules.json"


def find_manual(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise SystemExit(f"No such file: {path}")
        return path
    candidates = sorted((ROOT / "data").glob("*.pdf")) + sorted(ROOT.glob("*.pdf"))
    for path in candidates:
        if "design" in path.name.lower() and "rule" in path.name.lower():
            return path
    if candidates:
        return candidates[0]
    raise SystemExit(
        "No PDF found. Put the Design Rule Manual in data/ or pass --pdf <path>.")


def extract(pdf: Path) -> list[dict]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is required:  pip install pypdf")

    reader = PdfReader(str(pdf))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)

    # Page furniture and figure captions sit between the rules and would otherwise
    # be absorbed into whichever rule precedes them.
    text = re.sub(r"Page \d+ of \d+\s*\n?GENCHIP Confidential\s*", "", text)
    text = re.sub(r"Figure [\d\-]+:[^\n]*\n", "", text)

    # The heading appears in the table of contents as well as at the section itself;
    # the last occurrence is the real one.
    marker = "3. Layout Design Rules"
    if marker not in text:
        raise SystemExit(f"{pdf.name} does not contain a '{marker}' section - is this the manual?")
    body = text[text.rindex(marker):]

    rules: list[dict] = []
    for section in re.split(r"\n(?=3\.\d+ )", body):
        head = section.split("\n", 1)[0].strip()
        match = re.match(r"(3\.\d+)\s+(.*)", head)
        if not match:
            continue
        number, title = match.group(1), match.group(2).strip()
        for index, raw in re.findall(r"^\s*(\d+)\.\s+(.+?)(?=\n\s*\d+\.\s|\Z)", section,
                                     re.S | re.M):
            rule = " ".join(raw.split())
            rule = re.sub(r"\(Refer:?[^)]*\)", "", rule).strip(" .")
            # Figure labels leak in as short all-caps tails.
            rule = re.sub(r"\s+(?:BM0|M0|M1|DVB|BM0 Pin|M0 Pin)"
                          r"(?:\s+(?:BM0|M0|M1|Pin|\(Internal net\)))*$", "", rule)
            if len(rule) < 12:
                continue
            lower = rule.lower()
            technologies = [t for t, key in (("CFET", "cfet"), ("FinFET", "finfet"),
                                             ("GAA", "gaa")) if key in lower]
            rules.append({"id": f"{number}.{index}", "section": number,
                          "section_title": title, "rule": rule,
                          "technologies": technologies or ["all"]})
    return rules


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=None, help="path to the Design Rule Manual PDF")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    pdf = find_manual(args.pdf)
    rules = extract(pdf)
    if not rules:
        raise SystemExit(f"No rules could be extracted from {pdf.name}.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "source": "GENCHIP Design Rule Manual, June 2026 (GENCHIP Confidential)",
        "note": ("Rules transcribed verbatim from the manual. Almost all are relational "
                 "(\"X should equal Y\") rather than absolute, which is why they can be checked "
                 "against geometry without a numeric rule deck. Absolute parameter values "
                 "(M0 width, routing pitch, via extension) are named but not given in the manual, "
                 "so they are derived from the layout and reported as observed."),
        "rule_count": len(rules), "rules": rules}, indent=1), encoding="utf-8")

    sections = sorted({r["section"] for r in rules}, key=lambda s: [int(p) for p in s.split(".")])
    print(f"Extracted {len(rules)} rules from {pdf.name} across {len(sections)} sections.")
    print(f"Written to {out}")
    print("\nThis file is gitignored on purpose: it reproduces the manual's text, and the "
          "manual forbids redistribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
