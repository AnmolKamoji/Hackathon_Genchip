from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --- Anthropic (primary) ------------------------------------------------------
# claude-opus-5 is the current Opus. Note for anyone editing this file: on this
# model `temperature`, `top_p`, `top_k` and `budget_tokens` are REJECTED with a
# 400 - do not add them. Thinking is on by default; depth is controlled with
# output_config.effort instead.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
# The analyzer computes every number, so the model is only phrasing facts it was
# handed. Low effort is the right setting for that and keeps the demo responsive.
DEFAULT_EFFORT = "low"
# max_tokens caps thinking + response text together. Generous, since cost is
# driven by tokens actually produced, not by the ceiling.
DEFAULT_MAX_TOKENS = 16000
DEFAULT_ANTHROPIC_TIMEOUT = 120.0
DEFAULT_ANTHROPIC_RETRIES = 3

# --- Ollama (local fallback) ---------------------------------------------------
# qwen3:4b at Q4_K_M is ~2.6 GB of weights, which fits a 6 GB GPU (e.g. Radeon
# RX 5600M) alongside an 8k KV cache.
DEFAULT_OLLAMA_MODEL = "qwen3:4b"
DEFAULT_NUM_CTX = 8192
DEFAULT_OLLAMA_TIMEOUT = 300
# qwen3 writes long reasoning preambles even with think=false; without a cap a
# single answer can run for minutes.
DEFAULT_NUM_PREDICT = 400

# Hard cap on the metadata handed to a model. The tight value exists for the
# local fallback, whose context window is 8k tokens; applying it to Anthropic too
# meant a 1M-context model received only a third of the layer rows for no reason.
MAX_METADATA_CHARS = 12000            # local models (qwen3:4b @ num_ctx 8192)
MAX_METADATA_CHARS_ANTHROPIC = 28000  # ~7k tokens; cached, so repeats cost ~0.1x
TOP_N_LAYERS = 40
TOP_N_CELLS = 25


# Sentinels for the two non-answer outcomes. Callers must test with
# looks_like_failure() rather than sniffing for markdown: a perfectly good answer
# can begin with bold text (e.g. "**1. Headline:** ..."), so a startswith("**")
# check reports false failures.
FAILURE_PREFIX = "**No AI backend could answer.**"
DISABLED_MESSAGE = ("AI narrative is disabled (LLM_PROVIDER=none). "
                    "Deterministic answers are still available.")


def looks_like_failure(text: str) -> bool:
    """True when `text` is a backend-failure notice rather than a model answer."""
    return text.startswith(FAILURE_PREFIX) or text == DISABLED_MESSAGE


class LLMUnavailable(RuntimeError):
    """Raised internally when a backend cannot be reached or used."""


# --- host discovery -----------------------------------------------------------

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _wsl_nat_gateway() -> str | None:
    """The Windows host IP as seen from WSL2 in NAT networking mode.

    An AMD GPU is not visible inside WSL2 (there is no /dev/kfd), so Ollama runs
    natively on Windows. Under WSL's default NAT mode the host is reached through
    the vEthernet gateway, which sits in 172.16/12.

    In mirrored mode (`networkingMode=mirrored`) WSL shares the Windows network
    namespace, so 127.0.0.1 already reaches the host and the default route points
    at the physical LAN router instead. Returning that router would mean probing
    an unrelated device, so only WSL-style private ranges are offered.
    """
    try:
        import ipaddress
        import subprocess
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        parts = out.split()
        if "via" not in parts:
            return None
        ip = parts[parts.index("via") + 1]
        if ipaddress.ip_address(ip).is_private and ip.startswith("172."):
            return ip
    except Exception:
        pass
    return None


