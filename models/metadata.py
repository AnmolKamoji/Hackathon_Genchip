"""Metadata schema contract.

This module previously held a `LayerStats` dataclass that nothing imported and
that had drifted out of step with the real output (it defaulted `area_um2` and
`via_count` to `0.0`/`0`, the exact opposite of the rule the analyzer now
enforces). A stale schema definition is worse than none, because it invites
someone to trust it.

It is now a validator instead. The single most important invariant in this
project is that an undeterminable fact is `None`, never `0` - a confident zero is
a wrong answer, while `None` is an honest one. `validate_metadata` checks that
invariant and the presence of the fields the Q&A layer relies on, so a future
change that reintroduces a silent zero fails a test rather than a demo.
"""
from __future__ import annotations

from typing import Any

# Fields every metadata object must carry, whatever mode produced it.
REQUIRED_TOP_LEVEL = ("schema_version", "metadata_source", "warnings", "source",
                      "design", "layout", "cells", "layers", "technology")

REQUIRED_DESIGN = ("top_cell", "cell_count", "layer_count", "polygon_count",
                   "shape_count", "text_count", "via_count")

REQUIRED_LAYOUT = ("bbox_dbu", "width_um", "height_um", "bbox_area_um2")

REQUIRED_LAYER = ("layer", "datatype", "name", "polygon_count", "via_count",
                  "text_count", "area_um2", "density_percent")

# Facts that are unknowable from some inputs. Each must be None when unavailable,
# and must never be silently reported as zero.
NULLABLE_NOT_ZERO = ("via_count",)


class SchemaError(AssertionError):
    """Raised when a metadata object violates the contract."""


def validate_metadata(meta: dict[str, Any], *, source: str | None = None) -> None:
    """Raise SchemaError if `meta` breaks the contract. Returns None on success."""
    where = f" in {source}" if source else ""

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in meta]
    if missing:
        raise SchemaError(f"metadata{where} is missing top-level keys: {missing}")

    if not isinstance(meta["warnings"], list):
        raise SchemaError(f"metadata{where}: 'warnings' must be a list, got {type(meta['warnings'])}")

    design = meta["design"]
    missing = [k for k in REQUIRED_DESIGN if k not in design]
    if missing:
        raise SchemaError(f"metadata{where}: design is missing {missing}")

    missing = [k for k in REQUIRED_LAYOUT if k not in meta["layout"]]
    if missing:
        raise SchemaError(f"metadata{where}: layout is missing {missing}")

    mode = meta["metadata_source"]
    if mode not in ("gds", "sidecar", "fused"):
        raise SchemaError(f"metadata{where}: unknown metadata_source {mode!r}")

    # The core invariant, now conditioned on the layer map. Raw GDSII carries no
    # via semantics, so without a .lyp this must say "unknown", not "none
    # present". With a .lyp the via layers are named, so the count is derivable -
    # but it must then declare where it came from, since it rests on a naming
    # convention rather than an explicit flag.
    used_layermap = bool((meta.get("technology") or {}).get("layer_map_used"))
    if mode == "gds" and design["via_count"] is not None:
        if not used_layermap:
            raise SchemaError(
                f"metadata{where}: gds mode reports via_count={design['via_count']!r} with no "
                "layer map. A raw GDSII stream does not label vias, so this must be None - "
                "reporting 0 would be a confident wrong answer.")
        if not design.get("via_count_source"):
            raise SchemaError(
                f"metadata{where}: via_count={design['via_count']!r} was derived from the layer "
                "map but via_count_source is missing; a derived figure must say so.")
    # `None` and `0` are not interchangeable, and which one is correct depends on
    # whether anything identified the via layers:
    #   no layer map -> None on every layer. We cannot tell a via from a wire.
    #   layer map     -> a number on every layer, 0 included. "M0 has 0 vias" is a
    #                    determinate fact once the map says M0 is metal, and None
    #                    would wrongly claim it could not be determined.
    for i, row in enumerate(meta["layers"]):
        if mode != "gds":
            continue
        if not used_layermap and row.get("via_count") is not None:
            raise SchemaError(
                f"metadata{where}: layers[{i}] ({row.get('name')}) reports "
                f"via_count={row['via_count']!r} with no layer map; must be None, because "
                "nothing distinguishes a via from any other shape.")
        if used_layermap and row.get("via_count") is None:
            raise SchemaError(
                f"metadata{where}: layers[{i}] ({row.get('name')}) has via_count=None even though "
                "a layer map identified the via layers. Once via-ness is known the answer is a "
                "number - 0 for a non-via layer - and None would claim it was undeterminable.")

    for i, row in enumerate(meta["layers"]):
        missing = [k for k in REQUIRED_LAYER if k not in row]
        if missing:
            raise SchemaError(f"metadata{where}: layers[{i}] ({row.get('name')}) is missing {missing}")
        density = row.get("density_percent")
        if density is not None and not (0.0 <= density <= 100.0 + 1e-9):
            raise SchemaError(
                f"metadata{where}: layers[{i}] ({row.get('name')}) has "
                f"density_percent={density}, outside 0-100%.")

    for i, group in enumerate(meta.get("layer_groups", [])):
        union = group.get("union_area_um2")
        summed = group.get("sum_of_datatype_areas_um2")
        if isinstance(union, (int, float)) and isinstance(summed, (int, float)):
            # A union can never exceed the sum of its parts.
            if union > summed + 1e-9:
                raise SchemaError(
                    f"metadata{where}: layer_groups[{i}] ({group.get('label')}) has "
                    f"union_area_um2={union} greater than sum_of_datatype_areas_um2={summed}.")


