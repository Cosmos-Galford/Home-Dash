"use strict";
// Customize: drag/resize layout + themes. Purely client-side (saved in this browser's localStorage).

(function () {
  const COLS = 24, ROWS = 24;           // layout grid; positions are stored in cells so they scale with the screen
  const MIN_W = 2, MIN_H = 1;
  const LS_THEME = "dash.theme";
  const LS_LAYOUT = "dash.layout.v1";

  // Defaults: [x, y, w, h] in grid cells
  const DEFAULTS = {
    landscape: {
      clock:     [0, 0, 8, 5],
      toolbar:   [8, 0, 4, 3],
      alarms:    [8, 3, 4, 4],
      cal:       [0, 5, 8, 19],
      agenda:    [12, 0, 12, 10],
      reminders: [12, 10, 12, 8],
      texts:     [12, 18, 12, 6],
    },
    portrait: {
      clock:     [0, 0, 14, 3],
      toolbar:   [14, 0, 10, 3],
      alarms:    [0, 3, 24, 3],
      cal:       [0, 6, 24, 8],
      agenda:    [0, 14, 24, 5],
      reminders: [0, 19, 24, 3],
      texts:     [0, 22, 24, 2],
    },
  };
  DEFAULTS.landscape.cal = [0, 5, 12, 19];
  DEFAULTS.landscape.clock = [0, 0, 7, 5];
  DEFAULTS.landscape.toolbar = [7, 0, 5, 3];
  DEFAULTS.landscape.alarms = [7, 3, 5, 2];

  const THEMES = [
    { id: "default",  name: "Default",         sw: ["#0b0e14", "#141a24", "#5ab0ff"] },
    { id: "fall",     name: "Fall",            sw: ["#2a1608", "#46260f", "#ee8a2e"] },
    { id: "barbie",   name: "Barbie",          sw: ["#ffd9ec", "#fff0f8", "#e0218a"] },
    { id: "aero",     name: "Smoke Black Aero", sw: ["#05070a", "#2a3340", "#7fd0ff"] },
    { id: "swiss",    name: "Swiss",           sw: ["#ffffff", "#f1f1f1", "#e30613"] },
    { id: "cyberpunk",name: "Cyberpunk",       sw: ["#0a0212", "#1a0630", "#00f0ff"] },
  ];

  const $ = (id) => document.getElementById(id);
  const canvas = $("canvas");
  const widgets = [...canvas.querySelectorAll(".widget")];
  let editing = false;

  function store(key, val) { try { val === null ? localStorage.removeItem(key) : localStorage.setItem(key, val); } catch (e) { /* storage blocked */ } }
  function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  // ---- themes ---------------------------------------------------------------
  function setTheme(id) {
    if (id === "default") { delete document.documentElement.dataset.theme; store(LS_THEME, null); }
    else { document.documentElement.dataset.theme = id; store(LS_THEME, id); }
    renderThemes();
  }
  function renderThemes() {
    const cur = document.documentElement.dataset.theme || "default";
    const grid = $("theme-grid");
    grid.replaceChildren();
    for (const t of THEMES) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "theme-card" + (t.id === cur ? " active" : "");
      const sw = document.createElement("span");
      sw.className = "theme-sw";
      for (const c of t.sw) { const i = document.createElement("i"); i.style.background = c; sw.append(i); }
      const n = document.createElement("span");
      n.textContent = t.name;
      b.append(sw, n);
      b.addEventListener("click", () => setTheme(t.id));
      grid.append(b);
    }
  }

  // ---- layout ---------------------------------------------------------------
  const orient = () => (window.matchMedia("(max-aspect-ratio: 1/1)").matches ? "portrait" : "landscape");

  function readSaved() {
    try { return JSON.parse(load(LS_LAYOUT) || "{}") || {}; } catch (e) { return {}; }
  }
  function currentLayout() {
    const saved = readSaved()[orient()] || {};
    const out = {};
    for (const [id, d] of Object.entries(DEFAULTS[orient()])) out[id] = sane(saved[id]) || d.slice();
    return out;
  }
  function sane(r) {
    if (!Array.isArray(r) || r.length !== 4 || !r.every(Number.isFinite)) return null;
    let [x, y, w, h] = r.map(Math.round);
    w = Math.max(MIN_W, Math.min(COLS, w)); h = Math.max(MIN_H, Math.min(ROWS, h));
    x = Math.max(0, Math.min(COLS - w, x)); y = Math.max(0, Math.min(ROWS - h, y));
    return [x, y, w, h];
  }
  function saveLayout(layout) {
    const all = readSaved();
    all[orient()] = layout;
    store(LS_LAYOUT, JSON.stringify(all));
  }
  function place(node, r) {
    const [x, y, w, h] = r;
    node.style.left = (x / COLS * 100) + "%";
    node.style.top = (y / ROWS * 100) + "%";
    node.style.width = (w / COLS * 100) + "%";
    node.style.height = (h / ROWS * 100) + "%";
  }
  function applyLayout() {
    const layout = currentLayout();
    for (const w of widgets) place(w, layout[w.dataset.w]);
  }
  function resetLayout() {
    const all = readSaved();
    delete all[orient()];
    store(LS_LAYOUT, JSON.stringify(all));
    applyLayout();
  }

  // ---- drag + resize --------------------------------------------------------
  let drag = null;

  function onDown(e) {
    if (!editing) return;
    const w = e.target.closest(".widget");
    if (!w) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const layout = currentLayout();
    drag = {
      node: w, id: w.dataset.w,
      mode: e.target.closest(".resize-h") ? "resize" : "move",
      sx: e.clientX, sy: e.clientY,
      cw: rect.width / COLS, ch: rect.height / ROWS,
      start: layout[w.dataset.w].slice(),
      layout,
      pid: e.pointerId,
    };
    w.setPointerCapture(e.pointerId);
    w.classList.add("dragging");
    widgets.forEach((o) => { o.style.zIndex = o === w ? 20 : ""; });
  }
  function onMove(e) {
    if (!drag || e.pointerId !== drag.pid) return;
    const dx = Math.round((e.clientX - drag.sx) / drag.cw);
    const dy = Math.round((e.clientY - drag.sy) / drag.ch);
    let [x, y, w, h] = drag.start;
    if (drag.mode === "move") { x += dx; y += dy; }
    else { w += dx; h += dy; }
    const r = sane([x, y, w, h]);
    // keep position fixed while resizing (sane() may clamp it)
    if (drag.mode === "resize") { r[0] = drag.start[0]; r[1] = drag.start[1]; r[2] = Math.min(r[2], COLS - r[0]); r[3] = Math.min(r[3], ROWS - r[1]); }
    drag.layout[drag.id] = r;
    place(drag.node, r);
  }
  function onUp(e) {
    if (!drag || e.pointerId !== drag.pid) return;
    drag.node.classList.remove("dragging");
    saveLayout(drag.layout);
    drag = null;
  }

  for (const w of widgets) {
    const h = document.createElement("div");
    h.className = "resize-h";
    h.setAttribute("aria-hidden", "true");
    const tag = document.createElement("div");
    tag.className = "edit-tag";
    tag.textContent = w.dataset.label || w.dataset.w;
    w.append(tag, h);
    w.addEventListener("pointerdown", onDown);
    w.addEventListener("pointermove", onMove);
    w.addEventListener("pointerup", onUp);
    w.addEventListener("pointercancel", onUp);
  }
  // while editing, swallow clicks so buttons don't fire
  canvas.addEventListener("click", (e) => { if (editing) { e.preventDefault(); e.stopPropagation(); } }, true);

  function setEditing(on) {
    editing = on;
    document.body.classList.toggle("editing", on);
    $("edit-bar").hidden = !on;
    if (!on) widgets.forEach((o) => { o.style.zIndex = ""; });
  }

  // ---- wiring ---------------------------------------------------------------
  $("custom-btn").addEventListener("click", () => { renderThemes(); $("custom").showModal(); });
  $("custom-close").addEventListener("click", () => $("custom").close());
  $("layout-edit").addEventListener("click", () => { $("custom").close(); setEditing(true); });
  $("layout-reset").addEventListener("click", resetLayout);
  $("edit-reset").addEventListener("click", resetLayout);
  $("edit-done").addEventListener("click", () => setEditing(false));
  window.matchMedia("(max-aspect-ratio: 1/1)").addEventListener("change", applyLayout);

  applyLayout();
  renderThemes();
})();
