"""Read a KLayout layer-properties file (.lyp) as a technology layer map.

A raw GDSII stream stores only numeric (layer, datatype) pairs. A `.lyp` supplies
the technology's own name for each pair, which turns `layer_300` into `BM0` with
no sidecar required.

This is a *different vocabulary* from the semantic JSON sidecar, not a competing
one. For the reference files the sidecar says `BSPowerRail` (what the layer is
for) where the .lyp says `BM0` (which mask it is). Both are kept; neither
overwrites the other.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# KLayout writes `<source>layer/datatype@cellview</source>`, e.g. `300/0@1`.
# Ranges (`1-5/0`) and wildcards (`*/*`) are legal in the format but cannot be
# resolved to a single pair, so they are collected as unresolved rather than
# guessed at.
_SOURCE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*(?:@\s*\d+\s*)?$")

# Suffix conventions that mark a layer as a secondary copy of another. These
# corroborate the geometric duplication the parser detects by unioning regions;
# they are never the sole basis for a claim.
_SECONDARY_SUFFIXES = ("-PIN", "-DUPLICATE", "-TEXT", "-LABEL", "_PIN", "_DUPLICATE")


def _classify(name: str) -> str:
    upper = name.upper()
    for suffix in _SECONDARY_SUFFIXES:
        if upper.endswith(suffix):
            return suffix.lstrip("-_").lower()
    return "drawing"


def load_lyp(path: str | Path) -> dict[str, Any]:
    """Parse a .lyp into {"by_key": {(layer, datatype): {...}}, "warnings": [...]}.

    Raises ValueError if the file is not a readable layer-properties document.
    """
    p = Path(path)
    try:
        root = ET.parse(p).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"{p.name} is not valid XML: {exc}") from exc

    if root.tag != "layer-properties":
        raise ValueError(
            f"{p.name} has root element <{root.tag}>, expected <layer-properties>. "
            "This does not look like a KLayout .lyp file."
        )

    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    warnings: list[str] = []
    unresolved: list[str] = []
    duplicates: list[str] = []

    for props in root.iter("properties"):
        source = (props.findtext("source") or "").strip()
        name = (props.findtext("name") or "").strip()
        if not source:
            continue
        if not name:
            unresolved.append(f"{source} (no name)")
            continue
        m = _SOURCE.match(source)
        if not m:
            # Wildcards and ranges cannot map to one pair.
            unresolved.append(source)
            continue
        key = (int(m.group(1)), int(m.group(2)))
        # Display properties. Previously discarded, but they are what makes a layer
        # panel look like the one in KLayout: the same swatch colour and fill
        # pattern the engineer already associates with each layer.
        fill = (props.findtext("fill-color") or "").strip()
        frame = (props.findtext("frame-color") or "").strip()
        entry = {
            "technology_name": name,
            "source": source,
            "role": _classify(name),
            "fill_color": fill or None,
            "frame_color": frame or fill or None,
            "dither_pattern": (props.findtext("dither-pattern") or "").strip() or None,
            "visible": (props.findtext("visible") or "").strip().lower() != "false",
            "valid": (props.findtext("valid") or "").strip().lower() != "false",
        }
        if key in by_key and by_key[key]["technology_name"] != name:
            # Two names for one pair: keep the first, report the collision rather
            # than letting document order silently decide.
            duplicates.append(f"{key[0]}/{key[1]}: '{by_key[key]['technology_name']}' and '{name}'")
            continue
        by_key[key] = entry

    if not by_key:
        raise ValueError(
            f"{p.name} contains no usable layer entries "
            f"({len(unresolved)} source(s) could not be resolved to a layer/datatype pair)."
        )
    if unresolved:
        warnings.append(
            f"{len(unresolved)} .lyp entr{'y' if len(unresolved) == 1 else 'ies'} could not be "
            f"mapped to a single layer/datatype pair and were ignored "
            f"(e.g. {', '.join(unresolved[:3])})."
        )
    if duplicates:
        warnings.append(
            f"The .lyp maps the same layer/datatype to more than one name; the first was kept "
            f"({'; '.join(duplicates[:3])})."
        )

    return {
        "file": p.name,
        "by_key": by_key,
        "warnings": warnings,
        "entry_count": len(by_key),
    }


# The technology layer map ships with the tool. It is the default, not an
# optional extra: without it a raw GDS can only report `layer_300`, and via-ness,
# layer roles and every role aggregate are unavailable. A user-supplied .lyp still
# wins, and this is only the fallback.
BUNDLED_LAYERMAP = Path(__file__).resolve().parent.parent / "data" / "samples" / \
    "Titan_layer_properties.lyp"


def default_layermap() -> Path | None:
    """The bundled layer map, if it is still where it is expected to be."""
    return BUNDLED_LAYERMAP if BUNDLED_LAYERMAP.exists() else None


def find_layermap(gds: Path, search_dirs: list[Path] | None = None,
                  use_bundled: bool = True) -> Path | None:
    """Locate a .lyp for a GDS.

    Order: `<stem>.lyp`, then any .lyp beside the GDS or in `search_dirs`, then
    the bundled technology map. The bundled one comes last so a layout shipped
    with its own map is never overridden by ours.
    """
    candidates: list[Path] = []
    dirs = [gds.parent] + list(search_dirs or [])
    for d in dirs:
        candidates.append(d / (gds.stem + ".lyp"))
    for d in dirs:
        try:
            candidates.extend(sorted(d.glob("*.lyp")))
        except OSError:
            continue
    if use_bundled and BUNDLED_LAYERMAP.exists():
        candidates.append(BUNDLED_LAYERMAP)
    for c in candidates:
        if c.exists():
            return c
    return None


def annotate_layers(layers: list[dict[str, Any]], layermap: dict[str, Any] | None) -> list[str]:
    """Attach technology names to layer rows in place; return any warnings.

    `name` is left untouched - a sidecar's semantic name is not replaced by the
    mask name, because they answer different questions.
    """
    if not layermap:
        return []
    by_key = layermap["by_key"]
    matched = 0
    for row in layers:
        entry = by_key.get((row.get("layer"), row.get("datatype")))
        if not entry:
            continue
        matched += 1
        row["technology_name"] = entry["technology_name"]
        row["technology_role"] = entry["role"]
        # A row whose name was only ever a placeholder can adopt the real one.
        if row.get("name") in (None, "", f"layer_{row.get('layer')}"):
            row["name"] = entry["technology_name"]
            row["name_source"] = "lyp"
        elif row["name"] != entry["technology_name"]:
            row["name_source"] = "sidecar"

    warnings = list(layermap["warnings"])
    if layers and not matched:
        warnings.append(
            f"The layer map '{layermap['file']}' has {layermap['entry_count']} entries but none "
            "match a layer/datatype used by this design. It is probably for a different "
            "technology; layer names were left unchanged."
        )
    elif matched < len(layers):
        warnings.append(
            f"{len(layers) - matched} of {len(layers)} layer entries are not in the layer map "
            f"'{layermap['file']}', so those keep their original names."
        )
    return warnings
