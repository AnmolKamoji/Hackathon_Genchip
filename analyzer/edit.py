"""Apply edits to a layout and write a new GDSII.

The editing rule that shapes everything here: **the browser never edits the file.**
It records what the user did as a journal of operations, and this module replays that
journal against the real layout with KLayout. Two reasons, and they are the same two
reasons the numbers are computed in Python rather than in JavaScript:

* A polygon drawn on screen is a float on a canvas. A polygon in a GDSII file is an
  integer on the database grid. Rounding one to the other in the browser would put
  off-grid vertices in the file, which is how a layout stops being manufacturable
  without anything looking wrong.
* Every shape the viewer draws was flattened through the hierarchy, so the rectangle
  under the pointer may live in a child cell shared by twenty placements. Editing it
  by screen position would silently change all twenty. Each operation therefore names
  the cell it belongs to and carries the shape's outline *in that cell's own
  coordinates*, and this module refuses to apply an operation whose target it cannot
  find exactly.

Refusal is the point. An editor that guesses which polygon you meant is worse than
one that stops and says it could not find it.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .gds_parser import rank_top_cells

# Operations that change geometry. Anything else is rejected by name, so a typo in a
# journal cannot silently become a no-op that the report calls a success.
OPS = ("insert", "delete", "move", "replace", "transform", "insert_text",
       "delete_text", "boolean", "combine", "insert_instance", "delete_instance")


class EditError(ValueError):
    """An operation could not be applied. Carries the operation for the report."""

    def __init__(self, message: str, op: dict[str, Any] | None = None):
        super().__init__(message)
        self.op = op


def _to_dbu(value_um: float, dbu: float) -> int:
    """Micrometres to database units, on the grid.

    `round` rather than `int`: truncation biases every coordinate toward zero, which
    over a few thousand vertices is a systematic shrink rather than a rounding error.
    """
    return int(round(float(value_um) / dbu))


def _points_to_dbu(points: list[list[float]], dbu: float) -> list[tuple[int, int]]:
    return [(_to_dbu(x, dbu), _to_dbu(y, dbu)) for x, y in points]


def _layer_index(layout, spec: Any, layermap: dict[str, Any] | None):
    """Resolve a layer name or {"layer": n, "datatype": d} to a layer index.

    A name is resolved through the layer map, never invented: drawing on a layer the
    technology does not define would produce a file whose layer numbers mean nothing.
    """
    import klayout.db as db

    if isinstance(spec, dict):
        key = (int(spec["layer"]), int(spec.get("datatype", 0)))
    elif isinstance(spec, (list, tuple)) and len(spec) == 2:
        key = (int(spec[0]), int(spec[1]))
    else:
        name = str(spec)
        key = None
        for candidate, entry in ((layermap or {}).get("by_key") or {}).items():
            if entry.get("technology_name") == name:
                key = candidate
                break
        if key is None:
            raise EditError(
                f"unknown layer '{name}': it is not in the layer map, and a layer "
                "number cannot be invented for it")
    info = db.LayerInfo(key[0], key[1])
    found = layout.find_layer(info)
    return layout.layer(info) if found is None else found


def _shape_polygon(shape):
    """The shape as a polygon in its own cell's coordinates, or None if it is text."""
    import klayout.db as db

    if shape.is_box():
        return db.Polygon(shape.box)
    if shape.is_polygon():
        return shape.polygon
    if shape.is_path():
        return shape.path.polygon()
    return None


def _hull(poly) -> tuple[tuple[int, int], ...]:
    return tuple((pt.x, pt.y) for pt in poly.each_point_hull())


def _find_target(layout, target: dict[str, Any], layermap):
    """The one shape an operation refers to, or an error saying why not.

    Matching is on exact integer coordinates in the shape's own cell, plus the rank
    among identical siblings. Nothing here is a tolerance: if the file has moved on
    since the journal was recorded, the match fails and the operation is refused.
    """
    cell_name = target.get("cell")
    cell = layout.cell(cell_name) if cell_name else None
    if cell is None:
        raise EditError(f"cell '{cell_name}' is not in this layout")
    li = _layer_index(layout, target.get("layer_key") or target["layer"], layermap)
    wanted = tuple(tuple(p) for p in target["local_dbu"])
    rank = int(target.get("dup", 0))

    seen = 0
    for shape in cell.shapes(li).each():
        poly = _shape_polygon(shape)
        if poly is None:
            continue
        if _hull(poly) != wanted:
            continue
        if seen == rank:
            return cell, li, shape
        seen += 1
    raise EditError(
        f"the shape this edit refers to is no longer in {cell_name} on "
        f"{target.get('layer')} (looked for outline {wanted[:2]}… rank {rank}); "
        "nothing was changed")


