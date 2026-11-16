#!/usr/bin/env python3
"""
sms_poller.py - Pull SMS from an Android phone over Bluetooth MAP (BlueZ obexd),
prefilter with cheap rules, and queue candidates in SQLite for the LLM step.

Requirements (Fedora):
    sudo dnf install bluez bluez-obexd python3-dbus-fast
Setup:
    - Pair + trust the phone with bluetoothctl; allow "message access" on the phone.
    - obexd and this poller must run in the session of the auto-logged-in desktop/kiosk user
      (BlueZ 5.88's obexd needs an active logind seat; a lingering, seatless service user
      gets "obex_server_init failed"). install.sh enables both as that user's systemd
      user services:
        systemctl --user enable --now obex   (or: /usr/libexec/bluetooth/obexd -n -a &)
    - The SQLite DB is shared with the home-dash services through group home-dash (0660).
Usage:
    PHONE_MAC=AA:BB:CC:DD:EE:FF python3 sms_poller.py

Pipeline: this script writes rows to messages(status='pending').
Your LLM worker reads 'pending', writes events/reminders, sets status='done'.
Message bodies are never logged.

Extras (both controlled from the dashboard's Settings):
  - Recent texts: only while "Show recent texts" is on (needs a PIN), every inbox message is
    also copied to recent_texts for the dashboard. Nothing is kept when it is off.
  - Replies: rows the dashboard queues in `outbox` are sent as SMS by pushing a bMessage to
    the phone's outbox folder over MAP (PushMessage). Failed sends are NOT retried
    automatically (a retry could text someone twice); the dashboard shows "Failed".
"""
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time

# python3 -I (isolated) does not put this script's folder on sys.path; /opt/home-dashboard is root-owned.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dash_common as dc  # noqa: E402
from dbus_fast import Message, MessageType, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.constants import BusType  # noqa: E402

PHONE_MAC = os.environ.get("PHONE_MAC", "AA:BB:CC:DD:EE:FF")
DB_PATH = os.environ.get("DASH_DB", "dashboard.db")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))
MAX_MESSAGES = 50
RETRY_MAX_SECONDS = 300
OUTBOX_TICK_SECONDS = 2       # how often queued replies are looked for (the inbox is polled less often)
SEND_TIMEOUT_SECONDS = 45
TEXT_RETENTION_DAYS = int(os.environ.get("TEXT_RETENTION_DAYS", "3"))
RECENT_KEEP = 40

# Folder names vary by phone; try these in order. Absolute paths first so the result does not
# depend on which folder the session is currently in; ".." covers BlueZ builds without them.
INBOX_CANDIDATES = ["/telecom/msg/inbox", "telecom/msg/inbox", "/telecom/msg/INBOX", "telecom/msg/INBOX", "../inbox"]
OUTBOX_CANDIDATES = ["/telecom/msg/outbox", "../outbox", "telecom/msg/outbox", "/telecom/msg/OUTBOX", "../OUTBOX"]

# Cheap prefilter: only messages matching these go to the LLM.
KEYWORDS = re.compile(
    r"\b(refill|rx|prescription|pharmacy|ready for pickup|pick ?up|appointment|"
    r"reminder|reschedul\w*|confirm\w*|delivery|due|expires?|expir\w*|"
    r"payment|bill|overdue|flight|reservation|booking|picked up|picking up|dispensed|collected)\b",
    re.IGNORECASE,
)
# Sender allowlist (substrings of number/name). Empty = rely on keywords only.
TRUSTED_SENDERS = [s.strip().lower() for s in os.environ.get("TRUSTED_SENDERS", "cvs,walgreens").split(",") if s.strip()]

OBEX_BUS = "org.bluez.obex"
log = logging.getLogger("sms_poller")


def init_db() -> sqlite3.Connection:
    db = dc.open_db(DB_PATH)  # schema + migrations live in dash_common.py
    # Every message ever seen (even filtered out) so we never reprocess it.
    db.execute("CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY)")
    # A crash mid-send must not be retried blindly (it could text someone twice).
    db.execute("UPDATE outbox SET status='failed', error='Interrupted', updated_at=? WHERE status='sending'",
               (time.time(),))
    db.commit()
    return db


