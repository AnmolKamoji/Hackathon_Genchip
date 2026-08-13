---
name: gds-claim-audit
description: Audit answers (model or deterministic) for numbers that are not supported by the metadata, without burning API credits. Use after changing any answer text, prompt, or digest, or when asked to check whether answers are accurate.
---

# Auditing the answers

Every number in an answer must appear in the metadata that produced it. The
fact-checker enforces that contextually — a number *plus the field it is asserted
about* — because a bag-of-numbers membership test is useless here: layout metadata
holds hundreds of coordinates and indices, so almost any small integer appears
somewhere by coincidence. An early version passed "73 polygons" and "12 vias"
against a design that had neither.

## Running it

```bash
# No API calls: audits every deterministic answer. Use this by default.
.venv/bin/python tools/claimcheck.py --deterministic-only

# The negative control on its own - proves the checker can fail.
.venv/bin/python tools/claimcheck.py --self-test
```

**Budget discipline:** the API budget is small and reserved for the demo. Audit
offline by default. A live call is worth it only to test a *boundary* — whether the
model refuses something it should refuse — not to re-check arithmetic.

## When the checker flags something

Two possibilities, and they need opposite fixes:

1. **The answer is wrong.** Fix the answer.
2. **The checker does not know the field.** Fix the checker.

Case 2 is common after adding an analysis, and it is not a licence to ignore the
tool. Add the real source to the allowed set in `Checker.__init__`, keyed by the
kind of claim it is. Examples already handled:

- `arrangement.horizontal_pitches_um` → `lengths`
- `role_aggregates[*].total_area_um2` → `areas`
- `hierarchy.cell_count_total` → `cell_counts`
- per-net `layer_count` → `layer_counts`

If a number genuinely is not in the metadata, do not add it to the allowed set to
silence the tool. That inverts its purpose.

## Claim-kind collisions

The rules are ordered and several phrases match more than one. Each of these was a
real false positive:

| Phrase | Wrongly audited as | Fix |
|---|---|---|
| `240 polygon vertices` | 240 polygons | vertex rule ordered first, plus `(?!\s*vert)` on the polygon rule |
| `8 via/contact layer(s)` | 8 vias | lookahead widened to exclude a following `/` |
| `9 conductor layer(s)` | 9 within-layer conductors | `(?!\s+layer)` on the conductor rule |
| `5e-05 µm` | a claim of `-05 µm` | `NUM` now captures the exponent and refuses to start mid-word |

When adding a rule, put the **more specific pattern first** and add a negative
lookahead to the general one.

## Also check attribution, not just membership

`audit_attribution` catches a real figure pinned to the wrong layer — "BM0 covers
0.00246 µm²" when that is M0's area. Membership testing cannot see this, because
the number is genuinely in the metadata.

Sentence splitting is deliberately conservative: `_SPLIT` breaks on punctuation
followed by whitespace, **never on a bare period** (it tore `0.00246` in half) and
**never on a colon** (which separated `- **BM0**: 0.0153` from its label).

## Checklist after touching any answer text

- [ ] `--self-test` passes, so the checker can still catch fabrications
- [ ] `--deterministic-only` reports 0 unsupported
- [ ] Any new figure has a real source in `Checker.__init__`
- [ ] A number's breakdown sums to its headline (a via list once summed to 12
      under a count of 6)
- [ ] Prose that states an inferred figure says it is inferred
