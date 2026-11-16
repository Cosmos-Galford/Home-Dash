#!/usr/bin/env python3
"""
dashboard_server.py - Local web backend + static host for the home dashboard.

Serves the kiosk page at / and a small JSON API over the SQLite database that
sms_poller.py and llm_worker.py fill in. Business logic lives in dash_common.py.

    GET  /api/session                 lock state + non-secret settings (always allowed)
    POST /api/unlock {pin} | /api/lock | /api/activity
    GET  /api/state                   events, reminders, alarms, review queue
    POST /api/reminders               add a reminder   {title, due_date?, details?}
    POST /api/reminders/{id}/done     {"done": true|false}
    POST /api/{events|reminders}/{id}/confirm     clear the "needs review" flag
    DELETE /api/{events|reminders}/{id}
    POST /api/alarms                  add an alarm     {label, date, time}
    POST /api/events/{id}/alarm       alarm before an appointment {lead_minutes?}
    POST /api/alarms/{id}/shift       {steps: -1|1}    (15 min earlier/later, max 5 steps from the original)
    POST /api/alarms/{id}/ack | /snooze      DELETE /api/alarms/{id}
    GET  /api/alarms/due              alarms ringing or about to (works while locked)
    GET/POST /api/settings            (PIN required)    POST /api/pin, /api/pin/remove
    GET  /api/texts                   recent texts      (PIN required + turned on in settings)
    POST /api/texts/{uid}/reply       {body}            (PIN required + turned on in settings)

Security:
  - Binds to 127.0.0.1 by default (DASH_HOST / DASH_PORT to change).
  - Host header allowlist (blocks DNS-rebinding attacks from web pages).
  - Every write needs an "X-Dashboard: 1" header. Browsers can't send that
    cross-origin without a CORS preflight, and we never allow CORS, so other
    websites can't change your data.
  - Optional PIN (scrypt-hashed, throttled after 5 wrong tries, HttpOnly SameSite=Strict
    session cookie). It keeps casual visitors out of the dashboard / texts / settings; it is
    not protection against someone with a shell on this machine (the database is readable
    by the kiosk account).
  - Strict Content-Security-Policy; the front end renders text with
    textContent only, because message-derived text is untrusted input.
Old events, finished reminders and old texts are pruned (DASH_RETENTION_DAYS, TEXT_RETENTION_DAYS).
Pick-up reminders with no confirmation are closed after PICKUP_EXPIRE_DAYS (0 = never).
"""
import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

# python3 -I (isolated) does not put this script's folder on sys.path; /opt/home-dashboard is root-owned.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dash_common as dc  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Request, Response  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

DB_PATH = os.environ.get("DASH_DB", "dashboard.db")
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8080"))
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DASH_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
RETENTION_DAYS = int(os.environ.get("DASH_RETENTION_DAYS", "30"))
TEXT_RETENTION_DAYS = int(os.environ.get("TEXT_RETENTION_DAYS", "3"))
PICKUP_EXPIRE_DAYS = int(os.environ.get("PICKUP_EXPIRE_DAYS", "14"))
STATIC_DIR = Path(os.environ.get("DASH_STATIC", Path(__file__).parent / "static"))
COOKIE = "dash_session"

log = logging.getLogger("dashboard_server")
guard = dc.Guard()

TABLES = {"events": "events", "reminders": "reminders"}


@contextlib.contextmanager
def db():
    conn = dc.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def prune() -> None:
    now = datetime.now()
    cutoff_date = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    cutoff_ts = time.time() - RETENTION_DAYS * 86400
    text_cutoff = time.time() - TEXT_RETENTION_DAYS * 86400
    with db() as c:
        c.execute("DELETE FROM events WHERE date < ?", (cutoff_date,))
        c.execute("DELETE FROM reminders WHERE done IN (1, 2) AND created_at < ?", (cutoff_ts,))
        c.execute("DELETE FROM recent_texts WHERE seen_at < ?", (text_cutoff,))
        c.execute("DELETE FROM outbox WHERE created_at < ?", (text_cutoff,))
        c.execute("DELETE FROM alarms WHERE fire_at < ?", ((now - timedelta(days=RETENTION_DAYS)).strftime(dc.DT_FMT),))
        c.execute("DELETE FROM alarms WHERE event_id IS NOT NULL AND event_id NOT IN (SELECT id FROM events)")
        if PICKUP_EXPIRE_DAYS > 0:
            # Nobody confirmed the pick-up. Pharmacies return unclaimed orders to stock after
            # a week or two, so stop showing it (done=2 marks "expired", not "picked up").
            c.execute(
                "UPDATE reminders SET done=2 WHERE category='pickup' AND done=0 AND created_at < ?",
                (time.time() - PICKUP_EXPIRE_DAYS * 86400,),
            )