def make_uid(path: str, props: dict) -> str:
    # Session numbers change per connection, so use handle + timestamp + sender.
    handle = path.rsplit("/", 1)[-1]
    raw = f"{handle}|{props.get('Timestamp','')}|{props.get('SenderAddress','')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def worth_sending(sender: str, body: str) -> bool:
    s = sender.lower()
    if any(t in s for t in TRUSTED_SENDERS):
        return True
    return bool(KEYWORDS.search(body))


def unwrap(props: dict) -> dict:
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in props.items()}


async def open_session(bus: MessageBus):
    intro = await bus.introspect(OBEX_BUS, "/org/bluez/obex")
    client = bus.get_proxy_object(OBEX_BUS, "/org/bluez/obex", intro).get_interface(
        "org.bluez.obex.Client1"
    )
    session_path = await client.call_create_session(PHONE_MAC, {"Target": Variant("s", "map")})
    intro = await bus.introspect(OBEX_BUS, session_path)
    obj = bus.get_proxy_object(OBEX_BUS, session_path, intro)
    return client, session_path, obj.get_interface("org.bluez.obex.MessageAccess1")


async def goto_folder(mav, candidates) -> str:
    last_err = None
    for folder in candidates:
        try:
            await mav.call_set_folder(folder)
            return folder
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Could not open folder {candidates[0]}: {last_err}")


async def select_inbox(mav) -> None:
    await goto_folder(mav, INBOX_CANDIDATES)


def store_recent(db: sqlite3.Connection, uid: str, props: dict, sender: str, body: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO recent_texts(uid, sender, addr, ts, body, is_read, seen_at) VALUES (?,?,?,?,?,?,?)",
        (uid, sender, dc.normalize_number(props.get("SenderAddress")),
         dc.sanitize_text(props.get("Timestamp", ""), 24), body, 1 if props.get("Read") else 0, time.time()),
    )


def prune_recent(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM recent_texts WHERE seen_at < ?", (time.time() - TEXT_RETENTION_DAYS * 86400,))
    db.execute(
        "DELETE FROM recent_texts WHERE uid NOT IN (SELECT uid FROM recent_texts ORDER BY ts DESC, seen_at DESC LIMIT ?)",
        (RECENT_KEEP,),
    )


async def poll_once(db: sqlite3.Connection, mav) -> int:
    # SubjectLength (max 255) returns the start of the SMS text in 'Subject',
    # which avoids a separate Message.Get transfer per message.
    filters = {
        "MaxCount": Variant("q", MAX_MESSAGES),
        "SubjectLength": Variant("y", 255),
    }
    messages = await mav.call_list_messages("", filters)
    keep_texts = dc.get_setting(db, "texts_enabled") == "1" and dc.pin_is_set(db)
    queued = 0
    for path, raw_props in messages.items():
        props = unwrap(raw_props)
        uid = make_uid(path, props)
        # Everything from the phone is attacker-controlled: strip invisible/bidi/control characters
        # once, here, so nothing downstream (database, page, model) ever sees them.
        sender = dc.sanitize_text(props.get("Sender") or props.get("SenderAddress") or "", 80)
        body = dc.sanitize_text(props.get("Subject") or "", 500)
        if keep_texts:
            store_recent(db, uid, props, sender, body)  # before the 'seen' check: show old ones too
        if db.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone():
            continue
        db.execute("INSERT OR IGNORE INTO seen(uid) VALUES (?)", (uid,))
        if worth_sending(sender, body):
            db.execute(
                "INSERT OR IGNORE INTO messages(uid, sender, msg_ts, body, seen_at) VALUES (?,?,?,?,?)",
                (uid, sender, dc.sanitize_text(props.get("Timestamp", ""), 24), body, time.time()),
            )
            queued += 1
    if keep_texts:
        prune_recent(db)
    db.commit()
    return queued


# ------------------------------------------------------------------ sending replies


class TransferWatcher:
    """Remembers org.bluez.obex.Transfer1 'Status' changes (a transfer can finish before we look)."""

    MATCH = (
        "type='signal',interface='org.freedesktop.DBus.Properties',"
        "member='PropertiesChanged',path_namespace='/org/bluez/obex'"
    )

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.status: dict[str, str] = {}
        bus.add_message_handler(self._on_message)

    async def install(self) -> None:
        await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[self.MATCH],
        ))

    def _on_message(self, msg) -> None:
        try:
            if (msg.message_type == MessageType.SIGNAL and msg.member == "PropertiesChanged"
                    and msg.body and msg.body[0] == "org.bluez.obex.Transfer1"):
                st = msg.body[1].get("Status")
                if st is not None:
                    self.status[msg.path] = st.value if isinstance(st, Variant) else str(st)
        except Exception:  # noqa: BLE001  (never let a monitor break the bus loop)
            pass

    async def wait(self, path: str) -> None:
        end = time.monotonic() + SEND_TIMEOUT_SECONDS
        while time.monotonic() < end:
            st = self.status.get(path)
            if st == "complete":
                self.status.pop(path, None)
                return
            if st == "error":
                self.status.pop(path, None)
                raise RuntimeError("the phone rejected the message")
            await asyncio.sleep(0.5)
        raise TimeoutError("timed out waiting for the phone")


