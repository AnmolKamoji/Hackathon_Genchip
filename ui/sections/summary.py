"""Section 1 - GDS Summary.

One table, one row per uploaded file, in upload order. It answers "what is in this
file?" and nothing else.

Every cell is rendered as text on purpose. A column has to be able to hold both a
number and the word Unavailable, and a numeric column would coerce the second into
a blank - which reads as zero, which is the one thing this table must never say by
accident.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from analyzer.values import show
from ui.theme import section

COLUMNS = ["GDS Name", "Layers", "Vias", "Pins", "Transistor",
           "Width_um", "Height_um", "Polygons"]


def row_for(doc: dict[str, Any]) -> dict[str, str]:
    return {
        "GDS Name": doc["file"]["name"],
        "Layers": show(doc["layers"]["count"]),
        "Vias": show(doc["vias"]["count"]),
        "Pins": show(doc["pins"]["count"] if doc["pins"].get("available") else None),
        "Transistor": show(doc["devices"]["transistor_count"]),
        "Width_um": show(doc["layout"]["width_um"]),
        "Height_um": show(doc["layout"]["height_um"]),
        "Polygons": show(doc["geometry"]["drawn_shapes"]),
    }


def render(documents: list[dict[str, Any]]) -> None:
    st.markdown(section("GDS Summary"), unsafe_allow_html=True)
    # Upload order, never sorted: the order the files were given is the only order
    # the reviewer can predict.
    table = pd.DataFrame([row_for(d) for d in documents], columns=COLUMNS)
    st.dataframe(table, width="stretch", hide_index=True, key="gds_summary")
