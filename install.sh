#!/usr/bin/env bash
# install.sh - Install the home dashboard backend (SMS poller + LLM worker + web UI
# + Ollama) with hardened systemd units on Fedora (45) or a Debian-based system
# (Debian 12+/Raspberry Pi OS, Ubuntu 22.04+, Linux Mint, ...).
#
# Put install.sh, sms_poller.py, llm_worker.py, dashboard_server.py and the static/
# folder (index.html, app.js, style.css) in the same folder, then:
#   sudo ./install.sh --phone-mac AA:BB:CC:DD:EE:FF --desktop-user <kiosk-user> [--model gemma3:270m] [--yes]
#
# Layout:
#   /opt/home-dashboard/        code (root-owned, read-only to the services)
#   /var/lib/home-dashboard/    SQLite database (group home-dash, setgid dir mode 2770;
#                               members: home-dash and the kiosk/desktop user)
#   /etc/home-dashboard/        dashboard.env (settings, root:home-dash 0640)
#
# Design:
#   - Python dependencies: on Fedora they come from RPMs (dbus-fast, fastapi, uvicorn): no pip,
#     no venv, nothing from PyPI. On Debian-based systems the packaged FastAPI/pydantic are too
#     old for this code (pydantic v1), so a virtualenv is created in /opt/home-dashboard/venv
#     and fastapi, uvicorn, pydantic and dbus-fast are installed from PyPI into it (needs internet).
#   - sms_poller + obexd run as systemd *user* services of the kiosk/desktop user. BlueZ
#     5.88's obexd refuses to register its Bluetooth transport unless the user owns an
#     active logind seat, which a lingering, seatless "home-dash" user manager never has
#     ("obex_server_init failed"). The auto-logged-in kiosk user does. The poller has NO
#     network access (AF_UNIX only) and shares the SQLite DB through the home-dash group.
#   - llm_worker and the web UI run as system services, loopback-only network.
#   - Ollama comes from the upstream installer (Fedora's own package is far behind),
#     is bound to 127.0.0.1 and, after the model download, firewalled to loopback with
#     IPAddressDeny. Use dashboard-pull-model to change models.
#   - SELinux (Fedora) stays enforcing. Nothing listens off-box, so no firewall changes are needed.
set -euo pipefail

APP_USER=home-dash
APP_DIR=/opt/home-dashboard
STATE_DIR=/var/lib/home-dashboard
CONF_DIR=/etc/home-dashboard
LIB_DIR=/usr/local/lib/home-dashboard
ENV_FILE="$CONF_DIR/dashboard.env"
OLLAMA_KEEP_ALIVE_DEFAULT=10m
MODEL=""
PHONE_MAC=""
DESKTOP_USER=""
ASSUME_YES=0

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }

usage() {
  cat <<USAGE
Usage: sudo $0 --phone-mac AA:BB:CC:DD:EE:FF --desktop-user <user> [--model gemma3:270m] [--yes]
  --phone-mac     Bluetooth MAC of your Android phone (required on first install)
  --desktop-user  The auto-login kiosk/desktop account (required on first install). It runs
                  obexd and the poller, so it must have an active graphical login session.
  --model      Ollama model tag (default: gemma3:270m)
  --yes        Don't prompt before running the official Ollama installer
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phone-mac) PHONE_MAC="${2:-}"; shift 2 ;;
    --desktop-user) DESKTOP_USER="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown argument: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."

OS_ID="$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}")"
OS_LIKE="$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}")"
OS_VER="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-unknown}")"
if [[ "$OS_ID" == "fedora" ]]; then
  FAMILY=fedora
  command -v dnf >/dev/null || die "dnf not found."
  [[ "$OS_VER" == "45" ]] || warn "Written for Fedora 45; you are on $OS_VER. Continuing."
elif [[ "$OS_ID" =~ ^(debian|ubuntu|raspbian|linuxmint|pop|neon)$ || " $OS_LIKE " == *" debian "* || " $OS_LIKE " == *" ubuntu "* ]]; then
  FAMILY=debian
  command -v apt-get >/dev/null || die "apt-get not found."
else
  die "Supported: Fedora and Debian-based systems (Debian, Ubuntu, Raspberry Pi OS, Mint). Found: $OS_ID"
fi
command -v systemctl >/dev/null || die "systemd is required."
PYTHON=/usr/bin/python3

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in sms_poller.py llm_worker.py dashboard_server.py dash_common.py static/index.html static/app.js static/style.css static/customize.js static/themes.css; do
  [[ -f "$SRC_DIR/$f" ]] || die "Missing $f next to install.sh"
