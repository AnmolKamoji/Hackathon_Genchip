---
name: gds-verify
description: Verify any GDS analysis number against an independently-written computation before trusting it. Use when adding or changing a measurement, when a reported figure looks suspicious, or when asked whether results are correct.
---

# Verifying a layout number

The rule that has caught every real bug in this project:

> **Never verify the analyzer against itself.** Cross-check every number against a
> separately-written computation, and prove the check can fail before trusting a
> pass.

Comparing `analyze_gds()` output to `analyze_gds()` output, or to a cached
expectation copied from an earlier run of the same code, proves nothing. Several
bugs here survived a full green test suite precisely because the tests restated
what the code did.

## The procedure

1. **Get the ground truth.** `tools/ground_truth.py` re-reads the `.lyp` XML and
   the GDSII with raw KLayout and plain Python and imports nothing from
   `analyzer/`. Areas come from the shoelace formula, components from union-find,
   perimeter from summed edge lengths.

   ```bash
   .venv/bin/python tools/ground_truth.py            # human-readable
   .venv/bin/python tools/ground_truth.py --json     # for diffing
   ```

2. **Diff the tool against it.** `tests/test_ground_truth_agreement.py` does this
   for the whole reference set in the realistic `.gds` + bundled `.lyp` mode. Add
   a case there when you add a measurement.

3. **Use a different formulation, not the same call.** If the analyzer uses
   `Region.area()`, verify with the shoelace formula. If it uses
   `Region.inside()`, verify with `(poly - region).is_empty()`. If it uses
   `Region.merged().count()`, verify with union-find over pairwise
   `interacting()`. Same-call verification finds nothing.

4. **Prove the check can fail.** Perturb the expected value and confirm the check
   goes red. A verification that cannot fail is not a verification —
   `tools/claimcheck.py` runs a negative control for exactly this reason.

5. **Drive the real app, not just the functions.** `tests/test_app_connectivity_e2e.py`
   runs `app.py` through Streamlit's `AppTest`. The duplicate-element-ID crash and
   a `.lyp` being fed to the JSON stack parser were only ever visible from there.

## Bugs this procedure has caught

Keep these in mind; they are the shapes real defects take here.

| Defect | How it was caught |
|---|---|
| Both perimeter fields returned the *merged* outline (KLayout `Region`s use merged semantics by default), so "as drawn" was wrong wherever shapes abut | shoelace + summed edge lengths disagreed on `NPOLY-PATTERN-CUT` |
| A question about *vertices* was answered with a *polygon count*; "total metal area" with the cell bounding-box area | posing all 59 topics as questions and reading every answer |
| An empty top cell was selected alphabetically, making every real cell an "orphan" | a synthetic multi-top layout with a deliberately empty first cell |
| `layer_groups` unioned areas *across* different layer numbers, understating by 2.5× | an independent KLayout script per layer number |
| Cross-mode comparison fabricated a via delta for a file whose via count was unknown | `None - known` must be `None`, asserted directly |
| The prompt digest silently dropped every per-layer row once connectivity was added | measuring the digest size against the budget, not assuming |

## Checklist before claiming a number is right

- [ ] Computed a second time by different code, ideally a different formulation
- [ ] Checked on **all four** reference layouts, not just one
- [ ] Checked in the realistic mode (`.gds` + bundled `.lyp`, no sidecar)
- [ ] The negative control fails when the expected value is perturbed
- [ ] `tools/claimcheck.py` audits any prose that states the number
- [ ] `verify_setup.py` still passes end to end