def candidate_hosts() -> list[str]:
    explicit = os.getenv("OLLAMA_HOST", "").strip()
    hosts: list[str] = []
    if explicit:
        if not explicit.startswith("http"):
            explicit = f"http://{explicit}"
        hosts.append(explicit.rstrip("/"))
    # Works for a native Linux install and, under WSL mirrored networking, for
    # an Ollama server running on Windows.
    hosts.append("http://127.0.0.1:11434")
    if _is_wsl():
        ip = _wsl_nat_gateway()
        if ip:
            hosts.append(f"http://{ip}:11434")
    seen, ordered = set(), []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def _get_json(url: str, timeout: float = 4.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_ollama() -> tuple[str | None, list[str]]:
    """Return (working base URL, installed model names).

    The response shape is validated before a host is accepted, so an unrelated
    service occupying port 11434 is never sent prompt data.
    """
    for host in candidate_hosts():
        try:
            tags = _get_json(f"{host}/api/tags")
            if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
                continue
            return host, [m.get("name", "") for m in tags["models"] if isinstance(m, dict)]
        except Exception:
            continue
    return None, []


# --- configuration ------------------------------------------------------------

def anthropic_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


# Token usage per call, so the cost of a run can be measured instead of estimated.
# Kept in memory only: it is diagnostics, not state the app depends on.
USAGE: list[dict] = []


def reset_usage() -> None:
    USAGE.clear()


def usage_totals() -> dict:
    """Summed usage, with cached reads separated - they are billed at a tenth."""
    totals = {"calls": len(USAGE), "input": 0, "output": 0,
              "cache_write": 0, "cache_read": 0}
    for entry in USAGE:
        for key in ("input", "output", "cache_write", "cache_read"):
            totals[key] += entry.get(key, 0) or 0
    return totals


def _record_usage(model: str, usage) -> None:
    if usage is None:
        return
    USAGE.append({
        "model": model,
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
    })


def _anthropic_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def provider_chain() -> list[str]:
    """Backends to try, in order.

    LLM_PROVIDER=auto (default) prefers Anthropic for narrative quality and falls
    back to the local model, so the demo keeps working offline or if the API is
    unreachable. Naming a single provider disables the chain.
    """
    setting = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    if setting in ("anthropic", "ollama", "openai"):
        return [setting]
    if setting == "none":
        return []
    chain = []
    if _anthropic_configured():
        chain.append("anthropic")
    chain.append("ollama")
    return chain


def provider_status() -> dict[str, Any]:
    """Describe the configured backends for display in the UI sidebar."""
    chain = provider_chain()
    if not chain:
        return {"ready": False, "detail": "AI disabled (LLM_PROVIDER=none). Deterministic Q&A still works.",
                "chain": []}

    details: list[str] = []
    ready = False
    primary: str | None = None
    models: list[str] = []

    for name in chain:
        if name == "anthropic":
            if _anthropic_configured():
                details.append(f"Anthropic {anthropic_model()} (ready)")
                ready = True
                primary = primary or f"Anthropic · {anthropic_model()}"
            else:
                details.append("Anthropic (no ANTHROPIC_API_KEY)")
        elif name == "openai":
            if os.getenv("OPENAI_API_KEY"):
                details.append(f"OpenAI {os.getenv('OPENAI_MODEL', 'gpt-5')} (ready)")
                ready = True
                primary = primary or f"OpenAI · {os.getenv('OPENAI_MODEL', 'gpt-5')}"
            else:
                details.append("OpenAI (no OPENAI_API_KEY)")
        elif name == "ollama":
            host, installed = find_ollama()
            models = installed
            want = ollama_model()
            if not host:
                hint = " (WSL2 has no AMD GPU passthrough - run Ollama on Windows)" if _is_wsl() else ""
                details.append(f"Ollama (no server on {', '.join(candidate_hosts())}){hint}")
            elif not any(m == want or m.split(":")[0] == want.split(":")[0] for m in installed):
                details.append(f"Ollama at {host} (run: ollama pull {want})")
            else:
                details.append(f"Ollama {want} @ {host} (ready)")
                ready = True
                primary = primary or f"Ollama · {want}"

    return {
        "ready": ready,
        "chain": chain,
        "primary": primary,
        "models": models,
        "detail": (f"{primary} — fallback: " + " → ".join(details) if len(details) > 1 else details[0])
        if primary else " · ".join(details),
    }


# --- prompt sizing ------------------------------------------------------------

def _slim_connectivity(conn: dict[str, Any]) -> dict[str, Any]:
    """Reduce connectivity to the facts a review may state, plus its boundaries.

    The `not_derivable` block is carried deliberately: it is what stops a model
    from turning "4 vias overlap M0 and M1" into "VIA0 connects M0 to M1", or a
    net count built on an inferred stack into a statement of fact.
    """
    t1 = conn.get("intra_layer") or {}
    land = conn.get("landings") or {}
    nets = conn.get("nets") or {}
    out: dict[str, Any] = {
        "availability_note": (
            "Intra-layer connectivity is exact and needs no technology data. Via landings are "
            "measured plan-view overlap, which is NOT connection - GDSII stores no layer "
            "elevations. A net graph requires the vertical connection stack."),
        "intra_layer": {
            "availability": "GDS-only, exact",
            "total_conducting_shapes": t1.get("total_shapes"),
            "total_within_layer_conductors": t1.get("total_components"),
            "layers_with_abutting_shapes": t1.get("layers_with_abutting_shapes"),
            "per_layer": [{k: r[k] for k in ("name", "shape_count", "component_count") if k in r}
                          for r in (t1.get("layers") or [])
                          if r.get("component_count") != r.get("shape_count")],
        },
        "not_derivable": {
            k: v for k, v in (conn.get("limitations") or {}).items()
        },
    }
    if land.get("available"):
        out["via_contact_landings"] = {
            "availability": "GDS + LYP, measured overlap (not connection)",
            "connectors": [{
                "name": c["name"], "role": c["role"], "shape_count": c["shape_count"],
                "enclosed_by_every_shape": [o["name"] for o in c["overlaps"]
                                            if o["enclosure_ratio"] == 1.0],
                "touched_by_every_shape": [o["name"] for o in c["overlaps"]
                                           if o["interaction_ratio"] == 1.0],
                "shapes_overlapping_no_conductor": c["shapes_overlapping_no_conductor"],
            } for c in land.get("connectors", [])],
        }
    if nets.get("available"):
        # Three sources, three different standings. Collapsing them into
        # "provisional or not" made a sidecar-derived stack read as though it had
        # been guessed from geometry, which understates it.
        source = conn.get("stack_source") or ""
        if source == "supplied":
            standing, availability = "supplied", (
                "built from a connection stack supplied from technology data, so these net counts "
                "are exact for that stack")
        elif "sidecar" in source:
            standing, availability = "sidecar-named", (
                "built from a stack read off the semantic sidecar's own via layer names, which "
                "state each via's two endpoints (e.g. VIA_M0_M1). A naming convention rather than "
                "verified technology data, but not a geometric guess")
        else:
            standing, availability = "inferred", (
                "built from a stack INFERRED from layer naming and measured overlap, not from "
                "technology data, so these net counts are provisional")
        out["nets"] = {
            "availability": availability,
            "stack_source": source,
            "standing": standing,
            "provisional": standing == "inferred",
            **(nets.get("summary") or {}),
            # The summary alone cannot answer "what is each net?", which led a
            # model to correctly but unhelpfully report the count as all there was.
            "each_net": [{"net": n["net"], "shapes": n["shape_count"],
                          "layers": n["layers"], "area_um2": n["area_um2"]}
                         for n in (nets.get("nets") or [])[:TOP_N_LAYERS]],
            "stack_plausibility_warnings": nets.get("stack_plausibility_warnings") or [],
        }
        if len(nets.get("nets") or []) > TOP_N_LAYERS:
            out["nets"]["each_net_truncated"] = {
                "shown": TOP_N_LAYERS, "total": len(nets["nets"]),
                "note": "nets omitted had the fewest shapes"}
    else:
        out["nets"] = {"available": False,
                       "reason": nets.get("reason") or
                       "no connection stack supplied, so the net graph was not built"}
    return out


def _digest(metadata: dict[str, Any], cap: int | None = None) -> dict[str, Any]:
    """Shrink metadata to what a review actually needs."""
    if "comparison" in metadata and len(metadata) == 1:
        c = metadata["comparison"]
        # `layer_changes` lists every layer including unchanged ones, so it grows
        # without bound and would be the first thing a blind character truncation
        # cuts - taking the added/removed lists with it. Keep the signal instead.
        slim = {k: c[k] for k in ("file_a", "file_b", "comparable", "warnings", "summary",
                                  "layers_added", "layers_removed", "layers_modified") if k in c}
        for key in ("layers_added", "layers_removed", "layers_modified"):
            rows = slim.get(key) or []
            if len(rows) > TOP_N_LAYERS:
                slim[key] = rows[:TOP_N_LAYERS]
                slim[f"{key}_truncated"] = {"shown": TOP_N_LAYERS, "total": len(rows)}
        return {"comparison": slim}

    # Built in priority order, because a blind tail truncation drops whatever
    # comes last. `layer_groups` holds the authoritative per-name totals (the
    # only place the correct cross-layer area lives), so it must never be the
    # thing that falls off the end - the per-layer rows are the expendable part.
    out: dict[str, Any] = {
        k: metadata[k] for k in ("schema_version", "metadata_source", "warnings", "source",
                                 "design", "layout", "technology", "consistency")
        if k in metadata
    }

    groups = metadata.get("layer_groups", [])
    if groups:
        out["layer_groups_note"] = (
            "Authoritative per-layer-name totals. union_area_um2 is the physical coverage for "
            "that name and is already summed across its layer numbers - use it directly; do not "
            "add the per-row area_um2 values yourself."
        )
        out["layer_groups"] = [_slim_group(g) for g in groups[:TOP_N_LAYERS]]

    # Connectivity goes in ahead of the expendable per-layer rows: it is the only
    # place the net counts and the "what is not derivable" boundaries live, and a
    # model that cannot see them is liable to invent them.
    conn = metadata.get("connectivity")
    if conn and not conn.get("error"):
        out["connectivity"] = _slim_connectivity(conn)

    # Pitch metrics: the numbers a layout engineer quotes first. Without these the
    # model answered "what is the M0 pitch?" by describing how the shapes happened
    # to be arranged, which is not a routing pitch at all.
    pitch = metadata.get("pitch")
    if pitch and not pitch.get("error"):
        out["pitch_metrics"] = {
            "headline": pitch.get("headline"),
            "gate_pitch": pitch.get("gate_pitch"),
            "metal_pitches": pitch.get("metal_pitches"),
            "cell_dimensions": pitch.get("cell_dimensions"),
            "gear_ratio": pitch.get("gear_ratio"),
            "basis": pitch.get("basis"),
            "not_derivable": pitch.get("not_derivable"),
            "vocabulary": ("CPP, CGP, gate pitch and poly pitch all name the same number. "
                           "'How many gate pitches' asks for cell_dimensions.gate_pitches, a "
                           "count across the cell, not the pitch value."),
        }

    # Cell classification. The tool used to answer "frontside or backside" with "the
    # metadata has no such field", which was true and useless - BM0's VSS/VDD labels
    # are the answer. These facts must reach the model.
    cls = metadata.get("classification")
    if cls and not cls.get("error"):
        out["cell_classification"] = {
            "headline": cls.get("headline"),
            "availability": cls.get("availability"),
            "power_delivery": {k: cls["power_delivery"].get(k) for k in
                               ("power_delivery", "backside", "basis", "backside_labels",
                                "frontside_labels")},
            "technology": cls["technology"],
            "metal_solution": cls["metal_solution"],
            "routing_tracks": {k: cls["routing_tracks"].get(k) for k in
                               ("tracks", "tracks_used", "tracks_empty", "basis")},
            "cell_height": cls["cell_height"],
            "half_dr": {k: cls["half_dr"].get(k) for k in ("half_dr", "basis")},
            "orientation": cls["orientation"],
            "min_rt_number": cls.get("min_rt_number"),
        }

    # Tech-file parameters. Without these the model answered "what is the gate
    # extension?" by explaining what a gate extension is, which is not the question.
    params = (cls or {}).get("tech_parameters") or metadata.get("tech_parameters")
    if params and params.get("parameters"):
        comparison = params.get("comparison") or {}
        out["tech_file_parameters"] = {
            "poly_direction": params.get("poly_direction"),
            "measured_count": params.get("measured_count"),
            "unavailable_count": params.get("unavailable_count"),
            "parameters": {
                name: {k: v for k, v in record.items()
                       if k in ("value", "unit", "available", "drm_rule", "basis",
                                "compact_nm", "exceptions")}
                for name, record in params["parameters"].items()},
            "reference_comparison": ({
                "reference_file": comparison.get("reference_file"),
                "headline": comparison.get("headline"),
                "disagree": comparison.get("disagree"),
                "stated_only": comparison.get("stated_only"),
            } if comparison else None),
            "authority": (
                "Each parameter is measured from this layout to the definition in the "
                "cited GENCHIP Design Rule Manual rule. Quote `value` with `unit`. "
                "Where `available` is false there is no measurement: give the `basis` "
                "as the reason, and if a `stated_only` entry exists say the figure "
                "comes from the supplied tech file rather than from this layout. "
                "A stated figure is never a measurement of the cell."),
        }

    # Design-rule results, with the manual's own wording for anything that failed
    # or was checked, so the model can cite a rule rather than paraphrase one.
    drc = metadata.get("drc")
    if drc and not drc.get("error"):
        out["design_rules"] = {
            "source": drc.get("source"),
            "authority": ("The GENCHIP Design Rule Manual. A rule question may be answered from "
                          "these results, citing the rule id and the manual's wording. Anything "
                          "not in `results` was NOT checked - see rules_not_checked_count - so a "
                          "clean result is not a signoff DRC."),
            "technology": drc.get("technology", {}).get("used"),
            "technology_basis": drc.get("technology", {}).get("basis"),
            **{k: v for k, v in (drc.get("summary") or {}).items()},
            "violations": [{"id": v["id"], "rule": v["rule"], "detail": v["detail"]}
                           for v in drc.get("violations", [])],
            "checked": [{"id": r["id"], "status": r["status"], "rule": r["rule"],
                         "detail": r["detail"]}
                        for r in drc.get("results", [])][:TOP_N_LAYERS],
            "rules_not_checked_count": len(drc.get("rules_not_checked") or []),
            "caveat": drc.get("caveat"),
            "not_derivable": drc.get("not_derivable"),
        }

    hier = metadata.get("hierarchy")
    if hier and not hier.get("error"):
        out["hierarchy"] = {k: hier[k] for k in (
            "availability", "top_cell", "top_cell_count", "top_cells", "cell_count_total",
            "cell_count_in_scope", "max_depth_below_top", "depth_description", "empty_cells",
            "orphan_cells", "recursive_cells", "unresolved_reference_cells") if k in hier}

    meas = metadata.get("measurements")
    if meas and not meas.get("error"):
        out["measurements"] = {
            "availability": meas.get("availability"),
            "basis": meas.get("basis"),
            "role_aggregates": meas.get("role_aggregates"),
            "not_derivable": meas.get("not_derivable"),
            "per_layer": [
                {k: r[k] for k in ("name", "role", "shape_count", "shape_types", "area_um2",
                                   "perimeter_um", "merged_perimeter_um", "vertex_count",
                                   "max_vertices_in_one_polygon", "non_rectangular_shape_count",
                                   "observed_min_width_um", "observed_min_space_um",
                                   "path_widths_um") if k in r}
                for r in (meas.get("layers") or [])[:TOP_N_LAYERS]],
        }

    cells = metadata.get("cells", [])
    ranked_cells = sorted(cells, key=lambda x: (x.get("area_um2") or 0), reverse=True)
    out["cells"] = ranked_cells[:TOP_N_CELLS]
    if len(cells) > TOP_N_CELLS:
        out["cells_truncated"] = {"shown": TOP_N_CELLS, "total": len(cells),
                                  "note": "cells omitted had the smallest bounding-box area"}

    # Fill the remaining budget with layer rows, busiest first.
    layers = sorted(metadata.get("layers", []),
                    key=lambda x: (x.get("polygon_count") or 0), reverse=True)
    fixed = len(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    budget = max(0, (cap or MAX_METADATA_CHARS) - fixed - 200)  # headroom for wrapper keys
    kept: list[dict[str, Any]] = []
    used = 0
    for row in layers[:TOP_N_LAYERS]:
        slim = _slim_row(row)
        size = len(json.dumps(slim, separators=(",", ":"), ensure_ascii=False)) + 1
        if used + size > budget:
            break
        kept.append(slim)
        used += size
    out["layers"] = kept
    if len(kept) < len(layers):
        out["layers_truncated"] = {
            "shown": len(kept), "total": len(layers),
            "note": "layers omitted had the fewest polygons; layer_groups above still covers every name",
        }
    return out


def _slim_group(g: dict[str, Any]) -> dict[str, Any]:
    """Keep a group's conclusions, drop its per-layer-number working."""
    return {k: g[k] for k in (
        "label", "layer_numbers", "polygon_records", "unique_polygons",
        "union_area_um2", "sum_of_datatype_areas_um2", "union_density_percent",
        "geometry_duplicated_across_datatypes", "area_is_exclusive_to_this_name",
        "area_shared_with_other_layer_names",
    ) if k in g}


def _slim_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop per-row detail that is not needed to answer a question."""
    out = {k: row[k] for k in (
        "layer", "datatype", "name", "technology_name", "polygon_count", "via_count",
        "text_count", "area_um2", "density_percent", "geometry_source",
        "shares_layer_datatype_with",
    ) if k in row}
    sem = row.get("semantic")
    if isinstance(sem, dict) and sem.get("via"):
        out["is_via_layer"] = True
    return out


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# Keys the model needs to answer anything at all; never dropped. `connectivity`
# is here because dropping it removes the "what is not derivable" boundaries, and
# a model without those will state an inferred net count as fact.
_ESSENTIAL = ("schema_version", "metadata_source", "warnings", "source",
              "design", "layout", "technology", "consistency", "connectivity",
              "design_rules", "cell_classification", "pitch_metrics",
              "tech_file_parameters")


def _budget() -> int:
    """Prompt budget for the backend that will actually be tried first."""
    chain = provider_chain()
    if chain[:1] == ["anthropic"]:
        return _env_int("ANTHROPIC_METADATA_CHARS", MAX_METADATA_CHARS_ANTHROPIC)
    return MAX_METADATA_CHARS


def _compact(metadata: dict[str, Any], budget: int | None = None) -> str:
    """Serialize the digest within the prompt budget, always as valid JSON.

    Shrinking is structural, not a character truncation: cutting a JSON string
    mid-token produced a malformed document the model had to guess at, and it
    silently removed whichever section happened to be last. Lists are shortened
    in order of expendability instead, so what survives is well-formed and the
    authoritative per-name totals are the last thing to go.
    """
    cap = budget if budget is not None else _budget()
    d = _digest(metadata, cap)
    text = _dump(d)
    if len(text) <= cap:
        return text

    # Connectivity's own lists can grow with the design, so shrink them here too
    # rather than letting the whole block be dropped at the last resort below.
    conn = d.get("connectivity")
    if conn and len(text) > cap:
        for path, floor in ((("via_contact_landings", "connectors"), 2),
                            (("intra_layer", "per_layer"), 2),
                            (("nets", "each_net"), 2)):
            holder = conn.get(path[0]) or {}
            rows = holder.get(path[1]) or []
            if len(rows) > floor and len(text) > cap:
                holder[path[1]] = rows[:floor]
                holder[f"{path[1]}_truncated"] = {"shown": floor, "total": len(rows),
                                                  "note": "omitted to fit the prompt budget"}
                text = _dump(d)

    # Per-layer measurements grow with the design too, and are the largest single
    # block, so they are trimmed alongside the raw layer rows.
    rules_block = d.get("design_rules")
    if rules_block and len(text) > cap and len(rules_block.get("checked") or []) > 4:
        rules_block["checked"] = rules_block["checked"][:4]
        rules_block["checked_truncated"] = "shortened to fit the prompt budget"
        text = _dump(d)

    meas = d.get("measurements")
    if meas and len(text) > cap and len(meas.get("per_layer") or []) > 5:
        rows = meas["per_layer"]
        while len(text) > cap and len(meas["per_layer"]) > 5:
            keep = max(5, len(meas["per_layer"]) // 2)
            if keep == len(meas["per_layer"]):
                break
            meas["per_layer"] = meas["per_layer"][:keep]
            meas["per_layer_truncated"] = {"shown": keep, "total": len(rows),
                                           "note": "omitted to fit the prompt budget"}
            text = _dump(d)

    # Least to most valuable: raw rows, then cells, then group summaries. `layers`
    # keeps a floor rather than going to zero: once connectivity joined the digest
    # it consumed enough of the 12k local-model budget to evict every per-layer row,
    # which silently removed the answer to most layer questions.
    for key, floor in (("layers", 5), ("cells", 1), ("layer_groups", 3)):
        while len(text) > cap and len(d.get(key) or []) > floor:
            rows = d[key]
            keep = max(floor, len(rows) // 2)
            if keep == len(rows):
                keep = floor
            d[key] = rows[:keep]
            d[f"{key}_truncated"] = {"shown": keep, "total": len(metadata.get(key) or rows),
                                     "note": "omitted to fit the prompt budget"}
            text = _dump(d)

    # Still over: strip connectivity to its summary. The `not_derivable` boundaries
    # and the totals are what must survive - the per-connector and per-net detail is
    # expendable, and keeping it at the cost of every layer row is the wrong trade.
    if len(text) > cap and d.get("connectivity"):
        conn = d["connectivity"]
        for holder, key in (("via_contact_landings", "connectors"),
                            ("intra_layer", "per_layer"), ("nets", "each_net")):
            block = conn.get(holder) or {}
            if block.get(key):
                block[f"{key}_omitted"] = {"total": len(block[key]),
                                           "note": "omitted to fit the prompt budget"}
                block[key] = []
        text = _dump(d)

    if len(text) > cap:
        # Last resort: design-level facts only, still a well-formed document.
        d = {k: d[k] for k in _ESSENTIAL if k in d}
        d["detail_omitted"] = ("Per-layer and per-cell detail exceeded the prompt budget. "
                               "Only design-level totals are available here.")
        text = _dump(d)
    return text


_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_reasoning(text: str) -> str:
    """Remove <think> blocks emitted by reasoning models such as qwen3."""
    return _THINK.sub("", text).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# --- Anthropic backend --------------------------------------------------------

# Parameters a model may reject, and what to drop when it does. The API names the
# offending parameter in the 400 message, so the message is matched rather than the
# model id - a list of model ids goes stale the moment a new model ships.
_DEGRADATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("effort", ("output_config",)),
    ("fallback", ("betas", "fallbacks")),
    ("beta", ("betas", "fallbacks")),
    ("thinking", ("thinking",)),
    ("temperature", ("temperature",)),
    ("top_p", ("top_p",)),
    ("top_k", ("top_k",)),
)

# model -> parameter groups it has already rejected, so the wasted round trip
# happens once per process rather than once per question.
_UNSUPPORTED: dict[str, set[tuple[str, ...]]] = {}


def _call_with_degradation(client, kwargs: dict[str, Any]):
    """Send the request, dropping any parameter the model rejects, and retry.

    Model tiers do not accept the same parameters. `output_config.effort` arrived
    with Opus 4.5 and Sonnet 4.6, so Haiku 4.5 rejects it with "This model does not
    support the effort parameter" - and because the previous code recovered only
    from beta-related 400s, setting ANTHROPIC_MODEL to Haiku made every answer fail
    and fall through to the local model, silently disabling the AI narrative.

    Matching the error text rather than keeping a list of model ids means a model
    released after this code was written degrades correctly instead of failing. The
    first call to a model that rejects something costs one wasted round trip; every
    later call is clean, because the rejection is remembered.
    """
    import anthropic

    model = str(kwargs.get("model", ""))
    for keys in _UNSUPPORTED.get(model, set()):
        for key in keys:
            kwargs.pop(key, None)

    beta_endpoint = "betas" in kwargs or "fallbacks" in kwargs
    for _ in range(len(_DEGRADATIONS) + 1):
        try:
            return (client.beta.messages.create(**kwargs) if beta_endpoint
                    else client.messages.create(**kwargs))
        except anthropic.BadRequestError as exc:
            message = str(exc).lower()
            for needle, keys in _DEGRADATIONS:
                if needle in message and any(k in kwargs for k in keys):
                    for key in keys:
                        kwargs.pop(key, None)
                    _UNSUPPORTED.setdefault(model, set()).add(keys)
                    if needle in ("fallback", "beta"):
                        beta_endpoint = False
                    break
            else:
                raise                      # not a parameter we know how to drop
    raise LLMUnavailable("The request was rejected after dropping every optional "
                         "parameter this model could not accept.")


def _anthropic_call(system_blocks: list[dict], user_text: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set.")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("The 'anthropic' package is not installed. Run: pip install anthropic") from exc

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=_env_float("ANTHROPIC_TIMEOUT", DEFAULT_ANTHROPIC_TIMEOUT),
        # The SDK already retries 429/5xx/connection errors with backoff.
        max_retries=_env_int("ANTHROPIC_MAX_RETRIES", DEFAULT_ANTHROPIC_RETRIES),
    )

    kwargs: dict[str, Any] = {
        "model": anthropic_model(),
        "max_tokens": _env_int("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_text}],
        "output_config": {"effort": os.getenv("ANTHROPIC_EFFORT", DEFAULT_EFFORT)},
        # Safety classifiers can decline a request; 'default' re-serves it on
        # Anthropic's recommended fallback model instead of returning nothing.
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }

    response = _call_with_degradation(client, kwargs)

    # Check stop_reason before reading content: a refusal can carry an empty or
    # partial content list, so indexing content[0] unconditionally would break.
    if response.stop_reason == "refusal":
        detail = ""
        if getattr(response, "stop_details", None):
            detail = f" (category: {getattr(response.stop_details, 'category', None)})"
        raise LLMUnavailable(f"The model declined this request{detail}.")

    _record_usage(kwargs.get("model", ""), getattr(response, "usage", None))

    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise LLMUnavailable(f"Empty response (stop_reason={response.stop_reason}).")
    return text


# --- Ollama backend -----------------------------------------------------------

def _ollama_call(system: str, user: str) -> str:
    host, installed = find_ollama()
    if not host:
        raise LLMUnavailable(f"No Ollama server reachable (tried {', '.join(candidate_hosts())}).")
    want = ollama_model()
    if installed and not any(m == want or m.split(":")[0] == want.split(":")[0] for m in installed):
        raise LLMUnavailable(f"Ollama is running at {host} but '{want}' is not installed. Run: ollama pull {want}")

    payload = {
        "model": want,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "options": {
            "num_ctx": _env_int("OLLAMA_NUM_CTX", DEFAULT_NUM_CTX),
            # Without this cap qwen3 can emit >1400 tokens of preamble at ~7 tok/s.
            "num_predict": _env_int("OLLAMA_NUM_PREDICT", DEFAULT_NUM_PREDICT),
            "temperature": _env_float("OLLAMA_TEMPERATURE", 0.2),
        },
        # Keep the model resident so the second demo question is fast.
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "10m"),
    }
    think = os.getenv("OLLAMA_THINK", "false").strip().lower()
    if think in ("true", "false"):
        payload["think"] = think == "true"

    timeout = _env_float("OLLAMA_TIMEOUT", DEFAULT_OLLAMA_TIMEOUT)
    try:
        data = _post_json(f"{host}/api/chat", payload, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and "think" in payload:
            payload.pop("think")
            data = _post_json(f"{host}/api/chat", payload, timeout)
        else:
            raise LLMUnavailable(f"Ollama returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
    text = _strip_reasoning((data.get("message") or {}).get("content", "") or "")
    if not text:
        raise LLMUnavailable("Ollama returned an empty response.")
    return text


# --- OpenAI backend (optional, legacy) ---------------------------------------

def _openai_call(system: str, user: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMUnavailable("OPENAI_API_KEY is not set.")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5"), instructions=system, input=user)
    return response.output_text


# --- dispatch -----------------------------------------------------------------

def _run_chain(digest: str, instruction: str) -> str:
    """Try each backend in order; report every failure if none succeed.

    Each backend gets the prompt in its own shape. Anthropic takes the metadata
    as a cacheable system block with only the instruction in the user turn;
    Ollama and OpenAI have no comparable cache, so they get one combined prompt
    with the metadata inlined. Building both here is what lets a failed Anthropic
    call fall through to the local model without losing the metadata.
    """
    from .prompts import SYSTEM_PROMPT, build_system_blocks

    chain = provider_chain()
    if not chain:
        return DISABLED_MESSAGE

    combined = f"GDS METADATA:\n{digest}\n\n{instruction}"

    failures: list[str] = []
    for name in chain:
        try:
            if name == "anthropic":
                return _anthropic_call(build_system_blocks(digest), instruction)
            if name == "ollama":
                return _ollama_call(SYSTEM_PROMPT, combined)
            if name == "openai":
                return _openai_call(SYSTEM_PROMPT, combined)
        except LLMUnavailable as exc:
            failures.append(f"{name}: {exc}")
        except urllib.error.URLError as exc:
            failures.append(f"{name}: could not connect ({exc.reason})")
        except TimeoutError:
            failures.append(f"{name}: timed out")
        except Exception as exc:  # never surface a raw traceback in the dashboard
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    return (FAILURE_PREFIX + "\n\n"
            + "\n".join(f"- {f}" for f in failures)
            + "\n\nEvery number in the dashboard is computed locally, so the deterministic "
              "questions still work without a model.")


# --- public API ---------------------------------------------------------------

def ask_llm(metadata: dict[str, Any], question: str, history: list[dict] | None = None) -> str:
    from .prompts import build_question_turn
    instruction = build_question_turn(question)
    if history:
        convo = "\n".join(f"{h['role'].upper()}: {h['content']}" for h in history)
        instruction = f"EARLIER CONVERSATION:\n{convo}\n\n{instruction}"
    return _run_chain(_compact(metadata), instruction)


def generate_review(metadata: dict[str, Any]) -> str:
    from .prompts import ACCURACY_REMINDER
    instruction = (
        "Review the GDS metadata for useful engineering observations.\n\n"
        "Return these sections as markdown headings: Summary, Key Observations, "
        "Potential Review Areas, Limitations.\n"
        "Under Limitations, list the facts the analyzer marked unavailable (null).\n"
        f"Never label an observation as a DRC violation unless explicit DRC data exists. {ACCURACY_REMINDER}"
    )
    return _run_chain(_compact(metadata), instruction)


def generate_comparison(comparison: dict[str, Any]) -> str:
    from .prompts import ACCURACY_REMINDER
    instruction = (
        "Two revisions of the same layout were compared by a deterministic analyzer; the "
        "comparison JSON is above. Explain what changed, in this order:\n"
        "1. One-sentence headline of the most significant change.\n"
        "2. Layers added or removed, by exact name.\n"
        "3. Layers whose polygon or via counts moved, with direction and size.\n"
        "4. Whether the overall bounding box changed.\n\n"
        "If `comparable` is false, or `warnings` is non-empty, lead with that caveat and do "
        "not interpret the layer deltas as real design changes.\n"
        f"Do not speculate about intent, performance, or DRC compliance. {ACCURACY_REMINDER}"
    )
    return _run_chain(_compact({"comparison": comparison}), instruction)