done

if [[ -n "$PHONE_MAC" ]]; then
  [[ "$PHONE_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "Invalid MAC address: $PHONE_MAC"
  PHONE_MAC="${PHONE_MAC^^}"
elif [[ ! -f "$ENV_FILE" ]]; then
  usage; die "--phone-mac is required on first install."
fi

# The desktop user may come from a previous install (stored in the env file).
if [[ -z "$DESKTOP_USER" && -f "$ENV_FILE" ]]; then
  DESKTOP_USER="$(grep '^DESKTOP_USER=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
[[ -n "$DESKTOP_USER" ]] || { usage; die "--desktop-user is required on first install."; }
id "$DESKTOP_USER" &>/dev/null || die "No such user: $DESKTOP_USER"
[[ "$DESKTOP_USER" != "$APP_USER" && "$DESKTOP_USER" != "root" ]] \
  || die "--desktop-user must be your kiosk/desktop login account (not root or $APP_USER)."
DESKTOP_UID="$(id -u "$DESKTOP_USER")"
DESKTOP_HOME="$(getent passwd "$DESKTOP_USER" | cut -d: -f6)"
DESKTOP_GROUP="$(id -gn "$DESKTOP_USER")"
[[ -d "$DESKTOP_HOME" ]] || die "Home directory of $DESKTOP_USER not found: $DESKTOP_HOME"

# Fedora's own ollama package is older, stores models elsewhere (/var/lib/ollama) and
# would fight the upstream build for port 11434.
if [[ "$FAMILY" == fedora ]] && rpm -q ollama >/dev/null 2>&1; then
  die "Fedora's 'ollama' package is installed. Remove it first: sudo dnf remove ollama"
fi

# set_env KEY VALUE: replace or append a line in the env file.
set_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

# ---------------------------------------------------------------- packages
if [[ "$FAMILY" == fedora ]]; then
  info "Installing packages with dnf"
  dnf -y install \
    bluez bluez-obexd \
    python3 python3-dbus-fast python3-fastapi python3-uvicorn \
    curl zstd ca-certificates policycoreutils
else
  info "Installing packages with apt"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # bluez-obexd provides the obex user service; python3-venv for the virtualenv below.
  apt-get install -y bluez bluez-obexd python3 python3-venv curl zstd ca-certificates
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 or newer is required (found $(python3 --version 2>&1))."
fi

systemctl enable bluetooth.service
if ! systemctl start bluetooth.service; then
  warn "bluetooth.service did not start. No Bluetooth adapter? Check: rfkill list; ls /sys/class/bluetooth"
fi

# -------------------------------------------------------------------- user
info "Creating service user '$APP_USER'"
getent group bluetooth >/dev/null || groupadd --system bluetooth
if ! id "$APP_USER" &>/dev/null; then
  # Regular (non-system) UID so systemd will run a user manager for it.
  useradd --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin \
          --groups bluetooth "$APP_USER"
  passwd -l "$APP_USER" >/dev/null
else
  usermod -aG bluetooth "$APP_USER"
fi
# Shared state dir: setgid so new files (SQLite journal/WAL/shm) inherit group home-dash.
# The desktop user joins that group so the poller (running in its session) can write the DB.
install -d -o "$APP_USER" -g "$APP_USER" -m 2770 "$STATE_DIR"
chmod 2770 "$STATE_DIR"
usermod -aG "$APP_USER" "$DESKTOP_USER"
# SQLite creates its journal/WAL/shm files with the same mode as the main DB file, so the
# DB itself must be group-writable (0660). Pre-create it (an empty file is a valid DB).
DB_FILE="$STATE_DIR/dashboard.db"
[[ -e "$DB_FILE" ]] || install -o "$APP_USER" -g "$APP_USER" -m 0660 /dev/null "$DB_FILE"
for f in "$DB_FILE" "$DB_FILE"-wal "$DB_FILE"-shm "$DB_FILE"-journal; do
  if [[ -e "$f" ]]; then chgrp "$APP_USER" "$f"; chmod 0660 "$f"; fi
done

# -------------------------------------------------------------------- code
info "Installing application to $APP_DIR"
install -d -m 0755 "$APP_DIR" "$LIB_DIR"

if [[ "$FAMILY" == debian ]]; then
  info "Creating Python virtualenv in $APP_DIR/venv (installs from PyPI; needs internet)"
  python3 -m venv "$APP_DIR/venv"
  "$APP_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-input \
    'fastapi>=0.110,<1' 'pydantic>=2.5,<3' 'uvicorn>=0.27,<1' 'dbus-fast>=2.20,<4'
  PYTHON="$APP_DIR/venv/bin/python"
fi
install -m 0644 "$SRC_DIR/sms_poller.py" "$SRC_DIR/llm_worker.py" "$SRC_DIR/dashboard_server.py" "$SRC_DIR/dash_common.py" "$APP_DIR/"
rm -rf "$APP_DIR/static"
install -d -m 0755 "$APP_DIR/static"
install -m 0644 "$SRC_DIR"/static/index.html "$SRC_DIR"/static/app.js "$SRC_DIR"/static/style.css "$SRC_DIR"/static/customize.js "$SRC_DIR"/static/themes.css "$APP_DIR/static/"

# ------------------------------------------------------------------ config
info "Writing configuration"
install -d -o root -g "$APP_USER" -m 0750 "$CONF_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
PHONE_MAC=${PHONE_MAC}
DASH_DB=${STATE_DIR}/dashboard.db
TRUSTED_SENDERS=cvs,walgreens
POLL_SECONDS=90
OLLAMA_URL=http://127.0.0.1:11434/api/chat
OLLAMA_MODEL=${MODEL:-gemma3:270m}
WORKER_SECONDS=30
DESKTOP_USER=${DESKTOP_USER}
EOF
else
  info "Keeping existing $ENV_FILE"
  [[ -n "$PHONE_MAC" ]] && set_env PHONE_MAC "$PHONE_MAC"
  [[ -n "$MODEL" ]] && set_env OLLAMA_MODEL "$MODEL"
  set_env DESKTOP_USER "$DESKTOP_USER"
fi
ensure_env() { grep -q "^$1=" "$ENV_FILE" || echo "$1=$2" >> "$ENV_FILE"; }
ensure_env DASH_HOST 127.0.0.1
ensure_env DASH_PORT 8080
ensure_env DASH_RETENTION_DAYS 30
ensure_env DASH_STATIC "$APP_DIR/static"
chown root:"$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
MODEL="$(grep '^OLLAMA_MODEL=' "$ENV_FILE" | cut -d= -f2-)"

# ----------------------------------------------- pairing pre-flight script
cat > "$LIB_DIR/check-paired.sh" <<'EOF'
#!/bin/sh
# Refuse to start the poller unless the phone is paired AND trusted.
set -eu
[ -n "${PHONE_MAC:-}" ] || { echo "PHONE_MAC is not set" >&2; exit 1; }
info=$(bluetoothctl info "$PHONE_MAC" 2>/dev/null) || {
  echo "Phone $PHONE_MAC is unknown to BlueZ. Pair it first." >&2; exit 1; }
echo "$info" | grep -q "Paired: yes"  || { echo "Phone is not paired." >&2; exit 1; }
echo "$info" | grep -q "Trusted: yes" || { echo "Phone is not trusted." >&2; exit 1; }
EOF
chmod 0755 "$LIB_DIR/check-paired.sh"

# ------------------------------------------------------- systemd: poller
# User unit (needs the session D-Bus that obexd lives on). Runs only for the desktop/kiosk
# user, whose logind seat session lets obexd register its Bluetooth transport.
info "Installing systemd units"
cat > /etc/systemd/user/home-dashboard-poller.service <<'EOF'
[Unit]
Description=Home Dashboard SMS poller (Bluetooth MAP)
ConditionUser=@DESKTOP_USER@
Wants=obex.service
After=obex.service
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=/etc/home-dashboard/dashboard.env
WorkingDirectory=/var/lib/home-dashboard
ExecStartPre=/usr/local/lib/home-dashboard/check-paired.sh
ExecStart=/usr/bin/python3 -I /opt/home-dashboard/sms_poller.py
Restart=always
RestartSec=30
# 0007, not 0077: the DB is shared with home-dash through the group.
UMask=0007

# Hardening (the poller only needs the session bus socket and its database)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
# RuntimeDirectory: where text replies are written (a bMessage file) for obexd to push to the
# phone. obexd runs outside this sandbox, so /tmp (private here) would be invisible to it.
RuntimeDirectory=home-dashboard
RuntimeDirectoryMode=0700
ReadWritePaths=/var/lib/home-dashboard %t/home-dashboard
# NOT ProtectHome=yes: that also hides /run/user, where the session bus socket lives.
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged
MemoryMax=150M

[Install]
WantedBy=default.target
EOF

sed -i "s/@DESKTOP_USER@/${DESKTOP_USER}/" /etc/systemd/user/home-dashboard-poller.service

# Enable the poller for the desktop user ONLY. (A --global enable would also start it, and
# pull in obexd, in every other user's session, including root's.)
WANTS_DIR="$DESKTOP_HOME/.config/systemd/user/default.target.wants"
install -d -o "$DESKTOP_USER" -g "$DESKTOP_GROUP" -m 0755 \
  "$DESKTOP_HOME/.config" "$DESKTOP_HOME/.config/systemd" "$DESKTOP_HOME/.config/systemd/user" "$WANTS_DIR"
ln -sf /etc/systemd/user/home-dashboard-poller.service "$WANTS_DIR/home-dashboard-poller.service"
chown -h "$DESKTOP_USER:$DESKTOP_GROUP" "$WANTS_DIR/home-dashboard-poller.service"
systemctl --global disable home-dashboard-poller.service >/dev/null 2>&1 || true  # undo older installs

# Clean up the old design (poller + obexd as a lingering home-dash user service). obexd
# failed there with "obex_server_init failed" because that user has no logind seat.
rm -f "$STATE_DIR/.config/systemd/user/default.target.wants/home-dashboard-poller.service"
rm -rf "$STATE_DIR/.config/systemd/user/obex.service.d"
if id "$APP_USER" &>/dev/null; then
  loginctl disable-linger "$APP_USER" >/dev/null 2>&1 || true
  systemctl stop "user@$(id -u "$APP_USER").service" >/dev/null 2>&1 || true
fi

# ------------------------------------------------------- systemd: worker
cat > /etc/systemd/system/home-dashboard-worker.service <<'EOF'
[Unit]
Description=Home Dashboard LLM extraction worker
After=ollama.service
Wants=ollama.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=home-dash
Group=home-dash
EnvironmentFile=/etc/home-dashboard/dashboard.env
WorkingDirectory=/var/lib/home-dashboard
ExecStart=/usr/bin/python3 -I /opt/home-dashboard/llm_worker.py
Restart=always
RestartSec=10
UMask=0007

# Hardening: loopback only, no privileges, read-only system
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/home-dashboard
ProtectHome=yes
ProtectProc=invisible
ProcSubset=pid
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged
IPAddressDeny=any
IPAddressAllow=localhost
MemoryMax=200M

[Install]
WantedBy=multi-user.target
EOF

# --------------------------------------------------------- systemd: web
cat > /etc/systemd/system/home-dashboard-web.service <<'EOF'
[Unit]
Description=Home Dashboard web UI and API (localhost)
After=network.target

[Service]
Type=simple
User=home-dash
Group=home-dash
EnvironmentFile=/etc/home-dashboard/dashboard.env
WorkingDirectory=/var/lib/home-dashboard
ExecStart=/usr/bin/python3 -I /opt/home-dashboard/dashboard_server.py
Restart=always
RestartSec=5
UMask=0007

# Hardening: serves on loopback only
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/home-dashboard
ProtectHome=yes
ProtectProc=invisible
ProcSubset=pid
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged
IPAddressDeny=any
IPAddressAllow=localhost
MemoryMax=200M

[Install]
WantedBy=multi-user.target
EOF

# --------------------------------------------------------------- Ollama
if ! command -v ollama >/dev/null; then
  info "Ollama is not installed. The official installer (https://ollama.com/install.sh) will be run."
  info "Note: the download is large (up to a few GB depending on CPU architecture)."
  if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "Continue? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || die "Ollama is required. Install it, then re-run this script."
  fi
  curl -fsSL https://ollama.com/install.sh | sh
fi

info "Configuring Ollama (localhost only, sandboxed)"
install -d /etc/systemd/system/ollama.service.d
# Drop-ins survive Ollama upgrades; the installer rewrites ollama.service itself.
cat > /etc/systemd/system/ollama.service.d/10-local-hardening.conf <<EOF
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE_DEFAULT}"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
# Switches off Ollama's cloud features (remote inference, web search).
Environment="OLLAMA_NO_CLOUD=1"

NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/usr/share/ollama
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
CapabilityBoundingSet=
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
EOF

# Network lockdown (applied after model download; swapped out by the pull helper).
cat > "$APP_DIR/ollama-lockdown.conf" <<'EOF'
[Service]
IPAddressDeny=any
IPAddressAllow=localhost
EOF

cat > /usr/local/sbin/dashboard-pull-model <<'EOF'
#!/usr/bin/env bash
# Temporarily lift Ollama's network lockdown, pull a model, then re-apply it.
set -euo pipefail
MODEL="${1:?usage: dashboard-pull-model <model-tag>}"
DROPIN=/etc/systemd/system/ollama.service.d/20-no-network.conf
SRC=/opt/home-dashboard/ollama-lockdown.conf

relock() {
  install -m 0644 "$SRC" "$DROPIN"
  systemctl daemon-reload
  systemctl restart ollama
  echo "Ollama network lockdown re-applied."
}
trap relock EXIT

rm -f "$DROPIN"
systemctl daemon-reload
systemctl restart ollama
for _ in $(seq 1 30); do
  ollama list >/dev/null 2>&1 && break
  sleep 1
done
ollama pull "$MODEL"
EOF
chmod 0755 /usr/local/sbin/dashboard-pull-model

systemctl daemon-reload
systemctl enable ollama.service >/dev/null
info "Downloading model '$MODEL' (needs internet; lockdown is applied afterwards)"
/usr/local/sbin/dashboard-pull-model "$MODEL"

# Point the units at the right interpreter (system Python on Fedora, the venv on Debian).
sed -i "s|^ExecStart=/usr/bin/python3 -I|ExecStart=${PYTHON} -I|" \
  /etc/systemd/user/home-dashboard-poller.service \
  /etc/systemd/system/home-dashboard-worker.service \
  /etc/systemd/system/home-dashboard-web.service

# ------------------------------------------------------------- SELinux
# Make sure everything we created carries the default labels for its path.
if command -v restorecon >/dev/null && [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
  info "Restoring SELinux file contexts"
  restorecon -RF "$APP_DIR" "$STATE_DIR" "$LIB_DIR" "$CONF_DIR" \
    /etc/systemd/user/home-dashboard-poller.service \
    "$DESKTOP_HOME/.config/systemd" \
    /etc/systemd/system/home-dashboard-worker.service \
    /etc/systemd/system/home-dashboard-web.service \
    /usr/local/sbin/dashboard-pull-model || true
fi

# ------------------------------------------------------------- start up
info "Enabling and starting services"
systemctl daemon-reload
systemctl enable --now home-dashboard-worker.service home-dashboard-web.service
# No linger and no user manager for home-dash any more: obexd + the poller start with the
# desktop user's login session.
cat <<DONE

Installed. Dashboard: http://127.0.0.1:8080/  (run install-kiosk.sh for the touchscreen).

IMPORTANT: ${DESKTOP_USER} was just added to group '${APP_USER}' (shared database access).
Group changes only apply to a NEW login: reboot now, or log ${DESKTOP_USER} out and back in
(auto-login does this for you at boot). Then obexd and the poller start in that session.
The poller keeps retrying every 30 s until the phone is paired.

NEXT: pair the phone once (as root):
  sudo bluetoothctl
    power on
    agent KeyboardDisplay
    default-agent
    scan on                 # wait for the phone, then: scan off
    pair ${PHONE_MAC:-<MAC>}   # confirm the number on BOTH screens
    trust ${PHONE_MAC:-<MAC>}
    pairable off
    discoverable off
  (On GNOME you can instead pair in Settings > Bluetooth, then run:
     sudo bluetoothctl trust ${PHONE_MAC:-<MAC>} )
  Then on the phone, allow "Message access" for this device.

Useful commands:
  Poller logs:  sudo journalctl _UID=${DESKTOP_UID} --no-pager -f   (poller + obexd)
  Poller state: systemctl --user status home-dashboard-poller obex --no-pager   (as ${DESKTOP_USER})
  Worker logs:  sudo journalctl -u home-dashboard-worker -f
  Web logs:     sudo journalctl -u home-dashboard-web -f
  Restart poller: systemctl --user restart obex home-dashboard-poller   (as ${DESKTOP_USER})
  Settings:     sudo nano ${ENV_FILE}   (then restart the services)
  Change model: sudo dashboard-pull-model <tag>  (then edit OLLAMA_MODEL)
  SELinux denials (Fedora): sudo ausearch -m avc -ts recent
  Security score:  systemd-analyze security home-dashboard-worker.service
DONE
