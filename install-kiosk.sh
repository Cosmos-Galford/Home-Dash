#!/usr/bin/env bash
# install-kiosk.sh - Make a desktop session (Cinnamon, GNOME, Raspberry Pi OS, ...) on Fedora or a
# Debian-based system open the dashboard full-screen at login.
#   sudo ./install-kiosk.sh <desktop-username> [--autologin] [url]
#
# <desktop-username>  the account that runs the kiosk session (NOT home-dash).
#                     Use a dedicated non-admin account (no sudo/wheel): with auto-login,
#                     anyone at the keyboard gets that account's desktop.
# --autologin         configure the display manager (LightDM, or GDM/GDM3) to log
#                     that user in automatically at boot.
# url                 default: http://127.0.0.1:8080/?kiosk=1
set -euo pipefail

USER_NAME="${1:?usage: sudo $0 <desktop-username> [--autologin] [url]}"
shift
AUTOLOGIN=0
URL="http://127.0.0.1:8080/?kiosk=1"
for arg in "$@"; do
  case "$arg" in
    --autologin) AUTOLOGIN=1 ;;
    *) URL="$arg" ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
id "$USER_NAME" &>/dev/null || { echo "No such user: $USER_NAME" >&2; exit 1; }
[[ "$USER_NAME" != "home-dash" ]] || { echo "Use your desktop login account, not home-dash." >&2; exit 1; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
GROUP_NAME="$(id -gn "$USER_NAME")"

if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qxE 'wheel|sudo|adm'; then
  echo "WARNING: $USER_NAME is an administrator (wheel/sudo/adm). A dedicated non-admin kiosk user is safer." >&2
fi

# Package manager: dnf (Fedora) or apt-get (Debian, Ubuntu, Raspberry Pi OS, Mint).
pkg_install() {
  if command -v dnf >/dev/null; then dnf -y install "$@"
  elif command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
  else echo "No supported package manager (dnf/apt-get). Install manually: $*" >&2; return 1
  fi
}

# This must be the same account as install.sh --desktop-user: obexd and the poller run in
# its login session (BlueZ 5.88's obexd needs an active logind seat).
CFG_USER="$(grep '^DESKTOP_USER=' /etc/home-dashboard/dashboard.env 2>/dev/null | cut -d= -f2- || true)"
if [[ -n "$CFG_USER" && "$CFG_USER" != "$USER_NAME" ]]; then
  echo "WARNING: install.sh was run with --desktop-user $CFG_USER, but you are setting up $USER_NAME." >&2
  echo "         The Bluetooth poller only runs for $CFG_USER. Re-run install.sh with --desktop-user $USER_NAME." >&2
fi

if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  # Debian/Raspberry Pi OS call it "chromium"; Ubuntu only ships "chromium-browser" (a snap).
  pkg_install chromium || pkg_install chromium-browser
fi
command -v curl >/dev/null || pkg_install curl

cat > /usr/local/bin/dashboard-kiosk <<'LAUNCHER'
#!/bin/sh
URL="${1:-http://127.0.0.1:8080/?kiosk=1}"
BROWSER="$(command -v chromium || command -v chromium-browser)" || exit 1

# Only one kiosk at a time (some desktops start autostart entries twice). The browser inherits
# this lock, so it is held for as long as the kiosk runs.
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/dashboard-kiosk.lock"
command -v flock >/dev/null 2>&1 && { flock -n 9 || exit 0; }

# Keep the screen awake and unlocked. Cinnamon keys first, GNOME keys as a fallback;
# a schema that doesn't exist is silently ignored.
if command -v gsettings >/dev/null 2>&1; then
  gs() { gsettings set "$@" 2>/dev/null || true; }
  # Cinnamon
  gs org.cinnamon.desktop.session idle-delay 0
  gs org.cinnamon.desktop.screensaver lock-enabled false
  gs org.cinnamon.desktop.lockdown disable-lock-screen true
  gs org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0
  gs org.cinnamon.settings-daemon.plugins.power sleep-display-battery 0
  gs org.cinnamon.settings-daemon.plugins.power sleep-inactive-ac-timeout 0
  gs org.cinnamon.settings-daemon.plugins.power sleep-inactive-battery-timeout 0
  gs org.cinnamon.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
  gs org.cinnamon.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
  # GNOME
  gs org.gnome.desktop.session idle-delay 0
  gs org.gnome.desktop.screensaver lock-enabled false
  gs org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
fi
# X11 sessions: also turn off the X screensaver and display power-saving.
if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null 2>&1; then
  xset s off 2>/dev/null || true
  xset s noblank 2>/dev/null || true
  xset -dpms 2>/dev/null || true
fi

# Wait up to ~2 minutes for the dashboard service to come up.
i=0
until curl -fs -o /dev/null "$URL"; do
  i=$((i + 1))
  [ "$i" -gt 60 ] && break
  sleep 2
done

exec "$BROWSER" --kiosk --incognito --noerrdialogs --disable-infobars --no-first-run \
  --ozone-platform-hint=auto --password-store=basic --disable-features=Translate \
  --overscroll-history-navigation=0 --disable-pinch \
  --autoplay-policy=no-user-gesture-required "$URL"
LAUNCHER
chmod 0755 /usr/local/bin/dashboard-kiosk
command -v restorecon >/dev/null && restorecon -F /usr/local/bin/dashboard-kiosk || true

AUTOSTART_DIR="$HOME_DIR/.config/autostart"
install -d -o "$USER_NAME" -g "$GROUP_NAME" "$HOME_DIR/.config" "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/dashboard-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Home Dashboard Kiosk
Exec=/usr/local/bin/dashboard-kiosk "${URL}"
X-GNOME-Autostart-enabled=true
DESKTOP
chown "$USER_NAME":"$GROUP_NAME" "$AUTOSTART_DIR/dashboard-kiosk.desktop"
command -v restorecon >/dev/null && restorecon -RF "$HOME_DIR/.config" || true

# Raspberry Pi OS (labwc/Wayfire): also start it from labwc's own autostart if that is in use.
# The launcher's lock makes a double start harmless.
if [[ -d "$HOME_DIR/.config/labwc" ]] || { command -v labwc >/dev/null && [[ -d /etc/xdg/labwc ]]; }; then
  install -d -o "$USER_NAME" -g "$GROUP_NAME" "$HOME_DIR/.config/labwc"
  LW="$HOME_DIR/.config/labwc/autostart"
  grep -qs 'dashboard-kiosk' "$LW" || echo "/usr/local/bin/dashboard-kiosk \"${URL}\" &" >> "$LW"
  chown "$USER_NAME":"$GROUP_NAME" "$LW"
fi
# Raspberry Pi OS: switch off screen blanking (best effort; harmless elsewhere).
command -v raspi-config >/dev/null && raspi-config nonint do_blanking 1 2>/dev/null || true

if [[ $AUTOLOGIN -eq 1 ]]; then
  if [[ -d /etc/lightdm ]]; then
    # Cinnamon's and Raspberry Pi OS's usual display manager.
    install -d /etc/lightdm/lightdm.conf.d
    {
      echo "[Seat:*]"
      echo "autologin-user=${USER_NAME}"
      echo "autologin-user-timeout=0"
      [[ -f /usr/share/xsessions/cinnamon.desktop ]] && echo "autologin-session=cinnamon"
    } > /etc/lightdm/lightdm.conf.d/50-dashboard-kiosk.conf
    echo "LightDM auto-login enabled for $USER_NAME (/etc/lightdm/lightdm.conf.d/50-dashboard-kiosk.conf)."
  elif [[ -f /etc/gdm/custom.conf || -f /etc/gdm3/custom.conf ]]; then
    CONF=/etc/gdm/custom.conf
    [[ -f "$CONF" ]] || CONF=/etc/gdm3/custom.conf   # Debian and Ubuntu use gdm3
    cp -a "$CONF" "$CONF.bak.$(date +%s)"
    sed -i '/^AutomaticLoginEnable=/d;/^AutomaticLogin=/d' "$CONF"
    if grep -q '^\[daemon\]' "$CONF"; then
      sed -i "/^\[daemon\]/a AutomaticLoginEnable=True\nAutomaticLogin=${USER_NAME}" "$CONF"
    else
      printf '\n[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=%s\n' "$USER_NAME" >> "$CONF"
    fi
    echo "GDM auto-login enabled for $USER_NAME (backup saved next to $CONF)."
  else
    echo "Neither LightDM nor GDM found. Set up auto-login in your display manager (on Raspberry Pi OS: sudo raspi-config > System Options > Boot / Auto Login)." >&2
  fi
fi

cat <<DONE
Kiosk installed for $USER_NAME. Log in as that user, or run: dashboard-kiosk

Also:
  - The launcher turns off screen blanking, sleep and the lock screen for that session
    (Cinnamon, GNOME and plain X11). Check System Settings > Power Management and
    Screensaver afterwards if the screen still goes dark.
  - Check the timezone (timedatectl) so the clock and dates are right.
  - If it doesn't start at login, check: ls ~/.config/autostart/dashboard-kiosk.desktop
    (Cinnamon: System Settings > Startup Applications should list "Home Dashboard Kiosk").
  - Remove auto-login later by deleting /etc/lightdm/lightdm.conf.d/50-dashboard-kiosk.conf.
DONE
