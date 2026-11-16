#!/usr/bin/env python3
"""
dash_common.py - Code shared by sms_poller.py, llm_worker.py and dashboard_server.py.

Plain standard library only (no FastAPI, no D-Bus), so the logic here can be unit-tested
anywhere (see test_logic.py). Contents:

  - the SQLite schema and migrations (one copy for all three programs)
  - settings storage (key/value table) and the PIN lock (scrypt hash, throttling, sessions)
  - alarm helpers (an alarm N minutes before an appointment, manual alarms, "due" lookup)
  - helpers for recent texts and text replies (number cleanup, bMessage builder)

Privacy: message bodies are only kept in `recent_texts` when the "Show recent texts" setting
is on (it needs a PIN), and that table is emptied again when the setting is turned off.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta

# ------------------------------------------------------------------ constants

LEAD_STEP = 15                 # alarm lead time moves in 15-minute steps
SHIFT_STEP_MIN = 15            # the +/- buttons on an alarm move it by this much...
MAX_SHIFTS = 5                 # ...at most this many times away from its original time
MAX_LEAD = 24 * 60             # up to a day before
RING_GRACE_MIN = 10            # an alarm that was missed by more than this stays silent
DT_FMT = "%Y-%m-%dT%H:%M:%S"   # local time, no zone (the Pi's own clock)

IDLE_CHOICES = (0, 1, 2, 5, 10, 15, 30)   # minutes; 0 = never auto-lock
SNOOZE_CHOICES = (5, 10, 15)
FLASH_CHOICES = ("off", "gentle", "strong")
MAX_REPLY_CHARS = 300
REPLY_LIMIT = 5                # replies allowed per REPLY_WINDOW_S
REPLY_WINDOW_S = 600

DEFAULTS = {
    "lock_dashboard": "1",   # only effective once a PIN exists
    "idle_lock_min": "2",
    "texts_enabled": "0",
    "auto_alarms": "1",
    "default_lead": "60",
    "snooze_min": "10",
    "alarm_flash": "strong",   # how the ringing alarm screen flashes
}


class DashError(Exception):
    """An error that the web layer turns into an HTTP status + message."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ------------------------------------------------------------- untrusted text
