"use strict";

// ---- settings ---------------------------------------------------------------
const WEEK_START = 0;            // 0 = Sunday, 1 = Monday
const REFRESH_MS = 60 * 1000;    // data refresh
const DUE_POLL_MS = 10 * 1000;   // how often to ask the server which alarms are about to ring
const AGENDA_DAYS = 14;          // how far ahead the agenda looks
const MAX_MONTHS_AHEAD = 2;      // calendar navigation limit
const NIGHT_START = 23;          // dim the screen from 23:00 ...
const NIGHT_END = 6;             // ... until 06:00
const LEAD_STEP = 15;            // alarm lead time moves in 15-minute steps
const MAX_LEAD = 24 * 60;
const SHIFT_MIN = 15;            // the earlier/later buttons on an alarm move it by this much...
const MAX_SHIFTS = 5;            // ...up to this many times away from its original time
const RING_GRACE_MS = 10 * 60 * 1000;   // an alarm missed by more than this stays silent
const TEXT_AUTOHIDE_MS = 60 * 1000;     // recent texts hide themselves again after this
const TEXT_REFRESH_MS = 15 * 1000;
const IDLE_OPTIONS = [0, 1, 2, 5, 10, 15, 30];
const SNOOZE_OPTIONS = [5, 10, 15];
const FLASH_OPTIONS = ["off", "gentle", "strong"];
const FLASH_LABELS = { off: "Off", gentle: "Gentle", strong: "Strong" };
const QUICK_REPLIES = ["OK", "Thanks!", "On my way", "Can't talk now, I'll call you later"];

// ---- helpers ----------------------------------------------------------------
// SECURITY: message-derived text is untrusted. Only ever use textContent.
const $ = (id) => document.getElementById(id);
const pad2 = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const parseISO = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
// "2026-11-14T13:30:00" (no zone) is local time in JS, same as the Pi's clock.
const parseLocal = (s) => new Date(s);

// Defence in depth (the server strips these too): control characters, zero-width characters and
// bidi overrides can disguise text, e.g. make "moc.live" read as "evil.com".
const HIDDEN_CHARS = /[\u0000-\u001F\u007F-\u009F\u00AD\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F\uFEFF]/g;
const cleanDisplay = (s) => String(s == null ? "" : s).replace(HIDDEN_CHARS, "");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = cleanDisplay(text);   // never innerHTML
  return e;
}