def _inverse(trans_string: str | None):
    """The transform from top-cell coordinates back into a shape's own cell.

    The editor works in the coordinates the user can see, which are the top cell's.
    A shape inside a placed cell lives in different ones, and writing the user's
    coordinates straight into that cell would move the shape by the placement's own
    offset - visibly wrong on the first rotated instance, subtly wrong on the rest.
    """
    import klayout.db as db

    if not trans_string:
        return None
    inverse = db.ICplxTrans.from_s(str(trans_string)).inverted()
    if inverse.is_unity():
        return None
    return inverse


def _placements(layout, cell_name: str) -> int:
    """How many times this cell is placed under the top cell.

    Reported so an edit to a shared cell says how far it reaches before it is made,
    rather than after.
    """
    cell = layout.cell(cell_name)
    if cell is None:
        return 0
    total = 0
    for caller in cell.caller_cells():
        parent = layout.cell(caller)
        for inst in parent.each_inst():
            if inst.cell_index == cell.cell_index():
                total += inst.size()
    return max(total, 1)


# --- the operations ---------------------------------------------------------

def _op_insert(layout, op, layermap, dbu, notes):
    import klayout.db as db

    cell_name = op.get("cell")
    cell = layout.cell(cell_name) if cell_name else rank_top_cells(layout)[0]
    if cell is None:
        raise EditError(f"cell '{cell_name}' is not in this layout", op)
    li = _layer_index(layout, op["layer"], layermap)
    points = op.get("points")
    if not points or len(points) < 3:
        raise EditError("a polygon needs at least three points", op)
    poly = db.Polygon([db.Point(x, y) for x, y in _points_to_dbu(points, dbu)])
    if poly.area() == 0:
        raise EditError("that polygon has zero area", op)
    inverse = _inverse(op.get("trans"))
    if inverse is not None:
        poly = poly.transformed(inverse)
    cell.shapes(li).insert(poly)
    notes.append(f"inserted a {poly.num_points()}-point polygon on "
                 f"{op['layer']} in {cell.name}")


def _op_delete(layout, op, layermap, dbu, notes):
    cell, li, shape = _find_target(layout, op["target"], layermap)
    cell.shapes(li).erase(shape)
    notes.append(f"deleted a shape on {op['target'].get('layer')} in {cell.name}")


def _op_move(layout, op, layermap, dbu, notes):
    import klayout.db as db

    cell, li, shape = _find_target(layout, op["target"], layermap)
    dx = _to_dbu(op.get("dx_um", 0.0), dbu)
    dy = _to_dbu(op.get("dy_um", 0.0), dbu)
    poly = _shape_polygon(shape)
    step = db.Vector(dx, dy)
    inverse = _inverse(op["target"].get("trans"))
    if inverse is not None:
        # Only the rotation and mirror apply to a displacement; adding the
        # placement's own offset would move the shape twice.
        step = inverse.trans(step)
    cell.shapes(li).erase(shape)
    cell.shapes(li).insert(poly.transformed(db.Trans(step)))
    notes.append(f"moved a shape on {op['target'].get('layer')} by "
                 f"{dx * dbu:g}, {dy * dbu:g} µm")


def _op_replace(layout, op, layermap, dbu, notes):
    """Vertex edits, stretches and resizes all arrive as a new outline."""
    import klayout.db as db

    cell, li, shape = _find_target(layout, op["target"], layermap)
    points = op.get("points")
    if not points or len(points) < 3:
        raise EditError("the replacement polygon needs at least three points", op)
    poly = db.Polygon([db.Point(x, y) for x, y in _points_to_dbu(points, dbu)])
    if poly.area() == 0:
        raise EditError("the replacement polygon has zero area", op)
    inverse = _inverse(op["target"].get("trans"))
    if inverse is not None:
        poly = poly.transformed(inverse)
    cell.shapes(li).erase(shape)
    cell.shapes(li).insert(poly)
    notes.append(f"reshaped a shape on {op['target'].get('layer')} in {cell.name}")


