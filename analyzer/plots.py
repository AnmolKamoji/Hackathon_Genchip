"""Layout-shaped figures: the difference map and the density profile.

A note on the wording, because it was wrong first time. Labelling regions "removed"
and "added" invites the question *removed from what?* - the reader has to hold in
mind which file is the baseline before the colour means anything. Naming the two
files instead ("only in DCAP0_1", "only in DCAP0_2") is self-explanatory and needs
no convention. It also collapses the legend: the colour maps to a *file*, of which
there are two, rather than to a per-layer added/removed pair, which came to eight
entries for four layers.

A reviewer navigates a layout visually. Calibre's RVE exists so that an error
marker can be cross-probed straight to the shape in the layout, and KLayout's diff
writes a marker database for the same reason. A table of coordinates is a poor
substitute for seeing where a difference sits in the cell.

So the difference map draws the cell outline to scale, with every XOR region in
place, coloured by whether it was added or removed. It is the one view that
answers "what changed?" without reading a number.

The colours are deliberately conventional for a diff rather than pretty: red for
geometry that disappeared, green for geometry that appeared.
"""
from __future__ import annotations

from typing import Any

REMOVED = "#d62728"      # in A, gone from B
ADDED = "#2ca02c"        # new in B
OUTLINE = "#888888"
DIMENSION = "#58a6ff"   # dimension lines, distinct from any layer colour


def _short(name: str) -> str:
    """A file name short enough for a legend entry."""
    stem = name[:-4] if name.lower().endswith(".gds") else name
    return stem if len(stem) <= 24 else stem[:23] + "\u2026"