def runtime_dir() -> str:
    # systemd's RuntimeDirectory= (set in the unit) is writable for us AND readable by obexd,
    # which runs outside this service's sandbox as the same user. /tmp would be private.
    return os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0] or tempfile.gettempdir()


async def push_sms(mav, watcher: TransferWatcher, number: str, text: str) -> None:
    path = os.path.join(runtime_dir(), f"reply-{os.getpid()}-{int(time.time() * 1000)}.bmsg")
    with open(path, "wb") as f:
        f.write(dc.build_bmessage(number, text))
    try:
        await goto_folder(mav, OUTBOX_CANDIDATES)
        res = await mav.call_push_message(
            path, "",
            {"Transparent": Variant("b", False), "Retry": Variant("b", True), "Charset": Variant("s", "UTF-8")},
        )
        transfer = res[0] if isinstance(res, (list, tuple)) else res
        await watcher.wait(str(transfer))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            await select_inbox(mav)  # always go back so polling keeps working
        except Exception:  # noqa: BLE001
            pass


async def send_queued(db: sqlite3.Connection, mav, watcher: TransferWatcher) -> None:
    rows = db.execute("SELECT id, to_addr, body FROM outbox WHERE status='queued' ORDER BY id LIMIT 3").fetchall()
    for row in rows:
        rid, number, text = row["id"], row["to_addr"], row["body"]
        db.execute("UPDATE outbox SET status='sending', updated_at=? WHERE id=?", (time.time(), rid))
        db.commit()
        try:
            await push_sms(mav, watcher, number, text)
            db.execute("UPDATE outbox SET status='sent', error=NULL, updated_at=? WHERE id=?", (time.time(), rid))
            log.info("Reply %d sent", rid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"[:200]
            db.execute("UPDATE outbox SET status='failed', error=?, updated_at=? WHERE id=?",
                       (msg, time.time(), rid))
            log.warning("Reply %d failed (%s)", rid, type(e).__name__)
        db.commit()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = init_db()
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    watcher = TransferWatcher(bus)
    await watcher.install()
    backoff = 5

    while True:
        client = session_path = None
        try:
            log.info("Connecting to %s over MAP...", PHONE_MAC)
            client, session_path, mav = await open_session(bus)
            await select_inbox(mav)
            log.info("Connected.")
            backoff = 5
            next_poll = 0.0
            while True:
                if time.monotonic() >= next_poll:
                    n = await poll_once(db, mav)
                    if n:
                        log.info("Queued %d new candidate message(s)", n)
                    next_poll = time.monotonic() + POLL_SECONDS
                await send_queued(db, mav, watcher)
                await asyncio.sleep(OUTBOX_TICK_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Phones drop MAP when the screen sleeps; just reconnect with backoff.
            log.warning("Connection problem (%s: %s). Retrying in %ds", type(e).__name__, e, backoff)
        finally:
            if client and session_path:
                try:
                    await client.call_remove_session(session_path)
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RETRY_MAX_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