def _op_transform(layout, op, layermap, dbu, notes):
    import klayout.db as db

    cell, li, shape = _find_target(layout, op["target"], layermap)
    poly = _shape_polygon(shape)
    rotate = int(op.get("rotate", 0)) % 360
    if rotate not in (0, 90, 180, 270):
        raise EditError("rotation must be 0, 90, 180 or 270 degrees", op)
    mirror = bool(op.get("mirror"))
    about = op.get("about_um")
    if about:
        ax, ay = _to_dbu(about[0], dbu), _to_dbu(about[1], dbu)
    else:
        box = poly.bbox()
        ax = (box.left + box.right) // 2
        ay = (box.bottom + box.top) // 2
    # Rotate about a point: translate to the origin, turn, translate back. Doing it
    # in one step would rotate about the cell origin and fling the shape across
    # the layout.
    to_origin = db.Trans(db.Vector(-ax, -ay))
    turn = db.Trans(rotate // 90, mirror, 0, 0)
    back = db.Trans(db.Vector(ax, ay))
    cell.shapes(li).erase(shape)
    cell.shapes(li).insert(poly.transformed(to_origin).transformed(turn)
                           .transformed(back))
    notes.append(f"rotated {rotate}°{' and mirrored' if mirror else ''} a shape on "
                 f"{op['target'].get('layer')}")


def _op_insert_text(layout, op, layermap, dbu, notes):
    import klayout.db as db

    cell_name = op.get("cell")
    cell = layout.cell(cell_name) if cell_name else rank_top_cells(layout)[0]
    li = _layer_index(layout, op["layer"], layermap)
    at = op.get("at_um") or [0, 0]
    text = str(op.get("text", "")).strip()
    if not text:
        raise EditError("a label needs some text", op)
    point = db.Point(_to_dbu(at[0], dbu), _to_dbu(at[1], dbu))
    inverse = _inverse(op.get("trans"))
    if inverse is not None:
        point = inverse.trans(point)
    cell.shapes(li).insert(db.Text(text, db.Trans(db.Vector(point.x, point.y))))
    notes.append(f"added the label '{text}' on {op['layer']}")


def _op_delete_text(layout, op, layermap, dbu, notes):
    cell_name = op.get("cell")
    cell = layout.cell(cell_name) if cell_name else rank_top_cells(layout)[0]
    li = _layer_index(layout, op["layer"], layermap)
    at = op.get("at_um") or [0, 0]
    x, y = _to_dbu(at[0], dbu), _to_dbu(at[1], dbu)
    wanted = str(op.get("text", ""))
    for shape in cell.shapes(li).each():
        if not shape.is_text():
            continue
        text = shape.text
        if text.string == wanted and text.x == x and text.y == y:
            cell.shapes(li).erase(shape)
            notes.append(f"deleted the label '{wanted}' on {op['layer']}")
            return
    raise EditError(f"no label '{wanted}' at {at} on {op['layer']}", op)


def _op_boolean(layout, op, layermap, dbu, notes):
    """Layer-level booleans, the way KLayout's do them: exact, merged, in Region.

    Done here rather than in the browser because a boolean on floating-point screen
    polygons produces slivers - a subtraction that leaves a 0.3 nm shard is a DRC
    violation that nobody drew.
    """
    import klayout.db as db

    cell_name = op.get("cell")
    cell = layout.cell(cell_name) if cell_name else rank_top_cells(layout)[0]
    operation = str(op.get("operation", "or")).lower()
    if operation not in ("or", "not", "and", "xor"):
        raise EditError(f"unknown boolean '{operation}'", op)
    li_a = _layer_index(layout, op["layer_a"], layermap)
    li_b = _layer_index(layout, op["layer_b"], layermap)
    into = op.get("into") or op["layer_a"]
    li_into = _layer_index(layout, into, layermap)

    a = db.Region(cell.begin_shapes_rec(li_a))
    b = db.Region(cell.begin_shapes_rec(li_b))
    result = {"or": a + b, "not": a - b, "and": a & b, "xor": a ^ b}[operation]
    result.merge()
    if li_into in (li_a, li_b):
        cell.shapes(li_into).clear()
    cell.shapes(li_into).insert(result)
    notes.append(f"{operation.upper()} of {op['layer_a']} and {op['layer_b']} "
                 f"-> {into} ({result.count()} polygon(s))")


def _op_insert_instance(layout, op, layermap, dbu, notes):
    """Place a cell. This is the half of editing that is not drawing.

    A layout is built by placing cells, not by drawing every rectangle twice, and a
    placement is the one edit that stays correct when the cell it points at changes
    later. Arrays are placed as a single GDSII AREF rather than as n copies, which is
    what the format is for and what keeps the file small.
    """
    import klayout.db as db

    into_name = op.get("into")
    into = layout.cell(into_name) if into_name else rank_top_cells(layout)[0]
    if into is None:
        raise EditError(f"cell '{into_name}' is not in this layout", op)
    child = layout.cell(str(op.get("cell", "")))
    if child is None:
        raise EditError(f"there is no cell called '{op.get('cell')}' to place", op)
    if child.cell_index() == into.cell_index() or into.cell_index() in set(child.called_cells()):
        # GDSII cannot express this and KLayout will not read it back.
        raise EditError(
            f"placing '{child.name}' into '{into.name}' would make a cell contain "
            "itself, which GDSII cannot express", op)

    at = op.get("at_um") or [0, 0]
    rotate = int(op.get("rotate", 0)) % 360
    if rotate not in (0, 90, 180, 270):
        raise EditError("rotation must be 0, 90, 180 or 270 degrees", op)
    trans = db.Trans(rotate // 90, bool(op.get("mirror")),
                     _to_dbu(at[0], dbu), _to_dbu(at[1], dbu))

    array = op.get("array") or {}
    nx, ny = int(array.get("nx", 1) or 1), int(array.get("ny", 1) or 1)
    if nx > 1 or ny > 1:
        step_x = _to_dbu(array.get("dx_um", 0) or 0, dbu)
        step_y = _to_dbu(array.get("dy_um", 0) or 0, dbu)
        if (nx > 1 and not step_x) or (ny > 1 and not step_y):
            raise EditError("an array needs a step in the direction it repeats", op)
        placement = db.CellInstArray(child.cell_index(), trans,
                                     db.Vector(step_x, 0), db.Vector(0, step_y), nx, ny)
        notes.append(f"placed {nx}×{ny} of {child.name} in {into.name}")
    else:
        placement = db.CellInstArray(child.cell_index(), trans)
        notes.append(f"placed {child.name} in {into.name} at "
                     f"{at[0]:g}, {at[1]:g} µm")
    into.insert(placement)


def _op_delete_instance(layout, op, layermap, dbu, notes):
    """Remove one placement, matched on the cell and the exact transform."""
    into_name = op.get("into")
    into = layout.cell(into_name) if into_name else rank_top_cells(layout)[0]
    child = layout.cell(str(op.get("cell", "")))
    if into is None or child is None:
        raise EditError(f"no such cell: {op.get('into')} / {op.get('cell')}", op)
    wanted = str(op.get("trans", "")).strip()
    for inst in into.each_inst():
        if inst.cell_index != child.cell_index():
            continue
        if wanted and inst.trans.to_s() != wanted:
            continue
        into.erase(inst)
        notes.append(f"removed a placement of {child.name} from {into.name}")
        return
    raise EditError(f"no placement of {child.name} in {into.name}"
                    + (f" at {wanted}" if wanted else ""), op)


def _op_combine(layout, op, layermap, dbu, notes):
    """Merge or subtract the selected shapes, exactly.

    KLayout has these on a selection and they are the two edits that most often go
    wrong by hand: a merge done by dragging two rectangles until they look joined
    leaves a hairline gap, and a subtraction done by eye leaves a sliver. Both are
    computed here by Region, on integers, so neither can happen.
    """
    import klayout.db as db

    targets = op.get("targets") or []
    if len(targets) < 2:
        raise EditError("merging needs at least two shapes", op)
    operation = str(op.get("operation", "merge")).lower()
    if operation not in ("merge", "subtract"):
        raise EditError(f"unknown combine '{operation}'", op)

    found = [_find_target(layout, target, layermap) for target in targets]
    cells = {cell.name for cell, _, _ in found}
    layers = {li for _, li, _ in found}
    if len(cells) > 1 or len(layers) > 1:
        raise EditError(
            "these shapes are on different layers or in different cells; a boolean "
            "between them would have to invent which layer the result belongs to", op)
    cell, li = found[0][0], found[0][1]

    polygons = [_shape_polygon(shape) for _, _, shape in found]
    first = db.Region(polygons[0])
    rest = db.Region()
    for poly in polygons[1:]:
        rest.insert(poly)
    result = (first + rest) if operation == "merge" else (first - rest)
    result.merge()
    for _, _, shape in found:
        cell.shapes(li).erase(shape)
    cell.shapes(li).insert(result)
    notes.append(f"{operation}d {len(found)} shapes into {result.count()} polygon(s)")


_HANDLERS = {
    "combine": _op_combine,
    "insert_instance": _op_insert_instance,
    "delete_instance": _op_delete_instance,
    "insert": _op_insert,
    "delete": _op_delete,
    "move": _op_move,
    "replace": _op_replace,
    "transform": _op_transform,
    "insert_text": _op_insert_text,
    "delete_text": _op_delete_text,
    "boolean": _op_boolean,
}


# --- the journal ------------------------------------------------------------

def _off_grid(layout, dbu: float, grid_nm: float | None) -> dict[str, Any]:
    """Vertices that do not sit on the given design grid.

    The database unit is the finest the file can express; a design grid is coarser
    and is what a process actually wants. A shape can be perfectly on the database
    grid and still off the design grid, so this is checked and reported rather than
    silently corrected - snapping someone\'s geometry without telling them is worse.
    """
    if not grid_nm:
        return {}
    step = grid_nm / 1000.0 / dbu
    if step <= 1.0000001:
        return {}
    bad: dict[str, int] = {}
    for cell in layout.each_cell():
        for li in layout.layer_indexes():
            info = layout.get_info(li)
            for shape in cell.shapes(li).each():
                poly = _shape_polygon(shape)
                if poly is None:
                    continue
                for point in poly.each_point_hull():
                    if point.x % step or point.y % step:
                        key = f"{info.layer}/{info.datatype}"
                        bad[key] = bad.get(key, 0) + 1
                        break
    return bad


def apply_edits(gds_path: str | Path, edits: list[dict[str, Any]],
                out_path: str | Path | None = None,
                layermap: dict[str, Any] | None = None,
                atomic: bool = True,
                grid_nm: float | None = None) -> dict[str, Any]:
    """Replay an edit journal against a layout.

    `atomic` is the default and means what it says: if any operation cannot be
    applied, nothing is written. A half-applied journal is the worst outcome
    available - the file no longer matches either the original or what the editor
    shows - so the caller has to opt into it explicitly.

    Returns a report: what was applied, what was refused and why, which shared cells
    were touched and how many placements that reaches.
    """
    import klayout.db as db

    layout = db.Layout()
    layout.read(str(gds_path))
    tops = rank_top_cells(layout)
    if not tops:
        raise ValueError("GDS contains no top-level cell.")
    dbu = float(layout.dbu)

    # Measured before anything is applied, so the report can say what this edit did
    # rather than what the file already was.
    baseline = _off_grid(layout, dbu, grid_nm)

    notes: list[str] = []
    refused: list[dict[str, Any]] = []
    applied = 0
    shared: dict[str, int] = {}

    for index, op in enumerate(edits or []):
        kind = str(op.get("op", ""))
        handler = _HANDLERS.get(kind)
        if handler is None:
            problem = {"index": index, "op": kind or "(missing)",
                       "reason": f"unknown operation; expected one of {', '.join(OPS)}"}
            if atomic:
                raise EditError(problem["reason"], op)
            refused.append(problem)
            continue
        try:
            target = op.get("target") or {}
            if target.get("cell") and target["cell"] != tops[0].name:
                count = _placements(layout, target["cell"])
                if count > 1:
                    shared[target["cell"]] = count
            handler(layout, op, layermap, dbu, notes)
            applied += 1
        except EditError as exc:
            if atomic:
                raise
            refused.append({"index": index, "op": kind, "reason": str(exc)})
        except Exception as exc:                       # KLayout's own complaints
            if atomic:
                raise EditError(f"{kind}: {exc}", op) from exc
            refused.append({"index": index, "op": kind, "reason": str(exc)})

    # Off-grid shapes the *edit* introduced. Counting the whole file instead would
    # bury them: these layouts already sit on a half-nanometre grid, so a report of
    # "76 off-grid shapes" says nothing about the change just made. Landing off-grid
    # is the failure that survives review - the picture is right, the number reads
    # right, and the mask writer rounds it somewhere else.
    after = _off_grid(layout, dbu, grid_nm)
    added = {k: v - baseline.get(k, 0) for k, v in after.items()
             if v > baseline.get(k, 0)}
    off_grid = ({"checked": True, "grid_nm": grid_nm,
                 "added": sum(added.values()), "total": sum(after.values()),
                 "layers": [{"layer": k, "shapes": v} for k, v in sorted(added.items())]}
                if grid_nm else {"checked": False})

    written = None
    if out_path is not None and (applied or not edits):
        layout.write(str(out_path))
        written = str(out_path)

    return {
        "applied": applied,
        "requested": len(edits or []),
        "refused": refused,
        "notes": notes,
        "written": written,
        "top_cell": tops[0].name,
        "dbu_um": dbu,
        "shared_cells": [{"cell": name, "placements": count}
                         for name, count in sorted(shared.items())],
        "off_grid": off_grid,
        "warnings": [
            f"'{name}' is placed {count} times, so this edit changes all {count} "
            "of them. GDSII has one definition per cell; editing one copy is not "
            "something the format can express."
            for name, count in sorted(shared.items())
        ],
    }


def apply_to_bytes(gds_bytes: bytes, filename: str, edits: list[dict[str, Any]],
                   layermap: dict[str, Any] | None = None,
                   atomic: bool = True,
                   grid_nm: float | None = None) -> tuple[bytes, dict[str, Any]]:
    """Same, for an upload held in memory. Returns the edited file and the report."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / filename
        source.write_bytes(gds_bytes)
        target = Path(td) / f"edited_{filename}"
        report = apply_edits(source, edits, target, layermap=layermap, atomic=atomic,
                             grid_nm=grid_nm)
        data = target.read_bytes() if target.exists() else gds_bytes
    return data, report


def describe(edits: list[dict[str, Any]]) -> list[str]:
    """One readable line per operation, for the journal panel and the report."""
    out = []
    for op in edits or []:
        kind = op.get("op")
        target = op.get("target") or {}
        layer = op.get("layer") or target.get("layer") or "?"
        if kind == "insert":
            out.append(f"draw on {layer} ({len(op.get('points') or [])} points)")
        elif kind == "delete":
            out.append(f"delete a shape on {layer}")
        elif kind == "move":
            out.append(f"move a shape on {layer} by "
                       f"{op.get('dx_um', 0) * 1000:g}, {op.get('dy_um', 0) * 1000:g} nm")
        elif kind == "replace":
            out.append(f"reshape a shape on {layer}")
        elif kind == "transform":
            bits = []
            if op.get("rotate"):
                bits.append(f"rotate {op['rotate']}°")
            if op.get("mirror"):
                bits.append("mirror")
            out.append(f"{' and '.join(bits) or 'transform'} a shape on {layer}")
        elif kind == "insert_text":
            out.append(f"label '{op.get('text')}' on {layer}")
        elif kind == "delete_text":
            out.append(f"delete the label '{op.get('text')}' on {layer}")
        elif kind == "insert_instance":
            array = op.get("array") or {}
            count = int(array.get("nx", 1) or 1) * int(array.get("ny", 1) or 1)
            out.append(f"place {op.get('cell')}"
                       + (f" as a {array.get('nx')}×{array.get('ny')} array" if count > 1 else ""))
        elif kind == "delete_instance":
            out.append(f"remove a placement of {op.get('cell')}")
        elif kind == "combine":
            out.append(f"{op.get('operation', 'merge')} "
                       f"{len(op.get('targets') or [])} shapes on {layer}")
        elif kind == "boolean":
            out.append(f"{str(op.get('operation', '')).upper()} "
                       f"{op.get('layer_a')} and {op.get('layer_b')} "
                       f"into {op.get('into') or op.get('layer_a')}")
        else:
            out.append(str(kind))
    return out


def normalise(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A journal safe to store: known operations only, and nothing shared by reference.

    The editor keeps its journal in session state across reruns, so a caller holding
    a reference to the same list could mutate history after the fact.
    """
    clean = []
    for op in edits or []:
        if str(op.get("op")) in OPS:
            clean.append(copy.deepcopy(op))
    return clean