def validate_connectivity(conn: dict[str, Any]) -> None:
    """Raise SchemaError if a connectivity object breaks the contract.

    The invariants here are about not overstating what GDSII can support. The net
    graph depends on the vertical stack, which a .gds and .lyp do not contain, so
    a net graph must never appear without a recorded stack source - otherwise a
    provisional net count reads as an established one.
    """
    for key in ("intra_layer", "landings", "warnings", "limitations"):
        if key not in conn:
            raise SchemaError(f"connectivity is missing {key!r}")

    t1 = conn["intra_layer"]
    for key in ("tier", "availability", "layers", "total_shapes", "total_components"):
        if key not in t1:
            raise SchemaError(f"connectivity.intra_layer is missing {key!r}")
    if t1["availability"] != "GDS-only":
        raise SchemaError(
            f"connectivity.intra_layer claims availability {t1['availability']!r}; intra-layer "
            "connectivity needs no technology data and must be labelled GDS-only.")
    for i, row in enumerate(t1["layers"]):
        if row["component_count"] > row["shape_count"]:
            raise SchemaError(
                f"connectivity.intra_layer.layers[{i}] ({row.get('name')}) has "
                f"{row['component_count']} components from {row['shape_count']} shapes; merging "
                "shapes can only reduce the count.")
        if row["component_count"] < 1:
            raise SchemaError(
                f"connectivity.intra_layer.layers[{i}] ({row.get('name')}) has "
                f"component_count={row['component_count']}; a layer with shapes has at least one.")

    for key in ("vertical_stack", "physical_shorts", "physical_opens", "electrical_intent"):
        if key not in conn["limitations"]:
            raise SchemaError(
                f"connectivity.limitations is missing {key!r}. These boundaries are what stop an "
                "inferred result being read as a measured one.")

    nets = conn.get("nets")
    if nets and nets.get("available"):
        if not conn.get("stack_source"):
            raise SchemaError(
                "connectivity reports a net graph but records no stack_source. A net graph requires "
                "the vertical connection stack, so where it came from must be stated.")
        s = nets.get("summary") or {}
        if s.get("net_count", 0) != len(nets.get("nets") or []):
            raise SchemaError(
                f"connectivity.nets summary says net_count={s.get('net_count')} but lists "
                f"{len(nets.get('nets') or [])} nets.")

    proposed = conn.get("proposed_stack")
    if proposed and not proposed.get("requires_confirmation"):
        raise SchemaError(
            "connectivity.proposed_stack must set requires_confirmation; an inferred stack is not "
            "a technology fact and must not be presented as one.")


def validate_comparison(comparison: dict[str, Any]) -> None:
    """Raise SchemaError if a comparison object breaks the contract."""
    for key in ("file_a", "file_b", "comparable", "warnings", "summary",
                "layers_added", "layers_removed", "layers_modified", "layer_changes"):
        if key not in comparison:
            raise SchemaError(f"comparison is missing {key!r}")

    # An unknown minus a known is unknown, never a number.
    for row in comparison["layer_changes"]:
        if (row.get("via_count_a") is None or row.get("via_count_b") is None) \
                and row.get("via_delta") is not None:
            raise SchemaError(
                f"comparison: layer {row.get('layer')}/{row.get('datatype')} has an unavailable "
                f"via count on one side but via_delta={row['via_delta']!r}; must be None.")
