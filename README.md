# Home Dashboard

A private, offline home dashboard for a able to run on modern linux machines from fedora to debian based and is hardware agnostic arm64 and x86_64. It reads text messages from your Android phone over Bluetooth, uses a small local language model to
pick out appointments and reminders (for example "your prescription is ready for pickup"),
and shows them on a touchscreen kiosk page with a calendar, an agenda and a reminders panel.

Everything runs locally. Nothing is sent to a cloud service, and the services are
sandboxed and locked to the loopback network interface.

> **Status:** the pieces are written and the web UI/database logic is tested. The Bluetooth
> link now runs obexd and the poller in the **kiosk desktop user's session** (BlueZ 5.88's obexd
> won't start for a seatless, lingering service user). 
---

## How it works

```
Android phone
    |  Bluetooth MAP (Message Access Profile)
    v
obexd (BlueZ)  --D-Bus-->  sms_poller.py  --SQLite-->  llm_worker.py  --SQLite-->  dashboard_server.py  -->  Chromium kiosk
 (kiosk user's session)       keyword/sender              Ollama + small LLM          FastAPI + static page      http://127.0.0.1:8080
                           prefilter, queue            JSON extraction + checks
```

1. **sms_poller.py** connects to the phone through obexd, lists the inbox every 90 s, and
   keeps only messages from trusted senders (default `cvs`, `walgreens`) or containing
   keywords (refill, appointment, due, ...). Matches are queued in SQLite. It reads the
   start of each text (up to 255 characters) from MAP's `Subject` field.
2. **llm_worker.py** sends each queued text to Ollama with schema-constrained JSON output.
   It validates everything in Python: dates must parse and be plausible, times must be
   `HH:MM`. An "event" without a trustworthy date becomes a reminder flagged for review.
   Medication names are kept out of titles. Message bodies are blanked after processing.
