"""Reading a numeric limit out of the design rule manual - and refusing to.

This manual is relational. It equates one measurement to another ("these two
widths should be equal") far more often than it fixes a number, and it names M0
width, routing pitch and via extension as parameters without ever assigning them
values. So the honest result for most numeric checks is `unavailable` carrying the
measurement, and that is what this module is built to produce.

The danger is the opposite error, and it is a specific one. The manual illustrates
its relational rules with parenthetical examples - "(for example 16 nm)". A naive
"find a number in the rule text" would lift that 16 and present it as a limit, and
every layout would then be measured against an example. Three filters stop that,
in order:

1. **Strip brackets first.** Anything in parentheses or square brackets is an
   illustration in this manual, never the rule.
2. **Reject illustrative rules outright.** A rule whose remaining text says "for
   example", "e.g.", "such as" or "valid case" is not prescribing.
3. **Require prescriptive wording.** What is left must actually command:
   "minimum", "at least", "no less than", "maximum", "at most", "must be",
   "shall be".

Only a rule that survives all three yields a limit. Everything else reports
`unavailable` with the reason, and never `pass`.
"""
from __future__ import annotations

import re
from typing import Any

from analyzer.values import UNAVAILABLE

# Bracketed text is illustration in this manual, so it goes before anything is read.
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")

# A rule that says one of these is showing an example, not setting a limit.
_ILLUSTRATIVE = (
    "for example", "e.g.", "eg.", "example", "such as", "valid case",
    "for instance", "illustrat", "typical",
)

# A rule that prescribes says one of these.
_PRESCRIPTIVE_MIN = ("minimum", "at least", "no less than", "not less than",
                     "must be at least", "shall be at least")
_PRESCRIPTIVE_MAX = ("maximum", "at most", "no more than", "not more than",
                     "no greater than")
_PRESCRIPTIVE_ANY = ("must be", "shall be", "is required to be", "required to be")

# A number with an optional unit. nm and um are the only units this manual uses.
_VALUE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(nm|um|µm|micron[s]?)\b", re.I)


def strip_brackets(text: str) -> str:
    """Remove every parenthetical, then collapse the whitespace it left behind."""
    return re.sub(r"\s{2,}", " ", _BRACKETED.sub(" ", text or "")).strip()


def is_illustrative(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _ILLUSTRATIVE)


def prescriptive_kind(text: str) -> str | None:
    """"min", "max", "exact" - or None when the rule does not prescribe at all."""
    low = (text or "").lower()
    if any(w in low for w in _PRESCRIPTIVE_MIN):
        return "min"
    if any(w in low for w in _PRESCRIPTIVE_MAX):
        return "max"
    if any(w in low for w in _PRESCRIPTIVE_ANY):
        return "exact"
    return None


def required_value(rule_text: str) -> dict[str, Any]:
    """The limit this rule prescribes, or a refusal carrying its reason.

    Returns `{"available": bool, "value_um": float|None, "kind": str|None,
    "reason": str}`. `available` is True only when a limit was genuinely
    prescribed; the caller must render `unavailable` otherwise and must never
    substitute a pass.
    """
    raw = rule_text or ""
    stripped = strip_brackets(raw)

    if not stripped:
        return {"available": False, "value_um": None, "kind": None,
                "reason": "the rule text is empty once its illustrations are removed"}

    if is_illustrative(stripped):
        return {"available": False, "value_um": None, "kind": None,
                "reason": ("the rule illustrates rather than prescribes, so any "
                           "number in it is an example and not a limit")}

    kind = prescriptive_kind(stripped)
    if kind is None:
        return {"available": False, "value_um": None, "kind": None,
                "reason": ("the rule is relational: it states no prescriptive "
                           "wording, so it fixes no numeric limit")}

    match = _VALUE.search(stripped)
    if not match:
        return {"available": False, "value_um": None, "kind": kind,
                "reason": ("the rule prescribes but names no value, so the "
                           "parameter is named without being given a limit")}

    value = float(match.group(1))
    unit = match.group(2).lower()
    value_um = value / 1000.0 if unit == "nm" else value
    return {"available": True, "value_um": value_um, "kind": kind,
            "stated": f"{value:g} {unit}",
            "reason": f"the rule prescribes a {kind} of {value:g} {unit}"}


def compare(measured_um: float | None, rule_text: str) -> dict[str, Any]:
    """One measurement against one rule. Reports unavailable rather than pass.

    The flow the specification fixes is: geometry measured from the GDS -> the
    layer identified by the layer map -> the required value read from the manual ->
    the two compared. If the third step yields nothing, the comparison does not
    happen and the measurement stands alone.
    """
    limit = required_value(rule_text)
    out = {
        "measured_um": measured_um,
        "required_um": limit.get("value_um"),
        "required_kind": limit.get("kind"),
        "difference_um": None,
        "status": "unavailable",
        "why": limit["reason"],
    }
    if not limit["available"]:
        return out
    if measured_um is None:
        out["why"] = ("the manual prescribes a limit but the geometry does not "
                      "express this parameter, so there is nothing to compare")
        return out

    required = limit["value_um"]
    out["difference_um"] = measured_um - required
    if limit["kind"] == "min":
        out["status"] = "pass" if measured_um >= required else "violation"
    elif limit["kind"] == "max":
        out["status"] = "pass" if measured_um <= required else "violation"
    else:
        out["status"] = "pass" if abs(measured_um - required) < 1e-9 else "violation"
    out["why"] = limit["reason"]
    return out