async def prune_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(prune)
        except Exception as e:  # noqa: BLE001
            log.warning("Prune failed: %s", type(e).__name__)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db() as c:
        dc.init_schema(c)
    task = asyncio.create_task(prune_loop())
    yield
    task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.exception_handler(dc.DashError)
async def dash_error_handler(_request: Request, exc: dc.DashError):
    return JSONResponse({"detail": exc.msg}, status_code=exc.status)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    # require-trusted-types-for: the browser itself refuses innerHTML/eval-style sinks, so even a
    # future coding mistake can't turn message text into markup. Nothing external can load
    # (no images, frames, media, workers): a link in a text is never fetched by this page.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; frame-src 'none'; media-src 'none'; worker-src 'none'; "
        "manifest-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
        "require-trusted-types-for 'script'"
    )
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=()"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ access control


def csrf_guard(x_dashboard: str | None = Header(default=None)) -> None:
    if x_dashboard != "1":
        raise HTTPException(status_code=403, detail="Missing X-Dashboard header")


def _unlocked(request: Request, c: sqlite3.Connection) -> bool:
    return guard.valid(c, request.cookies.get(COOKIE))


def need_view(request: Request) -> None:
    """The dashboard itself: locked only if a PIN exists AND 'require PIN to open' is on."""
    with db() as c:
        s = dc.public_settings(c)
        if s["lock_dashboard"] and not _unlocked(request, c):
            raise HTTPException(status_code=401, detail="locked")


def need_private(request: Request) -> None:
    """Texts, replies and settings: always behind the PIN once one is set."""
    with db() as c:
        if not _unlocked(request, c):
            raise HTTPException(status_code=401, detail="locked")


def session_info(c: sqlite3.Connection, request: Request) -> dict:
    return {**dc.public_settings(c), "unlocked": _unlocked(request, c)}


def set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")


class PinBody(BaseModel):
    pin: str = Field(max_length=16)


class NewPinBody(BaseModel):
    current: str | None = Field(default=None, max_length=16)
    new: str = Field(max_length=16)


class CurrentPinBody(BaseModel):
    current: str = Field(max_length=16)


@app.get("/api/session")
def get_session(request: Request):
    with db() as c:
        return session_info(c, request)


@app.post("/api/unlock", dependencies=[Depends(csrf_guard)])
def unlock(body: PinBody, request: Request, response: Response):
    with db() as c:
        if not dc.pin_is_set(c):
            return session_info(c, request)
        if not guard.verify(c, body.pin):
            raise HTTPException(status_code=403, detail="Wrong PIN")
        guard.end(request.cookies.get(COOKIE))
        set_session_cookie(response, guard.start())
        return {**dc.public_settings(c), "unlocked": True}


