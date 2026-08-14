"""The one missing-value predicate, and the one number formatter.

Two rules in the specification govern every table in this application, and both
fail the same way when each section implements them itself: a metric that reads
`0` in one place and `Unavailable` in another is not a formatting inconsistency,
it is two different claims about the layout.

**Missing is not zero.** `via_count = 0` means the layer map named the via layers
and this layout has no shapes on them - a measurement. `via_count = Unavailable`
means via-ness could not be determined at all. `0`, `0.0` and `False` are
measurements and must never be rendered as missing; `None`, `""`, `"n/a"`,
`"unknown"` and any empty container are missing and must never be rendered as a
measurement.

**A delta always carries its sign.** `+2`, `-1`, `+0` - because a table of deltas
where positive numbers are bare reads as a table of values.
"""
from __future__ import annotations

from typing import Any

UNAVAILABLE = "Unavailable"
UNKNOWN = "unknown"

# Strings that mean "nothing was measured". Compared case-insensitively after
# stripping, because these arrive from JSON sidecars and hand-written catalogues
# as well as from the analyzer.
_MISSING_STRINGS = {"", "n/a", "na", "none", "null", "unknown", "unavailable", "-"}


def is_missing(value: Any) -> bool:
    """True when nothing was measured. `0`, `0.0` and `False` are measurements."""
    if value is None:
        return True
    if isinstance(value, bool):            # before the int check: bool is an int
        return False
    if isinstance(value, (int, float)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in _MISSING_STRINGS
    if isinstance(value, (list, tuple, set, dict, frozenset)):
        return len(value) == 0
    return False


def present(value: Any) -> bool:
    return not is_missing(value)


def number(value: Any, decimals: int = 4) -> str:
    """A known number, to at most `decimals` places, trailing zeros removed.

    0.1500 -> "0.15", 3.0 -> "3", 0 -> "0". Never returns "0" for a missing value:
    that is the whole point of `is_missing` being consulted first.
    """
    if is_missing(value):
        return UNAVAILABLE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return str(value)
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def delta(value: Any, decimals: int = 4, unit: str = "") -> str:
    """A signed delta. `+0` is a result - it says the two were measured and equal."""
    if is_missing(value):
        return UNAVAILABLE
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    body = number(abs(value), decimals)
    sign = "-" if value < 0 else "+"
    return f"{sign}{body}{unit}"


def show(value: Any, decimals: int = 4, unit: str = "") -> str:
    """Render any cell: a measurement, or the word Unavailable. Never blank."""
    if is_missing(value):
        return UNAVAILABLE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return number(value, decimals) + unit
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    return str(value)


def percent(new: Any, old: Any, decimals: int = 2) -> str:
    """((B - A) / A) x 100, with A as the denominator, always.

    A baseline of zero has no percentage - going from 0 to 6 vias is a real change
    but not a percentage one - so this returns "N/A" rather than infinity or a
    misleading 0.00%. The absolute difference is shown beside it by the caller.
    """
    if is_missing(new) or is_missing(old):
        return UNAVAILABLE
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)):
        return UNAVAILABLE
    if old == 0:
        return "N/A"
    return f"{(new - old) / old * 100:+.{decimals}f}%"


def difference(new: Any, old: Any):
    """B - A when both are numeric, else the categorical verdict.

    Named values are never subtracted: "M1 minus M0" is not a number, so a
    non-numeric pair reports Same or Different and a pair present on one side only
    reports which side.
    """
    a_missing, b_missing = is_missing(old), is_missing(new)
    if a_missing and b_missing:
        return None                                     # caller omits the row
    if b_missing:
        return "Only in A"
    if a_missing:
        return "Only in B"
    numeric = (isinstance(old, (int, float)) and not isinstance(old, bool)
               and isinstance(new, (int, float)) and not isinstance(new, bool))
    if numeric:
        return new - old
    return "Same" if str(old) == str(new) else "Different"
