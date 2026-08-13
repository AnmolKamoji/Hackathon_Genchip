/* GDS layout viewer.
 *
 * Canvas rather than a charting library, for three reasons that came out of using
 * the Plotly version: the toolbar has to live where the pointer can reach it (the
 * old one hid the moment you moved toward it), a layer toggle must not cost a
 * server round trip, and a ruler needs to snap to real vertices rather than to
 * whatever pixel was clicked.
 *
 * Everything here is view state. Every measurement shown is either carried in the
 * payload (already measured in Python) or derived from payload coordinates - never
 * from screen pixels.
 */
(function () {
  "use strict";

  const NS = window.GDSViewer || (window.GDSViewer = {});

  // ---------- geometry helpers (pure; unit-tested through the browser) -------

  function polyBBox(points) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of points) {
      if (x < x0) x0 = x;
      if (y < y0) y0 = y;
      if (x > x1) x1 = x;
      if (y > y1) y1 = y;
    }
    return [x0, y0, x1, y1];
  }

  // Ray casting. Used for "what shape is under the cursor", so it must be exact on
  // the rectangles these layouts are made of: a point on the boundary counts as
  // inside, otherwise clicking a wire's own edge selects nothing.
  function pointInPoly(px, py, points) {
    let inside = false;
    for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
      const [xi, yi] = points[i], [xj, yj] = points[j];
      if (Math.abs((xj - xi) * (py - yi) - (px - xi) * (yj - yi)) < 1e-12 &&
          px >= Math.min(xi, xj) - 1e-12 && px <= Math.max(xi, xj) + 1e-12 &&
          py >= Math.min(yi, yj) - 1e-12 && py <= Math.max(yi, yj) + 1e-12) {
        return true;                                   // exactly on an edge
      }
      const straddles = (yi > py) !== (yj > py);
      if (straddles && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function distToSegment(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy;
    let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const cx = ax + t * dx, cy = ay + t * dy;
    return { d: Math.hypot(px - cx, py - cy), x: cx, y: cy };
  }

  function fmtLen(um) {
    const nm = um * 1000;
    if (Math.abs(nm) >= 1000) return (nm / 1000).toFixed(3).replace(/\.?0+$/, "") + " µm";
    if (Math.abs(nm) < 0.001) return "0 nm";
    const r = Math.abs(nm) < 10 ? nm.toFixed(3) : nm.toFixed(2);
    return r.replace(/\.?0+$/, "") + " nm";
  }

  function fmtArea(um2) {
    if (um2 === 0) return "0";
    if (Math.abs(um2) < 1e-4) return (um2 * 1e6).toPrecision(4) + " nm²";
    return um2.toPrecision(4) + " µm²";
  }

  function fmtCoord(um) {
    return (um * 1000).toFixed(1).replace(/\.0$/, "");
  }

  // A "nice" step for the grid: 1, 2 or 5 times a power of ten, chosen so the
  // spacing on screen stays in a readable band as the zoom changes.
  function niceStep(spanUm, targetDivisions) {
    const raw = spanUm / Math.max(1, targetDivisions);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm >= 5 ? 5 : norm >= 2 ? 2 : 1;
    return step * mag;
  }

  NS.geom = { polyBBox, pointInPoly, distToSegment, fmtLen, fmtArea, fmtCoord, niceStep };

  // Inline SVG rather than unicode glyphs. The first build used characters like
  // 'ruler' and 'label' emoji, which render as empty boxes wherever the font lacks
  // them - a toolbar that is blank on someone else's machine is not a toolbar.
  const I = (d, extra) => '<svg viewBox="0 0 20 20" width="15" height="15" fill="none" ' +
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
    'stroke-linejoin="round">' + d + (extra || "") + '</svg>';
  const ICON = {
    fit:    I('<path d="M3 7V3h4M17 7V3h-4M3 13v4h4M17 13v4h-4"/>'),
    zoomIn: I('<circle cx="9" cy="9" r="5"/><path d="M13 13l4 4M9 7v4M7 9h4"/>'),
    zoomOut:I('<circle cx="9" cy="9" r="5"/><path d="M13 13l4 4M7 9h4"/>'),
    back:   I('<path d="M12 4l-6 6 6 6"/>'),
    fwd:    I('<path d="M8 4l6 6-6 6"/>'),
    pan:    I('<path d="M10 3v14M3 10h14M10 3l-2 2M10 3l2 2M10 17l-2-2M10 17l2-2M3 10l2-2M3 10l2 2M17 10l-2-2M17 10l-2 2"/>'),
    ruler:  I('<path d="M3 12l9-9 5 5-9 9z"/><path d="M6 11l1.6 1.6M9 8l1.6 1.6M12 5l1.6 1.6"/>'),
    area:   I('<rect x="3" y="5" width="14" height="10" rx="1"/><path d="M3 10h14"/>'),
    probe:  I('<circle cx="10" cy="10" r="6"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/>'),
    clear:  I('<path d="M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10"/>'),
    snap:   I('<path d="M10 3v5M10 12v5M3 10h5M12 10h5"/><rect x="8" y="8" width="4" height="4"/>'),
    grid:   I('<path d="M3 3h14v14H3zM8 3v14M13 3v14M3 8h14M3 13h14"/>'),
    label:  I('<path d="M3 8l6-5h8v8l-5 6z"/><circle cx="13.5" cy="6.5" r="1.2" fill="currentColor"/>'),
    fill:   I('<rect x="3" y="4" width="14" height="12" rx="1"/><path d="M10 4v12" /><path d="M3 4h7v12H3z" fill="currentColor" stroke="none" opacity=".55"/>'),
    save:   I('<path d="M10 3v9M6.5 8.5L10 12l3.5-3.5M4 15h12"/>'),
    swipe:  I('<rect x="3" y="4" width="14" height="12" rx="1"/><path d="M10 4v12"/>'),
    blink:  I('<circle cx="10" cy="10" r="6"/><path d="M10 4a6 6 0 010 12z" fill="currentColor" stroke="none"/>'),
  };


  // Canvas fill patterns, cached per colour+style. KLayout gives every layer a
  // dither pattern for a reason: a solid power rail spanning the cell hides
  // everything drawn under it, and turning the opacity down far enough to see
  // through it makes the rail itself invisible. Hatching solves both.
  const _patterns = {};
  function hatch(ctx, colour, style) {
    const key = colour + "|" + style;
    if (_patterns[key]) return _patterns[key];
    const size = 8;
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    g.strokeStyle = colour;
    g.lineWidth = 1.15;
    g.beginPath();
    if (style === "diag") {
      g.moveTo(-2, size + 2); g.lineTo(size + 2, -2);
      g.moveTo(-2, 2); g.lineTo(2, -2);
      g.moveTo(size - 2, size + 2); g.lineTo(size + 2, size - 2);
    } else if (style === "cross") {
      g.moveTo(0, 4); g.lineTo(size, 4);
      g.moveTo(4, 0); g.lineTo(4, size);
    } else {                                   // "dots"
      g.moveTo(4, 3.4); g.lineTo(4, 4.6);
    }
    g.stroke();
    const pat = ctx.createPattern(c, "repeat");
    _patterns[key] = pat;
    return pat;
  }

  // ---------- the viewer ----------------------------------------------------

  class Viewer {
    constructor(root, payload, options) {
      this.root = root;
      this.opts = Object.assign({ compare: false, onEvent: null }, options || {});
      this.data = payload;
      this.compare = !!payload.regions;          // comparison payload shape
      this.A = this.compare ? payload.a : payload;
      this.B = this.compare ? payload.b : null;

      this.visible = new Set(this.A.defaultOn);
      this.solo = null;
      this.mode = "pan";                          // pan | ruler | area | probe
      this.rulers = [];
      this.pending = null;                        // ruler being drawn
      this.selection = null;
      this.hover = null;
      this.snapOn = true;
      this.gridOn = true;
      this.labelsOn = true;
      this.fillOn = true;
      this.opacity = 0.55;
      this.compareMode = "overlay";               // a | b | overlay | xor | swipe | blink
      this.swipe = 0.5;
      this.blinkTimer = null;
      this.blinkShowA = true;
      this.history = [];
      this.historyIndex = -1;

      this.build();
      this.fit(false);
      this.draw();
    }

    // ---- DOM ----

    build() {
      this.root.innerHTML = "";
      this.root.classList.add("gv-root");

      this.toolbar = document.createElement("div");
      this.toolbar.className = "gv-toolbar";
      this.root.appendChild(this.toolbar);

      const body = document.createElement("div");
      body.className = "gv-body";
      this.root.appendChild(body);

      const stage = document.createElement("div");
      stage.className = "gv-stage";
      body.appendChild(stage);

      this.canvas = document.createElement("canvas");
      this.canvas.className = "gv-canvas";
      stage.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");

      this.readout = document.createElement("div");
      this.readout.className = "gv-readout";
      this.readout.style.display = "none";     // an empty readout is a stray pill
      stage.appendChild(this.readout);

      this.scalebar = document.createElement("div");
      this.scalebar.className = "gv-scalebar";
      stage.appendChild(this.scalebar);

      this.tip = document.createElement("div");
      this.tip.className = "gv-tip";
      this.tip.style.display = "none";
      stage.appendChild(this.tip);

      this.swipeHandle = document.createElement("div");
      this.swipeHandle.className = "gv-swipe";
      this.swipeHandle.style.display = "none";
      stage.appendChild(this.swipeHandle);

      this.panel = document.createElement("div");
      this.panel.className = "gv-panel";
      body.appendChild(this.panel);

      this.buildToolbar();
      this.buildPanel();
      this.bind();
    }

    button(parent, label, title, handler, opts) {
      const b = document.createElement("button");
      b.className = "gv-btn" + ((opts && opts.cls) ? " " + opts.cls : "");
      b.innerHTML = label;
      b.title = title;
      b.type = "button";
      b.addEventListener("click", (e) => { e.preventDefault(); handler(b); });
      parent.appendChild(b);
      return b;
    }

    group(parent, label) {
      const g = document.createElement("div");
      g.className = "gv-group";
      if (label) {
        const l = document.createElement("span");
        l.className = "gv-glabel";
        l.textContent = label;
        g.appendChild(l);
      }
      parent.appendChild(g);
      return g;
    }

    buildToolbar() {
      const t = this.toolbar;
      t.innerHTML = "";

      const nav = this.group(t, "View");
      this.button(nav, ICON.fit, "Zoom to fit (F)", () => { this.fit(); });
      this.button(nav, ICON.zoomIn, "Zoom in (+)", () => this.zoomBy(1.4));
      this.button(nav, ICON.zoomOut, "Zoom out (−)", () => this.zoomBy(1 / 1.4));
      this.btnBack = this.button(nav, ICON.back, "Previous view (Backspace)", () => this.goHistory(-1));
      this.btnFwd = this.button(nav, ICON.fwd, "Next view", () => this.goHistory(1));

      const tools = this.group(t, "Tools");
      this.modeButtons = {};
      const modes = [
        ["pan", ICON.pan, "Pan / select (V)"],
        ["ruler", ICON.ruler, "Ruler — click twice to measure, snaps to edges (R)"],
        ["area", ICON.area, "Area box — drag to measure a region (A)"],
        ["probe", ICON.probe, "Probe — click a shape for its full properties (P)"],
      ];
      for (const [mode, icon, title] of modes) {
        this.modeButtons[mode] = this.button(tools, icon, title, () => this.setMode(mode));
      }
      this.button(tools, ICON.clear, "Clear rulers and selection (Esc)", () => this.clearAnnotations());

      const disp = this.group(t, "Display");
      this.btnSnap = this.button(disp, ICON.snap, "Snap to vertices and edges (S)", () => {
        this.snapOn = !this.snapOn; this.sync(); this.draw();
      });
      this.btnGrid = this.button(disp, ICON.grid, "Grid (G)", () => {
        this.gridOn = !this.gridOn; this.sync(); this.draw();
      });
      this.btnLabels = this.button(disp, ICON.label, "Text labels (L)", () => {
        this.labelsOn = !this.labelsOn; this.sync(); this.draw();
      });
      this.btnFill = this.button(disp, ICON.fill, "Filled / outline only (O)", () => {
        this.fillOn = !this.fillOn; this.sync(); this.draw();
      });

      const op = document.createElement("input");
      op.type = "range"; op.min = "10"; op.max = "100"; op.value = String(this.opacity * 100);
      op.className = "gv-slider"; op.title = "Layer opacity";
      op.addEventListener("input", () => { this.opacity = op.value / 100; this.draw(); });
      disp.appendChild(op);

      if (this.compare) {
        const cmp = this.group(t, "Compare");
        this.cmpButtons = {};
        const views = [
          ["a", "A", "Show only the first layout"],
          ["b", "B", "Show only the second layout"],
          ["overlay", "A+B", "Both layouts overlaid"],
          ["xor", "XOR", "Only the differing regions"],
          ["swipe", ICON.swipe, "Wipe between the two — drag the divider"],
          ["blink", ICON.blink, "Blink between the two"],
        ];
        for (const [mode, icon, title] of views) {
          this.cmpButtons[mode] = this.button(cmp, icon, title, () => this.setCompareMode(mode));
        }
      }

      const out = this.group(t, "");
      this.button(out, ICON.save, "Save this view as a PNG", () => this.exportPNG());
      if (this.opts.onEvent) {
        this.button(out, "⤢ Expand", "Open the full-screen workspace", () => {
          this.opts.onEvent({ type: "expand" });
        }, { cls: "gv-primary" });
      }
    }

    buildPanel() {
      const p = this.panel;
      p.innerHTML = "";

      const head = document.createElement("div");
      head.className = "gv-phead";
      head.innerHTML = `<span>Layers</span><span class="gv-count">${this.A.layers.length}</span>`;
      p.appendChild(head);

      const quick = document.createElement("div");
      quick.className = "gv-quick";
      p.appendChild(quick);
      this.button(quick, "All", "Show every layer", () => {
        this.visible = new Set(this.A.layers.map((l) => l.name));
        this.solo = null; this.syncPanel(); this.draw();
      });
      this.button(quick, "Drawing", "Only the layers with unique geometry", () => {
        this.visible = new Set(this.A.defaultOn);
        this.solo = null; this.syncPanel(); this.draw();
      });
      this.button(quick, "None", "Hide every layer", () => {
        this.visible = new Set(); this.solo = null; this.syncPanel(); this.draw();
      });

      const search = document.createElement("input");
      search.type = "search";
      search.placeholder = "Filter layers…";
      search.className = "gv-search";
      search.addEventListener("input", () => {
        this.filter = search.value.trim().toLowerCase();
        this.syncPanel();
      });
      p.appendChild(search);

      this.layerList = document.createElement("div");
      this.layerList.className = "gv-layers";
      p.appendChild(this.layerList);

      this.info = document.createElement("div");
      this.info.className = "gv-info";
      p.appendChild(this.info);

      this.renderLayerRows();
    }

    renderLayerRows() {
      this.layerList.innerHTML = "";
      this.rowEls = {};
      for (const layer of this.A.layers) {
        const row = document.createElement("div");
        row.className = "gv-lrow";
        row.dataset.name = layer.name;

        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = this.visible.has(layer.name);
        box.addEventListener("change", () => {
          if (box.checked) this.visible.add(layer.name);
          else this.visible.delete(layer.name);
          if (this.solo && this.solo !== layer.name) this.solo = null;
          this.syncPanel();
          this.draw();
        });
        row.appendChild(box);

        const sw = document.createElement("span");
        sw.className = "gv-sw";
        sw.style.background = layer.colour;
        row.appendChild(sw);

        const name = document.createElement("span");
        name.className = "gv-lname";
        name.textContent = layer.name;
        name.title = `${layer.layer}/${layer.datatype} · ${layer.role} · ` +
                     `${layer.count} shape(s)` +
                     (layer.extent ? ` · extent ${fmtLen(layer.extent.w)} × ${fmtLen(layer.extent.h)}` : "");
        row.appendChild(name);

        const ld = document.createElement("span");
        ld.className = "gv-ld";
        ld.textContent = `${layer.layer}/${layer.datatype}`;
        row.appendChild(ld);

        const n = document.createElement("span");
        n.className = "gv-n";
        n.textContent = layer.count || layer.labelCount;
        row.appendChild(n);

        // Solo: the fastest way to answer "which of these is that shape?".
        const solo = document.createElement("button");
        solo.className = "gv-solo";
        solo.textContent = "◉";
        solo.type = "button";
        solo.title = "Isolate this layer (click again to restore)";
        solo.addEventListener("click", (e) => {
          e.preventDefault(); e.stopPropagation();
          if (this.solo === layer.name) {
            this.solo = null;
            this.visible = new Set(this.savedVisible || this.A.defaultOn);
          } else {
            if (!this.solo) this.savedVisible = new Set(this.visible);
            this.solo = layer.name;
            this.visible = new Set([layer.name]);
          }
          this.syncPanel();
          this.draw();
        });
        row.appendChild(solo);

        this.layerList.appendChild(row);
        this.rowEls[layer.name] = { row, box, solo };
      }
      this.syncPanel();
    }

    syncPanel() {
      for (const layer of this.A.layers) {
        const el = this.rowEls[layer.name];
        if (!el) continue;
        el.box.checked = this.visible.has(layer.name);
        el.row.classList.toggle("gv-soloed", this.solo === layer.name);
        const hidden = this.filter && !(
          layer.name.toLowerCase().includes(this.filter) ||
          `${layer.layer}/${layer.datatype}`.includes(this.filter));
        el.row.style.display = hidden ? "none" : "";
      }
      this.sync();
    }

    sync() {
      const set = (btn, on) => btn && btn.classList.toggle("gv-on", !!on);
      set(this.btnSnap, this.snapOn);
      set(this.btnGrid, this.gridOn);
      set(this.btnLabels, this.labelsOn);
      set(this.btnFill, this.fillOn);
      for (const m in this.modeButtons) set(this.modeButtons[m], this.mode === m);
      if (this.cmpButtons) {
        for (const m in this.cmpButtons) set(this.cmpButtons[m], this.compareMode === m);
      }
      if (this.btnBack) this.btnBack.disabled = this.historyIndex <= 0;
      if (this.btnFwd) this.btnFwd.disabled = this.historyIndex >= this.history.length - 1;
      this.renderInfo();
    }

    renderInfo() {
      const parts = [];
      if (this.selection) {
        const s = this.selection;
        parts.push(`<div class="gv-isec"><b>${s.layer}</b> <span class="gv-dim">${s.ld}</span></div>
          <div class="gv-irow"><span>Size</span><b>${fmtLen(s.shape.w)} × ${fmtLen(s.shape.h)}</b></div>
          <div class="gv-irow"><span>Area</span><b>${fmtArea(s.shape.a)}</b></div>
          <div class="gv-irow"><span>Centre</span><b>${fmtCoord(s.shape.cx)}, ${fmtCoord(s.shape.cy)} nm</b></div>
          <div class="gv-irow"><span>Origin</span><b>${fmtCoord(s.shape.x)}, ${fmtCoord(s.shape.y)} nm</b></div>
          ${s.shape.v ? `<div class="gv-irow"><span>Vertices</span><b>${s.shape.v}</b></div>` : ""}`);
      }
      if (this.rulers.length) {
        parts.push('<div class="gv-isec">Measurements</div>');
        this.rulers.forEach((r, i) => {
          parts.push(`<div class="gv-irow gv-mrow" data-ruler="${i}"><span>${r.kind === "area"
            ? "Box " + fmtLen(Math.abs(r.x1 - r.x0)) + " × " + fmtLen(Math.abs(r.y1 - r.y0))
            : fmtLen(Math.hypot(r.x1 - r.x0, r.y1 - r.y0))}</span>` +
            `<b>${r.kind === "area" ? fmtArea(Math.abs((r.x1 - r.x0) * (r.y1 - r.y0)))
                                    : "Δ " + fmtLen(r.x1 - r.x0) + ", " + fmtLen(r.y1 - r.y0)}</b></div>`);
        });
      }
      if (!parts.length) {
        parts.push('<div class="gv-hint">Click a shape for its dimensions. ' +
                   'Press <kbd>R</kbd> for the ruler, <kbd>F</kbd> to fit, <kbd>?</kbd> for all keys.</div>');
      }
      this.info.innerHTML = parts.join("");
    }

    // ---- view transform ----

    get dpr() { return window.devicePixelRatio || 1; }

    resize() {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      const w = Math.max(50, rect.width), h = Math.max(50, rect.height);
      this.canvas.width = Math.round(w * this.dpr);
      this.canvas.height = Math.round(h * this.dpr);
      this.canvas.style.width = w + "px";
      this.canvas.style.height = h + "px";
      this.vw = w; this.vh = h;
    }

    worldBounds() {
      const boxes = [this.A.bbox];
      if (this.B) boxes.push(this.B.bbox);
      let [x0, y0, x1, y1] = boxes[0];
      for (const b of boxes) {
        x0 = Math.min(x0, b[0]); y0 = Math.min(y0, b[1]);
        x1 = Math.max(x1, b[2]); y1 = Math.max(y1, b[3]);
      }
      // A layout can have geometry outside the cell boundary (power rails
      // deliberately overhang), so the fit must consider the drawn shapes too or
      // the rails sit off-screen at start.
      for (const src of [this.A, this.B]) {
        if (!src) continue;
        for (const layer of src.layers) {
          for (const s of layer.shapes) {
            const [a, b, c, d] = polyBBox(s.o);
            x0 = Math.min(x0, a); y0 = Math.min(y0, b);
            x1 = Math.max(x1, c); y1 = Math.max(y1, d);
          }
        }
      }
      if (!(x1 > x0)) { x0 -= 0.5; x1 += 0.5; }
      if (!(y1 > y0)) { y0 -= 0.5; y1 += 0.5; }
      return [x0, y0, x1, y1];
    }

    fit(record) {
      this.resize();
      const [x0, y0, x1, y1] = this.worldBounds();
      const pad = 0.08;
      const w = (x1 - x0) * (1 + pad * 2), h = (y1 - y0) * (1 + pad * 2);
      this.scale = Math.min(this.vw / w, this.vh / h);
      this.cx = (x0 + x1) / 2;
      this.cy = (y0 + y1) / 2;
      if (record !== false) this.pushHistory();
      this.draw();
    }

    sx(wx) { return (wx - this.cx) * this.scale + this.vw / 2; }
    sy(wy) { return this.vh / 2 - (wy - this.cy) * this.scale; }
    wx(sx) { return (sx - this.vw / 2) / this.scale + this.cx; }
    wy(sy) { return this.cy - (sy - this.vh / 2) / this.scale; }

    zoomBy(factor, anchorX, anchorY) {
      const ax = anchorX === undefined ? this.vw / 2 : anchorX;
      const ay = anchorY === undefined ? this.vh / 2 : anchorY;
      const wxa = this.wx(ax), wya = this.wy(ay);
      this.scale = Math.max(1e-3, Math.min(1e9, this.scale * factor));
      // Keep the world point under the cursor pinned to the cursor.
      this.cx = wxa - (ax - this.vw / 2) / this.scale;
      this.cy = wya + (ay - this.vh / 2) / this.scale;
      this.pushHistory();
      this.draw();
    }

    zoomToBox(x0, y0, x1, y1) {
      const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
      if (w < 1e-9 || h < 1e-9) return;
      this.scale = Math.min(this.vw / w, this.vh / h) * 0.92;
      this.cx = (x0 + x1) / 2;
      this.cy = (y0 + y1) / 2;
      this.pushHistory();
      this.draw();
    }

    pushHistory() {
      const state = { scale: this.scale, cx: this.cx, cy: this.cy };
      const last = this.history[this.historyIndex];
      if (last && Math.abs(last.scale - state.scale) < 1e-9 &&
          Math.abs(last.cx - state.cx) < 1e-12 && Math.abs(last.cy - state.cy) < 1e-12) return;
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push(state);
      if (this.history.length > 60) this.history.shift();
      this.historyIndex = this.history.length - 1;
      this.sync();
    }

    goHistory(step) {
      const i = this.historyIndex + step;
      if (i < 0 || i >= this.history.length) return;
      this.historyIndex = i;
      const s = this.history[i];
      this.scale = s.scale; this.cx = s.cx; this.cy = s.cy;
      this.sync();
      this.draw();
    }

    // ---- picking and snapping ----

    activeLayers(which) {
      const src = which === "b" ? this.B : this.A;
      if (!src) return [];
      const names = this.visible;
      return src.layers.filter((l) => names.has(l.name));
    }

    // Draw order: largest extent first, so a via drawn under a power rail is not
    // buried by it. The picker walks this same list backwards, which is what makes
    // "what you click" equal "what you see" - they disagreed while the picker used
    // payload order, and clicking a gate selected the rail underneath it.
    orderedLayers(which) {
      const area = (l) => (l.extent ? l.extent.w * l.extent.h : 0);
      return this.activeLayers(which).slice().sort((a, b) => area(b) - area(a));
    }

    pick(wxp, wyp) {
      // The smallest shape containing the point wins. Ordering by layer was not
      // enough: a gate sits inside a diffusion that sits inside a power rail, and
      // whichever layer happened to sort last took the click. Aiming at a small
      // shape on top of a large one and selecting the large one is the single most
      // irritating thing a viewer can do, and shape area settles it unambiguously.
      let best = null;
      for (const layer of this.activeLayers("a")) {
        for (const s of layer.shapes) {
          if (!pointInPoly(wxp, wyp, s.o)) continue;
          const area = Math.abs(s.w * s.h) || s.a || 0;
          if (!best || area < best.area) {
            best = { area, layer: layer.name, ld: `${layer.layer}/${layer.datatype}`,
                     colour: layer.colour, shape: s };
          }
        }
      }
      return best;
    }

    snap(wxp, wyp) {
      if (!this.snapOn) return { x: wxp, y: wyp, kind: null };
      const tolPx = 12;
      const tol = tolPx / this.scale;
      let best = null;
      for (const layer of this.activeLayers("a")) {
        for (const s of layer.shapes) {
          const [bx0, by0, bx1, by1] = polyBBox(s.o);
          if (wxp < bx0 - tol || wxp > bx1 + tol || wyp < by0 - tol || wyp > by1 + tol) continue;
          for (let i = 0; i < s.o.length; i++) {
            const [ax, ay] = s.o[i];
            const dv = Math.hypot(wxp - ax, wyp - ay);
            if (dv <= tol && (!best || dv < best.d - 1e-15 || best.kind === "edge")) {
              best = { x: ax, y: ay, d: dv, kind: "vertex" };
            }
          }
          if (!best || best.kind !== "vertex") {
            for (let i = 0, j = s.o.length - 1; i < s.o.length; j = i++) {
              const r = distToSegment(wxp, wyp, s.o[j][0], s.o[j][1], s.o[i][0], s.o[i][1]);
              if (r.d <= tol && (!best || r.d < best.d)) {
                best = { x: r.x, y: r.y, d: r.d, kind: "edge" };
              }
            }
          }
        }
      }
      return best || { x: wxp, y: wyp, kind: null };
    }

    // ---- interaction ----

    bind() {
      const c = this.canvas;
      this.drag = null;

      c.addEventListener("contextmenu", (e) => e.preventDefault());

      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const r = c.getBoundingClientRect();
        this.zoomBy(e.deltaY < 0 ? 1.18 : 1 / 1.18, e.clientX - r.left, e.clientY - r.top);
      }, { passive: false });

      c.addEventListener("pointerdown", (e) => {
        c.setPointerCapture(e.pointerId);
        const r = c.getBoundingClientRect();
        const px = e.clientX - r.left, py = e.clientY - r.top;
        const w = this.snap(this.wx(px), this.wy(py));

        if (this.compareMode === "swipe" && Math.abs(px - this.swipe * this.vw) < 14) {
          this.drag = { kind: "swipe" };
          return;
        }
        // Right button or middle always pans, whatever the tool - the escape hatch
        // that keeps a modal tool from trapping the view.
        if (e.button === 2 || e.button === 1 || (this.mode === "pan" && !e.shiftKey)) {
          this.drag = { kind: "pan", px, py, cx: this.cx, cy: this.cy, moved: false };
        } else if (this.mode === "pan" && e.shiftKey) {
          this.drag = { kind: "box", x0: px, y0: py, x1: px, y1: py };
        } else if (this.mode === "ruler") {
          if (!this.pending) this.pending = { kind: "line", x0: w.x, y0: w.y, x1: w.x, y1: w.y };
          else {
            this.pending.x1 = w.x; this.pending.y1 = w.y;
            this.rulers.push(this.pending); this.pending = null;
            this.sync();
          }
        } else if (this.mode === "area") {
          this.drag = { kind: "area", x0: w.x, y0: w.y, x1: w.x, y1: w.y };
        } else if (this.mode === "probe") {
          this.selection = this.pick(this.wx(px), this.wy(py));
          this.sync();
        }
        this.draw();
      });

      c.addEventListener("pointermove", (e) => {
        const r = c.getBoundingClientRect();
        const px = e.clientX - r.left, py = e.clientY - r.top;
        this.cursor = { px, py };
        const wxp = this.wx(px), wyp = this.wy(py);

        if (this.drag) {
          if (this.drag.kind === "pan") {
            this.cx = this.drag.cx - (px - this.drag.px) / this.scale;
            this.cy = this.drag.cy + (py - this.drag.py) / this.scale;
            this.drag.moved = true;
          } else if (this.drag.kind === "box") {
            this.drag.x1 = px; this.drag.y1 = py;
          } else if (this.drag.kind === "area") {
            const w = this.snap(wxp, wyp);
            this.drag.x1 = w.x; this.drag.y1 = w.y;
          } else if (this.drag.kind === "swipe") {
            this.swipe = Math.max(0.02, Math.min(0.98, px / this.vw));
          }
          this.draw();
          return;
        }

        this.snapped = this.snap(wxp, wyp);
        if (this.pending) { this.pending.x1 = this.snapped.x; this.pending.y1 = this.snapped.y; }
        const hit = this.mode === "ruler" ? null : this.pick(wxp, wyp);
        const changed = (hit && hit.shape) !== (this.hover && this.hover.shape);
        this.hover = hit;
        this.updateReadout(wxp, wyp);
        this.updateTip(hit, px, py);
        if (changed || this.pending || this.snapOn) this.draw();
      });

      const finish = (e) => {
        if (!this.drag) return;
        const d = this.drag;
        this.drag = null;
        if (d.kind === "box") {
          if (Math.abs(d.x1 - d.x0) > 6 && Math.abs(d.y1 - d.y0) > 6) {
            this.zoomToBox(this.wx(d.x0), this.wy(d.y0), this.wx(d.x1), this.wy(d.y1));
            return;
          }
        } else if (d.kind === "area") {
          if (Math.abs(d.x1 - d.x0) > 1e-9 && Math.abs(d.y1 - d.y0) > 1e-9) {
            this.rulers.push({ kind: "area", x0: d.x0, y0: d.y0, x1: d.x1, y1: d.y1 });
            this.sync();
          }
        } else if (d.kind === "pan") {
          if (!d.moved) {
            // A click that did not drag is a selection, so panning and picking
            // share the same button without a modifier.
            const r = this.canvas.getBoundingClientRect();
            const px = (e.clientX !== undefined ? e.clientX - r.left : d.px);
            const py = (e.clientY !== undefined ? e.clientY - r.top : d.py);
            this.selection = this.pick(this.wx(px), this.wy(py));
            this.sync();
          } else {
            this.pushHistory();
          }
        }
        this.draw();
      };
      c.addEventListener("pointerup", finish);
      c.addEventListener("pointercancel", () => { this.drag = null; this.draw(); });
      c.addEventListener("pointerleave", () => {
        this.hover = null; this.snapped = null; this.cursor = null;
        this.tip.style.display = "none";
        this.readout.style.display = "none";
        this.draw();
      });

      // Keyboard. Accepted when the pointer is over the viewer or the viewer holds
      // focus, and never when a text field inside it has focus - the layer filter
      // sits in this frame, and typing "ruler" there must not switch tools. The
      // focus half matters because the viewer shares the page with a chat box: a
      // hover-only rule loses the keys the moment the pointer drifts off.
      this.hot = false;
      this.root.tabIndex = 0;
      this.root.style.outline = "none";
      this.root.addEventListener("pointerenter", () => { this.hot = true; });
      this.root.addEventListener("pointerleave", () => { this.hot = false; });
      this.canvas.addEventListener("pointerdown", () => this.root.focus({ preventScroll: true }));
      this.keyHandler = (e) => {
        const focused = this.root.contains(document.activeElement);
        if (!this.hot && !focused) return;
        const tag = (e.target && e.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        const k = e.key.toLowerCase();
        const map = {
          f: () => this.fit(), r: () => this.setMode("ruler"), v: () => this.setMode("pan"),
          a: () => this.setMode("area"), p: () => this.setMode("probe"),
          s: () => { this.snapOn = !this.snapOn; this.sync(); this.draw(); },
          g: () => { this.gridOn = !this.gridOn; this.sync(); this.draw(); },
          l: () => { this.labelsOn = !this.labelsOn; this.sync(); this.draw(); },
          o: () => { this.fillOn = !this.fillOn; this.sync(); this.draw(); },
          "+": () => this.zoomBy(1.4), "=": () => this.zoomBy(1.4),
          "-": () => this.zoomBy(1 / 1.4), "_": () => this.zoomBy(1 / 1.4),
          escape: () => this.clearAnnotations(),
          backspace: () => this.goHistory(-1),
          "?": () => this.toggleHelp(),
        };
        if (map[k]) { e.preventDefault(); map[k](); return; }
        const pan = { arrowleft: [-1, 0], arrowright: [1, 0], arrowup: [0, 1], arrowdown: [0, -1] };
        if (pan[k]) {
          e.preventDefault();
          const [dx, dy] = pan[k];
          const step = 60 / this.scale;
          this.cx += dx * step; this.cy += dy * step;
          this.draw();
        }
      };
      window.addEventListener("keydown", this.keyHandler);

      this.ro = new ResizeObserver(() => { this.resize(); this.draw(); });
      this.ro.observe(this.canvas.parentElement);
    }

    setMode(mode) {
      this.mode = mode;
      this.pending = null;
      this.canvas.style.cursor = mode === "pan" ? "grab" : "crosshair";
      this.sync();
      this.draw();
    }

    setCompareMode(mode) {
      this.compareMode = mode;
      if (this.blinkTimer) { clearInterval(this.blinkTimer); this.blinkTimer = null; }
      if (mode === "blink") {
        this.blinkTimer = setInterval(() => {
          this.blinkShowA = !this.blinkShowA;
          this.draw();
        }, 650);
      }
      this.swipeHandle.style.display = mode === "swipe" ? "" : "none";
      this.sync();
      this.draw();
    }

    clearAnnotations() {
      this.rulers = [];
      this.pending = null;
      this.selection = null;
      this.sync();
      this.draw();
    }

    toggleHelp() {
      if (this.helpEl) { this.helpEl.remove(); this.helpEl = null; return; }
      const el = document.createElement("div");
      el.className = "gv-help";
      el.innerHTML = `<b>Keys</b>
        <div><kbd>F</kbd> fit view</div><div><kbd>+</kbd>/<kbd>-</kbd> zoom</div>
        <div><kbd>arrows</kbd> pan</div><div><kbd>Backspace</kbd> previous view</div>
        <div><kbd>V</kbd> pan/select</div><div><kbd>R</kbd> ruler</div>
        <div><kbd>A</kbd> area box</div><div><kbd>P</kbd> probe</div>
        <div><kbd>S</kbd> snapping</div><div><kbd>G</kbd> grid</div>
        <div><kbd>L</kbd> labels</div><div><kbd>O</kbd> outline only</div>
        <div><kbd>Esc</kbd> clear</div>
        <div class="gv-dim">Shift+drag zooms to a box · right-drag always pans · wheel zooms at the cursor</div>`;
      el.addEventListener("click", () => this.toggleHelp());
      this.canvas.parentElement.appendChild(el);
      this.helpEl = el;
    }

    updateReadout(wxp, wyp) {
      const s = this.snapped;
      const snapTxt = s && s.kind ? ` · snap ${s.kind}` : "";
      const perPx = 1 / this.scale;
      this.readout.style.display = "";
      this.readout.innerHTML =
        `<span>x <b>${fmtCoord(wxp)}</b> y <b>${fmtCoord(wyp)}</b> nm${snapTxt}</span>` +
        `<span class="gv-dim">${fmtLen(perPx)}/px</span>`;
    }

    updateTip(hit, px, py) {
      if (!hit) { this.tip.style.display = "none"; return; }
      const s = hit.shape;
      this.tip.innerHTML =
        `<b style="color:${hit.colour}">${hit.layer}</b> <span class="gv-dim">${hit.ld}</span><br>` +
        `<b>${fmtLen(s.w)} × ${fmtLen(s.h)}</b><br>` +
        `area ${fmtArea(s.a)}<br>` +
        `centre ${fmtCoord(s.cx)}, ${fmtCoord(s.cy)} nm`;
      this.tip.style.display = "";
      const w = this.tip.offsetWidth, h = this.tip.offsetHeight;
      this.tip.style.left = Math.min(this.vw - w - 8, px + 14) + "px";
      this.tip.style.top = Math.max(4, Math.min(this.vh - h - 8, py - h - 12)) + "px";
    }

    // ---- drawing ----

    draw() {
      if (!this.ctx) return;
      const ctx = this.ctx;
      const dpr = this.dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.vw, this.vh);
      ctx.fillStyle = "#0b0f14";
      ctx.fillRect(0, 0, this.vw, this.vh);

      if (this.gridOn) this.drawGrid(ctx);

      const cm = this.compareMode;
      if (!this.compare) {
        this.drawLayout(ctx, this.A, 1);
      } else if (cm === "a") {
        this.drawLayout(ctx, this.A, 1);
      } else if (cm === "b") {
        this.drawLayout(ctx, this.B, 1);
      } else if (cm === "overlay") {
        this.drawLayout(ctx, this.A, 0.75);
        this.drawLayout(ctx, this.B, 0.75);
      } else if (cm === "xor") {
        this.drawLayout(ctx, this.A, 0.18);
      } else if (cm === "blink") {
        this.drawLayout(ctx, this.blinkShowA ? this.A : this.B, 1);
      } else if (cm === "swipe") {
        const split = this.swipe * this.vw;
        ctx.save(); ctx.beginPath(); ctx.rect(0, 0, split, this.vh); ctx.clip();
        this.drawLayout(ctx, this.A, 1); ctx.restore();
        ctx.save(); ctx.beginPath(); ctx.rect(split, 0, this.vw - split, this.vh); ctx.clip();
        this.drawLayout(ctx, this.B, 1); ctx.restore();
        ctx.strokeStyle = "#58a6ff"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(split, 0); ctx.lineTo(split, this.vh); ctx.stroke();
        this.swipeHandle.style.left = split + "px";
      }

      if (this.compare && (cm === "xor" || cm === "overlay")) this.drawRegions(ctx);

      this.drawBoundary(ctx);
      if (this.labelsOn) this.drawLabels(ctx);
      this.drawAnnotations(ctx);
      this.drawScaleBar();
      if (this.compare) this.drawCompareLegend(ctx);
    }

    drawGrid(ctx) {
      const step = niceStep((this.vw / this.scale), 10);
      const x0 = Math.floor(this.wx(0) / step) * step;
      const y0 = Math.floor(this.wy(this.vh) / step) * step;
      ctx.lineWidth = 1;
      ctx.font = "10px ui-monospace, monospace";
      for (let x = x0; x <= this.wx(this.vw); x += step) {
        const px = Math.round(this.sx(x)) + 0.5;
        const major = Math.abs(x) < step / 2;
        ctx.strokeStyle = major ? "rgba(120,160,200,0.38)" : "rgba(120,160,200,0.11)";
        ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, this.vh); ctx.stroke();
      }
      for (let y = y0; y <= this.wy(0); y += step) {
        const py = Math.round(this.sy(y)) + 0.5;
        const major = Math.abs(y) < step / 2;
        ctx.strokeStyle = major ? "rgba(120,160,200,0.38)" : "rgba(120,160,200,0.11)";
        ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(this.vw, py); ctx.stroke();
      }
    }

    // Big background layers hatch; everything else is solid. The thresholds are
    // deliberately blunt - the aim is that no single layer can flood the canvas.
    fillStyleFor(layer, share) {
      if (share >= 0.85) return "diag";
      if (share >= 0.5) return "cross";
      return "solid";
    }

    drawLayout(ctx, src, alpha) {
      if (!src) return;
      const layers = this.orderedLayers(src === this.B ? "b" : "a");
      const cellArea = Math.max(1e-9, (this.A.width || 1) * (this.A.height || 1));
      for (const layer of layers) {
        ctx.strokeStyle = layer.colour;
        // A layer covering a large share of the cell is hatched rather than
        // flooded, so the geometry it sits under stays readable.
        const share = layer.extent ? (layer.extent.w * layer.extent.h) / cellArea : 0;
        const style = this.fillStyleFor(layer, share);
        ctx.fillStyle = style === "solid" ? layer.colour : hatch(ctx, layer.colour, style);
        ctx.lineWidth = 1;
        for (const s of layer.shapes) {
          const [bx0, by0, bx1, by1] = polyBBox(s.o);
          // Cull off-screen shapes: at deep zoom this is most of the cell.
          if (this.sx(bx1) < -4 || this.sx(bx0) > this.vw + 4 ||
              this.sy(by0) < -4 || this.sy(by1) > this.vh + 4) continue;
          ctx.beginPath();
          ctx.moveTo(this.sx(s.o[0][0]), this.sy(s.o[0][1]));
          for (let i = 1; i < s.o.length; i++) ctx.lineTo(this.sx(s.o[i][0]), this.sy(s.o[i][1]));
          ctx.closePath();
          if (this.fillOn) {
            ctx.globalAlpha = (style === "solid" ? this.opacity : Math.min(1, this.opacity * 1.5)) * alpha;
            ctx.fill();
          }
          ctx.globalAlpha = Math.min(1, 0.9 * alpha);
          ctx.stroke();
          ctx.globalAlpha = 1;
        }
      }
      // Selection and hover drawn last so they are never buried.
      if (this.hover && this.hover.shape) this.outline(ctx, this.hover.shape.o, "#ffffff", 1.5);
      if (this.selection && this.selection.shape) {
        this.outline(ctx, this.selection.shape.o, "#58a6ff", 2.5);
        this.drawShapeDims(ctx, this.selection.shape);
      }
    }

    outline(ctx, points, colour, width) {
      ctx.save();
      ctx.strokeStyle = colour;
      ctx.lineWidth = width;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(this.sx(points[0][0]), this.sy(points[0][1]));
      for (let i = 1; i < points.length; i++) ctx.lineTo(this.sx(points[i][0]), this.sy(points[i][1]));
      ctx.closePath();
      ctx.stroke();
      ctx.restore();
    }

    drawShapeDims(ctx, s) {
      // Width and height written on the selected shape, so the commonest question
      // is answered without reaching for the ruler at all.
      const x0 = this.sx(s.x), x1 = this.sx(s.x + s.w);
      const y0 = this.sy(s.y), y1 = this.sy(s.y + s.h);
      if (Math.abs(x1 - x0) < 26 && Math.abs(y0 - y1) < 14) return;
      ctx.save();
      ctx.fillStyle = "#58a6ff";
      ctx.font = "600 11px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillText(fmtLen(s.w), (x0 + x1) / 2, y1 - 6);
      ctx.textAlign = "left";
      ctx.fillText(fmtLen(s.h), x1 + 6, (y0 + y1) / 2);
      ctx.restore();
    }

    drawRegions(ctx) {
      for (const r of this.data.regions) {
        if (!this.visible.has(r.layer)) continue;
        ctx.beginPath();
        ctx.moveTo(this.sx(r.o[0][0]), this.sy(r.o[0][1]));
        for (let i = 1; i < r.o.length; i++) ctx.lineTo(this.sx(r.o[i][0]), this.sy(r.o[i][1]));
        ctx.closePath();
        ctx.fillStyle = r.side === "a" ? "rgba(214,39,40,0.55)" : "rgba(44,160,44,0.55)";
        ctx.strokeStyle = r.side === "a" ? "#d62728" : "#2ca02c";
        ctx.lineWidth = 1.2;
        ctx.fill();
        ctx.stroke();
      }
    }

    drawCompareLegend(ctx) {
      if (this.compareMode === "a" || this.compareMode === "b") return;
      const names = this.data.names;
      const items = [["#d62728", "only in " + names.a], ["#2ca02c", "only in " + names.b]];
      ctx.save();
      ctx.font = "11px ui-sans-serif, system-ui";
      let y = this.vh - 12;
      for (const [colour, text] of items.reverse()) {
        ctx.fillStyle = colour;
        ctx.fillRect(12, y - 9, 12, 9);
        ctx.fillStyle = "#c9d4e0";
        ctx.fillText(text, 30, y);
        y -= 16;
      }
      ctx.restore();
    }

    drawBoundary(ctx) {
      const [x0, y0, x1, y1] = this.A.bbox;
      ctx.save();
      ctx.strokeStyle = "rgba(194,176,212,0.9)";
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1;
      ctx.strokeRect(this.sx(x0), this.sy(y1), this.sx(x1) - this.sx(x0), this.sy(y0) - this.sy(y1));
      ctx.restore();
    }

    drawLabels(ctx) {
      ctx.save();
      ctx.font = "10px ui-monospace, monospace";
      for (const layer of this.activeLayers("a")) {
        ctx.fillStyle = layer.colour;
        for (const lab of layer.labels) {
          const px = this.sx(lab.x), py = this.sy(lab.y);
          if (px < -40 || px > this.vw + 40 || py < -20 || py > this.vh + 20) continue;
          ctx.beginPath();
          ctx.moveTo(px - 3, py); ctx.lineTo(px + 3, py);
          ctx.moveTo(px, py - 3); ctx.lineTo(px, py + 3);
          ctx.strokeStyle = layer.colour;
          ctx.stroke();
          ctx.fillText(lab.t, px + 6, py + 3);
        }
      }
      ctx.restore();
    }

    drawAnnotations(ctx) {
      const all = this.rulers.concat(this.pending ? [this.pending] : []);
      ctx.save();
      for (const r of all) {
        if (r.kind === "area") {
          const x0 = this.sx(r.x0), y0 = this.sy(r.y0), x1 = this.sx(r.x1), y1 = this.sy(r.y1);
          ctx.strokeStyle = "#f0b429";
          ctx.fillStyle = "rgba(240,180,41,0.12)";
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 3]);
          ctx.beginPath();
          ctx.rect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
          ctx.fill(); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#f0b429";
          ctx.font = "600 11px ui-monospace, monospace";
          ctx.fillText(`${fmtLen(Math.abs(r.x1 - r.x0))} × ${fmtLen(Math.abs(r.y1 - r.y0))}`,
                       Math.min(x0, x1) + 6, Math.min(y0, y1) + 15);
          ctx.fillText(fmtArea(Math.abs((r.x1 - r.x0) * (r.y1 - r.y0))),
                       Math.min(x0, x1) + 6, Math.min(y0, y1) + 29);
        } else {
          const x0 = this.sx(r.x0), y0 = this.sy(r.y0), x1 = this.sx(r.x1), y1 = this.sy(r.y1);
          ctx.strokeStyle = "#f0b429";
          ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
          for (const [px, py] of [[x0, y0], [x1, y1]]) {
            ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2);
            ctx.fillStyle = "#f0b429"; ctx.fill();
          }
          const dx = r.x1 - r.x0, dy = r.y1 - r.y0;
          const len = Math.hypot(dx, dy);
          const angle = Math.atan2(dy, dx) * 180 / Math.PI;
          ctx.fillStyle = "#f0b429";
          ctx.font = "600 12px ui-monospace, monospace";
          const label = `${fmtLen(len)}  Δ${fmtLen(dx)}, ${fmtLen(dy)}  ${angle.toFixed(1)}°`;
          ctx.fillText(label, (x0 + x1) / 2 + 8, (y0 + y1) / 2 - 8);
        }
      }
      // Snap indicator: shows the ruler will land on real geometry, not a pixel.
      if (this.snapped && this.snapped.kind && this.mode !== "pan") {
        const px = this.sx(this.snapped.x), py = this.sy(this.snapped.y);
        ctx.strokeStyle = this.snapped.kind === "vertex" ? "#ff7ac6" : "#58a6ff";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        if (this.snapped.kind === "vertex") ctx.rect(px - 5, py - 5, 10, 10);
        else ctx.arc(px, py, 5, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (this.drag && this.drag.kind === "box") {
        const d = this.drag;
        ctx.strokeStyle = "#58a6ff";
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1),
                       Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
        ctx.setLineDash([]);
      }
      ctx.restore();
    }

    drawScaleBar() {
      const target = 120;
      const step = niceStep(target / this.scale, 1);
      const px = step * this.scale;
      this.scalebar.innerHTML =
        `<div class="gv-sbar" style="width:${px.toFixed(1)}px"></div><span>${fmtLen(step)}</span>`;
    }

    exportPNG() {
      const link = document.createElement("a");
      link.download = (this.A.topCell || "layout") + ".png";
      link.href = this.canvas.toDataURL("image/png");
      link.click();
    }

    destroy() {
      if (this.blinkTimer) clearInterval(this.blinkTimer);
      if (this.ro) this.ro.disconnect();
      window.removeEventListener("keydown", this.keyHandler);
    }
  }

  NS.Viewer = Viewer;
  NS.mount = function (rootId, payload, options) {
    const root = document.getElementById(rootId);
    if (!root) throw new Error("viewer root not found: " + rootId);
    const v = new Viewer(root, payload, options);
    NS.instances = NS.instances || {};
    NS.instances[rootId] = v;
    return v;
  };
})();
