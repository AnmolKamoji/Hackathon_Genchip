---
name: gds-new-analysis
description: Add a new layout analysis and wire it through every layer that must know about it. Use when implementing a new measurement, metric, or check for the GDS reviewer.
---

# Adding an analysis end to end

A half-wired analysis is worse than none: the app shows a number the Q&A cannot
explain, the model invents figures it was never given, or the fact-checker flags
correct output. There are **eight** places that must change, and the order matters.

## 1. Decide availability first

Run the `gds-availability` checklist. If the answer is "Requires PDK" or "Requires
netlist", stop — the deliverable is a labelled refusal in `UNCOMPUTED_METRICS`, not
a feature.

## 2. Write the analyzer module

`analyzer/` — one module per concern (`measurements.py`, `hierarchy.py`,
`connectivity.py`). Rules:

- `None` for undeterminable, never `0`.
- Name measured quantities `observed_*` so they cannot read as verdicts.
- Include `availability` and `basis` strings, and a `not_derivable` block stating
  the boundaries.
- Guard expensive work with a shape-count limit and warn rather than silently
  approximating (`MAX_SHAPES_FOR_CHECKS`).
- Take the top cell from `gds_parser.rank_top_cells()`, never
  `sorted(top_cells())[0]` — that can select an empty placeholder cell.
- Beware KLayout defaults: a `Region` uses **merged semantics**, so
  `Region.perimeter()` on an unmerged region returns the merged outline.

## 3. Verify against ground truth

Add a case to `tools/ground_truth.py` and `tests/test_ground_truth_agreement.py`,
using a different formulation rather than the same KLayout call. See `gds-verify`.

## 4. Attach it to the metadata

Both entry points, or the app and CLI disagree:

- `app.py` → `process_extras()` / `process_connectivity()`, then attach onto the
  metadata dict.
- `analyze.py` → the per-file loop.

## 5. Answer questions about it deterministically

`ai/deterministic.py`:

- Add a `*_TRIGGER` regex and an `_*_answer()` helper.
- **Dispatch it before the loose branches.** The branches matching `area`, `size`
  and `each layer` will otherwise answer your question with a different metric.
- Check both forms: a question naming one layer, and the whole-design version.

## 6. Put it in the AI digest

`ai/llm.py`:

- Add a `_slim_*` reducer and include it in `_digest()` **before** the expendable
  per-layer rows.
- Add its lists to the shrink ladder in `_compact()`. Connectivity once evicted
  every layer row from the 12k local-model budget.
- If it carries boundaries the model must respect, add the key to `_ESSENTIAL`.

## 7. Validate the contract

`models/metadata.py` — add a `validate_*` asserting the invariants a future change
could break. Prefer invariants over values: "components ≤ shapes", "union ≤ sum",
"a net graph must record its stack source".

## 8. Teach the fact-checker and test it

- `tools/claimcheck.py` — add the new figures to the allowed sets, and a rule if
  the claim has a new phrasing. See `gds-claim-audit`.
- `tests/` — a unit test file, a ground-truth agreement case, and an entry in the
  `AppTest` end-to-end file.
- `CAPABILITIES.md` — add the row with its availability label.

## Final gate

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python verify_setup.py
.venv/bin/python tools/claimcheck.py --deterministic-only
.venv/bin/python tools/ground_truth.py
```

All four must be clean. A green unit suite alone has repeatedly hidden defects that
`verify_setup.py` or the app-level tests caught.