# Everything that arrives in a text message is attacker-controlled. We strip characters that
# are invisible or can reorder/disguise text (zero-width, bidi overrides such as U+202E,
# control characters, private-use) before it is stored, shown or sent to the model.
_DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def sanitize_text(value, max_len: int = 500) -> str:
    s = unicodedata.normalize("NFC", "" if value is None else str(value))
    s = re.sub(r"[\r\n\t\v\f]+", " ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _DROP_CATEGORIES)
    return re.sub(r" {2,}", " ", s).strip()[:max_len]


# --------------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    uid TEXT PRIMARY KEY, sender TEXT, msg_ts TEXT, body TEXT,
    status TEXT NOT NULL DEFAULT 'pending', seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid TEXT UNIQUE, title TEXT, date TEXT, time TEXT, details TEXT,
    urgency TEXT, needs_review INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid TEXT UNIQUE, title TEXT, due_date TEXT, details TEXT,
    urgency TEXT, needs_review INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    fire_at TEXT NOT NULL,            -- local 'YYYY-MM-DDTHH:MM:SS'
    event_id INTEGER,                 -- set for "before this appointment" alarms
    lead_minutes INTEGER,             -- only for event alarms
    source TEXT NOT NULL DEFAULT 'manual',
    snooze_until TEXT,
    acked INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    base_fire_at TEXT                 -- original time; +/- nudges are limited relative to this
);
CREATE UNIQUE INDEX IF NOT EXISTS alarms_one_per_event ON alarms(event_id) WHERE event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recent_texts (
    uid TEXT PRIMARY KEY, sender TEXT, addr TEXT, ts TEXT, body TEXT,
    is_read INTEGER DEFAULT 0, seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text_uid TEXT, to_addr TEXT NOT NULL, to_label TEXT, body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | sending | sent | failed
    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

# (table, column, DDL) - added to databases created by older versions.
MIGRATIONS = [
    ("messages", "attempts", "INTEGER DEFAULT 0"),
    ("reminders", "sender", "TEXT"),
    ("reminders", "category", "TEXT"),       # 'pickup' | 'refill' | 'bill' | 'other'
    ("reminders", "source", "TEXT DEFAULT 'text'"),
    ("alarms", "base_fire_at", "TEXT"),
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # already there
    conn.commit()


def open_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


# ------------------------------------------------------------------- settings


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row[0]
    return DEFAULTS.get(key, default)


def put_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def del_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (key,))


def pin_is_set(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "pin_hash") is not None


def public_settings(conn: sqlite3.Connection) -> dict:
    """Everything the page may know. Never includes the PIN hash or salt."""
    pin = pin_is_set(conn)
    return {
        "pin_set": pin,
        "pin_len": int(get_setting(conn, "pin_len", "0") or 0) if pin else 0,
        "lock_dashboard": pin and get_setting(conn, "lock_dashboard") == "1",
        "idle_lock_min": int(get_setting(conn, "idle_lock_min")),
        "texts_enabled": pin and get_setting(conn, "texts_enabled") == "1",
        "auto_alarms": get_setting(conn, "auto_alarms") == "1",
        "default_lead": int(get_setting(conn, "default_lead")),
        "snooze_min": int(get_setting(conn, "snooze_min")),
        "alarm_flash": get_setting(conn, "alarm_flash"),
    }


def update_settings(conn: sqlite3.Connection, changes: dict) -> dict:
    """Validate and store a partial settings change. Returns the new public settings."""
    ch = {k: v for k, v in changes.items() if v is not None}
    if "lock_dashboard" in ch:
        put_setting(conn, "lock_dashboard", "1" if ch["lock_dashboard"] else "0")
    if "idle_lock_min" in ch:
        if ch["idle_lock_min"] not in IDLE_CHOICES:
            raise DashError(400, "Unsupported auto-lock time")
        put_setting(conn, "idle_lock_min", str(ch["idle_lock_min"]))
    if "texts_enabled" in ch:
        if ch["texts_enabled"]:
            if not pin_is_set(conn):
                raise DashError(400, "Set a PIN before turning on recent texts")
            put_setting(conn, "texts_enabled", "1")
        else:
            put_setting(conn, "texts_enabled", "0")
            purge_texts(conn)  # nothing is kept once the feature is off
    if "auto_alarms" in ch:
        put_setting(conn, "auto_alarms", "1" if ch["auto_alarms"] else "0")
    if "default_lead" in ch:
        lead = ch["default_lead"]
        if lead % LEAD_STEP != 0 or not 0 <= lead <= MAX_LEAD:
            raise DashError(400, "Lead time must be a multiple of 15 minutes, up to 24 hours")
        put_setting(conn, "default_lead", str(lead))
    if "snooze_min" in ch:
        if ch["snooze_min"] not in SNOOZE_CHOICES:
            raise DashError(400, "Unsupported snooze time")
        put_setting(conn, "snooze_min", str(ch["snooze_min"]))
    if "alarm_flash" in ch:
        if ch["alarm_flash"] not in FLASH_CHOICES:
            raise DashError(400, "Unsupported flash setting")
        put_setting(conn, "alarm_flash", ch["alarm_flash"])
    conn.commit()
    return public_settings(conn)


def purge_texts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM recent_texts")
    conn.execute("DELETE FROM outbox")


# ------------------------------------------------------------------ PIN + sessions


def _hash_pin(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def set_pin(conn: sqlite3.Connection, pin: str) -> None:
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        raise DashError(400, "PIN must be 4 to 8 digits")
    salt = secrets.token_bytes(16)
    put_setting(conn, "pin_salt", salt.hex())
    put_setting(conn, "pin_hash", _hash_pin(pin, salt).hex())
    put_setting(conn, "pin_len", str(len(pin)))
    conn.commit()


def remove_pin(conn: sqlite3.Connection) -> None:
    for k in ("pin_salt", "pin_hash", "pin_len"):
        del_setting(conn, k)
    put_setting(conn, "texts_enabled", "0")
    purge_texts(conn)
    conn.commit()


def pin_matches(conn: sqlite3.Connection, pin: str) -> bool:
    h, s = get_setting(conn, "pin_hash"), get_setting(conn, "pin_salt")
    if not h or not s or not isinstance(pin, str) or not re.fullmatch(r"\d{1,16}", pin):
        return False
    return hmac.compare_digest(bytes.fromhex(h), _hash_pin(pin, bytes.fromhex(s)))


class Guard:
    """In-memory sessions plus brute-force throttling for the PIN.

    A session is a random cookie token. It expires after SESSION_MAX_AGE, or after the
    "auto-lock" idle time (the page pings /api/activity while someone is using it, so
    background polling does not keep it alive).
    """

    FREE_TRIES = 5          # wrong PINs allowed before lock-out starts
    LOCK_BASE_S = 30        # first lock-out, then doubles up to LOCK_MAX_S
    LOCK_MAX_S = 900
    IDLE_SLACK_S = 90       # page pings at least every ~20 s while in use

    def __init__(self, session_max_age: int = 12 * 3600):
        self.sessions: dict[str, dict] = {}
        self.max_age = session_max_age
        self.fails = 0
        self.until = 0.0

    # -- PIN checks (throttled)
    def verify(self, conn: sqlite3.Connection, pin: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now < self.until:
            raise DashError(429, f"Too many wrong PINs. Try again in {int(self.until - now) + 1} seconds.")
        if pin_matches(conn, pin):
            self.fails = 0
            return True
        self.fails += 1
        if self.fails >= self.FREE_TRIES:
            wait = min(self.LOCK_BASE_S * 2 ** (self.fails - self.FREE_TRIES), self.LOCK_MAX_S)
            self.until = now + wait
        return False

    # -- sessions
    def start(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"created": now, "last": now}
        return token

    def valid(self, conn: sqlite3.Connection, token: str | None, now: float | None = None) -> bool:
        if not pin_is_set(conn):
            return True  # no PIN, nothing to unlock
        s = self.sessions.get(token or "")
        if not s:
            return False
        now = time.time() if now is None else now
        idle = int(get_setting(conn, "idle_lock_min"))
        if now - s["created"] > self.max_age or (idle > 0 and now - s["last"] > idle * 60 + self.IDLE_SLACK_S):
            self.sessions.pop(token, None)
            return False
        return True

    def touch(self, token: str | None, now: float | None = None) -> None:
        s = self.sessions.get(token or "")
        if s:
            s["last"] = time.time() if now is None else now

    def end(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)

    def end_all(self) -> None:
        self.sessions.clear()


# ------------------------------------------------------------------ alarms


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def clamp_lead(minutes: int) -> int:
    minutes = int(round(minutes / LEAD_STEP) * LEAD_STEP)
    return max(0, min(MAX_LEAD, minutes))


def event_start(date_s: str, time_s: str) -> datetime:
    return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")


def fire_at_for(date_s: str, time_s: str, lead: int) -> str:
    return (event_start(date_s, time_s) - timedelta(minutes=lead)).strftime(DT_FMT)


def ensure_event_alarm(
    conn: sqlite3.Connection, event_id: int, date_s: str, time_s: str,
    lead: int | None = None, now: datetime | None = None,
) -> int | None:
    """Create the 'before this appointment' alarm if the event has a time and is still ahead."""
    if not date_s or not time_s:
        return None
    existing = conn.execute("SELECT id FROM alarms WHERE event_id=?", (event_id,)).fetchone()
    if existing:
        return existing[0]
    now = now or datetime.now()
    if event_start(date_s, time_s) <= now:
        return None
    if lead is None:
        lead = int(get_setting(conn, "default_lead"))
    lead = clamp_lead(lead)
    title = (conn.execute("SELECT title FROM events WHERE id=?", (event_id,)).fetchone() or ["Appointment"])[0]
    fire = fire_at_for(date_s, time_s, lead)
    cur = conn.execute(
        "INSERT INTO alarms(label, fire_at, event_id, lead_minutes, source, created_at, base_fire_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (title or "Appointment", fire, event_id, lead, "event", time.time(), fire),
    )
    return cur.lastrowid


def shift_alarm(conn: sqlite3.Connection, alarm_id: int, steps: int, now: datetime | None = None) -> dict:
    """Move an alarm earlier (negative) or later (positive) in 15-minute steps.

    At most MAX_SHIFTS steps away from the time it was created with, so a stray tap or a
    hostile page can't drag an alarm around indefinitely.
    """
    now = now or datetime.now()
    if not isinstance(steps, int) or steps == 0 or abs(steps) > MAX_SHIFTS:
        raise DashError(400, "Move an alarm by 15 minutes at a time")
    row = conn.execute(
        "SELECT a.id, a.fire_at, a.base_fire_at, a.event_id, e.date, e.time FROM alarms a"
        " LEFT JOIN events e ON e.id = a.event_id WHERE a.id=?",
        (alarm_id,),
    ).fetchone()
    if not row:
        raise DashError(404, "Not found")
    cur = parse_dt(row["fire_at"])
    base = parse_dt(row["base_fire_at"]) or cur
    new = cur + timedelta(minutes=SHIFT_STEP_MIN * steps)
    total = round((new - base).total_seconds() / 60 / SHIFT_STEP_MIN)
    if abs(total) > MAX_SHIFTS:
        raise DashError(400, f"An alarm can be moved at most {MAX_SHIFTS} times by {SHIFT_STEP_MIN} minutes")
    lead = None
    if row["event_id"] is not None:
        start = event_start(row["date"], row["time"])
        lead = int((start - new).total_seconds() // 60)
        if lead < 0:
            raise DashError(400, "An alarm can't ring after the appointment has started")
        if lead > MAX_LEAD:
            raise DashError(400, "Alarms can be at most 24 hours before")
    if new <= now:
        raise DashError(400, "That alarm time has already passed")
    conn.execute(
        "UPDATE alarms SET fire_at=?, base_fire_at=?, lead_minutes=COALESCE(?, lead_minutes),"
        " snooze_until=NULL, acked=0 WHERE id=?",
        (new.strftime(DT_FMT), base.strftime(DT_FMT), lead, alarm_id),
    )
    return {"id": alarm_id, "fire_at": new.strftime(DT_FMT), "lead_minutes": lead, "shift_steps": total}


def clean_label(label: str, what: str = "Label") -> str:
    label = sanitize_text(label, 200)
    if not label:
        raise DashError(400, f"{what} is required")
    return label[:80]


def clean_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise DashError(400, "Invalid date")


def clean_time(s: str | None) -> str:
    try:
        return datetime.strptime(s or "", "%H:%M").strftime("%H:%M")
    except ValueError:
        raise DashError(400, "Invalid time")


def add_manual_alarm(conn: sqlite3.Connection, label: str, date_s: str, time_s: str,
                     now: datetime | None = None) -> int:
    now = now or datetime.now()
    label = clean_label(label)
    d, t = clean_date(date_s), clean_time(time_s)
    if d is None:
        raise DashError(400, "Date is required")
    fire = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
    if fire <= now:
        raise DashError(400, "That time has already passed")
    cur = conn.execute(
        "INSERT INTO alarms(label, fire_at, source, created_at, base_fire_at) VALUES (?,?,?,?,?)",
        (label, fire.strftime(DT_FMT), "manual", time.time(), fire.strftime(DT_FMT)),
    )
    return cur.lastrowid


def add_manual_reminder(conn: sqlite3.Connection, title: str, due: str | None, details: str | None) -> int:
    title = clean_label(title, "Label")
    due = clean_date(due)
    details = sanitize_text(details, 240)
    cur = conn.execute(
        "INSERT INTO reminders(title, due_date, details, urgency, needs_review, done, created_at, source, category)"
        " VALUES (?,?,?,?,0,0,?,'manual','other')",
        (title, due, details, "normal", time.time()),
    )
    return cur.lastrowid


def snooze_alarm(conn: sqlite3.Connection, alarm_id: int, minutes: int | None = None,
                 now: datetime | None = None) -> str:
    now = now or datetime.now()
    if minutes is None:
        minutes = int(get_setting(conn, "snooze_min"))
    if minutes not in SNOOZE_CHOICES:
        raise DashError(400, "Unsupported snooze time")
    until = (now + timedelta(minutes=minutes)).strftime(DT_FMT)
    cur = conn.execute("UPDATE alarms SET snooze_until=? WHERE id=? AND acked=0", (until, alarm_id))
    if cur.rowcount == 0:
        raise DashError(404, "Not found")
    return until


ALARM_COLS = (
    "a.id, a.label, a.fire_at, a.base_fire_at, a.snooze_until, a.acked, a.event_id, a.lead_minutes, a.source,"
    " e.title AS event_title, e.date AS event_date, e.time AS event_time"
)


def alarm_row(r: sqlite3.Row) -> dict:
    trig = r["snooze_until"] or r["fire_at"]
    fire, base = parse_dt(r["fire_at"]), parse_dt(r["base_fire_at"]) or parse_dt(r["fire_at"])
    steps = round((fire - base).total_seconds() / 60 / SHIFT_STEP_MIN) if fire and base else 0
    return {
        "shift_steps": steps,   # negative = moved earlier, positive = later; limited to +/- MAX_SHIFTS
        "id": r["id"], "label": r["label"], "fire_at": r["fire_at"], "trigger": trig,
        "acked": bool(r["acked"]), "event_id": r["event_id"], "lead_minutes": r["lead_minutes"],
        "source": r["source"], "event_time": r["event_time"], "event_date": r["event_date"],
    }


def due_alarms(conn: sqlite3.Connection, now: datetime | None = None, lookahead_s: int = 20) -> list[dict]:
    """Alarms that are ringing now, or will within `lookahead_s` seconds (page polls every ~10 s)."""
    now = now or datetime.now()
    out = []
    rows = conn.execute(
        f"SELECT {ALARM_COLS} FROM alarms a LEFT JOIN events e ON e.id = a.event_id WHERE a.acked = 0"
    ).fetchall()
    for r in rows:
        d = alarm_row(r)
        trig = parse_dt(d["trigger"])
        if trig is None:
            continue
        if trig - timedelta(seconds=lookahead_s) <= now < trig + timedelta(minutes=RING_GRACE_MIN):
            out.append(d)
    return sorted(out, key=lambda x: x["trigger"])


def list_alarms(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """For the dashboard: open alarms, plus appointment alarms (so rows can show 'done')."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=1)).strftime(DT_FMT)
    rows = conn.execute(
        f"SELECT {ALARM_COLS} FROM alarms a LEFT JOIN events e ON e.id = a.event_id"
        " WHERE (a.acked = 0 OR a.event_id IS NOT NULL) AND a.fire_at >= ?"
        " ORDER BY COALESCE(a.snooze_until, a.fire_at)",
        (cutoff,),
    ).fetchall()
    return [alarm_row(r) for r in rows]


# ------------------------------------------------------------------ texts + replies

_NUMBER_RE = re.compile(r"^\+?\d{3,15}$")


def normalize_number(addr: str | None) -> str | None:
    """A phone number we could text back, or None (names, 'CVS', e-mail addresses...)."""
    if not addr:
        return None
    cleaned = re.sub(r"[\s().-]", "", str(addr))
    return cleaned if _NUMBER_RE.match(cleaned) else None


def queue_reply(conn: sqlite3.Connection, text_uid: str, body: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    if get_setting(conn, "texts_enabled") != "1" or not pin_is_set(conn):
        raise DashError(403, "Recent texts are turned off in settings")
    body = sanitize_text(body, MAX_REPLY_CHARS + 1)
    if not body:
        raise DashError(400, "Write a message first")
    if len(body) > MAX_REPLY_CHARS:
        raise DashError(400, f"Replies are limited to {MAX_REPLY_CHARS} characters")
    row = conn.execute("SELECT sender, addr FROM recent_texts WHERE uid=?", (text_uid,)).fetchone()
    if not row:
        raise DashError(404, "That text is no longer available")
    if not row["addr"]:
        raise DashError(400, "This sender has no phone number to reply to")
    recent = conn.execute("SELECT COUNT(*) FROM outbox WHERE created_at > ?", (now - REPLY_WINDOW_S,)).fetchone()[0]
    if recent >= REPLY_LIMIT:
        raise DashError(429, "Too many replies in a short time. Wait a few minutes.")
    cur = conn.execute(
        "INSERT INTO outbox(text_uid, to_addr, to_label, body, status, created_at, updated_at)"
        " VALUES (?,?,?,?, 'queued', ?, ?)",
        (text_uid, row["addr"], (row["sender"] or "")[:80], body, now, now),
    )
    return cur.lastrowid


_BMSG_TOKEN = re.compile(r"(?im)^(\s*)(BEGIN|END):")


def build_bmessage(number: str, text: str) -> bytes:
    """A MAP bMessage (v1.0) for sending one SMS, as pushed to the phone's outbox folder.

    LENGTH counts everything from 'BEGIN:MSG' through 'END:MSG' (with CRLFs), in bytes.
    """
    num = normalize_number(number)
    if not num:
        raise ValueError("invalid phone number")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BMSG_TOKEN.sub(r"\1\2 :", text)  # a text must not be able to close the envelope
    text = text.replace("\n", "\r\n")
    body = f"BEGIN:MSG\r\n{text}\r\nEND:MSG\r\n"
    lines = [
        "BEGIN:BMSG", "VERSION:1.0", "STATUS:UNREAD", "TYPE:SMS_GSM",
        "FOLDER:TELECOM/MSG/OUTBOX",
        "BEGIN:BENV", "BEGIN:VCARD", "VERSION:2.1", "N:", f"TEL:{num}", "END:VCARD",
        "BEGIN:BBODY", "CHARSET:UTF-8", f"LENGTH:{len(body.encode('utf-8'))}",
    ]
    head = "\r\n".join(lines) + "\r\n"
    tail = "END:BBODY\r\nEND:BENV\r\nEND:BMSG\r\n"
    return (head + body + tail).encode("utf-8")
