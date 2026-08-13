# Reference reports — historical baseline, do not regenerate

These three files are the **original** expected outputs that shipped with the
project, produced before the analyzer was audited and corrected.

They are kept deliberately unmodified. `tests/test_sidecar.py` compares current
output against them as an *independent* baseline, which is only meaningful while
they stay frozen: regenerating them would make the test compare the analyzer with
itself and it could never fail again.

The comparison is a **subset** check — every field these files record must still
match, while newer fields (`polygon_record_count`, `layer_groups`,
`geometry_fingerprint`, `technology_name`, …) may be added alongside. Row order is
compared by `(layer, datatype, name)` rather than by position, because the sort
key was fixed to include datatype (it previously ordered `"10"` before `"2"`
lexicographically).

Current output goes to `reports/` via:

    python analyze.py data/samples/NR2D1_1_RT_4.gds data/samples/NR2D1_2_RT_4.gds