def difference_map(xor: dict[str, Any], cell_bbox_um: list[float] | None = None,
                   max_regions: int = 400, only_layers: set[str] | None = None,
                   colour_by: str = "file",
                   layer_colours: dict[str, str] | None = None):
    """A to-scale plan view of the cell with every XOR region drawn in place.

    The legend carries one entry per *file*: a region is either only in the first
    layout or only in the second. Choosing which layers to draw belongs in a
    control, not in the legend, so `only_layers` does that instead.

    Returns a plotly figure, or None when there is nothing to draw.
    """
    import plotly.graph_objects as go

    if not xor.get("comparable") or xor["summary"]["identical"]:
        return None
    label_a = f"only in {_short(xor['file_a'])}"
    label_b = f"only in {_short(xor['file_b'])}"
    # `colour_by="layer"` uses the technology colours from the .lyp, so the map and
    # the layer panel show a layer in the same colour - the association a KLayout
    # user already has. `"file"` keeps the diff convention of one colour per side.
    by_layer = colour_by == "layer" and bool(layer_colours)

    fig = go.Figure()
    drawn = 0
    seen: set[str] = set()
    # Draw the removed and added sets separately rather than the combined XOR. Both
    # are already computed per region, and colouring the union by whether the
    # *layer* was added or removed made every region on a layer present in both
    # files come out the same colour - which was every region in a typical revision.
    for row in xor["layers"]:
        if row["identical"]:
            continue
        if only_layers is not None and row["name"] not in only_layers:
            continue
        # `removed` is geometry in A but not B, which is exactly "only in the first
        # file"; `added` is "only in the second".
        for block, colour, label in (("removed", REMOVED, label_a),
                                     ("added", ADDED, label_b)):
            if by_layer:
                # Colour identifies the layer; the side is carried by the outline
                # (solid for the first file, dashed for the second) and the hover.
                colour = (layer_colours or {}).get(row["name"], colour)
                label = row["name"]
            dash = "solid" if block == "removed" else "dot"
            for loc in (row.get(block) or {}).get("locations") or []:
                outline = loc.get("outline_um")
                if not outline or drawn >= max_regions:
                    continue
                xs = [p[0] for p in outline] + [outline[0][0]]
                ys = [p[1] for p in outline] + [outline[0][1]]
                fig.add_trace(go.Scatter(
                    x=xs, y=ys, fill="toself", mode="lines",
                    line=dict(color=colour, width=1.4 if by_layer else 1,
                              dash=dash if by_layer else "solid"),
                    fillcolor=colour, opacity=0.55 if by_layer else 0.6,
                    name=label, legendgroup=label, showlegend=label not in seen,
                    hovertemplate=(f"<b>{row['name']}</b><br>"
                                   f"{label_a if block == 'removed' else label_b}<br>"
                                   f"{loc['area_um2']:.6g} µm²<br>"
                                   f"{loc['width_um'] * 1000:.0f} × "
                                   f"{loc['height_um'] * 1000:.0f} nm<br>"
                                   f"at ({loc['centre_um'][0]}, {loc['centre_um'][1]}) µm"
                                   "<extra></extra>")))
                seen.add(label)
                drawn += 1

    if not drawn:
        return None

    if cell_bbox_um and len(cell_bbox_um) == 4:
        left, bottom, right, top = cell_bbox_um
        fig.add_shape(type="rect", x0=left, y0=bottom, x1=right, y1=top,
                      line=dict(color=OUTLINE, width=1, dash="dot"), fillcolor="rgba(0,0,0,0)")

    fig.update_layout(
        title=f"Difference map — {xor['file_a']} → {xor['file_b']}",
        xaxis_title="x (µm)", yaxis_title="y (µm)",
        showlegend=True, height=520,
        legend=dict(title="", orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0),
        margin=dict(l=60, r=20, t=50, b=50),
    )
    # Equal aspect: a layout drawn out of proportion is misleading.
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def density_profile(rows: list[dict[str, Any]], top: int = 12):
    """Per-layer coverage as a horizontal bar chart, densest first.

    Density is a comparison between layers, and a bar chart makes that comparison
    in one glance where a column of percentages does not.
    """
    import plotly.graph_objects as go

    dense = [r for r in rows if r.get("density_percent") is not None
             and r.get("polygon_count")]
    if not dense:
        return None
    dense.sort(key=lambda r: r["density_percent"], reverse=True)
    dense = dense[:top]
    fig = go.Figure(go.Bar(
        x=[r["density_percent"] for r in dense],
        y=[r["name"] for r in dense],
        orientation="h",
        marker=dict(color=[r["density_percent"] for r in dense],
                    colorscale="Blues", cmin=0, cmax=100),
        hovertemplate="<b>%{y}</b><br>%{x:.2f}% of the cell bounding box<extra></extra>",
    ))
    fig.update_layout(
        title="Layer coverage (% of cell bounding box)",
        xaxis_title="% of cell", height=max(260, 26 * len(dense) + 90),
        yaxis=dict(autorange="reversed"), margin=dict(l=140, r=20, t=50, b=40),
    )
    return fig


def similarity_matrix(multi: dict[str, Any]):
    """A heatmap of pairwise XOR area across several layouts.

    With more than two files the question becomes which are close to which, and a
    matrix of numbers is harder to scan than a coloured grid.
    """
    import plotly.graph_objects as go

    names = multi["files"]
    if len(names) < 3:
        return None
    z, text = [], []
    for a in names:
        zrow, trow = [], []
        for b in names:
            if a == b:
                zrow.append(None); trow.append("same file")
                continue
            cell = multi["matrix"][a][b]
            if not cell.get("comparable"):
                zrow.append(None); trow.append("not comparable")
            else:
                zrow.append(cell["total_xor_area_um2"])
                trow.append(f"{cell['total_xor_area_um2']:.6g} µm²<br>"
                            f"{cell['layers_changed']} layers, "
                            f"{cell['difference_regions']} regions")
        z.append(zrow); text.append(trow)
    fig = go.Figure(go.Heatmap(
        z=z, x=names, y=names, text=text, texttemplate="", hoverinfo="text",
        colorscale="Reds", colorbar=dict(title="XOR area<br>(µm²)")))
    fig.update_layout(title="How different is each pair?", height=380,
                      margin=dict(l=140, r=20, t=50, b=100))
    fig.update_xaxes(tickangle=-30)
    return fig