function button(label, cls, onClick) {
  const b = el("button", cls, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function fmtTime(hhmm) {
  if (!hhmm) return "All day";
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function fmtClock(localIso) {
  return parseLocal(localIso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function shortDate(s) {
  return parseISO(s).toLocaleDateString([], { month: "short", day: "numeric" });
}

function dayLabel(s) {
  const now = new Date();
  if (s === iso(now)) return "Today";
  if (s === iso(addDays(now, 1))) return "Tomorrow";
  return parseISO(s).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

function fmtMinutes(m) {
  if (m === 0) return "0 min";
  const h = Math.floor(m / 60), r = m % 60;
  if (!h) return `${r} min`;
  return r ? `${h} hr ${r} min` : `${h} hr`;
}

function fmtLead(m) {
  return m === 0 ? "at start time" : `${fmtMinutes(m)} before`;
}

// MAP timestamps look like 20261114T080500
function parseMapTs(ts) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(ts || "");
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}

function fmtTextTime(ts) {
  const d = parseMapTs(ts);
  if (!d) return "";
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return iso(d) === iso(new Date()) ? t : `${shortDate(iso(d))}, ${t}`;
}

function ageText(createdSec) {
  const days = Math.floor((Date.now() / 1000 - createdSec) / 86400);
  if (days <= 0) return "today";
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

// ---- state ------------------------------------------------------------------
let state = { events: [], reminders: [], review: [], alarms: [] };
let session = {
  pin_set: false, pin_len: 0, lock_dashboard: false, unlocked: true, idle_lock_min: 2,
  texts_enabled: false, auto_alarms: true, default_lead: 60, snooze_min: 10, alarm_flash: "strong",
};
let viewMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
let selected = null; // "YYYY-MM-DD" or null

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || "HTTP " + status);
    this.status = status;
    this.detail = detail || "";
  }
}

async function api(path, method = "GET", body) {
  const opts = { method, headers: { "X-Dashboard": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new ApiError(r.status, data && typeof data.detail === "string" ? data.detail : "");
  return data;
}

// Like api(), but if the session has expired it asks for the PIN and retries once.
async function call(path, method, body) {
  try {
    return await api(path, method, body);
  } catch (e) {
    if (e.status === 401 && session.pin_set) {
      session.unlocked = false;
      if (await ensureUnlocked("Enter PIN", !session.lock_dashboard)) return api(path, method, body);
    }
    throw e;
  }
}

function applySession(s) {
  if (!s) return;
  session = { ...session, ...s };
  $("lock-btn").hidden = !session.pin_set;
  if (!session.texts_enabled) hideTexts();
  renderTexts();
}

async function loadState() {
  const s = await call("/api/state");
  state = { events: s.events, reminders: s.reminders, review: s.review, alarms: s.alarms || [] };
  applySession(s.session);
}

async function refresh() {
  try {
    await loadState();
    $("status").className = "status online";
  } catch (e) {
    // A 401 just means the PIN screen was dismissed or is waiting; that isn't "offline".
    $("status").className = e.status === 401 ? "status online" : "status offline";
  }
  renderAll();
}

async function act(path, method, body) {
  try { await call(path, method, body); } catch (e) { /* refresh below shows real state */ }
  await refresh();
}

// ---- PIN keypad -------------------------------------------------------------
let padCtx = null;
let lockPromise = null;

function buildPad() {
  const grid = $("pad-grid");
  for (const k of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "back", "0", "ok"]) {
    grid.append(button(k === "back" ? "Del" : k === "ok" ? "OK" : k, "key", () => padKey(k)));
  }
  $("pad-cancel").addEventListener("click", () => padDone(null));
  const dlg = $("pad");
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); if (padCtx && padCtx.cancellable) padDone(null); });
  // A mandatory lock screen must never be closable (some browsers allow Esc to force-close).
  dlg.addEventListener("close", () => { if (padCtx && !padCtx.cancellable) dlg.showModal(); });
}

function drawDots() {
  const c = padCtx;
  const box = $("pad-dots");
  box.replaceChildren();
  const n = Math.max(c ? c.autoLen : 0, c ? c.value.length : 0, 4);
  for (let i = 0; i < n; i++) box.append(el("span", "pdot" + (c && i < c.value.length ? " filled" : "")));
}

// Resolves with the digits entered, or null if cancelled. The keypad stays open until closePad().
function askPin(title, { cancellable = true, autoLen = 0, message = "" } = {}) {
  return new Promise((resolve) => {
    padCtx = { value: "", autoLen, cancellable, resolve };
    $("pad-title").textContent = title;
    $("pad-msg").textContent = message;
    $("pad-cancel").hidden = !cancellable;
    drawDots();
    if (!$("pad").open) $("pad").showModal();
  });
}

function padDone(value) {
  const c = padCtx;
  if (!c) return;
  padCtx = null;
  c.resolve(value);
}

function closePad() {
  padCtx = null;
  if ($("pad").open) $("pad").close();
}

function padKey(k) {
  const c = padCtx;
  if (!c) return;
  if (k === "back") c.value = c.value.slice(0, -1);
  else if (k === "ok") {
    if (c.value.length >= 4) return padDone(c.value);
    $("pad-msg").textContent = "PINs have at least 4 digits";
  } else if (c.value.length < 8) {
    c.value += k;
    if (c.autoLen && c.value.length === c.autoLen) return padDone(c.value);
  }
  drawDots();
}

document.addEventListener("keydown", (e) => {
  if (!padCtx) return;
  if (/^[0-9]$/.test(e.key)) padKey(e.key);
  else if (e.key === "Backspace") padKey("back");
  else if (e.key === "Enter") padKey("ok");
});

// Make sure the session is unlocked (asks for the PIN if not). Resolves true/false.
function ensureUnlocked(title = "Enter PIN", cancellable = true) {
  if (!session.pin_set || session.unlocked) return Promise.resolve(true);
  if (lockPromise) return lockPromise;
  lockPromise = (async () => {
    let message = "";
    try {
      for (;;) {
        const pin = await askPin(title, { cancellable, autoLen: session.pin_len, message });
        if (pin === null) return false;
        try {
          applySession({ ...(await api("/api/unlock", "POST", { pin })), unlocked: true });
          return true;
        } catch (e) {
          message = e.detail || "Wrong PIN";
        }
      }
    } finally {
      closePad();
      lockPromise = null;
    }
  })();
  return lockPromise;
}

async function lockNow() {
  try { await api("/api/lock", "POST"); } catch (e) { /* ignore */ }
  session.unlocked = false;
  hideTexts();
  for (const id of ["settings", "reply", "add", "review"]) if ($(id).open) $(id).close();
  if (session.lock_dashboard) {
    state = { events: [], reminders: [], review: [], alarms: [] };
    renderAll();
    await ensureUnlocked("Enter PIN", false);
    await refresh();
  } else {
    renderAll();
  }
}

// ---- idle auto-lock ---------------------------------------------------------
let lastActivity = Date.now();
let lastPing = 0;

function noteActivity() {
  lastActivity = Date.now();
  scheduleTextHide();
  if (session.pin_set && session.unlocked && lastActivity - lastPing > 20000) {
    lastPing = lastActivity;
    api("/api/activity", "POST").catch(() => {});
  }
}

function idleCheck() {
  const mins = session.idle_lock_min;
  if (session.pin_set && session.unlocked && mins > 0 && Date.now() - lastActivity > mins * 60000) lockNow();
}

// ---- clock ------------------------------------------------------------------
function tick() {
  const now = new Date();
  $("time").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("date").textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  const h = now.getHours();
  // never dim the screen while an alarm is going off
  document.body.classList.toggle("night", (h >= NIGHT_START || h < NIGHT_END) && !ringing);
  checkRing();
}

// ---- calendar ---------------------------------------------------------------
function eventsByDate() {
  const map = new Map();
  for (const ev of state.events) {
    if (!map.has(ev.date)) map.set(ev.date, []);
    map.get(ev.date).push(ev);
  }
  return map;
}

function renderMonth() {
  const dow = $("dow");
  dow.replaceChildren();
  for (let i = 0; i < 7; i++) {
    const d = new Date(2023, 0, 1 + ((i + WEEK_START) % 7)); // 2023-01-01 is a Sunday
    dow.append(el("div", null, d.toLocaleDateString([], { weekday: "short" })));
  }

  $("month").textContent = viewMonth.toLocaleDateString([], { month: "long", year: "numeric" });
  const thisMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  $("prev").disabled = viewMonth <= thisMonth;
  $("next").disabled = viewMonth >= new Date(thisMonth.getFullYear(), thisMonth.getMonth() + MAX_MONTHS_AHEAD, 1);

  const byDate = eventsByDate();
  const todayStr = iso(new Date());
  const offset = (viewMonth.getDay() - WEEK_START + 7) % 7;
  const days = $("days");
  days.replaceChildren();

  for (let i = 0; i < 42; i++) {
    const d = new Date(viewMonth.getFullYear(), viewMonth.getMonth(), 1 - offset + i);
    const key = iso(d);
    const cell = el("button", "day");
    cell.type = "button";
    if (d.getMonth() !== viewMonth.getMonth()) cell.classList.add("other");
    if (key === todayStr) cell.classList.add("today");
    if (key === selected) cell.classList.add("selected");
    cell.append(el("span", "num", String(d.getDate())));

    const evs = byDate.get(key) || [];
    if (evs.length) {
      const dots = el("span", "dots");
      for (const ev of evs.slice(0, 3)) {
        const dot = el("span", "dot");
        if (ev.needs_review) dot.classList.add("review");
        else if (ev.urgency === "high") dot.classList.add("high");
        else if (ev.urgency === "low") dot.classList.add("low");
        dots.append(dot);
      }
      cell.append(dots);
    }
    cell.addEventListener("click", () => {
      selected = selected === key ? null : key;
      renderMonth();
      renderAgenda();
    });
    days.append(cell);
  }
}

// ---- agenda (with the alarm controls for each appointment) ------------------
function alarmByEvent() {
  const map = new Map();
  for (const a of state.alarms) if (a.event_id != null) map.set(a.event_id, a);
  return map;
}

function eventStart(ev) {
  return new Date(`${ev.date}T${ev.time}:00`);
}

async function shiftAlarm(a, dir) {
  try {
    await call(`/api/alarms/${a.id}/shift`, "POST", { steps: dir });
  } catch (e) {
    toast(e.detail || "Couldn't move the alarm");
  }
  await refresh();
}

// "15 min earlier" / "15 min later" buttons, shared by the Alarms panel and each appointment.
// At most MAX_SHIFTS presses away from the alarm's original time (the server enforces it too).
function shiftButtons(a) {
  const steps = a.shift_steps || 0;
  const earlier = button(`${SHIFT_MIN} min earlier`, "mini", () => shiftAlarm(a, -1));
  const later = button(`${SHIFT_MIN} min later`, "mini", () => shiftAlarm(a, +1));
  earlier.disabled = steps - 1 < -MAX_SHIFTS || parseLocal(a.fire_at).getTime() - SHIFT_MIN * 60000 <= Date.now();
  later.disabled = steps + 1 > MAX_SHIFTS || (a.event_id != null && a.lead_minutes - SHIFT_MIN < 0);
  const tip = `Moves the alarm ${SHIFT_MIN} minutes, up to ${MAX_SHIFTS} times`;
  earlier.title = later.title = tip;
  return [earlier, later];
}

function movedNote(a) {
  const steps = a.shift_steps || 0;
  return steps ? ` \u00b7 moved ${Math.abs(steps) * SHIFT_MIN} min ${steps < 0 ? "earlier" : "later"}` : "";
}

async function createEventAlarm(ev) {
  try {
    await call(`/api/events/${ev.id}/alarm`, "POST", {});
  } catch (e) {
    toast(e.detail || "Couldn't set the alarm");
  }
  await refresh();
}

function alarmRow(ev, alarms) {
  if (!ev.time || ev.needs_review) return null;
  const a = alarms.get(ev.id);
  const start = eventStart(ev).getTime();
  const row = el("div", "alarm-row");
  if (!a) {
    if (start <= Date.now()) return null;
    row.append(button("+ Alarm", "mini", () => createEventAlarm(ev)));
    return row;
  }
  if (a.acked) {
    row.append(el("span", "alarm-txt muted", "Alarm done"));
    return row;
  }
  const missed = parseLocal(a.trigger).getTime() + RING_GRACE_MS < Date.now();
  row.append(el("span", "alarm-txt" + (missed ? " muted" : ""),
    missed ? "Alarm missed" : `Alarm ${fmtClock(a.fire_at)} \u00b7 ${fmtLead(a.lead_minutes)}${movedNote(a)}`));
  if (!missed) row.append(...shiftButtons(a));
  row.append(button("Off", "mini", () => act(`/api/alarms/${a.id}`, "DELETE")));
  return row;
}

function renderAgenda() {
  const list = $("agenda");
  list.replaceChildren();
  const todayStr = iso(new Date());
  const lastStr = iso(addDays(new Date(), AGENDA_DAYS));
  const alarms = alarmByEvent();

  const evs = selected
    ? state.events.filter((e) => e.date === selected)
    : state.events.filter((e) => e.date >= todayStr && e.date <= lastStr);

  $("agenda-title").textContent = selected ? dayLabel(selected) : "Coming up";
  $("show-all").hidden = !selected;

  if (!evs.length) {
    list.append(el("li", "empty", selected ? "Nothing on this day." : "Nothing scheduled."));
    return;
  }

  let currentDay = null;
  for (const ev of evs) {
    if (!selected && ev.date !== currentDay) {
      currentDay = ev.date;
      list.append(el("li", "day-label", dayLabel(ev.date)));
    }
    const li = el("li", "item " + (ev.urgency || "normal"));
    const body = el("div", "body");
    const title = el("div", "title", ev.title);
    if (ev.needs_review) title.append(el("span", "flag", "?"));
    body.append(title);
    if (ev.details) body.append(el("div", "sub", ev.details));
    const row = alarmRow(ev, alarms);
    if (row) body.append(row);
    li.append(body, el("div", "when", fmtTime(ev.time)));
    list.append(li);
  }
}

// ---- alarms panel -----------------------------------------------------------
function renderAlarms() {
  const open = state.alarms
    .filter((a) => !a.acked)
    .sort((a, b) => (a.trigger < b.trigger ? -1 : 1));
  $("alarms-panel").hidden = open.length === 0;
  const list = $("alarms");
  list.replaceChildren();
  for (const a of open.slice(0, 4)) {
    const missed = parseLocal(a.trigger).getTime() + RING_GRACE_MS < Date.now();
    const li = el("li", "item" + (missed ? " low" : ""));
    const body = el("div", "body");
    body.append(el("div", "title", a.label));
    let sub = `${dayLabel(a.trigger.slice(0, 10))}, ${fmtClock(a.trigger)}`;
    if (a.event_id != null) sub += ` \u00b7 ${fmtLead(a.lead_minutes)}`;
    if (missed) sub = "Missed \u00b7 " + sub;
    else sub += movedNote(a);
    body.append(el("div", "sub", sub));
    if (!missed) {
      const row = el("div", "alarm-row");
      row.append(...shiftButtons(a));
      body.append(row);
    }
    li.append(body, button(missed ? "Clear" : "Cancel", "chip", () => act(`/api/alarms/${a.id}`, "DELETE")));
    list.append(li);
  }
}

// ---- ringing ----------------------------------------------------------------
let due = [];
let ringing = null;
const handled = new Set();   // "id|trigger" pairs already dismissed/snoozed here
let audioCtx = null;
let beepTimer = null;

function audio() {
  if (!audioCtx) {
    const C = window.AudioContext || window.webkitAudioContext;
    if (!C) return null;
    audioCtx = new C();
  }
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

function beepOnce() {
  const ctx = audio();
  if (!ctx || ctx.state !== "running") { $("ring-hint").hidden = false; return false; }
  $("ring-hint").hidden = true;
  const t = ctx.currentTime;
  for (let i = 0; i < 3; i++) {
    const at = t + i * 0.25;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "square";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.25, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(at);
    osc.stop(at + 0.2);
  }
  return true;
}

function ringKey(a) { return `${a.id}|${a.trigger}`; }

function checkRing() {
  if (ringing) return;
  const now = Date.now();
  const a = due.find((x) => !handled.has(ringKey(x)) && parseLocal(x.trigger).getTime() <= now);
  if (a) startRinging(a);
}

function setFlashClass() {
  const ring = $("ring");
  ring.classList.remove("flash-strong", "flash-gentle", "flash-off");
  ring.classList.add("flash-" + (FLASH_OPTIONS.includes(session.alarm_flash) ? session.alarm_flash : "strong"));
}

let previewTimer = null;
function previewRing() {
  if (ringing) return;
  setFlashClass();
  $("ring-label").textContent = "Test alarm";
  $("ring-sub").textContent = "This is how an alarm looks and sounds";
  $("ring-snooze").textContent = "Snooze";
  $("ring-hint").hidden = true;
  if (!$("ring").open) $("ring").showModal();
  beepOnce();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => { if (!ringing && $("ring").open) $("ring").close(); }, 4000);
}

function startRinging(a) {
  ringing = a;
  document.body.classList.remove("night");
  setFlashClass();
  $("ring-label").textContent = a.label;
  let sub = "";
  if (a.event_time) sub = `Starts at ${fmtTime(a.event_time)}` + (a.lead_minutes ? ` \u00b7 ${fmtLead(a.lead_minutes)}` : "");
  else sub = fmtClock(a.fire_at);
  $("ring-sub").textContent = sub;
  $("ring-snooze").textContent = `Snooze ${session.snooze_min} min`;
  $("ring-hint").hidden = true;
  if (!$("ring").open) $("ring").showModal();
  beepOnce();
  clearInterval(beepTimer);
  beepTimer = setInterval(beepOnce, 1500);
}

function stopRinging() {
  clearInterval(beepTimer);
  beepTimer = null;
  ringing = null;
  if ($("ring").open) $("ring").close();
}

async function finishRing(path, body) {
  const a = ringing;
  if (!a) {   // a settings preview: just close it
    clearTimeout(previewTimer);
    if ($("ring").open) $("ring").close();
    return;
  }
  handled.add(ringKey(a));
  stopRinging();
  due = due.filter((x) => x.id !== a.id);
  try { await api(path.replace("{id}", a.id), "POST", body); } catch (e) { /* the next poll shows the truth */ }
  await pollDue();
  refresh();
}

async function pollDue() {
  try { due = (await api("/api/alarms/due")).alarms || []; } catch (e) { /* offline: keep what we have */ }
  checkRing();
}

// ---- reminders --------------------------------------------------------------
function dueInfo(r) {
  if (!r.due_date) return { text: "", overdue: false };
  const todayStr = iso(new Date());
  if (r.due_date < todayStr) return { text: "Overdue \u00b7 " + shortDate(r.due_date), overdue: true };
  if (r.due_date === todayStr) return { text: "Due today", overdue: false };
  return { text: "Due " + shortDate(r.due_date), overdue: false };
}

function renderReminders() {
  const list = $("reminders");
  list.replaceChildren();
  if (!state.reminders.length) {
    list.append(el("li", "empty", "All caught up."));
  }
  for (const r of state.reminders) {
    const pickup = r.category === "pickup";
    const li = el("li", "item " + (r.urgency || "normal"));
    const body = el("div", "body");
    const title = el("div", "title", r.title);
    if (r.needs_review) title.append(el("span", "flag", "?"));
    body.append(title);
    const due = dueInfo(r);
    if (due.text) body.append(el("div", "sub" + (due.overdue ? " overdue" : ""), due.text));
    if (pickup && r.created_at) body.append(el("div", "sub", `Text received ${ageText(r.created_at)}`));
    if (r.details) body.append(el("div", "sub", r.details));
    const label = pickup ? "Picked up" : "Done";
    li.append(body, button(label, "chip ok", async () => {
      await act(`/api/reminders/${r.id}/done`, "POST", { done: true });
      toast(pickup ? "Marked picked up" : "Marked done", "Undo", () => act(`/api/reminders/${r.id}/done`, "POST", { done: false }));
    }));
    list.append(li);
  }

  const n = state.review.length;
  const btn = $("review-btn");
  btn.hidden = n === 0;
  btn.textContent = `${n} to review`;
}

// ---- review dialog ----------------------------------------------------------
function renderReview() {
  const list = $("review-list");
  list.replaceChildren();
  if (!state.review.length) {
    list.append(el("li", "empty", "Nothing to review."));
    if ($("review").open) $("review").close();
    return;
  }
  for (const it of state.review) {
    const li = el("li", "item " + (it.urgency || "normal"));
    const body = el("div", "body");
    body.append(el("div", "title", it.title));
    const kind = it.kind === "events" ? "Event" : "Reminder";
    body.append(el("div", "sub", it.day ? `${kind} \u00b7 ${shortDate(it.day)}` : kind));
    if (it.details) body.append(el("div", "sub", it.details));
    const actions = el("div", "actions");
    actions.append(
      button("Keep", "chip ok", () => act(`/api/${it.kind}/${it.id}/confirm`, "POST")),
      button("Delete", "chip danger", () => act(`/api/${it.kind}/${it.id}`, "DELETE")),
    );
    li.append(body, actions);
    list.append(li);
  }
}

// ---- links in texts ---------------------------------------------------------
// Links are drawn as plain buttons, never as <a href>: nothing can be prefetched, previewed or
// followed by accident. Tapping one only opens a confirmation dialog; the page is not contacted
// until you press "Open" there. (The page's CSP also blocks it from loading anything external.)
const LINK_RE = /\b(?:https?:\/\/|www\.)[^\s<>"'`]+/gi;
const SHORTENERS = new Set(["bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "cutt.ly", "rb.gy",
  "buff.ly", "tiny.cc", "shorturl.at", "t.ly", "lnkd.in", "s.id"]);
let pendingLink = null;

function parseLink(raw) {
  const text = raw.replace(/[.,;:!?)\]}'"]+$/, "");   // trailing punctuation belongs to the sentence
  let u;
  try { u = new URL(/^www\./i.test(text) ? "https://" + text : text); } catch (e) { return null; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  const host = u.hostname.toLowerCase();
  const warnings = [];
  let blocked = false;
  if (u.username || u.password) {
    blocked = true;
    warnings.push("This address contains a username/password section. Scammers use that to make a link look like a trusted site. It cannot be opened from here.");
  }
  if (host.split(".").some((p) => p.startsWith("xn--"))) warnings.push("The web address uses special characters that can imitate a real site (a look-alike).");
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.startsWith("[")) warnings.push("It points to a raw number address instead of a named website.");
  if (SHORTENERS.has(host)) warnings.push("This is a shortened link, so the real destination is hidden.");
  if (u.protocol === "http:") warnings.push("This link is not encrypted (http, not https).");
  if (u.port) warnings.push(`It uses an unusual port (${u.port}).`);
  return { text, url: u.href, host, warnings, blocked };
}

function linkButton(info) {
  const shown = info.text.length > 42 ? info.text.slice(0, 41) + "\u2026" : info.text;
  const b = el("button", "linkbtn", shown);
  b.type = "button";
  b.title = "Link: asks before opening";
  b.addEventListener("click", (e) => { e.stopPropagation(); openLinkDialog(info); });
  return b;
}

// Fills `parent` with the text, turning anything that looks like a web address into a link button.
function linkify(parent, text) {
  const t = cleanDisplay(text);
  let last = 0, m;
  LINK_RE.lastIndex = 0;
  while ((m = LINK_RE.exec(t))) {
    const info = parseLink(m[0]);
    if (!info) continue;
    if (m.index > last) parent.append(document.createTextNode(t.slice(last, m.index)));
    parent.append(linkButton(info));
    last = m.index + info.text.length;
    LINK_RE.lastIndex = last;
  }
  if (last < t.length) parent.append(document.createTextNode(t.slice(last)));
}

function openLinkDialog(info) {
  pendingLink = info;
  $("link-host").textContent = info.host;
  $("link-url").textContent = info.url;
  $("link-msg").textContent = "";
  const list = $("link-warn");
  list.replaceChildren();
  for (const w of info.warnings) list.append(el("li", null, w));
  $("link-open").disabled = info.blocked;
  $("link-confirm").showModal();
}

function openPendingLink() {
  const info = pendingLink;
  if (!info || info.blocked) return;
  $("link-confirm").close();
  pendingLink = null;
  hideTexts();
  // noopener + noreferrer: the site learns nothing about this dashboard and can't reach back into it.
  window.open(info.url, "_blank", "noopener,noreferrer");
}

async function copyPendingLink() {
  if (!pendingLink) return;
  try {
    await navigator.clipboard.writeText(pendingLink.url);
    $("link-msg").textContent = "Copied. Nothing has been opened.";
  } catch (e) {
    $("link-msg").textContent = "Couldn't copy here. Select the address above instead.";
  }
}

// ---- recent texts -----------------------------------------------------------
let textsVisible = false;
let textsData = { items: [], replies: [] };
let textsTimer = null;
let textsHideTimer = null;
let replyTarget = null;

function scheduleTextHide() {
  clearTimeout(textsHideTimer);
  if (textsVisible) textsHideTimer = setTimeout(hideTexts, TEXT_AUTOHIDE_MS);
}

function hideTexts() {
  textsVisible = false;
  textsData = { items: [], replies: [] };   // don't keep message text in memory while hidden
  clearTimeout(textsHideTimer);
  clearInterval(textsTimer);
  textsTimer = null;
  renderTexts();
}

async function loadTexts() {
  try {
    textsData = await call("/api/texts");
  } catch (e) {
    if (e.status === 403) { session.texts_enabled = false; hideTexts(); }
    else if (e.status === 401) hideTexts();
  }
  renderTexts();
}

async function toggleTexts() {
  if (textsVisible) { hideTexts(); return; }
  if (!(await ensureUnlocked("Enter PIN to view texts"))) return;
  textsVisible = true;
  await loadTexts();
  clearInterval(textsTimer);
  textsTimer = setInterval(loadTexts, TEXT_REFRESH_MS);
  scheduleTextHide();
}

function replyStatus(r) {
  if (r.status === "sent") return "Sent";
  if (r.status === "failed") return "Not sent" + (r.error ? ` (${r.error})` : "");
  return "Sending\u2026";
}

function renderTexts() {
  const panel = $("texts-panel");
  panel.hidden = !session.texts_enabled;
  panel.classList.toggle("open", textsVisible);
  $("texts-toggle").textContent = textsVisible ? "Hide" : "Show";
  const list = $("texts");
  list.replaceChildren();
  if (!session.texts_enabled || !textsVisible) return;
  if (!textsData.items.length) {
    list.append(el("li", "empty", "No recent texts."));
    return;
  }
  for (const t of textsData.items) {
    const li = el("li", "item text" + (t.is_read ? "" : " unread"));
    const body = el("div", "body");
    const head = el("div", "title", t.sender || "Unknown");
    head.append(el("span", "ts", fmtTextTime(t.ts)));
    const msg = el("div", "sub msg");
    linkify(msg, t.body);
    body.append(head, msg);
    for (const r of textsData.replies.filter((x) => x.text_uid === t.uid).reverse()) {
      body.append(el("div", "sub mine" + (r.status === "failed" ? " overdue" : ""), `You: ${r.body}  \u00b7 ${replyStatus(r)}`));
    }
    li.append(body);
    if (t.can_reply) li.append(button("Reply", "chip", () => openReply(t)));
    list.append(li);
  }
}

function openReply(t) {
  replyTarget = t;
  $("reply-to").textContent = `Reply to ${t.sender || "this number"}`;
  $("reply-orig").textContent = t.body;
  $("reply-text").value = "";
  $("reply-error").textContent = "";
  const quick = $("reply-quick");
  quick.replaceChildren();
  for (const q of QUICK_REPLIES) quick.append(button(q, "chip", () => { $("reply-text").value = q; }));
  $("reply").showModal();
}

async function sendReply() {
  const text = $("reply-text").value.trim();
  if (!text) { $("reply-error").textContent = "Write a message or tap a quick reply."; return; }
  $("reply-send").disabled = true;
  try {
    await call(`/api/texts/${replyTarget.uid}/reply`, "POST", { body: text });
    $("reply").close();
    toast("Sending reply\u2026");
    watchReplies();
  } catch (e) {
    $("reply-error").textContent = e.detail || "Couldn't queue the reply";
  } finally {
    $("reply-send").disabled = false;
  }
}

// After sending, check every few seconds until the reply is no longer "sending".
let watchTimer = null;
function watchReplies() {
  clearInterval(watchTimer);
  let n = 0;
  watchTimer = setInterval(async () => {
    n += 1;
    await loadTexts();
    const pending = textsData.replies.some((r) => r.status === "queued" || r.status === "sending");
    if (!pending || n > 20 || !textsVisible) {
      clearInterval(watchTimer);
      const last = textsData.replies[0];
      if (last && last.status === "failed") toast("Reply failed. The phone may not allow sending from this device.");
      else if (last && last.status === "sent") toast("Reply sent");
    }
  }, 3000);
}

// ---- add reminder / alarm ---------------------------------------------------
let addKind = "reminder";

function setAddKind(kind) {
  addKind = kind;
  $("tab-reminder").classList.toggle("active", kind === "reminder");
  $("tab-alarm").classList.toggle("active", kind === "alarm");
  $("add-reminder-fields").hidden = kind !== "reminder";
  $("add-alarm-fields").hidden = kind !== "alarm";
  $("add-error").textContent = "";
}

function openAdd() {
  for (const id of ["add-label", "add-due", "add-note", "add-time"]) $(id).value = "";
  $("add-date").value = iso(new Date());
  setAddKind("reminder");
  $("add").showModal();
}

async function saveAdd() {
  const label = $("add-label").value.trim();
  if (!label) { $("add-error").textContent = "Give it a label."; return; }
  try {
    if (addKind === "reminder") {
      await call("/api/reminders", "POST", {
        title: label, due_date: $("add-due").value || null, details: $("add-note").value.trim() || null,
      });
    } else {
      const date = $("add-date").value, time = $("add-time").value;
      if (!date || !time) { $("add-error").textContent = "Pick a date and a time."; return; }
      await call("/api/alarms", "POST", { label, date, time });
    }
  } catch (e) {
    $("add-error").textContent = e.detail || "Couldn't save that";
    return;
  }
  $("add").close();
  toast(addKind === "reminder" ? "Reminder added" : "Alarm set");
  await refresh();
  pollDue();
}

// ---- settings ---------------------------------------------------------------
function settingsError(msg) { $("settings-error").textContent = msg || ""; }

function setSwitch(id, on, disabled) {
  const b = $(id);
  b.classList.toggle("on", !!on);
  b.setAttribute("aria-checked", on ? "true" : "false");
  b.disabled = !!disabled;
}

function stepOption(list, current, dir) {
  const i = Math.max(0, list.indexOf(current));
  return list[Math.min(list.length - 1, Math.max(0, i + dir))];
}

function renderSettings() {
  const s = session;
  $("pin-state").textContent = s.pin_set ? "(set)" : "(not set)";
  $("pin-set").textContent = s.pin_set ? "Change" : "Set PIN";
  $("pin-remove").hidden = !s.pin_set;
  setSwitch("sw-lock", s.lock_dashboard, !s.pin_set);
  setSwitch("sw-texts", s.texts_enabled, !s.pin_set);
  setSwitch("sw-auto", s.auto_alarms, false);
  $("idle-val").textContent = s.idle_lock_min ? `${s.idle_lock_min} min` : "Never";
  $("idle-dn").disabled = !s.pin_set || s.idle_lock_min === IDLE_OPTIONS[0];
  $("idle-up").disabled = !s.pin_set || s.idle_lock_min === IDLE_OPTIONS[IDLE_OPTIONS.length - 1];
  $("lead-val").textContent = fmtLead(s.default_lead);
  $("lead-dn").disabled = s.default_lead <= 0;
  $("lead-up").disabled = s.default_lead >= MAX_LEAD;
  $("flash-val").textContent = FLASH_LABELS[s.alarm_flash] || "Strong";
  $("flash-dn").disabled = s.alarm_flash === FLASH_OPTIONS[0];
  $("flash-up").disabled = s.alarm_flash === FLASH_OPTIONS[FLASH_OPTIONS.length - 1];
  $("snooze-val").textContent = `${s.snooze_min} min`;
  $("snooze-dn").disabled = s.snooze_min === SNOOZE_OPTIONS[0];
  $("snooze-up").disabled = s.snooze_min === SNOOZE_OPTIONS[SNOOZE_OPTIONS.length - 1];
  $("texts-hint").textContent = s.pin_set
    ? "While this is on, the Pi keeps your recent texts (about 3 days) so they can be shown here after you enter the PIN. Turning it off deletes them."
    : "Set a PIN first. Recent texts are only available behind a PIN.";
}

async function setSetting(patch) {
  try {
    applySession(await api("/api/settings", "POST", patch));
    settingsError("");
  } catch (e) {
    if (e.status === 401) { session.unlocked = false; settingsError("PIN needed again. Close and reopen Settings."); }
    else settingsError(e.detail || "Couldn't save that setting");
  }
  renderSettings();
}

async function openSettings() {
  if (!(await ensureUnlocked("Enter PIN for settings"))) return;
  settingsError("");
  renderSettings();
  $("settings").showModal();
}

async function changePin() {
  settingsError("");
  try {
    let current = null;
    if (session.pin_set) {
      current = await askPin("Enter current PIN");
      if (current === null) return;
    }
    const first = await askPin(session.pin_set ? "New PIN (4 to 8 digits)" : "Choose a PIN (4 to 8 digits)");
    if (first === null) return;
    const again = await askPin("Enter it again");
    if (again === null) return;
    if (first !== again) { settingsError("Those two PINs didn't match. Nothing was changed."); return; }
    applySession({ ...(await api("/api/pin", "POST", { current, new: first })), unlocked: true });
    toast("PIN saved");
  } catch (e) {
    settingsError(e.detail || "Couldn't save the PIN");
  } finally {
    closePad();
    renderSettings();
  }
}

async function removePin() {
  settingsError("");
  try {
    const current = await askPin("Enter current PIN to remove it");
    if (current === null) return;
    applySession({ ...(await api("/api/pin/remove", "POST", { current })), unlocked: true });
    hideTexts();
    toast("PIN removed. Recent texts are off and deleted.");
  } catch (e) {
    settingsError(e.detail || "Couldn't remove the PIN");
  } finally {
    closePad();
    renderSettings();
  }
}

// ---- toast ------------------------------------------------------------------
let toastTimer = null;
function toast(message, actionLabel, onAction) {
  const t = $("toast");
  t.replaceChildren(el("span", null, message));
  if (actionLabel) {
    t.append(button(actionLabel, "link", async () => { t.hidden = true; await onAction(); }));
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---- wiring -----------------------------------------------------------------
function renderAll() {
  renderMonth();
  renderAgenda();
  renderAlarms();
  renderReminders();
  renderReview();
  renderTexts();
}

$("prev").addEventListener("click", () => {
  viewMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() - 1, 1);
  renderMonth();
});
$("next").addEventListener("click", () => {
  viewMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 1);
  renderMonth();
});
$("show-all").addEventListener("click", () => { selected = null; renderMonth(); renderAgenda(); });
$("review-btn").addEventListener("click", () => { renderReview(); $("review").showModal(); });
$("review-close").addEventListener("click", () => $("review").close());

$("add-btn").addEventListener("click", openAdd);
$("tab-reminder").addEventListener("click", () => setAddKind("reminder"));
$("tab-alarm").addEventListener("click", () => setAddKind("alarm"));
$("add-save").addEventListener("click", saveAdd);
$("add-cancel").addEventListener("click", () => $("add").close());

$("settings-btn").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", () => $("settings").close());
$("lock-btn").addEventListener("click", lockNow);
$("pin-set").addEventListener("click", changePin);
$("pin-remove").addEventListener("click", removePin);
$("sw-lock").addEventListener("click", () => setSetting({ lock_dashboard: !session.lock_dashboard }));
$("sw-texts").addEventListener("click", () => setSetting({ texts_enabled: !session.texts_enabled }));
$("sw-auto").addEventListener("click", () => setSetting({ auto_alarms: !session.auto_alarms }));
$("idle-dn").addEventListener("click", () => setSetting({ idle_lock_min: stepOption(IDLE_OPTIONS, session.idle_lock_min, -1) }));
$("idle-up").addEventListener("click", () => setSetting({ idle_lock_min: stepOption(IDLE_OPTIONS, session.idle_lock_min, +1) }));
$("lead-dn").addEventListener("click", () => setSetting({ default_lead: Math.max(0, session.default_lead - LEAD_STEP) }));
$("lead-up").addEventListener("click", () => setSetting({ default_lead: Math.min(MAX_LEAD, session.default_lead + LEAD_STEP) }));
$("snooze-dn").addEventListener("click", () => setSetting({ snooze_min: stepOption(SNOOZE_OPTIONS, session.snooze_min, -1) }));
$("snooze-up").addEventListener("click", () => setSetting({ snooze_min: stepOption(SNOOZE_OPTIONS, session.snooze_min, +1) }));
$("flash-dn").addEventListener("click", () => setSetting({ alarm_flash: stepOption(FLASH_OPTIONS, session.alarm_flash, -1) }));
$("flash-up").addEventListener("click", () => setSetting({ alarm_flash: stepOption(FLASH_OPTIONS, session.alarm_flash, +1) }));
$("test-sound").addEventListener("click", previewRing);

$("texts-toggle").addEventListener("click", toggleTexts);
$("link-open").addEventListener("click", openPendingLink);
$("link-copy").addEventListener("click", copyPendingLink);
$("link-cancel").addEventListener("click", () => { pendingLink = null; $("link-confirm").close(); });
$("reply-send").addEventListener("click", sendReply);
$("reply-cancel").addEventListener("click", () => $("reply").close());

$("ring-snooze").addEventListener("click", () => finishRing("/api/alarms/{id}/snooze", {}));
$("ring-dismiss").addEventListener("click", () => finishRing("/api/alarms/{id}/ack"));
$("ring").addEventListener("click", () => audio());   // a tap lets the browser start sound
$("ring").addEventListener("cancel", (e) => e.preventDefault());
$("ring").addEventListener("close", () => { if (ringing) $("ring").showModal(); });

for (const ev of ["pointerdown", "keydown", "touchstart"]) document.addEventListener(ev, noteActivity, { passive: true });
document.addEventListener("pointerdown", () => audio(), { once: true });

if (new URLSearchParams(location.search).has("kiosk")) document.body.classList.add("kiosk");

async function boot() {
  buildPad();
  tick();
  setInterval(tick, 1000);
  setInterval(idleCheck, 5000);
  setInterval(pollDue, DUE_POLL_MS);
  pollDue();
  try { applySession(await api("/api/session")); } catch (e) { /* offline: refresh() shows the status dot */ }
  if (session.pin_set && session.lock_dashboard && !session.unlocked) await ensureUnlocked("Enter PIN", false);
  await refresh();
  setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); pollDue(); } });
}

boot();