@app.post("/api/lock", dependencies=[Depends(csrf_guard)])
def lock(request: Request, response: Response):
    guard.end(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/activity", dependencies=[Depends(csrf_guard)])
def activity(request: Request):
    guard.touch(request.cookies.get(COOKIE))
    return {"ok": True}


# ---------------------------------------------------------------------- settings


class SettingsBody(BaseModel):
    lock_dashboard: bool | None = None
    idle_lock_min: int | None = None
    texts_enabled: bool | None = None
    auto_alarms: bool | None = None
    default_lead: int | None = None
    snooze_min: int | None = None
    alarm_flash: str | None = Field(default=None, max_length=10)


@app.get("/api/settings", dependencies=[Depends(need_private)])
def get_settings():
    with db() as c:
        return dc.public_settings(c)


@app.post("/api/settings", dependencies=[Depends(csrf_guard), Depends(need_private)])
def post_settings(body: SettingsBody):
    with db() as c:
        return dc.update_settings(c, body.model_dump())


@app.post("/api/pin", dependencies=[Depends(csrf_guard), Depends(need_private)])
def post_pin(body: NewPinBody, request: Request, response: Response):
    with db() as c:
        if dc.pin_is_set(c):
            if not body.current or not guard.verify(c, body.current):
                raise HTTPException(status_code=403, detail="Current PIN is wrong")
        dc.set_pin(c, body.new)
        guard.end_all()  # old sessions are void; this device gets a fresh one
        set_session_cookie(response, guard.start())
        return {**dc.public_settings(c), "unlocked": True}


@app.post("/api/pin/remove", dependencies=[Depends(csrf_guard), Depends(need_private)])
def remove_pin(body: CurrentPinBody, response: Response):
    with db() as c:
        if not dc.pin_is_set(c):
            return {**dc.public_settings(c), "unlocked": True}
        if not guard.verify(c, body.current):
            raise HTTPException(status_code=403, detail="Current PIN is wrong")
        dc.remove_pin(c)  # also turns texts off and deletes stored texts
        guard.end_all()
        response.delete_cookie(COOKIE, path="/")
        return {**dc.public_settings(c), "unlocked": True}


# ------------------------------------------------------------------------ state


class DoneBody(BaseModel):
    done: bool


@app.get("/api/state", dependencies=[Depends(need_view)])
def get_state(request: Request):
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = (today + timedelta(days=100)).isoformat()
    with db() as c:
        events = [
            dict(r)
            for r in c.execute(
                "SELECT id, title, date, time, details, urgency, needs_review FROM events"
                " WHERE date BETWEEN ? AND ? ORDER BY date, COALESCE(time, '99:99'), id",
                (start, end),
            )
        ]
        reminders = [
            dict(r)
            for r in c.execute(
                "SELECT id, title, due_date, details, urgency, needs_review, category, source, created_at"
                " FROM reminders WHERE done = 0 ORDER BY (due_date IS NULL), due_date,"
                " CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at"
            )
        ]
        review = [
            dict(r)
            for r in c.execute(
                "SELECT 'events' AS kind, id, title, date AS day, details, urgency FROM events WHERE needs_review = 1"
                " UNION ALL "
                "SELECT 'reminders', id, title, due_date, details, urgency FROM reminders"
                " WHERE needs_review = 1 AND done = 0 LIMIT 50"
            )
        ]
        alarms = dc.list_alarms(c)
        session = session_info(c, request)
    return {
        "today": today.isoformat(), "events": events, "reminders": reminders, "review": review,
        "alarms": alarms, "session": session,
    }


class ReminderIn(BaseModel):
    title: str = Field(max_length=200)
    due_date: str | None = Field(default=None, max_length=10)
    details: str | None = Field(default=None, max_length=400)


@app.post("/api/reminders", dependencies=[Depends(csrf_guard), Depends(need_view)])
def add_reminder(body: ReminderIn):
    with db() as c:
        rid = dc.add_manual_reminder(c, body.title, body.due_date, body.details)
    return {"id": rid}


@app.post("/api/reminders/{rid}/done", dependencies=[Depends(csrf_guard), Depends(need_view)])
def set_done(rid: int, body: DoneBody):
    with db() as c:
        cur = c.execute("UPDATE reminders SET done = ? WHERE id = ?", (int(body.done), rid))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ----------------------------------------------------------------------- alarms
# These are declared before the generic /api/{kind}/... routes below on purpose.


class AlarmIn(BaseModel):
    label: str = Field(max_length=200)
    date: str = Field(max_length=10)
    time: str = Field(max_length=5)


class ShiftBody(BaseModel):
    steps: int


class OptLeadBody(BaseModel):
    lead_minutes: int | None = None


class SnoozeBody(BaseModel):
    minutes: int | None = None


@app.get("/api/alarms/due")
def alarms_due():
    # Deliberately not behind the PIN: an alarm must still ring on the lock screen.
    with db() as c:
        return {"alarms": dc.due_alarms(c)}


@app.post("/api/alarms", dependencies=[Depends(csrf_guard), Depends(need_view)])
def add_alarm(body: AlarmIn):
    with db() as c:
        return {"id": dc.add_manual_alarm(c, body.label, body.date, body.time)}


@app.post("/api/events/{event_id}/alarm", dependencies=[Depends(csrf_guard), Depends(need_view)])
def event_alarm(event_id: int, body: OptLeadBody):
    with db() as c:
        ev = c.execute("SELECT id, date, time FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            raise HTTPException(status_code=404, detail="Not found")
        if not ev["time"]:
            raise HTTPException(status_code=400, detail="This appointment has no time, so there is nothing to count back from")
        lead = body.lead_minutes
        if lead is not None and (lead % dc.LEAD_STEP or not 0 <= lead <= dc.MAX_LEAD):
            raise HTTPException(status_code=400, detail="Lead time must be a multiple of 15 minutes, up to 24 hours")
        aid = dc.ensure_event_alarm(c, ev["id"], ev["date"], ev["time"], lead)
        if aid is None:
            raise HTTPException(status_code=400, detail="That appointment has already started")
    return {"id": aid}


@app.post("/api/alarms/{alarm_id}/shift", dependencies=[Depends(csrf_guard), Depends(need_view)])
def shift_alarm(alarm_id: int, body: ShiftBody):
    with db() as c:
        return dc.shift_alarm(c, alarm_id, body.steps)


@app.post("/api/alarms/{alarm_id}/ack", dependencies=[Depends(csrf_guard)])
def ack_alarm(alarm_id: int):
    with db() as c:
        cur = c.execute("UPDATE alarms SET acked=1, snooze_until=NULL WHERE id=?", (alarm_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@app.post("/api/alarms/{alarm_id}/snooze", dependencies=[Depends(csrf_guard)])
def snooze(alarm_id: int, body: SnoozeBody):
    with db() as c:
        return {"until": dc.snooze_alarm(c, alarm_id, body.minutes)}


@app.delete("/api/alarms/{alarm_id}", dependencies=[Depends(csrf_guard), Depends(need_view)])
def delete_alarm(alarm_id: int):
    with db() as c:
        cur = c.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ------------------------------------------------------------------------ texts

UID_PATTERN = r"^[0-9a-f]{24}$"


@app.get("/api/texts", dependencies=[Depends(need_private)])
def get_texts():
    with db() as c:
        s = dc.public_settings(c)
        if not s["texts_enabled"]:
            raise HTTPException(status_code=403, detail="Recent texts are turned off in settings")
        items = [
            dict(r)
            for r in c.execute(
                "SELECT uid, sender, ts, body, is_read, (addr IS NOT NULL) AS can_reply"
                " FROM recent_texts ORDER BY ts DESC, seen_at DESC LIMIT 30"
            )
        ]
        replies = [
            dict(r)
            for r in c.execute(
                "SELECT id, text_uid, to_label, body, status, error, created_at FROM outbox"
                " ORDER BY id DESC LIMIT 20"
            )
        ]
    for it in items:
        it["can_reply"] = bool(it["can_reply"])
        it["is_read"] = bool(it["is_read"])
    return {"items": items, "replies": replies}


class ReplyBody(BaseModel):
    body: str = Field(max_length=2000)


@app.post("/api/texts/{uid}/reply", dependencies=[Depends(csrf_guard), Depends(need_private)])
def reply_to_text(body: ReplyBody, uid: str = PathParam(pattern=UID_PATTERN)):
    with db() as c:
        return {"id": dc.queue_reply(c, uid, body.body)}


# ---------------------------------------------------- generic confirm / delete


@app.post("/api/{kind}/{item_id}/confirm", dependencies=[Depends(csrf_guard), Depends(need_view)])
def confirm(kind: Literal["events", "reminders"], item_id: int):
    with db() as c:
        cur = c.execute(f"UPDATE {TABLES[kind]} SET needs_review = 0 WHERE id = ?", (item_id,))
        if cur.rowcount and kind == "events" and dc.get_setting(c, "auto_alarms") == "1":
            ev = c.execute("SELECT date, time FROM events WHERE id=?", (item_id,)).fetchone()
            if ev and ev["time"]:
                dc.ensure_event_alarm(c, item_id, ev["date"], ev["time"])
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@app.delete("/api/{kind}/{item_id}", dependencies=[Depends(csrf_guard), Depends(need_view)])
def delete_item(kind: Literal["events", "reminders"], item_id: int):
    with db() as c:
        cur = c.execute(f"DELETE FROM {TABLES[kind]} WHERE id = ?", (item_id,))
        if kind == "events":
            c.execute("DELETE FROM alarms WHERE event_id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)
