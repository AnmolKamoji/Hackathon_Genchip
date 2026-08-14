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

  // Areas at this scale are nanometre-squared: a 20 × 25 nm region is 500 nm², and
  // writing it as 0.0005000 µm² makes a reader count zeros to compare two rows.
  // Cell-scale figures stay in µm² so they read the same as the report beside them.
  function fmtArea(um2) {
    if (um2 === 0) return "0";
    if (Math.abs(um2) < 0.01) {
      return String(Number((um2 * 1e6).toPrecision(4))) + " nm²";
    }
    return um2.toPrecision(4) + " µm²";
  }

  function fmtCoord(um) {
    return (um * 1000).toFixed(1).replace(/\.0$/, "");
  }

  // File names in a 232px panel: the extension is noise and the stem is what
  // distinguishes two uploads. The full name is always in the row's tooltip.
  function shortName(name) {
    const stem = String(name || "").replace(/\.[^.]+$/, "");
    return stem.length > 14 ? stem.slice(0, 13) + "…" : stem;
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
    net:    I('<circle cx="5" cy="5" r="2"/><circle cx="15" cy="5" r="2"/><circle cx="10" cy="15" r="2"/><path d="M5 7v3h10V7M10 10v3"/>'),
    tracks: I('<path d="M3 5h14M3 10h14M3 15h14"/><path d="M7 3v14" opacity=".5"/>'),
    bookmark: I('<path d="M6 3h8v14l-4-3-4 3z"/>'),
    share:  I('<path d="M12 3h5v5M17 3l-7 7M8 5H4v11h11v-4"/>'),
    step:   I('<path d="M7 4l6 6-6 6"/>'),
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
      this.markers = payload.markers || (this.A && this.A.markers) || [];
      this.nets = payload.nets || (this.A && this.A.nets) || [];
      this.tracks = payload.tracks || (this.A && this.A.tracks) || {};
      this.tree = payload.tree || (this.A && this.A.tree) || {};
      // Instance boundaries start hidden and the depth limit starts at the whole
      // tree: KLayout opens at full depth too, and a box drawn round every cell
      // before the user asked for it is just another layer of clutter.
      this.cellBoxesOn = false;
      this.depthLimit = this.tree.maxDepth || 0;
      this.activePlacement = null;
      this.activeCell = null;
      this.visited = new Set();
      this.waived = {};
      this.activeMarker = null;
      this.netHighlight = null;
      this.traceLock = false;
      this.probeA = null;
      this.tracksOn = false;
      this.bookmarks = [];
      this.presets = [];
      this.activeRegion = null;
      this.visitedRegions = new Set();
      this.findQuery = "";
      this.findHits = [];
      this.findIndex = -1;
      // Editing is off unless the page said the layout may be edited: a read-only
      // mount must not offer a tool that cannot save.
      this.edit = payload.editable || null;
      if (this.edit && this.initEditor) this.initEditor();
      // A comparison opens on the differences: that is the question it was opened
      // to answer, and the layer list is one click away.
      this.tab = this.compare ? "diffs" : "layers";

      this.build();
      this.fit(false);
      this.draw();
    }

    // Replace the geometry without disturbing the view.
    //
    // This is what a rerun looks like after an edit was written: the drawing has to
    // become what Python actually wrote to the file, while the zoom, the visible
    // layers and the open tab stay where the user left them. Rebuilding the viewer
    // instead would throw the user back to the whole-cell view after every edit,
    // which makes a sequence of small changes unusable.
    setPayload(payload, revision) {
      const keepVisible = new Set(this.visible);
      const keepTab = this.tab;
      const view = { scale: this.scale, cx: this.cx, cy: this.cy };

      this.data = payload;
      this.compare = !!payload.regions;
      this.A = this.compare ? payload.a : payload;
      this.B = this.compare ? payload.b : null;
      this.markers = payload.markers || [];
      this.nets = payload.nets || [];
      this.tracks = payload.tracks || {};
      this.tree = payload.tree || {};
      this.edit = payload.editable || this.edit;
      this.revision = revision;

      // Selections point at objects that no longer exist.
      this.selection = null;
      this.selected = [];
      this.activeMarker = null;
      this.netHighlight = null;
      this.probeA = this.probeB = null;
      this.findHits = [];
      this.findIndex = -1;
      if (this.editState) this.editState.pending = null;

      // A layer that has just gained its first shape was not in the old visible set,
      // so it would be drawn invisible - the edit would appear to have done nothing.
      const known = new Set(this.A.layers.map((l) => l.name));
      this.visible = new Set([...keepVisible].filter((n) => known.has(n)));
      for (const name of this.A.defaultOn || []) {
        if (!keepVisible.has(name) && known.has(name)) continue;
        this.visible.add(name);
      }
      this.tab = keepTab;
      this.scale = view.scale; this.cx = view.cx; this.cy = view.cy;
      this.buildToolbar();
      if (!this.opts.noPanel) this.buildPanel();
      this.sync();
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

      // A viewer normally owns its layer panel. In the side-by-side comparison it
      // does not: two panels listing the same layers is two places to keep in step,
      // and the shared one belongs to neither drawing.
      if (!this.opts.noPanel) {
        this.panel = document.createElement("div");
        this.panel.className = "gv-panel";
        body.appendChild(this.panel);
      }

      this.buildToolbar();
      if (!this.opts.noPanel) this.buildPanel();
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
        ["net", ICON.net, "Trace net — click a shape to highlight everything joined to it (N)"],
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
      this.btnTracks = this.button(disp, ICON.tracks,
        "Routing grid — the track centres from the track-guide layers (T)", () => {
          this.tracksOn = !this.tracksOn; this.sync(); this.draw();
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

      // Find shapes by their measured size. KLayout answers "which wires are
      // narrower than 21 nm?" by writing a DRC script; here it is a query over the
      // dimensions the analyzer already measured, so the answer is the same number
      // the report quotes.
      const find = this.group(t, "Find");
      this.findInput = document.createElement("input");
      this.findInput.type = "search";
      this.findInput.className = "gv-find";
      this.findInput.placeholder = "w<21, h>50, a<300…";
      this.findInput.title = "Find shapes by measured size: w, h or a (area), " +
                             "with < > or =, in nm. Enter steps through the hits.";
      this.findInput.addEventListener("input", () => this.runFind(this.findInput.value));
      this.findInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); this.stepFind(e.shiftKey ? -1 : 1); }
        if (e.key === "Escape") { this.findInput.value = ""; this.runFind(""); }
      });
      find.appendChild(this.findInput);
      this.findCount = document.createElement("span");
      this.findCount.className = "gv-fcount";
      find.appendChild(this.findCount);
      this.button(find, ICON.fwd, "Next match (Enter)", () => this.stepFind(1));
      this.button(find, "→▤", "Turn these hits into markers you can step through",
                  () => this.findToMarkers());

      if (this.edit && this.buildEditToolbar) this.buildEditToolbar(t);

      // Everything else, behind one button. The toolbar holds what gets used on
      // every layout; a menu holds what gets used on some of them, and burying the
      // rest in a section further down the page meant scrolling away from the
      // drawing to reach a tool that is about the drawing.
      const more = this.group(t, "");
      this.btnMore = this.button(more, "More tools ▾",
                                 "Every other tool (M)", () => this.toggleToolMenu(),
                                 { cls: "gv-primary" });

      const out = this.group(t, "");
      this.button(out, ICON.save, "Save this view as a PNG", () => this.exportPNG());
      this.button(out, ICON.bookmark, "Bookmark this view", () => this.addBookmark());
      this.button(out, ICON.share, "Copy a link to exactly this view", () => this.copyState());
      if (this.opts.onEvent) {
        this.button(out, "⤢ Expand", "Open the full-screen workspace", () => {
          this.opts.onEvent({ type: "expand" });
        }, { cls: "gv-primary" });
      }
    }

    buildPanel() {
      const p = this.panel;
      if (!p) return;
      p.innerHTML = "";

      // Tabs, because a review needs several different lists and stacking them all
      // in one column means scrolling past the layers to reach a violation.
      this.tabBar = document.createElement("div");
      this.tabBar.className = "gv-tabs";
      p.appendChild(this.tabBar);
      // A comparison has different questions: the rule and net lists belong to one
      // layout, and an empty tab is worse than no tab. What replaces them is the
      // difference browser, which is the whole point of the view.
      const tabs = this.compare
        ? [["diffs", "Diffs", (this.data.regions || []).length],
           ["layers", "Layers", this.A.layers.length],
           ["views", "Views", ""]]
        : [["layers", "Layers", this.A.layers.length],
           ["markers", "Rules", this.markers.length],
           ["nets", "Nets", this.nets.length],
           ["cells", "Cells", (this.tree.cells || []).length],
           ...(this.edit ? [["edit", "Edit", ""]] : []),
           ["views", "Views", ""]];
      this.tabButtons = {};
      for (const [id, label, count] of tabs) {
        const b = document.createElement("button");
        b.className = "gv-tab";
        b.type = "button";
        b.innerHTML = label + (count === "" ? "" : ` <span class="gv-n">${count}</span>`);
        b.addEventListener("click", () => { this.tab = id; this.renderPanel(); });
        this.tabBar.appendChild(b);
        this.tabButtons[id] = b;
      }

      this.panelBody = document.createElement("div");
      this.panelBody.className = "gv-pbody";
      p.appendChild(this.panelBody);

      this.info = document.createElement("div");
      this.info.className = "gv-info";
      p.appendChild(this.info);

      this.renderPanel();
    }

    renderPanel() {
      if (!this.panel) return;
      for (const id in this.tabButtons) {
        this.tabButtons[id].classList.toggle("gv-on", this.tab === id);
      }
      this.panelBody.innerHTML = "";
      // Buttons living in the panel body are replaced on every render, so the
      // handles must not outlive them - sync() would otherwise style a node that
      // is no longer on the page.
      this.btnCellBoxes = null;
      if (this.tab === "layers") this.renderLayersTab();
      else if (this.tab === "markers") this.renderMarkersTab();
      else if (this.tab === "nets") this.renderNetsTab();
      else if (this.tab === "cells") this.renderCellsTab();
      else if (this.tab === "diffs") this.renderDiffsTab();
      else if (this.tab === "shapes") this.renderShapesTab();
      else if (this.tab === "instances") this.renderInstancesTab();
      else if (this.tab === "edit" && this.renderEditTab) this.renderEditTab();
      else this.renderViewsTab();
      this.sync();
    }

    renderLayersTab() {
      const quick = document.createElement("div");
      quick.className = "gv-quick";
      this.panelBody.appendChild(quick);
      this.button(quick, "All", "Show every layer", () => {
        this.visible = new Set(this.A.layers.map((l) => l.name));
        this.solo = null; this.renderPanel(); this.draw();
      });
      this.button(quick, "Drawing", "Only the layers with unique geometry", () => {
        this.visible = new Set(this.A.defaultOn);
        this.solo = null; this.renderPanel(); this.draw();
      });
      this.button(quick, "None", "Hide every layer", () => {
        this.visible = new Set(); this.solo = null; this.renderPanel(); this.draw();
      });

      const search = document.createElement("input");
      search.type = "search";
      search.placeholder = "Filter layers…";
      search.className = "gv-search";
      search.value = this.filter || "";
      search.addEventListener("input", () => {
        this.filter = search.value.trim().toLowerCase();
        this.syncPanel();
      });
      this.panelBody.appendChild(search);

      this.layerList = document.createElement("div");
      this.layerList.className = "gv-layers";
      this.panelBody.appendChild(this.layerList);
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

        // Edit Layer Stack: the swatch is a colour input. A .lyp gives every layer
        // a colour, but two layers that matter to *this* review can easily share a
        // near-identical one, and being able to change it beats squinting.
        const sw = document.createElement("input");
        sw.type = "color";
        sw.className = "gv-sw";
        sw.value = /^#[0-9a-f]{6}$/i.test(layer.colour || "") ? layer.colour : "#8aa0b6";
        sw.title = `${layer.name} colour — from the layer map, change it for this session`;
        sw.addEventListener("input", () => {
          layer.colour = sw.value;
          this.draw();
        });
        sw.addEventListener("click", (e) => e.stopPropagation());
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

    // ---- marker browser (KLayout calls this RVE) ----

    renderMarkersTab() {
      if (!this.markers.length) {
        this.panelBody.innerHTML =
          '<div class="gv-hint">No rule results. Design rule checking needs the ' +
          'design rule manual catalogue, which is not in this repository.</div>';
        return;
      }
      const bar = document.createElement("div");
      bar.className = "gv-quick";
      this.panelBody.appendChild(bar);
      this.button(bar, "◀", "Previous result (Shift+N)", () => this.stepMarker(-1));
      this.button(bar, "▶", "Next result (N)", () => this.stepMarker(1));
      const only = document.createElement("label");
      only.className = "gv-check";
      only.innerHTML = '<input type="checkbox"> failures only';
      const box = only.querySelector("input");
      box.checked = !!this.failuresOnly;
      box.addEventListener("change", () => {
        this.failuresOnly = box.checked;
        this.renderPanel();
      });
      bar.appendChild(only);

      const list = document.createElement("div");
      list.className = "gv-layers";
      this.panelBody.appendChild(list);
      this.markerRows = {};
      for (const m of this.visibleMarkers()) {
        const row = document.createElement("div");
        row.className = "gv-mkr gv-st-" + m.status.replace(/\s+/g, "-");
        row.dataset.id = m.id;
        if (!this.visited.has(m.id)) row.classList.add("gv-unvisited");
        if (this.waived[m.id]) row.classList.add("gv-waived");
        row.innerHTML =
          `<span class="gv-dot"></span>` +
          `<span class="gv-mid">${m.id}</span>` +
          `<span class="gv-mrule">${m.rule ? m.rule.slice(0, 90) : ""}</span>` +
          `<span class="gv-mst">${m.status}</span>`;
        row.title = (m.detail || "") + (m.layers.length ? `\nlayers: ${m.layers.join(", ")}` : "");
        row.addEventListener("click", () => this.showMarker(m));
        list.appendChild(row);
        this.markerRows[m.id] = row;
      }
    }

    visibleMarkers() {
      return this.failuresOnly
        ? this.markers.filter((m) => m.status === "violation")
        : this.markers;
    }

    // Cross-probe: isolate the layers the check actually read and fit the view to
    // them. This is the step that turns a rule result into something you can look
    // at - the reviewer's alternative is typing coordinates by hand.
    showMarker(marker) {
      this.activeMarker = marker;
      this.visited.add(marker.id);
      this.tab = "markers";
      if (marker.box) {
        this.zoomToBoxPadded(marker.box, 2.5);
      } else if (marker.layers && marker.layers.length) {
        const present = marker.layers.filter(
          (n) => this.A.layers.some((l) => l.name === n));
        if (present.length) {
          if (!this.solo) this.savedVisible = new Set(this.visible);
          this.visible = new Set(present);
          this.solo = null;
          this.zoomToLayers(present);
        }
      }
      this.renderPanel();
      this.draw();
    }

    stepMarker(step) {
      const list = this.visibleMarkers();
      if (!list.length) return;
      let i = this.activeMarker ? list.findIndex((m) => m.id === this.activeMarker.id) : -1;
      i = (i + step + list.length) % list.length;
      this.showMarker(list[i]);
      const row = this.markerRows && this.markerRows[list[i].id];
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }

    zoomToLayers(names) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const layer of this.A.layers) {
        if (!names.includes(layer.name)) continue;
        for (const s of layer.shapes) {
          const [a, b, c, d] = polyBBox(s.o);
          x0 = Math.min(x0, a); y0 = Math.min(y0, b);
          x1 = Math.max(x1, c); y1 = Math.max(y1, d);
        }
      }
      if (x1 > x0 && y1 > y0) {
        const padX = (x1 - x0) * 0.25 + 0.002, padY = (y1 - y0) * 0.25 + 0.002;
        this.zoomToBox(x0 - padX, y0 - padY, x1 + padX, y1 + padY);
      }
    }

    // ---- nets ----

    renderNetsTab() {
      if (!this.nets.length) {
        this.panelBody.innerHTML =
          '<div class="gv-hint">No net graph. Nets need a connection stack — the one ' +
          'thing a .gds and .lyp cannot supply, because GDSII stores no layer ' +
          'elevations.</div>';
        return;
      }
      const bar = document.createElement("div");
      bar.className = "gv-quick";
      this.panelBody.appendChild(bar);
      this.button(bar, "Clear", "Stop highlighting", () => {
        this.netHighlight = null; this.probeA = null; this.allNets = false;
        this.renderPanel(); this.draw();
      });
      // Trace All Nets: one colour per net, which answers "is this one net or two?"
      // across the whole cell at once rather than one click at a time.
      this.button(bar, "All", "Colour every net at once", () => {
        this.allNets = !this.allNets;
        this.netHighlight = null;
        this.renderPanel();
        this.draw();
      });
      const lock = document.createElement("label");
      lock.className = "gv-check";
      lock.innerHTML = '<input type="checkbox"> keep trace tool armed';
      const lb = lock.querySelector("input");
      lb.checked = this.traceLock;
      lb.addEventListener("change", () => { this.traceLock = lb.checked; });
      bar.appendChild(lock);

      const list = document.createElement("div");
      list.className = "gv-layers";
      this.panelBody.appendChild(list);
      for (const net of this.nets) {
        const row = document.createElement("div");
        row.className = "gv-mkr gv-net";
        if (this.netHighlight && this.netHighlight.net === net.net) row.classList.add("gv-soloed");
        row.innerHTML =
          `<span class="gv-mid">${net.net}</span>` +
          `<span class="gv-mrule">${net.layers.join(", ")}</span>` +
          `<span class="gv-mst">${net.shapeCount} shp</span>`;
        row.title = `${net.shapeCount} shape(s) across ${net.layers.length} layer(s), ` +
                    `area ${fmtArea(net.area)}` +
                    (net.provisional ? " — provisional: rests on an inferred stack" : "");
        row.addEventListener("click", () => this.highlightNet(net, true));
        list.appendChild(row);
      }
    }

    highlightNet(net, zoom) {
      this.netHighlight = net;
      // Showing every layer the net touches, or its shapes would be hidden behind
      // whichever layers happened to be off.
      for (const name of net.layers) this.visible.add(name);
      this.solo = null;
      if (zoom) {
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const s of net.shapes) {
          const [a, b, c, d] = polyBBox(s.o);
          x0 = Math.min(x0, a); y0 = Math.min(y0, b);
          x1 = Math.max(x1, c); y1 = Math.max(y1, d);
        }
        if (x1 > x0) {
          const px = (x1 - x0) * 0.2 + 0.002, py = (y1 - y0) * 0.2 + 0.002;
          this.zoomToBox(x0 - px, y0 - py, x1 + px, y1 + py);
        }
      }
      this.tab = "nets";
      this.renderPanel();
      this.draw();
    }

    // Which net owns the shape under this point. Matched on geometry rather than on
    // an id, because the net extraction merges touching shapes and so does not hand
    // back the same polygon objects the layer list holds.
    netAt(wxp, wyp) {
      for (const net of this.nets) {
        for (const s of net.shapes) {
          if (pointInPoly(wxp, wyp, s.o)) return net;
        }
      }
      return null;
    }

    traceAt(wxp, wyp, second) {
      const net = this.netAt(wxp, wyp);
      if (second) {
        // Two-point connectivity: the question a reviewer actually asks is "are
        // these two things on the same net?", and a yes/no beats two highlights.
        this.probeB = net;
        this.sync();
        this.draw();
        return;
      }
      this.probeA = net;
      this.probeB = null;
      if (net) this.highlightNet(net, false);
      else { this.netHighlight = null; this.renderPanel(); this.draw(); }
    }

    // ---- find shapes by measured size ----

    // Query forms: `w<21`, `h>=50`, `a<300`, `<21` (either side), `20` (either side
    // exactly 20 nm). Lengths in nanometres, areas in square nanometres, because
    // those are the units the numbers are quoted in everywhere else here.
    parseFind(text) {
      const m = String(text || "").trim().toLowerCase()
        .match(/^([wha])?\s*(<=|>=|<|>|=)?\s*([0-9]*\.?[0-9]+)$/);
      if (!m) return null;
      return { dim: m[1] || "any", op: m[2] || "=", value: parseFloat(m[3]) };
    }

    runFind(text) {
      this.findQuery = text;
      const q = this.parseFind(text);
      this.findHits = [];
      this.findIndex = -1;
      if (q) {
        const test = (value) => {
          // A 0.05 nm database unit means an exact match has to tolerate the grid,
          // or `w=21` finds nothing on a shape measured 21.000000000000004 nm.
          if (q.op === "=") return Math.abs(value - q.value) < 0.025;
          if (q.op === "<") return value < q.value;
          if (q.op === "<=") return value <= q.value;
          if (q.op === ">") return value > q.value;
          return value >= q.value;
        };
        for (const layer of this.orderedLayers()) {
          for (const s of layer.shapes) {
            const w = s.w * 1000, h = s.h * 1000, a = s.a * 1e6;
            const hit = q.dim === "w" ? test(w)
                      : q.dim === "h" ? test(h)
                      : q.dim === "a" ? test(a)
                      : (test(w) || test(h));
            if (hit) this.findHits.push({ layer: layer.name, colour: layer.colour,
                                          ld: `${layer.layer}/${layer.datatype}`, shape: s });
          }
        }
      }
      if (this.findCount) {
        this.findCount.textContent = !text ? ""
          : q ? `${this.findHits.length} hit${this.findHits.length === 1 ? "" : "s"}`
          : "?";
        this.findCount.title = q ? "" : "Try w<21, h>50 or a<300 (nanometres)";
      }
      this.draw();
    }

    stepFind(step) {
      if (!this.findHits.length) return;
      this.findIndex = (this.findIndex + step + this.findHits.length) % this.findHits.length;
      const hit = this.findHits[this.findIndex];
      const s = hit.shape;
      // Enough padding to see what the shape sits next to. Note this can zoom
      // *out*: a hit taller than the current view has to fit on screen to be
      // looked at, and half a highlighted shape is not an answer.
      this.zoomToBoxPadded([s.x, s.y, s.x + s.w, s.y + s.h], 0.6);
      // Selecting it puts the measured numbers in the panel, so a hit is never a
      // highlight without a figure beside it.
      this.selection = hit;
      this.sync();
      this.draw();
    }

    // Shapes To Markers: the hits become rows in the same browser the rule results
    // use, so a search and a check are reviewed the same way - stepped through,
    // ticked off, and cross-probed to the geometry.
    findToMarkers() {
      if (!this.findHits.length) { this.toast("Nothing found to convert"); return; }
      const query = this.findQuery || "find";
      const made = this.findHits.map((hit, index) => ({
        id: `find.${index + 1}`,
        section: "search",
        rule: `${hit.layer} matches ${query}`,
        status: "not checked",
        detail: `${fmtLen(hit.shape.w)} × ${fmtLen(hit.shape.h)}, area ${fmtArea(hit.shape.a)}`,
        layers: [hit.layer],
        observed: {"width_nm": +(hit.shape.w * 1000).toFixed(4),
                   "height_nm": +(hit.shape.h * 1000).toFixed(4)},
        box: [hit.shape.x, hit.shape.y, hit.shape.x + hit.shape.w, hit.shape.y + hit.shape.h],
      }));
      // Kept separate from the rule results: a search is not a check, and mixing
      // them would let "12 markers" mean two different things.
      this.markers = this.markers.filter((m) => m.section !== "search").concat(made);
      this.visited = new Set([...this.visited].filter((id) => !String(id).startsWith("find.")));
      this.tab = "markers";
      this.buildPanel();
      this.toast(`${made.length} hit(s) are now markers`);
    }

    drawFindHits(ctx) {
      ctx.save();
      ctx.lineWidth = 2;
      for (let i = 0; i < this.findHits.length; i++) {
        const s = this.findHits[i].shape;
        const x = this.sx(s.x), y = this.sy(s.y + s.h);
        const w = this.sx(s.x + s.w) - x, h = this.sy(s.y) - y;
        const current = i === this.findIndex;
        ctx.strokeStyle = current ? "#7ee787" : "rgba(126,231,135,0.65)";
        ctx.setLineDash(current ? [] : [3, 2]);
        ctx.strokeRect(x - 1.5, y - 1.5, w + 3, h + 3);
      }
      ctx.restore();
    }

    // ---- difference browser (the comparison's marker list) ----

    // Largest first, because "what changed?" is answered by the biggest region and
    // a list in file order buries it. Every row is clickable: the region is what a
    // reviewer wants on screen, and finding it by eye in a wipe is the slow way.
    renderDiffsTab() {
      const regions = this.data.regions || [];
      const summary = this.data.summary || {};
      const names = this.data.names || { a: "A", b: "B" };

      const head = document.createElement("div");
      head.className = "gv-dsum";
      head.innerHTML =
        `<div class="gv-irow"><span>Differing regions</span><b>${regions.length}</b></div>` +
        `<div class="gv-irow"><span>Layers changed</span><b>${summary.layersChanged ?? "-"} / ` +
        `${summary.layersCompared ?? "-"}</b></div>` +
        (summary.xorAreaUm2 != null
          ? `<div class="gv-irow"><span>XOR area</span><b>${fmtArea(summary.xorAreaUm2)}</b></div>` : "") +
        (summary.removedAreaUm2 != null
          ? `<div class="gv-irow"><span>Only in ${names.a}</span><b>${fmtArea(summary.removedAreaUm2)}</b></div>` : "") +
        (summary.addedAreaUm2 != null
          ? `<div class="gv-irow"><span>Only in ${names.b}</span><b>${fmtArea(summary.addedAreaUm2)}</b></div>` : "");
      this.panelBody.appendChild(head);

      if (!regions.length) {
        const hint = document.createElement("div");
        hint.className = "gv-hint";
        hint.style.padding = "8px 10px";
        hint.textContent = "No geometric difference on any compared layer. " +
          "The two layouts are identical where they overlap.";
        this.panelBody.appendChild(hint);
        return;
      }

      const bar = document.createElement("div");
      bar.className = "gv-quick";
      this.panelBody.appendChild(bar);
      this.button(bar, "◀", "Previous difference (Shift+N)", () => this.stepRegion(-1));
      this.button(bar, "▶", "Next difference (N)", () => this.stepRegion(1));
      this.button(bar, "Fit all", "Frame every difference", () => {
        this.activeRegion = null;
        this.zoomToRegions(regions);
        this.renderPanel();
      });

      const list = document.createElement("div");
      list.className = "gv-layers";
      this.panelBody.appendChild(list);
      this.regionRows = {};
      for (const r of this.orderedRegions()) {
        const row = document.createElement("div");
        row.className = "gv-mkr gv-diff gv-side-" + r.side;
        if (this.activeRegion === r) row.classList.add("gv-soloed");
        if (!this.visitedRegions.has(this.regionKey(r))) row.classList.add("gv-unvisited");
        row.innerHTML =
          `<span class="gv-dot"></span>` +
          `<span class="gv-mid">${r.layer}</span>` +
          // − and + rather than "only in": the panel is 232px wide, and the file
          // name is what has to survive the truncation, not the preposition.
          `<span class="gv-mrule">${r.side === "a" ? "−" : "+"} ` +
          `${shortName(r.side === "a" ? names.a : names.b)}</span>` +
          `<span class="gv-mst">${fmtArea(r.a)}</span>`;
        row.title = `${r.layer} · only in ${r.side === "a" ? names.a : names.b} · ` +
                    `area ${fmtArea(r.a)}`;
        row.addEventListener("click", () => this.showRegion(r));
        list.appendChild(row);
        this.regionRows[this.regionKey(r)] = row;
      }
    }

    regionKey(r) {
      return `${r.layer}|${r.side}|${r.o[0][0]},${r.o[0][1]}`;
    }

    orderedRegions() {
      return (this.data.regions || []).slice().sort((p, q) => (q.a || 0) - (p.a || 0));
    }

    showRegion(region) {
      this.activeRegion = region;
      this.visitedRegions.add(this.regionKey(region));
      this.visible.add(region.layer);
      // A difference cannot be looked at in single-layout mode, and "only in A"
      // means nothing when B is what is drawn.
      if (this.compareMode === "a" || this.compareMode === "b") {
        this.setCompareMode("overlay");
      }
      this.zoomToRegions([region]);
      this.tab = "diffs";
      this.renderPanel();
      this.draw();
    }

    stepRegion(step) {
      const list = this.orderedRegions();
      if (!list.length) return;
      let i = this.activeRegion ? list.indexOf(this.activeRegion) : -1;
      i = (i + step + list.length) % list.length;
      this.showRegion(list[i]);
      const row = this.regionRows && this.regionRows[this.regionKey(list[i])];
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }

    zoomToRegions(regions) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const r of regions) {
        const [a, b, c, d] = polyBBox(r.o);
        x0 = Math.min(x0, a); y0 = Math.min(y0, b);
        x1 = Math.max(x1, c); y1 = Math.max(y1, d);
      }
      // One region gets generous padding so its surroundings stay on screen; "fit
       // all" is already framing the whole set and needs only a margin.
      if (x1 > x0 || y1 > y0) {
        this.zoomToBoxPadded([x0, y0, x1, y1], regions.length === 1 ? 2.5 : 0.15);
      }
    }


    // ---- the tool menu ----

    // Two kinds of tool live here. Some act on the drawing and are already loaded,
    // so they happen immediately. The rest run over the file in Python; those send
    // a request to the page and it answers below the viewer. The menu says which is
    // which rather than making everything look equally instant.
    toolMenu() {
      const here = [
        ["layers", "Layers", "Show, hide, solo and recolour layers"],
        ["markers", "Rule results", "Every design rule result, clickable"],
        ["nets", "Nets", "Trace one net, or colour them all"],
        ["cells", "Cell tree", "Placements, instance boxes, hierarchy depth"],
        ["views", "Saved views", "Bookmarks, layer sets, share a view"],
        ["shapes", "Browse shapes", "Every shape with its measurements"],
        ["instances", "Browse instances", "Every placement with its transform"],
      ];
      const page = [
        ["technology", "Technology", "What is loaded and what each input unlocks"],
        ["drc", "DRC", "The bundled catalogue, or your own deck"],
        ["lvs", "LVS", "Against a schematic netlist you supply"],
        ["netlist", "Netlist", "Devices, nets and a SPICE export"],
        ["parasitics", "Parasitics", "Wire length, coupling and via count; R and C with a process file"],
        ["stack3d", "2.5D view", "Needs a layer stack: elevation and thickness"],
        ["density", "Density map", "Coverage per window"],
        ["diff", "Diff", "Structural: cells, shapes, instances, texts"],
        ["xor", "XOR", "Geometric difference between two layouts"],
      ];
      return { here, page };
    }

    toggleToolMenu() {
      if (this.menuEl) { this.closeToolMenu(); return; }
      const { here, page } = this.toolMenu();
      const menu = document.createElement("div");
      menu.className = "gv-menu";

      const section = (title, note) => {
        const head = document.createElement("div");
        head.className = "gv-mhead";
        head.innerHTML = `${title}<span class="gv-dim">${note}</span>`;
        menu.appendChild(head);
      };
      const item = (id, label, hint, handler, enabled) => {
        const row = document.createElement("button");
        row.className = "gv-mitem";
        row.type = "button";
        row.dataset.tool = id;
        row.disabled = enabled === false;
        row.innerHTML = `<span>${label}</span><span class="gv-dim">${hint}</span>`;
        row.addEventListener("click", () => { this.closeToolMenu(); handler(); });
        menu.appendChild(row);
      };

      section("In the viewer", "instant");
      for (const [id, label, hint] of here) {
        item(id, label, hint, () => this.openViewerTool(id));
      }
      section("Runs on the file", this.opts.onEvent ? "opens below" : "open these from the app");
      for (const [id, label, hint] of page) {
        item(id, label, hint, () => this.requestTool(id), !!this.opts.onEvent);
      }

      this.canvas.parentElement.appendChild(menu);
      this.menuEl = menu;
      const rect = this.btnMore.getBoundingClientRect();
      const host = this.canvas.parentElement.getBoundingClientRect();
      menu.style.left = Math.max(6, Math.min(rect.left - host.left,
                                             host.width - menu.offsetWidth - 8)) + "px";
      menu.style.top = "6px";
      this.btnMore.classList.add("gv-on");
      // Clicking anywhere else closes it, which is what every other menu does.
      this.menuAway = (event) => {
        if (!menu.contains(event.target) && event.target !== this.btnMore) {
          this.closeToolMenu();
        }
      };
      setTimeout(() => document.addEventListener("pointerdown", this.menuAway), 0);
    }

    closeToolMenu() {
      if (this.menuAway) document.removeEventListener("pointerdown", this.menuAway);
      this.menuAway = null;
      if (this.menuEl) this.menuEl.remove();
      this.menuEl = null;
      if (this.btnMore) this.btnMore.classList.remove("gv-on");
    }

    openViewerTool(id) {
      if (id === "shapes" || id === "instances") {
        this.tab = id;
      } else {
        this.tab = id;
      }
      this.renderPanel();
      this.draw();
    }

    // A tool the page owns. The viewer cannot run it - it has geometry, not the
    // file - so it asks, and says so if nothing is listening.
    requestTool(id) {
      if (!this.opts.onEvent) {
        this.toast("This tool runs on the file. Open the layout from the app to use it.");
        return;
      }
      this.opts.onEvent({ type: "tool", tool: id, nonce: Date.now() });
      this.toast(`Opening ${id} below the viewer…`);
    }

    // ---- browse shapes and instances, in the viewer ----

    renderShapesTab() {
      const body = this.panelBody;
      const bar = document.createElement("div");
      bar.className = "gv-quick";
      body.appendChild(bar);
      this.button(bar, "Visible only", "Only the layers that are switched on", () => {
        this.shapesVisibleOnly = !this.shapesVisibleOnly;
        this.renderPanel();
      });
      const search = document.createElement("input");
      search.type = "search";
      search.className = "gv-search";
      search.placeholder = "Filter by layer…";
      search.value = this.shapesFilter || "";
      search.addEventListener("input", () => {
        this.shapesFilter = search.value.trim().toLowerCase();
        this.renderPanel();
      });
      body.appendChild(search);

      const list = document.createElement("div");
      list.className = "gv-layers";
      body.appendChild(list);

      const rows = [];
      for (const layer of this.A.layers) {
        if (this.shapesVisibleOnly && !this.visible.has(layer.name)) continue;
        if (this.shapesFilter && !layer.name.toLowerCase().includes(this.shapesFilter)) continue;
        layer.shapes.forEach((shape, index) => {
          if (!shape._del) rows.push({ layer: layer.name, index, shape });
        });
      }
      rows.sort((a, b) => Math.min(a.shape.w, a.shape.h) - Math.min(b.shape.w, b.shape.h));

      const head = document.createElement("div");
      head.className = "gv-isec";
      head.style.padding = "6px 8px 2px";
      head.textContent = `${rows.length} shape(s), narrowest first`;
      list.appendChild(head);

      for (const row of rows.slice(0, 500)) {
        const el = document.createElement("div");
        el.className = "gv-mkr gv-net";
        el.innerHTML =
          `<span class="gv-mid">${row.layer.slice(0, 6)}</span>` +
          `<span class="gv-mrule">${fmtLen(row.shape.w)} × ${fmtLen(row.shape.h)}</span>` +
          `<span class="gv-mst">${fmtArea(row.shape.a)}</span>`;
        el.title = `${row.layer} #${row.index}\n` +
                   `origin ${fmtCoord(row.shape.x)}, ${fmtCoord(row.shape.y)} nm\n` +
                   `${row.shape.v} vertices`;
        el.addEventListener("click", () => {
          this.selection = { layer: row.layer, shape: row.shape,
                             ld: "", colour: (this.A.layers.find(
                                 (l) => l.name === row.layer) || {}).colour };
          this.zoomToBoxPadded([row.shape.x, row.shape.y,
                                row.shape.x + row.shape.w, row.shape.y + row.shape.h], 2.5);
          this.sync();
          this.draw();
        });
        list.appendChild(el);
      }
      if (rows.length > 500) {
        const note = document.createElement("div");
        note.className = "gv-hint";
        note.style.padding = "6px 8px";
        note.textContent = `Showing the 500 narrowest of ${rows.length}. Filter by layer to see more.`;
        list.appendChild(note);
      }
    }

    renderInstancesTab() {
      const body = this.panelBody;
      const placements = (this.tree && this.tree.placements) || [];
      if (!placements.length) {
        body.innerHTML =
          `<div class="gv-hint" style="padding:8px 10px">` +
          `<b>${(this.tree && this.tree.top) || "This cell"}</b> is flat — it contains ` +
          `no instances, so there is nothing to browse. Its shapes are under ` +
          `<b>Browse shapes</b>.</div>`;
        return;
      }
      const list = document.createElement("div");
      list.className = "gv-layers";
      body.appendChild(list);
      const head = document.createElement("div");
      head.className = "gv-isec";
      head.style.padding = "6px 8px 2px";
      head.textContent = `${placements.length} placement(s)`;
      list.appendChild(head);
      for (const placement of placements) {
        const el = document.createElement("div");
        el.className = "gv-mkr gv-cell";
        el.style.paddingLeft = (6 + (placement.depth - 1) * 11) + "px";
        el.innerHTML =
          `<span class="gv-mid">L${placement.depth}</span>` +
          `<span class="gv-mrule">${placement.cell}</span>` +
          `<span class="gv-mst">${placement.orient || ""}</span>`;
        el.title = `${placement.path}\nin ${placement.parent}`;
        el.addEventListener("click", () => this.showPlacement(placement));
        list.appendChild(el);
      }
    }

    // ---- cell tree ----

    // KLayout's cell list plus its hierarchy-depth control. The geometry here is
    // drawn flattened, so limiting the depth cannot hide shapes the way it does in
    // KLayout; what it does is decide how deep the instance boundaries go, which is
    // the part that answers "which cell is this?" on a block you did not draw.
    renderCellsTab() {
      const tree = this.tree || {};
      const cells = tree.cells || [];
      if (!cells.length) {
        this.panelBody.innerHTML =
          '<div class="gv-hint">No cell list. The cell tree is read from the GDSII ' +
          'file itself, so this only happens when the layout was not analysed.</div>';
        return;
      }

      const bar = document.createElement("div");
      bar.className = "gv-quick";
      this.panelBody.appendChild(bar);
      this.btnCellBoxes = this.button(bar, "Boxes", "Outline every instance (H)", () => {
        this.cellBoxesOn = !this.cellBoxesOn;
        this.renderPanel();
        this.draw();
      });
      this.button(bar, "Fit top", "Fit the top cell", () => {
        this.activePlacement = null;
        this.activeCell = null;
        this.fit(true);
        this.renderPanel();
      });

      if ((tree.maxDepth || 0) > 0) {
        const wrap = document.createElement("div");
        wrap.className = "gv-depth";
        wrap.innerHTML = `<span class="gv-dim">Levels</span>`;
        const slider = document.createElement("input");
        slider.type = "range";
        slider.className = "gv-slider";
        slider.min = "1";
        slider.max = String(tree.maxDepth);
        slider.value = String(this.depthLimit || tree.maxDepth);
        const out = document.createElement("b");
        out.textContent = slider.value + " / " + tree.maxDepth;
        slider.addEventListener("input", () => {
          this.depthLimit = parseInt(slider.value, 10);
          out.textContent = slider.value + " / " + tree.maxDepth;
          this.draw();
        });
        wrap.appendChild(slider);
        wrap.appendChild(out);
        this.panelBody.appendChild(wrap);
      }

      const list = document.createElement("div");
      list.className = "gv-layers";
      this.panelBody.appendChild(list);

      // Definitions first: every cell in the file, top cell leading. Then the
      // placements as a path tree, so a cell used twice is two rows you can visit.
      const head = (text) => {
        const h = document.createElement("div");
        h.className = "gv-isec";
        h.style.padding = "6px 6px 2px";
        h.textContent = text;
        list.appendChild(h);
      };
      head(cells.length === 1 ? "Cell" : `Cells (${cells.length})`);
      for (const cell of cells) {
        const row = document.createElement("div");
        row.className = "gv-mkr gv-cell";
        row.innerHTML =
          `<span class="gv-mid">${cell.isTop ? "top" : ""}</span>` +
          `<span class="gv-mrule">${cell.name}</span>` +
          `<span class="gv-mst">${cell.shapes} shp</span>`;
        row.title = `${cell.shapes} shape(s), ${cell.placements} instance placement(s), ` +
                    `${cell.levels} level(s) below` +
                    (cell.bbox ? `\nbbox ${fmtLen(cell.bbox[2] - cell.bbox[0])} × ` +
                                 `${fmtLen(cell.bbox[3] - cell.bbox[1])}` : "");
        row.addEventListener("click", () => this.showCell(cell));
        // Placing a cell into itself is the one thing GDSII cannot express, so the
        // top cell is not offered here rather than refused after the click.
        if (this.edit && this.armPlacement && !cell.isTop) {
          const place = document.createElement("button");
          place.className = "gv-solo";
          place.type = "button";
          place.textContent = "＋";
          place.title = `Place an instance of ${cell.name}`;
          place.addEventListener("click", (e) => {
            e.preventDefault(); e.stopPropagation();
            this.armPlacement(cell.name);
          });
          row.appendChild(place);
        }
        list.appendChild(row);
      }

      const placements = tree.placements || [];
      if (placements.length) {
        head(`Placements (${placements.length}${tree.truncated ? "+" : ""})`);
        for (const p of placements) {
          const row = document.createElement("div");
          row.className = "gv-mkr gv-cell";
          if (this.activePlacement && this.activePlacement.id === p.id) {
            row.classList.add("gv-soloed");
          }
          row.style.paddingLeft = (6 + (p.depth - 1) * 11) + "px";
          row.innerHTML =
            `<span class="gv-mid">L${p.depth}</span>` +
            `<span class="gv-mrule">${p.cell}</span>` +
            `<span class="gv-mst">${p.orient || ""}</span>`;
          row.title = `${p.path}\nin ${p.parent}, ${p.orient}` +
                      (p.bbox ? `\nat ${fmtCoord(p.bbox[0])}, ${fmtCoord(p.bbox[1])}` : "");
          row.addEventListener("click", () => this.showPlacement(p));
          list.appendChild(row);
        }
      } else {
        const hint = document.createElement("div");
        hint.className = "gv-hint";
        hint.style.padding = "8px 8px 4px";
        hint.textContent = tree.note ||
          "This layout is flat: the top cell contains no instances.";
        this.panelBody.appendChild(hint);
      }
    }

    showCell(cell) {
      this.activePlacement = null;
      this.activeCell = cell;
      if (cell.bbox) this.zoomToBoxPadded(cell.bbox);
      this.tab = "cells";
      this.renderPanel();
      this.draw();
    }

    showPlacement(p) {
      this.activePlacement = p;
      this.activeCell = null;
      this.cellBoxesOn = true;
      if (p.bbox) this.zoomToBoxPadded(p.bbox);
      this.tab = "cells";
      this.renderPanel();
      this.draw();
    }

    zoomToBoxPadded(bbox, pad) {
      // The padding is context: a 20 nm region filling the screen tells you nothing
      // about what it sits next to, which is the first thing you want to know.
      const f = pad == null ? 0.25 : pad;
      const [x0, y0, x1, y1] = bbox;
      const px = (x1 - x0) * f + 0.002, py = (y1 - y0) * f + 0.002;
      this.zoomToBox(x0 - px, y0 - py, x1 + px, y1 + py);
    }

    // ---- saved views and layer sets ----

    renderViewsTab() {
      const bar = document.createElement("div");
      bar.className = "gv-quick";
      this.panelBody.appendChild(bar);
      this.button(bar, "Bookmark view", "Save the current zoom and pan", () => this.addBookmark());
      this.button(bar, "Save layer set", "Save which layers are on", () => this.addPreset());

      const list = document.createElement("div");
      list.className = "gv-layers";
      this.panelBody.appendChild(list);

      const section = (label) => {
        const el = document.createElement("div");
        el.className = "gv-isec";
        el.textContent = label;
        list.appendChild(el);
      };

      section("Bookmarked views");
      if (!this.bookmarks.length) {
        const el = document.createElement("div");
        el.className = "gv-hint";
        el.textContent = "None yet. A bookmark returns to an exact zoom and position.";
        list.appendChild(el);
      }
      this.bookmarks.forEach((bm, i) => {
        const row = document.createElement("div");
        row.className = "gv-mkr";
        row.innerHTML = `<span class="gv-mid">${i + 1}</span>` +
                        `<span class="gv-mrule">${bm.name}</span>` +
                        `<span class="gv-mst">${fmtLen(1 / bm.scale)}/px</span>`;
        row.addEventListener("click", () => {
          this.scale = bm.scale; this.cx = bm.cx; this.cy = bm.cy;
          this.pushHistory(); this.draw();
        });
        list.appendChild(row);
      });

      section("Layer sets");
      this.presets.forEach((ps, i) => {
        const row = document.createElement("div");
        row.className = "gv-mkr";
        row.innerHTML = `<span class="gv-mid">${i + 1}</span>` +
                        `<span class="gv-mrule">${ps.name}</span>` +
                        `<span class="gv-mst">${ps.layers.length}</span>`;
        row.addEventListener("click", () => {
          this.visible = new Set(ps.layers); this.solo = null;
          this.renderPanel(); this.draw();
        });
        list.appendChild(row);
      });

      section("Share");
      const share = document.createElement("div");
      share.className = "gv-hint";
      share.innerHTML =
        'A view is just numbers, so it can be copied. <b>Copy view</b> puts the zoom, ' +
        'position and layer set on the clipboard; paste it below to go back there — ' +
        'including on someone else\'s machine.';
      list.appendChild(share);
      const row = document.createElement("div");
      row.className = "gv-quick";
      this.button(row, "Copy view", "Copy this exact view", () => this.copyState());
      const input = document.createElement("input");
      input.className = "gv-search";
      input.placeholder = "paste a view here";
      input.addEventListener("change", () => {
        if (this.applyState(input.value)) input.value = "";
      });
      list.appendChild(row);
      list.appendChild(input);
    }

    addBookmark() {
      const name = "View " + (this.bookmarks.length + 1) +
        " · " + fmtLen(1 / this.scale) + "/px";
      this.bookmarks.push({ name, scale: this.scale, cx: this.cx, cy: this.cy });
      this.tab = "views";
      this.renderPanel();
    }

    addPreset() {
      const layers = Array.from(this.visible);
      this.presets.push({ name: `Set ${this.presets.length + 1} · ${layers.length} layers`, layers });
      this.tab = "views";
      this.renderPanel();
    }

    viewState() {
      return {
        v: 1, s: this.scale, x: this.cx, y: this.cy,
        l: Array.from(this.visible), t: this.tracksOn, f: this.fillOn,
        g: this.gridOn, o: this.opacity,
      };
    }

    copyState() {
      const text = btoa(JSON.stringify(this.viewState()));
      const done = () => this.toast("View copied — paste it in the Views tab to return here");
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, () => this.toast(text));
      } else {
        this.toast(text);
      }
      this.lastState = text;
      return text;
    }

    applyState(text) {
      try {
        const st = JSON.parse(atob(String(text).trim()));
        if (!st || st.v !== 1) throw new Error("not a view");
        this.scale = st.s; this.cx = st.x; this.cy = st.y;
        this.visible = new Set(st.l || []);
        this.tracksOn = !!st.t; this.fillOn = !!st.f; this.gridOn = !!st.g;
        if (typeof st.o === "number") this.opacity = st.o;
        this.solo = null;
        this.pushHistory();
        this.renderPanel();
        this.draw();
        this.toast("View restored");
        return true;
      } catch (e) {
        this.toast("That does not look like a copied view");
        return false;
      }
    }

    toast(message) {
      if (this.toastEl) this.toastEl.remove();
      const el = document.createElement("div");
      el.className = "gv-toast";
      el.textContent = message;
      this.canvas.parentElement.appendChild(el);
      this.toastEl = el;
      setTimeout(() => { if (el.parentElement) el.remove(); }, 3200);
    }

    syncPanel() {
      if (!this.panel || this.tab !== "layers" || !this.rowEls) { this.sync(); return; }
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
      set(this.btnTracks, this.tracksOn);
      set(this.btnCellBoxes, this.cellBoxesOn);
      for (const m in this.modeButtons) set(this.modeButtons[m], this.mode === m);
      if (this.cmpButtons) {
        for (const m in this.cmpButtons) set(this.cmpButtons[m], this.compareMode === m);
      }
      if (this.btnBack) this.btnBack.disabled = this.historyIndex <= 0;
      if (this.btnFwd) this.btnFwd.disabled = this.historyIndex >= this.history.length - 1;
      this.renderInfo();
    }

    renderInfo() {
      // No info box without a panel: in the side-by-side comparison the viewers have
      // neither, and a zoom would otherwise throw on the first pushHistory.
      if (!this.info) return;
      const parts = [];

      if (this.activeMarker) {
        const m = this.activeMarker;
        parts.push(`<div class="gv-isec">Rule ${m.id} — ${m.status}</div>
          <div class="gv-mtext">${m.rule || ""}</div>
          <div class="gv-mtext gv-dim">${m.detail || ""}</div>` +
          (m.layers.length ? `<div class="gv-irow"><span>Layers</span><b>${m.layers.join(", ")}</b></div>` : "") +
          `<div class="gv-quick" style="padding:6px 0 0">
             <button class="gv-btn" data-act="waive">${this.waived[m.id] ? "Un-waive" : "Waive"}</button>
             <button class="gv-btn" data-act="report">Copy report</button>
           </div>`);
      }

      if (this.probeA || this.probeB) {
        const a = this.probeA, b = this.probeB;
        let verdict;
        if (a && b) {
          verdict = a.net === b.net
            ? `<b style="color:#3fb950">same net (${a.net})</b>`
            : `<b style="color:#f0883e">different nets (${a.net} and ${b.net})</b>`;
        } else if (a) {
          verdict = `net <b>${a.net}</b> — shift-click a second shape to compare`;
        } else {
          verdict = "no net at that point";
        }
        // Physical connectivity only. Whether two shapes are *meant* to be joined
        // needs a netlist, and this must not be read as an LVS result.
        parts.push(`<div class="gv-isec">Net probe</div><div class="gv-mtext">${verdict}</div>
          <div class="gv-mtext gv-dim">Physical connectivity from the connection stack.
          Whether they are <i>meant</i> to be joined needs a netlist.</div>`);
      }

      if (this.netHighlight) {
        const n = this.netHighlight;
        parts.push(`<div class="gv-isec">Net ${n.net}</div>
          <div class="gv-irow"><span>Shapes</span><b>${n.shapeCount}</b></div>
          <div class="gv-irow"><span>Layers</span><b>${n.layers.length}</b></div>
          <div class="gv-irow"><span>Area</span><b>${fmtArea(n.area)}</b></div>`);
      }

      if (this.activeRegion) {
        const r = this.activeRegion;
        const names = this.data.names || { a: "A", b: "B" };
        const [x0, y0, x1, y1] = polyBBox(r.o);
        parts.push(`<div class="gv-isec">Difference on ${r.layer}</div>
          <div class="gv-irow"><span>Only in</span><b>${r.side === "a" ? names.a : names.b}</b></div>
          <div class="gv-irow"><span>Area</span><b>${fmtArea(r.a)}</b></div>
          <div class="gv-irow"><span>Extent</span><b>${fmtLen(x1 - x0)} × ${fmtLen(y1 - y0)}</b></div>
          <div class="gv-irow"><span>Origin</span><b>${fmtCoord(x0)}, ${fmtCoord(y0)} nm</b></div>
          <div class="gv-mtext gv-dim">A geometric difference on one layer. Whether it
          changes behaviour needs a netlist or a schematic.</div>`);
      }

      if (this.activePlacement) {
        const p = this.activePlacement;
        const box = p.bbox;
        parts.push(`<div class="gv-isec">Instance ${p.cell}</div>
          <div class="gv-mtext gv-dim">${p.path || ""}</div>
          <div class="gv-irow"><span>In</span><b>${p.parent || ""}</b></div>
          <div class="gv-irow"><span>Level</span><b>${p.depth}</b></div>
          <div class="gv-irow"><span>Orientation</span><b>${p.orient || "R0"}</b></div>` +
          (box ? `<div class="gv-irow"><span>Size</span><b>${fmtLen(box[2] - box[0])} × ${fmtLen(box[3] - box[1])}</b></div>
                  <div class="gv-irow"><span>Origin</span><b>${fmtCoord(box[0])}, ${fmtCoord(box[1])} nm</b></div>` : "") +
          `<div class="gv-irow"><span>Shapes in cell</span><b>${p.shapes}</b></div>`);
      } else if (this.activeCell) {
        const c = this.activeCell;
        const box = c.bbox;
        parts.push(`<div class="gv-isec">Cell ${c.name}${c.isTop ? " (top)" : ""}</div>
          <div class="gv-irow"><span>Shapes</span><b>${c.shapes}</b></div>
          <div class="gv-irow"><span>Placements inside</span><b>${c.placements}</b></div>
          <div class="gv-irow"><span>Levels below</span><b>${c.levels}</b></div>` +
          (box ? `<div class="gv-irow"><span>Bounding box</span><b>${fmtLen(box[2] - box[0])} × ${fmtLen(box[3] - box[1])}</b></div>` : ""));
      }

      if (this.selection) {
        const s = this.selection;
        parts.push(`<div class="gv-isec">${s.layer} <span class="gv-dim">${s.ld}</span></div>
          <div class="gv-irow"><span>Size</span><b>${fmtLen(s.shape.w)} × ${fmtLen(s.shape.h)}</b></div>
          <div class="gv-irow"><span>Area</span><b>${fmtArea(s.shape.a)}</b></div>
          <div class="gv-irow"><span>Centre</span><b>${fmtCoord(s.shape.cx)}, ${fmtCoord(s.shape.cy)} nm</b></div>
          <div class="gv-irow"><span>Origin</span><b>${fmtCoord(s.shape.x)}, ${fmtCoord(s.shape.y)} nm</b></div>
          ${s.shape.v ? `<div class="gv-irow"><span>Vertices</span><b>${s.shape.v}</b></div>` : ""}`);
      }

      if (this.rulers.length) {
        parts.push('<div class="gv-isec">Measurements</div>');
        this.rulers.forEach((r, i) => {
          const text = r.kind === "area"
            ? `${fmtLen(Math.abs(r.x1 - r.x0))} × ${fmtLen(Math.abs(r.y1 - r.y0))}`
            : fmtLen(Math.hypot(r.x1 - r.x0, r.y1 - r.y0));
          const value = r.kind === "area"
            ? fmtArea(Math.abs((r.x1 - r.x0) * (r.y1 - r.y0)))
            : `Δ ${fmtLen(r.x1 - r.x0)}, ${fmtLen(r.y1 - r.y0)}`;
          parts.push(`<div class="gv-irow gv-mrow"><span>${text}</span><b>${value}</b>` +
                     `<button class="gv-x" data-del="${i}" title="Delete">×</button></div>`);
        });
        parts.push('<div class="gv-quick" style="padding:6px 0 0">' +
                   '<button class="gv-btn" data-act="copy-measure">Copy measurements</button></div>');
      }

      if (!parts.length) {
        parts.push('<div class="gv-hint">Click a shape for its dimensions. ' +
                   '<kbd>R</kbd> ruler · double-click a shape to measure it · ' +
                   '<kbd>N</kbd> next rule result · <kbd>T</kbd> routing grid · ' +
                   '<kbd>?</kbd> all keys.</div>');
      }
      this.info.innerHTML = parts.join("");

      // Delegated, because this markup is rebuilt on every state change.
      this.info.querySelectorAll("[data-del]").forEach((b) => {
        b.addEventListener("click", () => {
          this.rulers.splice(Number(b.dataset.del), 1);
          this.sync(); this.draw();
        });
      });
      this.info.querySelectorAll("[data-act]").forEach((b) => {
        b.addEventListener("click", () => {
          const act = b.dataset.act;
          if (act === "waive" && this.activeMarker) {
            const id = this.activeMarker.id;
            if (this.waived[id]) delete this.waived[id];
            else this.waived[id] = true;
            this.renderPanel();
          } else if (act === "report") {
            this.copyReport();
          } else if (act === "copy-measure") {
            this.copyMeasurements();
          }
        });
      });
    }

    // A review is only useful if it leaves the tool. KLayout's marker database is a
    // local file; text on the clipboard goes into a ticket or a message.
    copyReport() {
      const lines = ["Rule review — " + (this.A.topCell || this.A.title)];
      for (const m of this.markers) {
        if (m.status === "pass") continue;
        lines.push(`[${m.status}${this.waived[m.id] ? ", waived" : ""}] ${m.id} — ${m.rule}`);
        if (m.detail) lines.push("    " + m.detail);
      }
      this.copyText(lines.join("\n"), `${lines.length - 1} result(s) copied`);
    }

    copyMeasurements() {
      const lines = this.rulers.map((r, i) => r.kind === "area"
        ? `${i + 1}. box ${fmtLen(Math.abs(r.x1 - r.x0))} × ${fmtLen(Math.abs(r.y1 - r.y0))} = ` +
          fmtArea(Math.abs((r.x1 - r.x0) * (r.y1 - r.y0)))
        : `${i + 1}. ${fmtLen(Math.hypot(r.x1 - r.x0, r.y1 - r.y0))} ` +
          `(dx ${fmtLen(r.x1 - r.x0)}, dy ${fmtLen(r.y1 - r.y0)})`);
      this.copyText(lines.join("\n"), `${lines.length} measurement(s) copied`);
    }

    copyText(text, message) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => this.toast(message),
                                                 () => this.toast("Copy blocked by the browser"));
      } else {
        this.toast("Copy is not available in this browser");
      }
      this.lastCopied = text;
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
          if (s._del) continue;                // deleted by an uncommitted edit
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
          if (s._del) continue;                // deleted by an uncommitted edit
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

      // Auto-measure. Clicking inside a shape and getting its width and height
      // without aiming at two edges is the measurement a reviewer makes most often.
      c.addEventListener("dblclick", (e) => {
        const r = c.getBoundingClientRect();
        const wxp = this.wx(e.clientX - r.left), wyp = this.wy(e.clientY - r.top);
        const hit = this.pick(wxp, wyp);
        if (!hit) return;
        e.preventDefault();
        const s2 = hit.shape;
        this.rulers.push({ kind: "line", x0: s2.x, y0: wyp, x1: s2.x + s2.w, y1: wyp });
        this.rulers.push({ kind: "line", x0: wxp, y0: s2.y, x1: wxp, y1: s2.y + s2.h });
        this.selection = hit;
        this.sync();
        this.draw();
      });

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
        // An edit tool owns the left button. Right and middle still pan, which is
        // what stops a modal drawing tool from trapping the view.
        if (this.editDown && e.button === 0 && this.editDown(e, px, py, w)) {
          this.draw();
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
        } else if (this.mode === "net") {
          // Shift-click asks the second question: same net as the last one?
          this.traceAt(this.wx(px), this.wy(py), e.shiftKey);
          if (!this.traceLock && !e.shiftKey) this.setMode("pan");
        }
        this.draw();
      });

      c.addEventListener("pointermove", (e) => {
        const r = c.getBoundingClientRect();
        const px = e.clientX - r.left, py = e.clientY - r.top;
        this.cursor = { px, py };
        const wxp = this.wx(px), wyp = this.wy(py);

        if (this.editMove && this.editMove(e, px, py, wxp, wyp)) {
          this.draw();
          return;
        }
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
        if (this.pending) {
          let nx = this.snapped.x, ny = this.snapped.y;
          if (e.shiftKey) {
            // Pitch and width measurements are axis-aligned; a free-angle ruler
            // reads a hypotenuse and looks like a wrong answer.
            if (Math.abs(nx - this.pending.x0) >= Math.abs(ny - this.pending.y0)) {
              ny = this.pending.y0;
            } else {
              nx = this.pending.x0;
            }
          }
          this.pending.x1 = nx; this.pending.y1 = ny;
        }
        const hit = this.mode === "ruler" ? null : this.pick(wxp, wyp);
        const changed = (hit && hit.shape) !== (this.hover && this.hover.shape);
        this.hover = hit;
        this.updateReadout(wxp, wyp);
        this.updateTip(hit, px, py);
        if (changed || this.pending || this.snapOn) this.draw();
      });

      const finish = (e) => {
        if (this.editUp && this.editUp(e)) { this.draw(); return; }
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
        // The editor takes the keys it owns before the view shortcuts see them:
        // with a selection, Delete has to delete a shape rather than do nothing.
        if (this.editKey && this.editKey(e, k)) return;
        const map = {
          f: () => this.fit(), r: () => this.setMode("ruler"), v: () => this.setMode("pan"),
          a: () => this.setMode("area"), p: () => this.setMode("probe"),
          s: () => { this.snapOn = !this.snapOn; this.sync(); this.draw(); },
          g: () => { this.gridOn = !this.gridOn; this.sync(); this.draw(); },
          l: () => { this.labelsOn = !this.labelsOn; this.sync(); this.draw(); },
          o: () => { this.fillOn = !this.fillOn; this.sync(); this.draw(); },
          t: () => { this.tracksOn = !this.tracksOn; this.sync(); this.draw(); },
          m: () => this.toggleToolMenu(),
          h: () => { this.cellBoxesOn = !this.cellBoxesOn; this.renderPanel(); this.draw(); },
          "+": () => this.zoomBy(1.4), "=": () => this.zoomBy(1.4),
          "-": () => this.zoomBy(1 / 1.4), "_": () => this.zoomBy(1 / 1.4),
          escape: () => this.clearAnnotations(),
          backspace: () => this.goHistory(-1),
          "?": () => this.toggleHelp(),
        };
        if (k === "n") {                       // step the list this view is about
          e.preventDefault();
          if (this.compare) this.stepRegion(e.shiftKey ? -1 : 1);
          else if (this.markers.length) this.stepMarker(e.shiftKey ? -1 : 1);
          else this.setMode("net");
          return;
        }
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
      this.closeToolMenu();
      this.rulers = [];
      this.pending = null;
      this.selection = null;
      // Escape clears everything the user put on the drawing, which includes the
      // highlights: leaving a marker or an instance outlined after "clear" makes
      // the key feel broken.
      this.activeMarker = null;
      this.activePlacement = null;
      this.activeCell = null;
      this.activeRegion = null;
      this.netHighlight = null;
      this.probeA = null;
      this.probeB = null;
      this.renderPanel();
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
        <div><kbd>T</kbd> routing grid</div><div><kbd>H</kbd> cell boxes</div>
        <div><kbd>N</kbd> next result</div><div><kbd>Shift+N</kbd> previous</div>
        <div><kbd>Esc</kbd> clear</div>
        <div class="gv-dim">Shift+drag zooms to a box · right-drag always pans · wheel zooms at the cursor</div>
        <div class="gv-dim">Find box: <b>w&lt;21</b>, <b>h&gt;50</b>, <b>a&lt;300</b> — sizes in nm,
        Enter steps through the hits</div>
        <div class="gv-dim">Double-click a shape to measure it · Shift holds a ruler to one axis</div>`;
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

      if (this.tracksOn) this.drawTracks(ctx);
      if (this.allNets) this.drawAllNets(ctx);
      if (this.netHighlight) this.drawNet(ctx);
      if (this.cellBoxesOn || this.activePlacement) this.drawCellBoxes(ctx);
      if (this.findHits.length) this.drawFindHits(ctx);
      if (this.drawEdit) this.drawEdit(ctx);
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
          if (s._del) continue;                // deleted by an uncommitted edit
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
        const active = this.activeRegion === r;
        ctx.beginPath();
        ctx.moveTo(this.sx(r.o[0][0]), this.sy(r.o[0][1]));
        for (let i = 1; i < r.o.length; i++) ctx.lineTo(this.sx(r.o[i][0]), this.sy(r.o[i][1]));
        ctx.closePath();
        ctx.fillStyle = r.side === "a" ? "rgba(214,39,40,0.55)" : "rgba(44,160,44,0.55)";
        ctx.strokeStyle = r.side === "a" ? "#d62728" : "#2ca02c";
        ctx.lineWidth = active ? 2.4 : 1.2;
        ctx.fill();
        ctx.stroke();
        if (active) {
          // A ring outside the region, so the highlight cannot be mistaken for
          // extra differing area.
          const [x0, y0, x1, y1] = polyBBox(r.o);
          ctx.save();
          ctx.setLineDash([4, 3]);
          ctx.strokeStyle = "#f0b429";
          ctx.lineWidth = 1.6;
          ctx.strokeRect(this.sx(x0) - 4, this.sy(y1) - 4,
                         this.sx(x1) - this.sx(x0) + 8, this.sy(y0) - this.sy(y1) + 8);
          ctx.restore();
        }
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

    drawNet(ctx) {
      const net = this.netHighlight;
      ctx.save();
      for (const s of net.shapes) {
        ctx.beginPath();
        ctx.moveTo(this.sx(s.o[0][0]), this.sy(s.o[0][1]));
        for (let i = 1; i < s.o.length; i++) ctx.lineTo(this.sx(s.o[i][0]), this.sy(s.o[i][1]));
        ctx.closePath();
        ctx.fillStyle = "rgba(255,214,102,0.30)";
        ctx.strokeStyle = "#ffd666";
        ctx.lineWidth = 2;
        ctx.fill();
        ctx.stroke();
      }
      ctx.restore();
    }

    // A colour per net, spread around the wheel so neighbours differ. Nets are
    // sorted by size first, so the big power nets always take the same colours and
    // the picture does not reshuffle when a layer is toggled.
    drawAllNets(ctx) {
      const nets = this.nets.slice().sort((a, b) => (b.shapeCount || 0) - (a.shapeCount || 0));
      ctx.save();
      ctx.lineWidth = 2;
      nets.forEach((net, index) => {
        const hue = (index * 137.508) % 360;      // golden angle: no two adjacent
        ctx.strokeStyle = `hsl(${hue}, 85%, 62%)`;
        ctx.fillStyle = `hsla(${hue}, 85%, 62%, 0.22)`;
        for (const shape of net.shapes) {
          if (!this.visible.has(shape.layer)) continue;
          ctx.beginPath();
          ctx.moveTo(this.sx(shape.o[0][0]), this.sy(shape.o[0][1]));
          for (let i = 1; i < shape.o.length; i++) {
            ctx.lineTo(this.sx(shape.o[i][0]), this.sy(shape.o[i][1]));
          }
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
        }
      });
      ctx.restore();
    }

    drawTracks(ctx) {
      // The routing grid from the track-guide layers. Drawn as centre lines so
      // "is this wire on grid?" is answered by looking rather than measuring.
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      const [bx0, by0, bx1, by1] = this.A.bbox;
      for (const metal in this.tracks) {
        if (metal.startsWith("_")) continue;
        const t = this.tracks[metal];
        ctx.strokeStyle = "rgba(88,166,255,0.55)";
        for (const posNm of t.positionsNm || []) {
          const at = posNm / 1000;
          ctx.beginPath();
          if (t.axis === "y") {
            ctx.moveTo(this.sx(bx0), this.sy(at)); ctx.lineTo(this.sx(bx1), this.sy(at));
          } else {
            ctx.moveTo(this.sx(at), this.sy(by0)); ctx.lineTo(this.sx(at), this.sy(by1));
          }
          ctx.stroke();
        }
      }
      const cpp = this.tracks._cpp;
      if (cpp && cpp.columnsNm && cpp.columnsNm.length) {
        ctx.strokeStyle = "rgba(255,122,198,0.6)";
        for (const posNm of cpp.columnsNm) {
          const at = posNm / 1000;
          ctx.beginPath();
          ctx.moveTo(this.sx(at), this.sy(by0));
          ctx.lineTo(this.sx(at), this.sy(by1));
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    drawCellBoxes(ctx) {
      // Instance boundaries, to the depth the slider allows. Names are only drawn
      // when the box is big enough on screen to hold one, which is what keeps a
      // dense array from turning into a block of overlapping text.
      const placements = (this.tree && this.tree.placements) || [];
      const limit = this.depthLimit || (this.tree && this.tree.maxDepth) || 0;
      ctx.save();
      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "top";
      for (const p of placements) {
        if (!p.bbox) continue;
        const active = this.activePlacement && this.activePlacement.id === p.id;
        if (!active && (!this.cellBoxesOn || p.depth > limit)) continue;
        const [x0, y0, x1, y1] = p.bbox;
        const px = this.sx(x0), py = this.sy(y1);
        const w = this.sx(x1) - px, h = this.sy(y0) - py;
        ctx.setLineDash(active ? [] : [4, 3]);
        ctx.lineWidth = active ? 2 : 1;
        ctx.strokeStyle = active ? "#f0b429" : "rgba(240,180,41,0.42)";
        ctx.strokeRect(px + 0.5, py + 0.5, w, h);
        if (active) {
          ctx.fillStyle = "rgba(240,180,41,0.10)";
          ctx.fillRect(px + 0.5, py + 0.5, w, h);
        }
        if (w > 34 && h > 13) {
          ctx.fillStyle = active ? "#ffd77a" : "rgba(240,180,41,0.72)";
          ctx.fillText(p.cell, px + 3, py + 2);
        }
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


  // ---------- the side-by-side comparison ------------------------------------
  //
  // Two drawings, one layer panel. The panel is the only thing they share: a
  // checkbox writes into both viewers and redraws them, while zoom, pan, rulers and
  // history stay per-viewer. Sharing the panel is the point - two lists of the same
  // layers is two things to keep in step, and nobody compares layouts by ticking the
  // same box twice.
  //
  // Layers are keyed by layer/datatype, not by name: two layers can share a display
  // name, and the key has to be the thing the file actually stores.
  class DualViewer {
    constructor(root, payload, options) {
      this.root = root;
      this.data = payload;
      this.opts = options || {};
      this.filter = "";
      root.innerHTML = "";
      root.className = "gv-dual";

      const names = payload.names || {};
      this.halves = [];
      for (const [side, label, name] of [["a", "A — Reference", names.a],
                                         ["b", "B — Revision", names.b]]) {
        const half = document.createElement("div");
        half.className = "gv-half";
        const title = document.createElement("div");
        title.className = "gv-htitle";
        title.innerHTML = `<b>${label}</b><span class="gv-dim">${name || ""}</span>`;
        half.appendChild(title);
        const stage = document.createElement("div");
        stage.className = "gv-hstage";
        half.appendChild(stage);
        root.appendChild(half);
        // Each half forwards the tool menu's requests up, tagged with the side it
        // came from - "run the netlist" means nothing without saying on which of
        // the two files.
        const onEvent = this.opts.onEvent
          ? (event) => this.opts.onEvent({ ...event, side, file: name })
          : null;
        const viewer = new NS.Viewer(stage, payload[side], { noPanel: true, onEvent });
        this.halves.push({ side, viewer, name });
      }

      this.panel = document.createElement("div");
      this.panel.className = "gv-panel gv-shared";
      root.appendChild(this.panel);
      this.buildPanel();
    }

    // The union of both layouts' layers. A layer in only one of them is listed once
    // and controls the side that has it; the other side has nothing to hide.
    layers() {
      const out = new Map();
      for (const { side, viewer } of this.halves) {
        for (const layer of viewer.A.layers) {
          const key = `${layer.layer}/${layer.datatype}`;
          const entry = out.get(key) || {
            key, name: layer.name, layer: layer.layer, datatype: layer.datatype,
            colour: layer.colour, role: layer.role, sides: {}, count: 0,
          };
          entry.sides[side] = layer.name;
          entry.count += layer.count || 0;
          if (!entry.colour) entry.colour = layer.colour;
          out.set(key, entry);
        }
      }
      return [...out.values()].sort((x, y) => x.layer - y.layer || x.datatype - y.datatype);
    }

    isOn(entry) {
      for (const { side, viewer } of this.halves) {
        const name = entry.sides[side];
        if (name && viewer.visible.has(name)) return true;
      }
      return false;
    }

    set(entry, on) {
      for (const { side, viewer } of this.halves) {
        const name = entry.sides[side];
        if (!name) continue;                       // this layer is not in that file
        if (on) viewer.visible.add(name);
        else viewer.visible.delete(name);
        viewer.solo = null;
      }
    }

    // Toggling redraws; it never rebuilds a payload, reads a file or asks the page
    // for anything. That is what keeps it instant and the two views in step.
    apply(redraw) {
      for (const { viewer } of this.halves) {
        viewer.syncPanel();
        if (redraw !== false) viewer.draw();
      }
      this.renderRows();
    }

    setAll(which) {
      for (const entry of this.layers()) {
        const on = which === "all" ? true
                 : which === "none" ? false
                 : entry.role !== "derived";       // "drawing" keeps the same meaning
        this.set(entry, on);
      }
      this.apply();
    }

    buildPanel() {
      const p = this.panel;
      p.innerHTML = "";
      const head = document.createElement("div");
      head.className = "gv-phead";
      head.innerHTML = `<span>Layers</span><span class="gv-count">${this.layers().length}</span>`;
      p.appendChild(head);

      const quick = document.createElement("div");
      quick.className = "gv-quick";
      p.appendChild(quick);
      const button = (label, title, handler) => {
        const b = document.createElement("button");
        b.className = "gv-btn";
        b.type = "button";
        b.textContent = label;
        b.title = title;
        b.addEventListener("click", handler);
        quick.appendChild(b);
      };
      button("All", "Show every layer in both", () => this.setAll("all"));
      button("Drawing", "Only the layers with unique geometry, in both",
             () => this.setAll("drawing"));
      button("None", "Hide every layer in both", () => this.setAll("none"));

      const search = document.createElement("input");
      search.type = "search";
      search.className = "gv-search";
      search.placeholder = "Filter layers…";
      search.value = this.filter;
      // The filter changes which rows are listed and nothing else: a layer hidden by
      // the search keeps whatever visibility it had.
      search.addEventListener("input", () => {
        this.filter = search.value.trim().toLowerCase();
        this.renderRows();
      });
      p.appendChild(search);

      this.rows = document.createElement("div");
      this.rows.className = "gv-layers";
      p.appendChild(this.rows);

      const note = document.createElement("div");
      note.className = "gv-hint";
      note.style.padding = "8px 10px";
      note.innerHTML = "One list, both drawings. Zoom, pan and rulers stay separate.";
      p.appendChild(note);

      this.renderRows();
    }

    renderRows() {
      this.rows.innerHTML = "";
      for (const entry of this.layers()) {
        if (this.filter && !entry.name.toLowerCase().includes(this.filter) &&
            !entry.key.includes(this.filter)) continue;
        const row = document.createElement("div");
        row.className = "gv-lrow";
        row.dataset.key = entry.key;

        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = this.isOn(entry);
        box.addEventListener("change", () => { this.set(entry, box.checked); this.apply(); });
        row.appendChild(box);

        const swatch = document.createElement("input");
        swatch.type = "color";
        swatch.className = "gv-sw";
        swatch.value = /^#[0-9a-f]{6}$/i.test(entry.colour || "") ? entry.colour : "#8aa0b6";
        swatch.title = `${entry.name} colour, from the layer map`;
        swatch.addEventListener("input", () => {
          for (const { side, viewer } of this.halves) {
            const name = entry.sides[side];
            const layer = name && viewer.A.layers.find((l) => l.name === name);
            if (layer) layer.colour = swatch.value;
          }
          entry.colour = swatch.value;
          this.apply();
        });
        swatch.addEventListener("click", (e) => e.stopPropagation());
        row.appendChild(swatch);

        const name = document.createElement("span");
        name.className = "gv-lname";
        name.textContent = entry.name;
        const only = !entry.sides.a ? " — only in B" : !entry.sides.b ? " — only in A" : "";
        name.title = `${entry.key} · ${entry.count} shape(s)${only}`;
        row.appendChild(name);

        const key = document.createElement("span");
        key.className = "gv-ld";
        key.textContent = entry.key;
        row.appendChild(key);

        const mark = document.createElement("span");
        mark.className = "gv-n";
        mark.textContent = only ? (entry.sides.a ? "A" : "B") : "";
        mark.title = only.trim();
        row.appendChild(mark);

        this.rows.appendChild(row);
      }
    }
  }

  NS.DualViewer = DualViewer;
  NS.mountDual = function (rootId, payload, options) {
    const root = document.getElementById(rootId);
    if (!root) throw new Error("viewer root not found: " + rootId);
    const dual = new DualViewer(root, payload, options);
    NS.instances = NS.instances || {};
    NS.instances[rootId] = dual;
    return dual;
  };

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