3. **dashboard_server.py** serves the page and a small JSON API from the same database.
4. **static/** is the kiosk page: clock, month calendar (dots mark days with events),
   agenda for the next 14 days, reminders with Done/Undo, and a "to review" queue.

## Components

| File | What it is |
|---|---|
| `install.sh` | Installs everything on Fedora 45 (packages, user, services, Ollama, model). |
| `uninstall.sh` | Removes it (`--purge` also deletes data, config and the user). |
| `install-kiosk.sh` | Makes the desktop open the dashboard full-screen at login (Cinnamon/LightDM, GDM). |
| `install-clock-keeper.sh` | Remembers the time across reboots on a Pi with no RTC and no network. |
| `sms_poller.py` | Bluetooth MAP poller (uses `dbus-fast`). |
| `llm_worker.py` | Extraction worker (Ollama, default model `gemma3:270m`). |
| `dashboard_server.py` | FastAPI backend and static file host. |
| `static/` | `index.html`, `app.js`, `style.css` for the kiosk page. |
| `test_extraction.py` | Tries the model on sample texts so you can judge accuracy. |

## Requirements

- Raspberry Pi running **Fedora 45** (aarch64) with a Bluetooth adapter and a desktop session
  for the kiosk (written against Cinnamon with LightDM).
- An **Android** phone (MAP). iPhones do not expose full texts over Bluetooth.
- Internet access once, for `dnf` and the model download. Afterwards it can run offline.
- Python dependencies come from Fedora RPMs (`python3-dbus-fast`, `python3-fastapi`,
  `python3-uvicorn`), built for Fedora 45's Python 3.15. There is no pip and no venv.

## Install

Keep all files together exactly as in this folder (`install.sh` expects `static/` beside it).

```bash
# 1. Make sure the clock is right (a Pi with no network starts with a stale clock)
sudo timedatectl set-ntp false
sudo timedatectl set-time "2026-11-13 03:30:00"     # use the real date and time
sudo timedatectl set-ntp true
sudo ./install-clock-keeper.sh                      # remembers the time across restarts

# 2. Install the backend (use your phone's Bluetooth MAC)
sudo ./install.sh --phone-mac AA:BB:CC:DD:EE:FF --desktop-user <desktop-username>
#    options: --model gemma3:1b   --yes (skip the Ollama prompt)
#    <desktop-username> = the auto-login kiosk account (same one as step 4). obexd and the
#    poller run in ITS session, and it joins group home-dash for database access.
#    Reboot (or log that user out and in) afterwards so the group change applies.

# 3. Pair the phone (see below), then allow "Message access" on the phone

# 4. Touchscreen kiosk (use a dedicated NON-admin desktop account)
sudo ./install-kiosk.sh <desktop-username> --autologin
```

If Fedora's own `ollama` package is installed, remove it first (`sudo dnf remove ollama`).
The installer uses Ollama's upstream build, which is much newer.

### Pairing the phone

The poller refuses to start until the phone is **paired and trusted**.

```bash
sudo bluetoothctl
  power on
  agent KeyboardDisplay
  default-agent
  scan on              # wait for the phone, then: scan off
  pair AA:BB:CC:DD:EE:FF      # confirm the number on BOTH screens
  trust AA:BB:CC:DD:EE:FF
  pairable off
  discoverable off
```

On the desktop you can instead pair in Settings > Bluetooth, then run
`sudo bluetoothctl trust <MAC>`. Finally, on the phone, allow **Message access** for the
Pi when it asks.

## Features added after the first release

**PIN.** Settings > Privacy. Set a 4-8 digit PIN with the on-screen keypad, then choose whether the whole dashboard
asks for it on load ("Ask for the PIN when the dashboard opens") or only texts and settings do. It auto-locks after
2 minutes idle (adjustable). 5 wrong tries starts a lock-out (30 s, doubling to 15 min). Alarms still ring on the lock
screen. A PIN keeps visitors out; it does not protect against someone with a shell on the Pi.

**Recent texts.** Settings > "Show recent texts" (needs a PIN). While on, the Pi keeps your last ~40 texts for 3 days
(`TEXT_RETENTION_DAYS`); turning it off, or removing the PIN, deletes them. The panel starts hidden, needs the PIN to
reveal, and hides itself after 60 s of no touches.

**Replying.** Tap Reply on a text from a real phone number (not "CVS"-style names), pick a quick reply or type one
(max 300 characters; 5 replies per 10 minutes). It is sent through the phone over Bluetooth MAP. Failed sends are not
retried automatically because that could text someone twice. *Untested on real hardware:* phones differ in whether
they accept sending over MAP. If it says "Not sent", see Troubleshooting.

**Alarms.** Ringing flashes the whole screen red and white (Settings > Alarm flash: Strong, Gentle or Off; Strong
cycles about once per second, under the 3-per-second photosensitivity guideline; **Preview** shows it). Every appointment with a time gets an alarm 1 hour before (Settings > Alarms to change the default or
turn it off). On the appointment, or on the **Alarms** panel, **15 min earlier** / **15 min later** nudge an alarm by 15 minutes, up
to 5 times either way from its original time (appointment alarms never ring after the appointment starts);
**Off** removes it and **+ Alarm** adds one. **+ Add > Alarm** makes a labelled one-off alarm. Ringing shows a full
screen with Snooze (5/10/15 min) and Dismiss and beeps through the browser. Alarms missed by more than 10 minutes
(for example the Pi was off) stay silent and show "Missed". The alarm only rings while the dashboard page is open
and the Pi's speakers/HDMI audio are working. Re-run `install-kiosk.sh` once so Chromium may play sound unattended.

**Manual reminders.** **+ Add > Reminder** takes a label, optional due date and note. Typing needs a keyboard; on a
touchscreen without one install an on-screen keyboard (for example `onboard`) and enable it in your desktop's
accessibility settings.

## Supported systems

- **Fedora 45** (tested target): packages from dnf, SELinux stays enforcing.
- **Debian-based** (Debian 12/13, Raspberry Pi OS, Ubuntu 22.04+, Linux Mint): `install.sh` detects apt and installs
  `bluez`, `bluez-obexd`, `python3-venv`, curl and zstd. Debian's packaged FastAPI/pydantic are too old for this code,
  so it builds a virtualenv at `/opt/home-dashboard/venv` and installs `fastapi`, `pydantic` (v2), `uvicorn` and
  `dbus-fast` from PyPI into it (internet needed at install; version ranges are pinned loosely, not hash-locked). The
  systemd units use that venv's Python. Everything else (users, sandboxing, Ollama, PIN, alarms) is identical.
  `install-kiosk.sh` also handles `chromium`/`chromium-browser`, GDM3, LightDM and Raspberry Pi OS (labwc autostart,
  screen blanking). The Debian path has not been run on real hardware: report anything that fails.
- Needs systemd with logind (the poller runs in the kiosk user's login session) and Python 3.10+.

## Configuration

Settings live in `/etc/home-dashboard/dashboard.env` (root:home-dash, mode 0640). Restart
the services after editing.

| Key | Default | Meaning |
|---|---|---|
| `PHONE_MAC` | (required) | Bluetooth address of the phone. |
| `TRUSTED_SENDERS` | `cvs,walgreens` | Sender substrings that always go to the model. |
| `POLL_SECONDS` | `90` | How often the inbox is checked. |
| `OLLAMA_MODEL` | `gemma3:270m` | Model tag. `gemma3:1b` is more accurate (about 0.8 GB). |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/chat` | Ollama endpoint. |
| `WORKER_SECONDS` | `30` | How often the worker checks for queued texts. |
| `DASH_DB` | `/var/lib/home-dashboard/dashboard.db` | SQLite database. |
| `DASH_HOST` / `DASH_PORT` | `127.0.0.1` / `8080` | Web bind address. Localhost only by default. |
| `DASH_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host header allowlist (optional key). |
| `DASH_RETENTION_DAYS` | `30` | Old events and finished reminders are deleted after this. |
| `KEEP_BODIES` | `0` | Set to `1` to keep message text after processing (debugging only). |

Add more senders or keywords by editing `TRUSTED_SENDERS` and the `KEYWORDS` pattern in
`sms_poller.py`.

## Day to day

```bash
# Logs (use --no-pager so the terminal doesn't garble). KIOSK = your desktop username
sudo journalctl _UID=$(id -u KIOSK) --no-pager -n 40 -f            # poller + obexd (user services)
sudo journalctl -u home-dashboard-worker --no-pager -n 40 -f
sudo journalctl -u home-dashboard-web --no-pager -n 40 -f

# Status of the poller (run as the desktop user, in its session)
systemctl --user status home-dashboard-poller obex --no-pager

# Restart the poller
systemctl --user restart obex home-dashboard-poller

# Change the model (pulls it with the network lock lifted, then re-locks)
sudo dashboard-pull-model gemma3:1b
sudo sed -i 's|^OLLAMA_MODEL=.*|OLLAMA_MODEL=gemma3:1b|' /etc/home-dashboard/dashboard.env
sudo systemctl restart home-dashboard-worker

# Judge model accuracy on sample texts (run next to llm_worker.py)
python3 test_extraction.py
python3 test_extraction.py gemma3:1b
```

The dashboard is at <http://127.0.0.1:8080/>. Tap a calendar day to filter the agenda to it.
Tap "N to review" to keep or delete items the model was unsure about.

## Security model

- **Dedicated user:** the worker and web UI run as `home-dash` (locked password, no login shell).
- **Poller:** a systemd *user* service enabled for the kiosk desktop user only (its session owns
  the logind seat that obexd needs). Network: `AF_UNIX` only. Refuses to start unless the phone is
  paired and trusted. The kiosk user joins group `home-dash` and shares the database
  (`/var/lib/home-dashboard`, setgid dir `2770`, DB `0660`); so Chromium's account can read and
  write the message database. Use a non-admin kiosk account.
- **Worker and web:** system services with a read-only filesystem, no capabilities,
  `IPAddressDeny=any` plus `IPAddressAllow=localhost`, memory caps and syscall filters.
- **Ollama:** bound to `127.0.0.1`, cloud features off (`OLLAMA_NO_CLOUD=1`), sandboxed, and
  firewalled to loopback after the model is downloaded.
- **Web UI:** localhost only; Host-header allowlist; writes require an `X-Dashboard: 1`
  header (blocks cross-site requests); strict Content-Security-Policy; message-derived text
  is only ever rendered with `textContent`. There is **no login**, so do not expose it to a network.
- **Privacy:** message bodies are blanked after processing; old data is pruned automatically;
  medication names and Rx numbers are kept out of titles and details.
- **SELinux** stays enforcing; **firewalld** needs no changes.

Remaining risks: anyone with physical or root access to the Pi can read the database, so
use full-disk encryption if the device is accessible to others. With `--autologin`, anyone at
the keyboard gets the kiosk user's desktop, so use a non-admin account.

### Hostile text messages (prompt injection, XSS, bad links)

Anyone can text you, so every message is treated as attacker-controlled.

- **Prompt injection.** (1) Characters that hide or reorder text (zero-width, bidi overrides, control characters) are
  stripped when a text arrives. (2) Messages that look like instructions to an AI ("ignore previous instructions",
  fake `system:` roles, JSON/markup aimed at the model, ...) are **quarantined** and never shown to the model (the
  worker log says "Quarantined", never the text). (3) Everything else reaches the model wrapped in random-coded
  "untrusted data" markers, with a system prompt that says to ignore any instructions inside. (4) The model has no
  tools and no network; whatever it returns is re-validated in Python: unknown types are ignored, dates must be
  plausible, and titles/details are scrubbed of links, e-mail addresses, phone and account numbers and markup.
  (5) One sender can create at most `MAX_ITEMS_PER_SENDER_DAY` (default 10) items per day.
  A small model can still be fooled by a cleverly worded text, so the worst case is designed to be a wrong calendar
  entry or reminder, not anything more.
- **XSS.** The page only ever puts text on screen with `textContent`. On top of that the Content-Security-Policy
  includes `require-trusted-types-for 'script'` (the browser refuses HTML-injection sinks), no inline scripts or
  styles, and nothing external can load. `test_logic.py` fails if an HTML sink, an `<a>` tag or an address
  assignment is ever added to the front end.
- **Links in texts.** Shown as buttons, not anchors, so nothing is prefetched, previewed or opened on tap. Tapping
  shows a confirmation with the real domain and the full address; **Open** is the only thing that contacts the site
  (in a new window with `noopener,noreferrer`), and **Copy link** opens nothing. Warnings are shown for look-alike
  (punycode) domains, raw IP addresses, shortened links, plain http and odd ports. Addresses containing
  `user:password@` are refused. Only `http`/`https` are ever recognised.

Residual risks: a spoofed sender name (alphanumeric sender IDs can be faked) can make a "was picked up" text close
that sender's pick-up reminders, and a link can still be a real phishing site that you choose to open. In kiosk mode
the opened site takes over the screen: close its window (Ctrl+W / Alt+F4 on a keyboard, or restart the kiosk) to come
back. If the dashboard has no keyboard, prefer **Copy link** and open it on another device.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Poller log: `Phone ... is not paired` / `not trusted` | Pair and trust the phone (see above). It retries every 30 s. |
| `ModuleNotFoundError: dbus_next` | An old `sms_poller.py` was installed. Re-run `install.sh` with the current files. |
| `PermissionError` connecting to the session bus | A `ProtectHome=yes` unit hides `/run/user`. The current `install.sh` uses `read-only` for the poller. |
| `obex_server_init failed`, then `Connection problem (DBusError)` | obexd is running without a logind seat (BlueZ 5.88). It must run in the kiosk user's graphical session: re-run `install.sh --desktop-user <user>`, reboot, and check `loginctl list-sessions` shows that user with a seat. If it persists, see DEBUG-HANDOFF.md ("If it still fails"). |
| Reply shows "Not sent" | Check Settings on the phone allow "Message access" with sending, then `journalctl _UID=$(id -u KIOSK) --no-pager -n 40` for the error. Some phones refuse MAP sending; receiving is unaffected. |
| `unable to open database file` / `attempt to write a readonly database` in the poller | The kiosk user isn't in group `home-dash` yet (log out/in or reboot), or `dashboard.db` isn't `0660 home-dash:home-dash`. |
| Nothing shows on the dashboard | The poller or worker isn't running, or no text matched the filters. Check both logs. |
| Dates/clock wrong after restart | Pi 4 and earlier have no RTC. Run `install-clock-keeper.sh`. |
| A service crashes right at startup with a memory-mapping error | Remove `MemoryDenyWriteExecute=yes` from that unit (Python 3.15). |
| Kiosk doesn't start at login | Check `~/.config/autostart/dashboard-kiosk.desktop` and Cinnamon's Startup Applications. |
| Screen blanks | Check Power Management and Screensaver settings; the launcher also sets them each login. |
| Terminal looks garbled with `lines 1-27` | That's the `less` pager. Press `q`, run `reset`, and use `--no-pager`. |
| SELinux suspected | `sudo ausearch -m avc,user_avc -ts recent`. (`semodule -DB` shows hidden denials; undo with `semodule -B`.) |

## Uninstall

```bash
sudo ./uninstall.sh          # keeps /var/lib/home-dashboard and /etc/home-dashboard
sudo ./uninstall.sh --purge  # deletes data, config and the home-dash user
```

Ollama, its models and the dnf packages are left in place (the script prints how to remove Ollama).

## Known limitations

- Long texts are truncated to 255 characters (MAP `SubjectLength`). A `Message.Get` fetch could be added.
- The MAP inbox folder name varies by phone; the poller tries `telecom/msg/inbox` variants.
- A 270M-parameter model is weak. Expect misreads; the review queue and validation exist for that reason.
  Use `test_extraction.py` and move to `gemma3:1b` if accuracy is poor.
- No weather or Google Calendar (both need the internet and conflict with the offline design).
  A read-only ICS sync through the worker would be the local-only way to add calendars.
- Tested only in pieces: shell syntax, systemd unit verification and the database/API logic
  were checked; the full stack has not yet run end to end on the Pi.
