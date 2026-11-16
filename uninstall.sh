#!/usr/bin/env bash
# uninstall.sh - Remove the home dashboard backend from Fedora.
#   sudo ./uninstall.sh            # remove services and code, KEEP data and config
#   sudo ./uninstall.sh --purge    # also delete the database, config and the user
# Left alone: Ollama itself, downloaded models and the dnf/apt packages (bluez, python3-*).
# To remove Ollama (installed by its upstream script, so dnf/apt don't know about it):
#   sudo systemctl disable --now ollama; sudo rm -rf /etc/systemd/system/ollama.service* \
#     /usr/local/lib/ollama /usr/local/bin/ollama /usr/share/ollama; sudo userdel ollama
set -euo pipefail

APP_USER=home-dash
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

# Desktop/kiosk user that ran obexd + the poller (recorded by install.sh).
DESKTOP_USER="$(grep '^DESKTOP_USER=' /etc/home-dashboard/dashboard.env 2>/dev/null | cut -d= -f2- || true)"
if [[ -n "$DESKTOP_USER" ]] && id "$DESKTOP_USER" &>/dev/null; then
  DHOME="$(getent passwd "$DESKTOP_USER" | cut -d: -f6)"
  rm -f "$DHOME/.config/systemd/user/default.target.wants/home-dashboard-poller.service"
  # Stop the running poller in that user's session, if any.
  DUID="$(id -u "$DESKTOP_USER")"
  if [[ -S "/run/user/$DUID/bus" ]]; then
    runuser -u "$DESKTOP_USER" -- env XDG_RUNTIME_DIR="/run/user/$DUID" \
      systemctl --user stop home-dashboard-poller.service 2>/dev/null || true
  fi
  gpasswd -d "$DESKTOP_USER" "$APP_USER" >/dev/null 2>&1 || true
fi

if id "$APP_USER" &>/dev/null; then
  UID_APP="$(id -u "$APP_USER")"
  systemctl stop "user@${UID_APP}.service" 2>/dev/null || true
  loginctl disable-linger "$APP_USER" 2>/dev/null || true
fi

systemctl disable --now home-dashboard-worker.service home-dashboard-web.service 2>/dev/null || true
systemctl --global disable home-dashboard-poller.service 2>/dev/null || true
rm -f /var/lib/home-dashboard/.config/systemd/user/default.target.wants/home-dashboard-poller.service

rm -f /etc/systemd/system/home-dashboard-worker.service \
      /etc/systemd/system/home-dashboard-web.service \
      /etc/systemd/user/home-dashboard-poller.service \
      /etc/systemd/system/ollama.service.d/10-local-hardening.conf \
      /etc/systemd/system/ollama.service.d/20-no-network.conf \
      /usr/local/sbin/dashboard-pull-model
rm -rf /opt/home-dashboard /usr/local/lib/home-dashboard

systemctl daemon-reload
systemctl restart ollama.service 2>/dev/null || true

if [[ $PURGE -eq 1 ]]; then
  rm -rf /etc/home-dashboard /var/lib/home-dashboard
  if id "$APP_USER" &>/dev/null; then userdel "$APP_USER" 2>/dev/null || true; fi
  echo "Purged data, config and user."
else
  echo "Kept /var/lib/home-dashboard and /etc/home-dashboard. Use --purge to delete them."
fi
echo "Done."