def difference_grid(multi: dict[str, Any], reference: str,
                    cell_bbox_um: list[float] | None = None,
                    max_regions_each: int = 250):
    """Small multiples: the reference layout against each of the others.

    This is how a revision family is actually reviewed - one golden database, every
    candidate compared back to it. A single overlay of five revisions cannot be read
    (which colour was which?), and ten separate pairwise maps is worse. A row of
    panels on shared axes lets the eye compare positions directly: if the same
    region lights up in every panel, that region is where all the churn is.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    others = [n for n in multi["files"] if n != reference]
    if not others:
        return None

    cols = min(len(others), 3)
    rowcount = (len(others) + cols - 1) // cols
    fig = make_subplots(rows=rowcount, cols=cols, subplot_titles=others,
                        horizontal_spacing=0.06, vertical_spacing=0.12)

    seen: set[str] = set()
    for index, other in enumerate(others):
        r, c = index // cols + 1, index % cols + 1
        pair = next((p for p in multi["pairs"]
                     if {p["a"], p["b"]} == {reference, other}), None)
        detail = (pair or {}).get("detail")
        if not detail or not detail.get("comparable"):
            fig.add_annotation(text="not comparable", row=r, col=c,
                               showarrow=False, x=0.5, y=0.5, xref="x domain",
                               yref="y domain")
            continue
        # Orientation matters: `removed` means "in A, gone from B", so when the
        # reference is B the sense is inverted and the colours must swap.
        flip = detail["file_a"] != reference
        label_ref = f"only in {_short(reference)}"
        label_other = "only in this layout"
        drawn = 0
        for row in detail["layers"]:
            if row["identical"]:
                continue
            for block, verb in (("removed", "removed"), ("added", "added")):
                sense = verb
                if flip:
                    sense = "added" if verb == "removed" else "removed"
                # "removed" here is geometry the reference has and the panel's
                # layout does not.
                colour = ADDED if sense == "added" else REMOVED
                label = label_other if sense == "added" else label_ref
                for loc in (row.get(block) or {}).get("locations") or []:
                    outline = loc.get("outline_um")
                    if not outline or drawn >= max_regions_each:
                        continue
                    key = label
                    fig.add_trace(go.Scatter(
                        x=[p[0] for p in outline] + [outline[0][0]],
                        y=[p[1] for p in outline] + [outline[0][1]],
                        fill="toself", mode="lines", opacity=0.6,
                        line=dict(color=colour, width=1), fillcolor=colour,
                        name=label, legendgroup=label, showlegend=key not in seen,
                        hovertemplate=(f"<b>{row['name']}</b><br>{label}<br>"
                                       f"{loc['area_um2']:.6g} µm²<extra></extra>")),
                        row=r, col=c)
                    seen.add(key)
                    drawn += 1
        if cell_bbox_um and len(cell_bbox_um) == 4:
            left, bottom, right, top = cell_bbox_um
            fig.add_shape(type="rect", x0=left, y0=bottom, x1=right, y1=top,
                          line=dict(color=OUTLINE, width=1, dash="dot"),
                          fillcolor="rgba(0,0,0,0)", row=r, col=c)

    fig.update_layout(
        title=f"Each layout against the reference — {reference}",
        height=max(320, 300 * rowcount), showlegend=True,
        legend=dict(title="", orientation="h", yanchor="bottom", y=1.02),
        margin=dict(l=50, r=20, t=90, b=40))
    # Equal aspect on every panel; a layout drawn out of proportion misleads.
    for index in range(len(others)):
        suffix = "" if index == 0 else str(index + 1)
        fig.update_layout(**{f"yaxis{suffix}": dict(
            scaleanchor=f"x{suffix}", scaleratio=1)})
    return fig


def change_hotspot(multi: dict[str, Any], cell_bbox_um: list[float] | None = None,
                   bins: int = 24, reference: str | None = None):
    """Where the churn is: difference area accumulated over a grid of the cell.

    With several revisions the useful question stops being "what changed in this
    pair?" and becomes "which part of the cell keeps being edited?". Every
    difference region from every comparison is dropped into a bin by its centre and
    weighted by its area, so a repeatedly-touched region stands out however the
    revisions are paired.
    """
    import plotly.graph_objects as go

    pairs = [p for p in multi["pairs"] if p.get("comparable") and p.get("detail")]
    if reference:
        pairs = [p for p in pairs if reference in (p["a"], p["b"])]
    if not pairs:
        return None

    points: list[tuple[float, float, float]] = []
    for pair in pairs:
        for row in pair["detail"]["layers"]:
            if row["identical"]:
                continue
            for loc in row["xor"]["locations"]:
                x, y = loc["centre_um"]
                points.append((x, y, loc["area_um2"]))
    if not points:
        return None

    if cell_bbox_um and len(cell_bbox_um) == 4:
        left, bottom, right, top = cell_bbox_um
    else:
        left = min(p[0] for p in points); right = max(p[0] for p in points)
        bottom = min(p[1] for p in points); top = max(p[1] for p in points)
    width = (right - left) or 1.0
    height = (top - bottom) or 1.0
    nx = max(1, bins)
    ny = max(1, int(round(bins * height / width))) if width else bins

    grid = [[0.0] * nx for _ in range(ny)]
    hits = [[0] * nx for _ in range(ny)]
    for x, y, area in points:
        ix = min(nx - 1, max(0, int((x - left) / width * nx)))
        iy = min(ny - 1, max(0, int((y - bottom) / height * ny)))
        grid[iy][ix] += area
        hits[iy][ix] += 1

    xs = [left + (i + 0.5) * width / nx for i in range(nx)]
    ys = [bottom + (j + 0.5) * height / ny for j in range(ny)]
    text = [[f"{grid[j][i]:.6g} µm² over {hits[j][i]} difference(s)"
             if hits[j][i] else "no differences" for i in range(nx)] for j in range(ny)]
    fig = go.Figure(go.Heatmap(
        z=grid, x=xs, y=ys, text=text, hoverinfo="text",
        colorscale="Hot_r", colorbar=dict(title="difference<br>area (µm²)")))
    scope = f" (vs {reference})" if reference else f" across {len(pairs)} pair(s)"
    fig.update_layout(
        title=f"Where the changes concentrate{scope}",
        xaxis_title="x (µm)", yaxis_title="y (µm)", height=420,
        margin=dict(l=60, r=20, t=50, b=50))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def layout_view(outlines: dict[str, Any], only_layers: set[str] | None = None,
                show_labels: bool = True, show_dimensions: bool = True,
                fallback_colours: dict[str, str] | None = None):
    """A KLayout-style plan view of one layout, with dimensions already measured.

    Two differences from a layout viewer, both deliberate:

    * every shape carries its own **width × height** in the hover, so a dimension
      is read rather than measured. Reaching for the ruler tool to answer "how wide
      is that wire?" is the slow part of inspecting a layout, and the answer is
      already in the file;
    * the cell's overall extent is annotated directly on the drawing as a dimension
      line, the way it would be on a drawing sheet.

    Shapes are drawn in the technology's own `.lyp` colours, so the picture matches
    what the engineer sees in KLayout.
    """
    import plotly.graph_objects as go

    rows = [r for r in outlines["layers"]
            if (only_layers is None or r["name"] in only_layers)]
    if not rows:
        return None

    fig = go.Figure()
    drawn = 0
    # Larger layers first, so a big rail cannot hide a via drawn under it.
    for row in sorted(rows, key=lambda r: -(r["extent"]["width_um"] * r["extent"]["height_um"]
                                            if r["extent"] else 0)):
        colour = row.get("colour") or (fallback_colours or {}).get(row["name"]) or OUTLINE
        first = True
        for shape in row["shapes"]:
            outline = shape["outline_um"]
            fig.add_trace(go.Scatter(
                x=[p[0] for p in outline] + [outline[0][0]],
                y=[p[1] for p in outline] + [outline[0][1]],
                fill="toself", mode="lines", opacity=0.5,
                line=dict(color=colour, width=1),
                fillcolor=colour,
                name=row["name"], legendgroup=row["name"], showlegend=first,
                hovertemplate=(
                    f"<b>{row['name']}</b> {row['layer']}/{row['datatype']}<br>"
                    f"<b>{shape['width_um'] * 1000:.0f} × {shape['height_um'] * 1000:.0f} nm</b><br>"
                    f"area {shape['area_um2']:.6g} µm²<br>"
                    f"centre ({shape['centre_um'][0]}, {shape['centre_um'][1]}) µm<br>"
                    f"origin ({shape['left_um']}, {shape['bottom_um']}) µm"
                    "<extra></extra>")))
            first = False
            drawn += 1
        if show_labels and row["labels"]:
            fig.add_trace(go.Scatter(
                x=[lab["at_um"][0] for lab in row["labels"]],
                y=[lab["at_um"][1] for lab in row["labels"]],
                mode="markers+text", text=[lab["text"] for lab in row["labels"]],
                textposition="middle right", textfont=dict(size=9, color=colour),
                marker=dict(symbol="x", size=5, color=colour),
                name=f"{row['name']} (labels)", legendgroup=row["name"],
                showlegend=False,
                hovertemplate=f"<b>%{{text}}</b><br>{row['name']} label<br>"
                              "at (%{x}, %{y}) µm<extra></extra>"))

    if not drawn:
        return None

    left, bottom, right, top = outlines["cell_bbox_um"]
    width, height = outlines["cell_width_um"], outlines["cell_height_um"]
    fig.add_shape(type="rect", x0=left, y0=bottom, x1=right, y1=top,
                  line=dict(color=OUTLINE, width=1, dash="dot"),
                  fillcolor="rgba(0,0,0,0)")

    if show_dimensions and width and height:
        # Dimension lines outside the cell, as on a drawing: an arrow at each end
        # and the measurement written on it. This is the part that replaces
        # measuring by hand.
        pad_x = height * 0.09
        pad_y = width * 0.09
        y_dim = bottom - pad_x
        x_dim = left - pad_y
        fig.add_shape(type="line", x0=left, y0=y_dim, x1=right, y1=y_dim,
                      line=dict(color=DIMENSION, width=1))
        fig.add_shape(type="line", x0=x_dim, y0=bottom, x1=x_dim, y1=top,
                      line=dict(color=DIMENSION, width=1))
        for x in (left, right):          # extension ticks
            fig.add_shape(type="line", x0=x, y0=y_dim, x1=x, y1=bottom,
                          line=dict(color=DIMENSION, width=1, dash="dot"))
        for y in (bottom, top):
            fig.add_shape(type="line", x0=x_dim, y0=y, x1=left, y1=y,
                          line=dict(color=DIMENSION, width=1, dash="dot"))
        fig.add_annotation(x=(left + right) / 2, y=y_dim, text=f"<b>{width * 1000:.0f} nm</b>",
                           showarrow=False, yshift=-11, font=dict(color=DIMENSION, size=11))
        fig.add_annotation(x=x_dim, y=(bottom + top) / 2, text=f"<b>{height * 1000:.0f} nm</b>",
                           showarrow=False, textangle=-90, xshift=-11,
                           font=dict(color=DIMENSION, size=11))

    fig.update_layout(
        title=(f"{outlines['top_cell']} — {width * 1000:.0f} × {height * 1000:.0f} nm "
               f"({width} × {height} µm)"),
        xaxis_title="x (µm)", yaxis_title="y (µm)", height=560,
        legend=dict(title="", orientation="v"),
        margin=dict(l=70, r=20, t=48, b=60))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig
