/* The layout editor.
 *
 * Loaded after viewer.js and hung on the same prototype, so an edit tool has the
 * viewer's transforms, snapping and picking rather than a second copy of them.
 *
 * Two rules run through all of it:
 *
 *   1. The browser never writes the file. Every change is recorded against the
 *      shape's identity - the cell it lives in and its outline in that cell's own
 *      database units - and replayed in Python by KLayout. What you see here is a
 *      preview; after a commit the page sends back what was actually written, and
 *      the preview is replaced by it. A disagreement is therefore visible on the
 *      next commit rather than discovered in the fab.
 *
 *   2. Coordinates land on the grid before they are shown, not after. A vertex that
 *      reads 20 nm on screen and 20.0003 nm in the file is the kind of thing that
 *      passes review and fails manufacturing.
 */
(function () {
  "use strict";

  const NS = window.GDSViewer;
  if (!NS || !NS.Viewer) return;
  const P = NS.Viewer.prototype;
  const geom = NS.geom || {};
  const polyBBox = geom.polyBBox;
  const pointInPoly = geom.pointInPoly;
  const fmtLen = geom.fmtLen;
  const fmtArea = geom.fmtArea;

  const EDIT_MODES = ["edit", "rect", "poly", "wire", "text", "place"];
  const HANDLE_PX = 5;

  // --- state ---------------------------------------------------------------

  P.initEditor = function () {
    this.selected = [];
    this.editState = {
      layer: null,             // the layer new geometry goes on
      gridNm: null,            // snap step; null means the database unit
      pending: null,           // geometry being drawn
      handles: [],             // grab points for the current selection
      history: [],             // local undo stack
      future: [],
      clipboard: null,
      lastDelta: null,         // for repeat-duplicate
      dirty: false,
      trackSnap: true,
      committing: false,
    };
    const drawn = (this.edit.drawnLayers || []);
    const first = drawn.find((n) => /^M[0-9]/i.test(n)) || drawn[0];
    this.editState.layer = first || (this.edit.layers[0] || {}).name || null;
    const steps = this.edit.gridStepsNm || [];
    this.editState.gridNm = steps.includes(1) ? 1 : (steps[0] || null);
  };

  P.editing = function () {
    return !!this.edit && EDIT_MODES.indexOf(this.mode) >= 0;
  };

  P.editLayerEntry = function (name) {
    return (this.edit.layers || []).find((l) => l.name === (name || this.editState.layer));
  };

  // --- grid and snapping ---------------------------------------------------

  // The editor's own snap: the view's vertex/edge snap first, because lining a new
  // shape up with an existing edge is the commonest thing anyone does, then the
  // routing grid, then the plain design grid.
  P.editSnap = function (wxp, wyp, opts) {
    const state = this.editState;
    const options = opts || {};
    let x = wxp, y = wyp, kind = null;

    if (this.snapOn) {
      const near = this.snap(wxp, wyp);
      if (near && near.kind) { x = near.x; y = near.y; kind = near.kind; }
    }
    if (kind === null && state.trackSnap && !options.noTracks) {
      const track = this.nearestTrack(wxp, wyp);
      if (track) {
        if (track.axis === "y") { y = track.at; } else { x = track.at; }
        kind = "track";
      }
    }
    if (kind === null && state.gridNm) {
      const step = state.gridNm / 1000;
      x = Math.round(wxp / step) * step;
      y = Math.round(wyp / step) * step;
      kind = "grid";
    }
    // Whatever produced the point, it still has to sit on the database grid: that
    // is the only resolution the file can hold.
    const dbu = (this.edit.dbuNm || 1) / 1000;
    return { x: Math.round(x / dbu) * dbu, y: Math.round(y / dbu) * dbu, kind: kind };
  };

  P.nearestTrack = function (wxp, wyp) {
    // Snapping a wire to the routing grid is the standard-cell version of snapping
    // to a guide, and it is the thing that makes a hand edit land on-pitch instead
    // of one database unit beside it.
    const tracks = this.tracks || {};
    const layer = this.editState.layer || "";
    const entry = tracks[layer] || tracks[layer.toUpperCase()];
    if (!entry || !entry.positionsNm) return null;
    const axis = entry.axis === "x" ? "x" : "y";
    const value = axis === "y" ? wyp : wxp;
    const tol = 8 / this.scale;
    let best = null;
    for (const nm of entry.positionsNm) {
      const at = nm / 1000;
      const gap = Math.abs(at - value);
      if (gap <= tol && (!best || gap < best.gap)) best = { at: at, gap: gap, axis: axis };
    }
    return best;
  };

  // --- local application ---------------------------------------------------

  P.editLayerRow = function (name) {
    let row = this.A.layers.find((l) => l.name === name);
    if (row) return row;
    // Drawing on a layer the file does not use yet: it needs a row before it can
    // hold a shape, and the technology's own colour so it does not appear grey.
    const entry = this.editLayerEntry(name) || {};
    row = {
      name: name, layer: entry.layer, datatype: entry.datatype,
      role: entry.role || "drawing", colour: entry.colour || "#8aa0b6",
      shapes: [], labels: [], count: 0, labelCount: 0, extent: null,
    };
    this.A.layers.push(row);
    this.visible.add(name);
    return row;
  };

  P.editRecord = function (undo, redo, label) {
    const state = this.editState;
    state.history.push({ undo: undo, redo: redo, label: label });
    if (state.history.length > 200) state.history.shift();
    state.future.length = 0;
    state.dirty = true;
    this.renderPanel();
  };

  P.editUndo = function () {
    const state = this.editState;
    const step = state.history.pop();
    if (!step) { this.toast("Nothing to undo"); return; }
    step.undo();
    state.future.push(step);
    state.dirty = state.history.length > 0;
    this.selected = [];
    this.renderPanel();
    this.draw();
  };

  P.editRedo = function () {
    const state = this.editState;
    const step = state.future.pop();
    if (!step) { this.toast("Nothing to redo"); return; }
    step.redo();
    state.history.push(step);
    state.dirty = true;
    this.selected = [];
    this.renderPanel();
    this.draw();
  };

  P.addShape = function (layerName, points, label) {
    const row = this.editLayerRow(layerName);
    const shape = this.shapeFromPoints(points);
    row.shapes.push(shape);
    row.count = row.shapes.filter((s) => !s._del).length;
    this.editRecord(
      () => { const i = row.shapes.indexOf(shape); if (i >= 0) row.shapes.splice(i, 1); },
      () => { row.shapes.push(shape); },
      label || `draw on ${layerName}`);
    return shape;
  };

  P.shapeFromPoints = function (points) {
    const [x0, y0, x1, y1] = polyBBox(points);
    let area = 0;
    for (let i = 0; i < points.length; i++) {
      const [ax, ay] = points[i];
      const [bx, by] = points[(i + 1) % points.length];
      area += ax * by - bx * ay;
    }
    return {
      o: points.map((p) => [p[0], p[1]]),
      w: +(x1 - x0).toFixed(6), h: +(y1 - y0).toFixed(6),
      a: +Math.abs(area / 2).toFixed(9),
      cx: +((x0 + x1) / 2).toFixed(6), cy: +((y0 + y1) / 2).toFixed(6),
      x: +x0.toFixed(6), y: +y0.toFixed(6), v: points.length,
      _new: true,
    };
  };

  P.reshape = function (entry, points) {
    const shape = entry.shape;
    const before = shape.o.map((p) => [p[0], p[1]]);
    const apply = (pts) => {
      const fresh = this.shapeFromPoints(pts);
      shape.o = fresh.o; shape.w = fresh.w; shape.h = fresh.h; shape.a = fresh.a;
      shape.cx = fresh.cx; shape.cy = fresh.cy; shape.x = fresh.x; shape.y = fresh.y;
      shape.v = fresh.v;
      if (shape.id) shape._dirty = true;
    };
    apply(points);
    this.editRecord(() => apply(before), () => apply(points),
                    `reshape on ${entry.layer}`);
  };

  P.deleteSelection = function () {
    if (!this.selected.length) { this.toast("Nothing selected"); return; }
    const items = this.selected.slice();
    const apply = (flag) => {
      for (const item of items) {
        if (item.shape.id) item.shape._del = flag;
        else {
          const row = this.A.layers.find((l) => l.name === item.layer);
          const i = row.shapes.indexOf(item.shape);
          if (flag && i >= 0) row.shapes.splice(i, 1);
          else if (!flag && i < 0) row.shapes.push(item.shape);
        }
      }
    };
    apply(true);
    this.selected = [];
    this.selection = null;
    this.editRecord(() => apply(false), () => apply(true),
                    `delete ${items.length} shape(s)`);
    this.draw();
  };

  P.moveSelection = function (dx, dy, record) {
    const items = this.selected.slice();
    if (!items.length) return;
    const shift = (ddx, ddy) => {
      for (const item of items) {
        const shape = item.shape;
        shape.o = shape.o.map((p) => [+(p[0] + ddx).toFixed(6), +(p[1] + ddy).toFixed(6)]);
        shape.x = +(shape.x + ddx).toFixed(6);
        shape.y = +(shape.y + ddy).toFixed(6);
        shape.cx = +(shape.cx + ddx).toFixed(6);
        shape.cy = +(shape.cy + ddy).toFixed(6);
        if (shape.id) shape._dirty = true;
      }
    };
    shift(dx, dy);
    if (record !== false) {
      this.editState.lastDelta = { dx: dx, dy: dy };
      this.editRecord(() => shift(-dx, -dy), () => shift(dx, dy),
                      `move ${items.length} shape(s)`);
    }
  };

  P.transformSelection = function (rotate, mirror) {
    const items = this.selected.slice();
    if (!items.length) { this.toast("Nothing selected"); return; }
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const item of items) {
      const [a, b, c, d] = polyBBox(item.shape.o);
      x0 = Math.min(x0, a); y0 = Math.min(y0, b);
      x1 = Math.max(x1, c); y1 = Math.max(y1, d);
    }
    const ox = (x0 + x1) / 2, oy = (y0 + y1) / 2;
    const dbu = (this.edit.dbuNm || 1) / 1000;
    const grid = (v) => Math.round(v / dbu) * dbu;
    const turn = (angle, flip) => {
      const rad = angle * Math.PI / 180;
      const cos = Math.round(Math.cos(rad)), sin = Math.round(Math.sin(rad));
      for (const item of items) {
        const shape = item.shape;
        const points = shape.o.map(([px, py]) => {
          let dx = px - ox, dy = py - oy;
          if (flip) dx = -dx;
          return [grid(ox + dx * cos - dy * sin), grid(oy + dx * sin + dy * cos)];
        });
        const fresh = this.shapeFromPoints(points);
        Object.assign(shape, {
          o: fresh.o, w: fresh.w, h: fresh.h, a: fresh.a,
          cx: fresh.cx, cy: fresh.cy, x: fresh.x, y: fresh.y, v: fresh.v,
        });
        if (shape.id) shape._dirty = true;
      }
    };
    turn(rotate, mirror);
    this.editRecord(() => turn(-rotate, mirror), () => turn(rotate, mirror),
                    rotate ? `rotate ${rotate}°` : "mirror");
    this.draw();
  };

  P.copySelection = function () {
    if (!this.selected.length) { this.toast("Nothing selected"); return; }
    this.editState.clipboard = this.selected.map((item) => ({
      layer: item.layer, points: item.shape.o.map((p) => [p[0], p[1]]),
    }));
    this.toast(`Copied ${this.selected.length} shape(s)`);
  };

  P.pasteClipboard = function (dx, dy) {
    const clip = this.editState.clipboard;
    if (!clip || !clip.length) { this.toast("Nothing copied"); return; }
    const offset = dx === undefined ? (this.editState.gridNm || 1) / 1000 * 5 : dx;
    const offsetY = dy === undefined ? 0 : dy;
    const made = [];
    for (const item of clip) {
      const points = item.points.map((p) => [+(p[0] + offset).toFixed(6),
                                             +(p[1] + offsetY).toFixed(6)]);
      made.push({ layer: item.layer, shape: this.addShape(item.layer, points, "paste") });
    }
    this.selected = made;
    this.draw();
  };

  P.duplicateSelection = function () {
    if (!this.selected.length) { this.toast("Nothing selected"); return; }
    this.copySelection();
    const last = this.editState.lastDelta;
    this.pasteClipboard(last ? last.dx : undefined, last ? last.dy : undefined);
  };

  // Step and repeat. KLayout has this as a dialog; here it takes the last move as
  // the step, because the move that placed the first copy is the one you want.
  P.arraySelection = function (count) {
    if (!this.selected.length) { this.toast("Nothing selected"); return; }
    const step = this.editState.lastDelta;
    if (!step || (!step.dx && !step.dy)) {
      this.toast("Move a copy once first — that move becomes the step");
      return;
    }
    const source = this.selected.map((item) => ({
      layer: item.layer, points: item.shape.o.map((p) => [p[0], p[1]]),
    }));
    const made = [];
    for (let n = 1; n <= count; n++) {
      for (const item of source) {
        const points = item.points.map((p) => [+(p[0] + step.dx * n).toFixed(6),
                                               +(p[1] + step.dy * n).toFixed(6)]);
        made.push({ layer: item.layer, shape: this.addShape(item.layer, points, "array") });
      }
    }
    this.selected = made;
    this.toast(`${count} copy(ies) at ${fmtLen(step.dx)}, ${fmtLen(step.dy)}`);
    this.draw();
  };

  // Merge and subtract are the two edits that most often go wrong by hand: two
  // rectangles dragged until they look joined leave a hairline gap, and a
  // subtraction done by eye leaves a sliver. Neither is computed here - the
  // selection is sent to KLayout and comes back as whatever it really is.
  P.combineSelection = function (operation) {
    const items = this.selected.filter((item) => item.shape.id && !item.shape._del);
    if (items.length < 2) {
      this.toast("Select two or more saved shapes on the same layer");
      return;
    }
    const layers = new Set(items.map((item) => item.layer));
    if (layers.size > 1) {
      this.toast("Those shapes are on different layers — a boolean between them " +
                 "would have to invent which layer the result belongs to");
      return;
    }
    if (items.some((item) => item.shape._new || item.shape._dirty)) {
      this.toast("Apply the pending changes first: a boolean runs against the file");
      return;
    }
    if (!this.opts.onEvent) { this.toast("This viewer is read-only"); return; }
    this.editState.committing = true;
    this.renderPanel();
    this.opts.onEvent({
      type: "commit",
      gridNm: this.editState.gridNm,
      edits: [{
        op: "combine", operation: operation,
        targets: items.map((item) => ({
          layer: item.layer, cell: item.shape.id.cell,
          local_dbu: item.shape.id.local_dbu, dup: item.shape.id.dup,
        })),
      }],
      nonce: Date.now(),
    });
    this.toast(`${operation === "merge" ? "Merging" : "Subtracting"} ${items.length} shapes…`);
  };

  // Placing a cell is the other half of editing: a layout is built by placing
  // cells, not by drawing every rectangle twice, and a placement stays correct when
  // the cell it points at changes later.
  P.armPlacement = function (name) {
    if (!this.edit) return;
    this.editState.placing = name;
    this.editState.placeRotate = 0;
    this.editState.placeMirror = false;
    this.setMode("place");
    this.toast(`Click where ${name} should go — [ and ] rotate it first`);
  };

  // --- selection -----------------------------------------------------------

  P.selectAt = function (wxp, wyp, add) {
    const hit = this.pick(wxp, wyp);
    if (!hit) {
      if (!add) { this.selected = []; this.selection = null; }
      return null;
    }
    const entry = { layer: hit.layer, shape: hit.shape };
    const at = this.selected.findIndex((s) => s.shape === hit.shape);
    if (add) {
      if (at >= 0) this.selected.splice(at, 1);
      else this.selected.push(entry);
    } else {
      this.selected = [entry];
    }
    this.selection = hit;
    this.buildHandles();
    return entry;
  };

  P.selectInBox = function (x0, y0, x1, y1, add) {
    const lo = [Math.min(x0, x1), Math.min(y0, y1)];
    const hi = [Math.max(x0, x1), Math.max(y0, y1)];
    const found = [];
    for (const layer of this.activeLayers("a")) {
      for (const shape of layer.shapes) {
        if (shape._del) continue;
        const [bx0, by0, bx1, by1] = polyBBox(shape.o);
        // Wholly inside, the way a rubber band works everywhere else: a band that
        // grabs everything it grazes selects the power rail every time.
        if (bx0 >= lo[0] && by0 >= lo[1] && bx1 <= hi[0] && by1 <= hi[1]) {
          found.push({ layer: layer.name, shape: shape });
        }
      }
    }
    this.selected = add ? this.selected.concat(found) : found;
    this.selection = found.length
      ? { layer: found[0].layer, shape: found[0].shape,
          ld: "", colour: (this.A.layers.find((l) => l.name === found[0].layer) || {}).colour }
      : null;
    this.buildHandles();
    return found.length;
  };

  P.buildHandles = function () {
    const state = this.editState;
    state.handles = [];
    if (this.selected.length !== 1) return;
    const shape = this.selected[0].shape;
    // Vertices are the general case and work for any polygon; a rectangle also gets
    // edge midpoints, because dragging an edge is how a wire is widened.
    shape.o.forEach((point, index) => {
      state.handles.push({ kind: "vertex", index: index, x: point[0], y: point[1] });
    });
    if (shape.o.length === 4) {
      shape.o.forEach((point, index) => {
        const next = shape.o[(index + 1) % 4];
        state.handles.push({
          kind: "edge", index: index,
          x: (point[0] + next[0]) / 2, y: (point[1] + next[1]) / 2,
        });
      });
    }
  };

  P.handleAt = function (px, py) {
    const state = this.editState;
    for (const handle of state.handles) {
      if (Math.abs(this.sx(handle.x) - px) <= HANDLE_PX + 2 &&
          Math.abs(this.sy(handle.y) - py) <= HANDLE_PX + 2) return handle;
    }
    return null;
  };

  // --- pointer -------------------------------------------------------------

  P.editDown = function (event, px, py, snapped) {
    if (!this.editing()) return false;
    const state = this.editState;
    const point = this.editSnap(this.wx(px), this.wy(py));

    if (this.mode === "edit") {
      const handle = this.handleAt(px, py);
      if (handle && this.selected.length === 1) {
        state.drag = {
          kind: "handle", handle: handle,
          before: this.selected[0].shape.o.map((p) => [p[0], p[1]]),
        };
        return true;
      }
      const hit = this.pick(this.wx(px), this.wy(py));
      const already = hit && this.selected.some((s) => s.shape === hit.shape);
      if (hit && (already || !event.shiftKey)) {
        if (!already) this.selectAt(this.wx(px), this.wy(py), event.shiftKey);
        state.drag = { kind: "move", x: point.x, y: point.y, moved: false };
        return true;
      }
      if (hit) { this.selectAt(this.wx(px), this.wy(py), true); return true; }
      state.drag = { kind: "band", x0: this.wx(px), y0: this.wy(py),
                     x1: this.wx(px), y1: this.wy(py), add: event.shiftKey };
      return true;
    }

    if (this.mode === "rect") {
      state.drag = { kind: "rect", x0: point.x, y0: point.y, x1: point.x, y1: point.y };
      return true;
    }

    if (this.mode === "poly") {
      if (!state.pending) state.pending = { kind: "poly", points: [[point.x, point.y]] };
      else {
        const points = state.pending.points;
        const first = points[0];
        const close = Math.hypot(this.sx(first[0]) - px, this.sy(first[1]) - py) < 9;
        if (close && points.length >= 3) this.finishPolygon();
        else points.push([point.x, point.y]);
      }
      return true;
    }

    if (this.mode === "wire") {
      if (!state.pending) state.pending = { kind: "wire", points: [[point.x, point.y]] };
      else state.pending.points.push([point.x, point.y]);
      return true;
    }

    if (this.mode === "place") {
      const cell = state.placing;
      if (!cell) { this.toast("Pick a cell in the Cells tab first"); return true; }
      // A placement is not previewed locally: the browser has no geometry for the
      // cell it is placing, and drawing a guess would be a lie about what will be
      // written. It goes straight to KLayout and comes back as what it really is.
      if (!this.opts.onEvent) { this.toast("This viewer is read-only"); return true; }
      state.committing = true;
      this.renderPanel();
      this.opts.onEvent({
        type: "commit", gridNm: state.gridNm, nonce: Date.now(),
        edits: [{ op: "insert_instance", cell: cell, into: this.edit.topCell,
                  at_um: [point.x, point.y], rotate: state.placeRotate || 0,
                  mirror: !!state.placeMirror }],
      });
      this.toast(`Placing ${cell}…`);
      return true;
    }

    if (this.mode === "text") {
      const text = window.prompt("Label text");
      if (text && text.trim()) this.addLabel(this.editState.layer, text.trim(), point);
      return true;
    }
    return false;
  };

  P.editMove = function (event, px, py) {
    if (!this.editing()) return false;
    const state = this.editState;
    const point = this.editSnap(this.wx(px), this.wy(py));
    state.cursor = point;

    if (state.pending) {
      state.pending.hover = [point.x, point.y];
      return true;
    }
    if (!state.drag) return false;

    if (state.drag.kind === "move") {
      const dx = point.x - state.drag.x, dy = point.y - state.drag.y;
      if (dx || dy) {
        this.moveSelection(dx, dy, false);
        state.drag.x = point.x; state.drag.y = point.y;
        state.drag.moved = true;
        state.drag.totalX = (state.drag.totalX || 0) + dx;
        state.drag.totalY = (state.drag.totalY || 0) + dy;
        this.buildHandles();
      }
      return true;
    }
    if (state.drag.kind === "handle") {
      const shape = this.selected[0].shape;
      const points = shape.o.map((p) => [p[0], p[1]]);
      const handle = state.drag.handle;
      if (handle.kind === "vertex") {
        points[handle.index] = [point.x, point.y];
      } else {
        // An edge drag moves both of its ends, along the edge's normal only, so a
        // rectangle stays a rectangle - the whole point of dragging an edge.
        const a = handle.index, b = (handle.index + 1) % points.length;
        const horizontal = Math.abs(points[a][1] - points[b][1]) < 1e-9;
        if (horizontal) { points[a][1] = point.y; points[b][1] = point.y; }
        else { points[a][0] = point.x; points[b][0] = point.x; }
      }
      const fresh = this.shapeFromPoints(points);
      Object.assign(shape, { o: fresh.o, w: fresh.w, h: fresh.h, a: fresh.a,
                             cx: fresh.cx, cy: fresh.cy, x: fresh.x, y: fresh.y,
                             v: fresh.v });
      this.buildHandles();
      return true;
    }
    if (state.drag.kind === "band") {
      state.drag.x1 = this.wx(px); state.drag.y1 = this.wy(py);
      return true;
    }
    if (state.drag.kind === "rect") {
      state.drag.x1 = point.x; state.drag.y1 = point.y;
      return true;
    }
    return false;
  };

  P.editUp = function () {
    if (!this.editing()) return false;
    const state = this.editState;
    const drag = state.drag;
    if (!drag) return false;
    state.drag = null;

    if (drag.kind === "move") {
      if (drag.moved) {
        const dx = drag.totalX || 0, dy = drag.totalY || 0;
        const items = this.selected.slice();
        state.lastDelta = { dx: dx, dy: dy };
        const shift = (ddx, ddy) => {
          for (const item of items) {
            const shape = item.shape;
            shape.o = shape.o.map((p) => [+(p[0] + ddx).toFixed(6), +(p[1] + ddy).toFixed(6)]);
            shape.x = +(shape.x + ddx).toFixed(6); shape.y = +(shape.y + ddy).toFixed(6);
            shape.cx = +(shape.cx + ddx).toFixed(6); shape.cy = +(shape.cy + ddy).toFixed(6);
            if (shape.id) shape._dirty = true;
          }
        };
        this.editRecord(() => shift(-dx, -dy), () => shift(dx, dy),
                        `move ${items.length} shape(s) by ${fmtLen(dx)}, ${fmtLen(dy)}`);
      }
      return true;
    }
    if (drag.kind === "handle") {
      const entry = this.selected[0];
      const after = entry.shape.o.map((p) => [p[0], p[1]]);
      entry.shape.o = drag.before;                 // rewind, then record properly
      this.reshape(entry, after);
      this.buildHandles();
      return true;
    }
    if (drag.kind === "band") {
      const count = this.selectInBox(drag.x0, drag.y0, drag.x1, drag.y1, drag.add);
      if (!count && !drag.add) { this.selected = []; this.selection = null; }
      return true;
    }
    if (drag.kind === "rect") {
      const { x0, y0, x1, y1 } = drag;
      if (Math.abs(x1 - x0) > 1e-9 && Math.abs(y1 - y0) > 1e-9) {
        const shape = this.addShape(this.editState.layer,
          [[x0, y0], [x0, y1], [x1, y1], [x1, y0]]);
        this.selected = [{ layer: this.editState.layer, shape: shape }];
        this.buildHandles();
        this.checkDrawn(shape);
      }
      return true;
    }
    return false;
  };

  P.finishPolygon = function () {
    const state = this.editState;
    const pending = state.pending;
    state.pending = null;
    if (!pending || pending.points.length < 3) return;
    const shape = this.addShape(this.editState.layer, pending.points);
    this.selected = [{ layer: this.editState.layer, shape: shape }];
    this.buildHandles();
    this.checkDrawn(shape);
    this.draw();
  };

  // A wire is a centre line plus a width: the way a router thinks, and the way
  // KLayout's path tool works. The width defaults to what this layer already
  // measures, which is nearly always what you want on a standard cell.
  P.finishWire = function () {
    const state = this.editState;
    const pending = state.pending;
    state.pending = null;
    if (!pending || pending.points.length < 2) return;
    const widthNm = this.wireWidthNm();
    const half = widthNm / 2000;
    const left = [], right = [];
    for (let i = 0; i < pending.points.length - 1; i++) {
      const [ax, ay] = pending.points[i];
      const [bx, by] = pending.points[i + 1];
      const len = Math.hypot(bx - ax, by - ay);
      if (len < 1e-12) continue;
      const nx = -(by - ay) / len * half, ny = (bx - ax) / len * half;
      left.push([ax + nx, ay + ny], [bx + nx, by + ny]);
      right.push([ax - nx, ay - ny], [bx - nx, by - ny]);
    }
    if (!left.length) return;
    const points = left.concat(right.reverse());
    const dbu = (this.edit.dbuNm || 1) / 1000;
    const snapped = points.map((p) => [Math.round(p[0] / dbu) * dbu,
                                       Math.round(p[1] / dbu) * dbu]);
    const shape = this.addShape(this.editState.layer, snapped,
                                `wire on ${this.editState.layer}`);
    this.selected = [{ layer: this.editState.layer, shape: shape }];
    this.buildHandles();
    this.checkDrawn(shape);
    this.draw();
  };

  P.wireWidthNm = function () {
    const state = this.editState;
    if (state.wireWidthNm) return state.wireWidthNm;
    const measured = this.measuredWidthNm(state.layer);
    return measured || 20;
  };

  // What this layout already measures for a layer. Not a rule: a rule would need
  // the rule deck, and this is the layout's own narrowest shape.
  P.measuredWidthNm = function (name) {
    const row = this.A.layers.find((l) => l.name === name);
    if (!row || !row.shapes.length) return null;
    let best = Infinity;
    for (const shape of row.shapes) {
      if (shape._del || shape._new) continue;
      best = Math.min(best, Math.min(shape.w, shape.h) * 1000);
    }
    return isFinite(best) ? +best.toFixed(3) : null;
  };

  // Immediate feedback on a new shape, against what the rest of the layer measures.
  // This is deliberately not called a rule check: the real one runs in Python over
  // the design rule manual and comes back on commit.
  P.checkDrawn = function (shape) {
    const measured = this.measuredWidthNm(this.editState.layer);
    if (!measured) return;
    const narrow = Math.min(shape.w, shape.h) * 1000;
    if (narrow + 1e-6 < measured) {
      this.toast(`${fmtLen(narrow / 1000)} is narrower than the ` +
                 `${fmtLen(measured / 1000)} this layer measures elsewhere — ` +
                 `the rule check runs on commit`);
    }
  };

  P.addLabel = function (layerName, text, point) {
    const row = this.editLayerRow(layerName);
    const label = { t: text, x: point.x, y: point.y, _new: true };
    row.labels.push(label);
    row.labelCount = row.labels.length;
    this.editRecord(
      () => { const i = row.labels.indexOf(label); if (i >= 0) row.labels.splice(i, 1); },
      () => { row.labels.push(label); },
      `label '${text}' on ${layerName}`);
    this.draw();
  };

  // --- keyboard ------------------------------------------------------------

  P.editKey = function (event, key) {
    if (!this.edit) return false;
    const state = this.editState;
    const meta = event.ctrlKey || event.metaKey;

    // Tool keys, live whenever the layout is editable so switching into the editor
    // is one keystroke rather than a trip to the toolbar.
    if (!meta && !event.shiftKey) {
      const tools = { e: "edit", d: "rect", w: "wire", q: "poly" };
      if (tools[key]) { event.preventDefault(); this.setMode(tools[key]); return true; }
    }

    if (meta && key === "z") {
      event.preventDefault();
      if (event.shiftKey) this.editRedo(); else this.editUndo();
      return true;
    }
    if (meta && key === "y") { event.preventDefault(); this.editRedo(); return true; }
    if (meta && key === "c") { event.preventDefault(); this.copySelection(); return true; }
    if (meta && key === "v") { event.preventDefault(); this.pasteClipboard(); this.draw(); return true; }
    if (meta && key === "d") { event.preventDefault(); this.duplicateSelection(); this.draw(); return true; }
    if (meta && key === "a" && this.editing()) {
      event.preventDefault();
      this.selected = [];
      for (const layer of this.activeLayers("a")) {
        for (const shape of layer.shapes) {
          if (!shape._del) this.selected.push({ layer: layer.name, shape: shape });
        }
      }
      this.buildHandles();
      this.renderPanel();
      this.draw();
      return true;
    }
    if (meta && key === "s") { event.preventDefault(); this.commitEdits(); return true; }

    if (!this.editing()) return false;

    if (key === "delete" || key === "backspace") {
      event.preventDefault(); this.deleteSelection(); return true;
    }
    if (key === "escape") {
      event.preventDefault();
      if (state.pending) { state.pending = null; }
      else { this.selected = []; this.selection = null; state.handles = []; }
      this.draw();
      return true;
    }
    if (key === "enter") {
      event.preventDefault();
      if (state.pending && state.pending.kind === "poly") this.finishPolygon();
      else if (state.pending && state.pending.kind === "wire") this.finishWire();
      return true;
    }
    if (key === "[" || key === "]") {
      event.preventDefault();
      if (this.mode === "place") {
        // Rotating what is about to be placed, before it exists.
        state.placeRotate = (((state.placeRotate || 0) + (key === "[" ? 90 : 270)) % 360);
        this.toast(`${state.placing} will be placed at R${state.placeRotate}`);
        return true;
      }
      this.transformSelection(key === "[" ? 90 : -90, false);
      return true;
    }
    if (key === "m" && this.selected.length) {
      event.preventDefault(); this.transformSelection(0, true); return true;
    }
    const pan = { arrowleft: [-1, 0], arrowright: [1, 0], arrowup: [0, 1], arrowdown: [0, -1] };
    if (pan[key] && this.selected.length) {
      event.preventDefault();
      const step = (state.gridNm || this.edit.dbuNm || 1) / 1000 * (event.shiftKey ? 10 : 1);
      this.moveSelection(pan[key][0] * step, pan[key][1] * step, true);
      this.buildHandles();
      this.draw();
      return true;
    }
    return false;
  };

  // --- the journal ---------------------------------------------------------

  // The commit is a *state difference*, not a replay of what the user did. Ten moves
  // of one shape are one replacement; a shape drawn and then deleted is nothing at
  // all. It also means an operation can never refer to something that only existed
  // in the middle of the session.
  P.buildJournal = function () {
    const ops = [];
    for (const layer of this.A.layers) {
      for (const shape of layer.shapes) {
        const target = shape.id
          ? { layer: layer.name, cell: shape.id.cell, local_dbu: shape.id.local_dbu,
              dup: shape.id.dup, trans: shape.id.trans }
          : null;
        if (shape._del) {
          if (target) ops.push({ op: "delete", target: target });
        } else if (shape._dirty && target) {
          ops.push({ op: "replace", target: target, points: shape.o });
        } else if (shape._new) {
          ops.push({ op: "insert", layer: layer.name, points: shape.o });
        }
      }
      for (const label of layer.labels || []) {
        if (label._new) {
          ops.push({ op: "insert_text", layer: layer.name, text: label.t,
                     at_um: [label.x, label.y] });
        } else if (label._del) {
          ops.push({ op: "delete_text", layer: layer.name, text: label.t,
                     at_um: [label.x, label.y] });
        }
      }
    }
    return ops;
  };

  P.editSummary = function () {
    const journal = this.buildJournal();
    const counts = { insert: 0, delete: 0, replace: 0, insert_text: 0, delete_text: 0 };
    for (const op of journal) counts[op.op] = (counts[op.op] || 0) + 1;
    return { journal: journal, counts: counts, total: journal.length };
  };

  P.commitEdits = function () {
    const summary = this.editSummary();
    if (!summary.total) { this.toast("No changes to apply"); return; }
    if (!this.opts.onEvent) {
      this.toast("This viewer is read-only — open the editor from the app to save");
      return;
    }
    this.editState.committing = true;
    this.renderPanel();
    this.opts.onEvent({ type: "commit", edits: summary.journal,
                        gridNm: this.editState.gridNm, nonce: Date.now() });
    this.toast(`Applying ${summary.total} change(s)…`);
  };

  P.discardEdits = function () {
    if (!this.opts.onEvent) return;
    this.opts.onEvent({ type: "discard", nonce: Date.now() });
  };

  // --- drawing -------------------------------------------------------------

  P.drawEdit = function (ctx) {
    if (!this.edit) return;
    const state = this.editState;

    // Everything not yet written to the file, marked as such: an edit that looks
    // identical to saved geometry is how someone closes the tab and loses work.
    ctx.save();
    for (const layer of this.A.layers) {
      for (const shape of layer.shapes) {
        if (!shape._new && !shape._dirty && !shape._del) continue;
        ctx.beginPath();
        ctx.moveTo(this.sx(shape.o[0][0]), this.sy(shape.o[0][1]));
        for (let i = 1; i < shape.o.length; i++) {
          ctx.lineTo(this.sx(shape.o[i][0]), this.sy(shape.o[i][1]));
        }
        ctx.closePath();
        ctx.setLineDash(shape._del ? [3, 3] : []);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = shape._del ? "#f85149" : "#7ee787";
        ctx.stroke();
        if (shape._del) {
          const [x0, y0, x1, y1] = polyBBox(shape.o);
          ctx.beginPath();
          ctx.moveTo(this.sx(x0), this.sy(y0)); ctx.lineTo(this.sx(x1), this.sy(y1));
          ctx.moveTo(this.sx(x0), this.sy(y1)); ctx.lineTo(this.sx(x1), this.sy(y0));
          ctx.stroke();
        }
      }
    }
    ctx.restore();

    // Selection.
    ctx.save();
    ctx.setLineDash([]);
    ctx.strokeStyle = "#58a6ff";
    ctx.lineWidth = 2;
    for (const item of this.selected) {
      const [x0, y0, x1, y1] = polyBBox(item.shape.o);
      ctx.strokeRect(this.sx(x0) - 1.5, this.sy(y1) - 1.5,
                     this.sx(x1) - this.sx(x0) + 3, this.sy(y0) - this.sy(y1) + 3);
    }
    // Handles, only with one shape selected: eight boxes per shape on a multiple
    // selection is a field of blue squares with nothing to grab.
    if (this.selected.length === 1) {
      for (const handle of state.handles) {
        const px = this.sx(handle.x), py = this.sy(handle.y);
        ctx.fillStyle = handle.kind === "vertex" ? "#58a6ff" : "#dce9ff";
        ctx.fillRect(px - HANDLE_PX, py - HANDLE_PX, HANDLE_PX * 2, HANDLE_PX * 2);
        ctx.strokeStyle = "#0b0f14";
        ctx.lineWidth = 1;
        ctx.strokeRect(px - HANDLE_PX, py - HANDLE_PX, HANDLE_PX * 2, HANDLE_PX * 2);
      }
    }
    ctx.restore();

    if (state.drag && state.drag.kind === "band") {
      ctx.save();
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = "#58a6ff";
      ctx.fillStyle = "rgba(88,166,255,0.12)";
      const x = Math.min(this.sx(state.drag.x0), this.sx(state.drag.x1));
      const y = Math.min(this.sy(state.drag.y0), this.sy(state.drag.y1));
      const w = Math.abs(this.sx(state.drag.x1) - this.sx(state.drag.x0));
      const h = Math.abs(this.sy(state.drag.y1) - this.sy(state.drag.y0));
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }

    if (state.drag && state.drag.kind === "rect") {
      this.drawGhostRect(ctx, state.drag);
    }
    if (state.pending) this.drawPending(ctx, state.pending);
    ctx.setLineDash([]);
  };

  P.drawGhostRect = function (ctx, drag) {
    const entry = this.editLayerEntry() || {};
    ctx.save();
    ctx.fillStyle = entry.colour || "#7ee787";
    ctx.globalAlpha = 0.35;
    const x = Math.min(this.sx(drag.x0), this.sx(drag.x1));
    const y = Math.min(this.sy(drag.y0), this.sy(drag.y1));
    const w = Math.abs(this.sx(drag.x1) - this.sx(drag.x0));
    const h = Math.abs(this.sy(drag.y1) - this.sy(drag.y0));
    ctx.fillRect(x, y, w, h);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = "#7ee787";
    ctx.lineWidth = 1.4;
    ctx.strokeRect(x, y, w, h);
    // The size, while it is being drawn: the alternative is drawing it, measuring
    // it, undoing it and drawing it again.
    ctx.fillStyle = "#dce9ff";
    ctx.font = "600 11px ui-monospace, monospace";
    ctx.fillText(`${fmtLen(Math.abs(drag.x1 - drag.x0))} × ${fmtLen(Math.abs(drag.y1 - drag.y0))}`,
                 x + 4, y - 6);
    ctx.restore();
  };

  P.drawPending = function (ctx, pending) {
    const points = pending.points.slice();
    if (pending.hover) points.push(pending.hover);
    if (!points.length) return;
    ctx.save();
    ctx.strokeStyle = "#7ee787";
    ctx.lineWidth = pending.kind === "wire" ? 1.8 : 1.4;
    ctx.setLineDash([5, 3]);
    ctx.beginPath();
    ctx.moveTo(this.sx(points[0][0]), this.sy(points[0][1]));
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(this.sx(points[i][0]), this.sy(points[i][1]));
    }
    if (pending.kind === "poly" && points.length > 2) ctx.closePath();
    ctx.stroke();
    ctx.setLineDash([]);
    for (const point of pending.points) {
      ctx.fillStyle = "#7ee787";
      ctx.fillRect(this.sx(point[0]) - 3, this.sy(point[1]) - 3, 6, 6);
    }
    if (pending.kind === "wire" && points.length > 1) {
      const last = points[points.length - 1], prev = points[points.length - 2];
      ctx.fillStyle = "#dce9ff";
      ctx.font = "600 11px ui-monospace, monospace";
      ctx.fillText(`${fmtLen(Math.hypot(last[0] - prev[0], last[1] - prev[1]))} · ` +
                   `width ${fmtLen(this.wireWidthNm() / 1000)}`,
                   this.sx(last[0]) + 8, this.sy(last[1]) - 8);
    }
    ctx.restore();
  };

  // --- toolbar and panel ---------------------------------------------------

  P.buildEditToolbar = function (bar) {
    const group = this.group(bar, "Edit");
    this.editButtons = {};
    const tools = [
      ["edit", "▣", "Select and move (E) — drag a handle to reshape"],
      ["rect", "▭", "Draw a rectangle (D)"],
      ["poly", "⬠", "Draw a polygon — click each corner, Enter or click the first point to close"],
      ["wire", "⌇", "Draw a wire — a centre line with this layer's width, Enter to finish"],
      ["text", "T", "Place a label"],
    ];
    for (const [mode, glyph, title] of tools) {
      this.editButtons[mode] = this.button(group, glyph, title, () => this.setMode(mode));
    }
    this.btnUndo = this.button(group, "↶", "Undo (Ctrl+Z)", () => this.editUndo());
    this.btnRedo = this.button(group, "↷", "Redo (Ctrl+Shift+Z)", () => this.editRedo());
    this.btnApply = this.button(group, "Apply", "Write these changes to the file (Ctrl+S)",
                                () => this.commitEdits(), { cls: "gv-primary" });
  };

  P.renderEditTab = function () {
    const state = this.editState;
    const body = this.panelBody;

    const bar = document.createElement("div");
    bar.className = "gv-quick";
    body.appendChild(bar);
    this.button(bar, "↶ Undo", "Undo (Ctrl+Z)", () => this.editUndo());
    this.button(bar, "↷ Redo", "Redo (Ctrl+Shift+Z)", () => this.editRedo());

    // Active layer: what a new shape is drawn on. Only the technology's layers are
    // offered, because a layer with no number cannot be written to a GDSII.
    const layerRow = document.createElement("div");
    layerRow.className = "gv-erow";
    layerRow.innerHTML = '<span class="gv-dim">Layer</span>';
    const select = document.createElement("select");
    select.className = "gv-select";
    const names = (this.edit.layers || []).map((l) => l.name);
    for (const name of names) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      if (name === state.layer) option.selected = true;
      select.appendChild(option);
    }
    select.addEventListener("change", () => {
      state.layer = select.value;
      this.visible.add(state.layer);
      this.renderPanel();
      this.draw();
    });
    layerRow.appendChild(select);
    body.appendChild(layerRow);

    const gridRow = document.createElement("div");
    gridRow.className = "gv-erow";
    gridRow.innerHTML = '<span class="gv-dim">Grid</span>';
    const grid = document.createElement("select");
    grid.className = "gv-select";
    for (const step of this.edit.gridStepsNm || []) {
      const option = document.createElement("option");
      option.value = String(step);
      option.textContent = `${step} nm`;
      if (Math.abs(step - state.gridNm) < 1e-9) option.selected = true;
      grid.appendChild(option);
    }
    grid.addEventListener("change", () => {
      state.gridNm = parseFloat(grid.value);
      this.draw();
    });
    gridRow.appendChild(grid);
    body.appendChild(gridRow);

    const track = document.createElement("label");
    track.className = "gv-check";
    track.style.padding = "2px 10px 6px";
    track.innerHTML = '<input type="checkbox"> snap to routing tracks';
    const box = track.querySelector("input");
    box.checked = state.trackSnap;
    box.addEventListener("change", () => { state.trackSnap = box.checked; });
    body.appendChild(track);

    const ops = document.createElement("div");
    ops.className = "gv-quick";
    body.appendChild(ops);
    const merge = this.button(ops, "Merge", "Join the selected shapes into one, exactly",
                              () => this.combineSelection("merge"));
    const cut = this.button(ops, "Subtract", "Cut the later shapes out of the first",
                            () => this.combineSelection("subtract"));
    const rotate = this.button(ops, "⟲", "Rotate 90° ([)", () => this.transformSelection(90, false));
    const flip = this.button(ops, "⇄", "Mirror (M)", () => this.transformSelection(0, true));
    for (const button of [merge, cut]) button.disabled = this.selected.length < 2;
    for (const button of [rotate, flip]) button.disabled = !this.selected.length;

    const measured = this.measuredWidthNm(state.layer);
    const hint = document.createElement("div");
    hint.className = "gv-hint";
    hint.style.padding = "0 10px 8px";
    hint.innerHTML = measured
      ? `${state.layer} measures <b>${fmtLen(measured / 1000)}</b> at its narrowest ` +
        "elsewhere in this layout. That is a measurement, not a rule — the rule " +
        "check runs on the file after you apply."
      : `Nothing is drawn on ${state.layer} yet, so there is no measured width to ` +
        "compare a new shape against.";
    body.appendChild(hint);

    const summary = this.editSummary();
    const list = document.createElement("div");
    list.className = "gv-layers";
    body.appendChild(list);

    if (!summary.total) {
      const empty = document.createElement("div");
      empty.className = "gv-hint";
      empty.style.padding = "6px 10px";
      empty.innerHTML =
        "No changes yet. <kbd>D</kbd> rectangle · <kbd>E</kbd> select · drag a handle " +
        "to reshape · <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicate · <kbd>[</kbd> <kbd>]</kbd> " +
        "rotate · <kbd>M</kbd> mirror · <kbd>Del</kbd> delete.";
      list.appendChild(empty);
    } else {
      const head = document.createElement("div");
      head.className = "gv-isec";
      head.style.padding = "6px 8px 2px";
      head.textContent = `${summary.total} pending change(s)`;
      list.appendChild(head);
      const wording = {
        insert: "draw", delete: "delete", replace: "reshape",
        insert_text: "label", delete_text: "remove label",
      };
      summary.journal.forEach((op, index) => {
        const row = document.createElement("div");
        row.className = "gv-mkr gv-cell";
        row.innerHTML =
          `<span class="gv-mid">${index + 1}</span>` +
          `<span class="gv-mrule">${wording[op.op] || op.op} ` +
          `${op.layer || (op.target && op.target.layer) || ""}</span>` +
          `<span class="gv-mst">${op.op === "insert" ? (op.points || []).length + "pt" : ""}</span>`;
        list.appendChild(row);
      });
    }

    const actions = document.createElement("div");
    actions.className = "gv-quick";
    actions.style.padding = "8px";
    body.appendChild(actions);
    const apply = this.button(actions, state.committing ? "Applying…" : "Apply to file",
                              "Write these changes with KLayout and re-run the checks",
                              () => this.commitEdits(), { cls: "gv-primary" });
    apply.disabled = !summary.total || state.committing;
    const discard = this.button(actions, "Discard", "Throw away every pending change",
                                () => this.discardEdits());
    discard.disabled = !summary.total;

    const note = document.createElement("div");
    note.className = "gv-hint";
    note.style.padding = "0 10px 10px";
    note.innerHTML =
      "Applying writes a <b>new file</b> with KLayout — the upload is never modified — " +
      "then re-reads it, so what you see afterwards is what was written.";
    body.appendChild(note);
  };

  // Keep the toolbar's edit buttons in step with the mode. sync() is called after
  // every state change, so this is the one place that has to know about them.
  const baseSync = P.sync;
  P.sync = function () {
    baseSync.call(this);
    if (!this.edit || !this.editButtons) return;
    for (const mode in this.editButtons) {
      this.editButtons[mode].classList.toggle("gv-on", this.mode === mode);
    }
    const summary = this.editSummary();
    if (this.btnApply) {
      this.btnApply.disabled = !summary.total || this.editState.committing;
      this.btnApply.textContent = summary.total ? `Apply ${summary.total}` : "Apply";
    }
    if (this.btnUndo) this.btnUndo.disabled = !this.editState.history.length;
    if (this.btnRedo) this.btnRedo.disabled = !this.editState.future.length;
  };

  // Extra shortcuts, added to the view's own map: E and D pick the two tools that
  // get used constantly.
  const baseSetMode = P.setMode;
  P.setMode = function (mode) {
    baseSetMode.call(this, mode);
    if (EDIT_MODES.indexOf(mode) >= 0 && this.tab !== "edit") {
      this.tab = "edit";
      this.renderPanel();
    }
  };
})();
