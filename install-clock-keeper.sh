#!/usr/bin/env bash
# install-clock-keeper.sh - Keep the clock roughly right across reboots on a Raspberry
# Pi with no battery-backed RTC (Pi 4 and earlier) and no network. Fedora equivalent of
# Debian's fake-hwclock.
#
#   1. Set the correct time first:
#        sudo timedatectl set-ntp false
#        sudo timedatectl set-time "2026-11-13 03:30:00"
#        sudo timedatectl set-ntp true      # so it self-corrects when a network is present
#   2. sudo ./install-clock-keeper.sh [--yes]
#
# What it does: saves the time every 15 minutes and at shutdown, and restores it at boot
# (only ever moving the clock forward). After a power cut the clock is at most ~15 minutes
# behind instead of weeks behind. Once a network is available, chrony corrects it exactly.
set -euo pipefail

ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required." >&2; exit 1; }

if ls /dev/rtc* >/dev/null 2>&1; then
  echo "Note: this machine has a hardware clock (/dev/rtc*). With a battery fitted you may not"
  echo "      need this; 'sudo hwclock --systohc' writes the time to it. Installing anyway."
fi

echo "Current system time: $(date)  (timezone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown))"
if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "Is that correct? The saved time starts from this. [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Set the correct time first (see the top of this script)."; exit 1; }
fi

install -d -m 0755 /var/lib/clock-keeper

cat > /usr/local/sbin/clock-keeper <<'EOF'
#!/bin/sh
# clock-keeper: remember the time across reboots on boards without a battery-backed RTC.
#   clock-keeper restore      move the clock forward to the last saved time (never backwards)
#   clock-keeper save         remember the current time (never goes backwards)
#   clock-keeper force-save   remember the current time unconditionally
set -eu
DIR="${CLOCK_KEEPER_DIR:-/var/lib/clock-keeper}"
F="$DIR/time"
now=$(date -u +%s)
saved=0
[ -r "$F" ] && saved=$(cat "$F" 2>/dev/null || echo 0)
case "$saved" in ''|*[!0-9]*) saved=0 ;; esac
write() { mkdir -p "$DIR"; echo "$1" > "$F.tmp" && mv -f "$F.tmp" "$F"; }
case "${1:-}" in
  restore)
    if [ "$saved" -gt "$now" ]; then
      if [ -n "${CLOCK_KEEPER_DRYRUN:-}" ]; then
        echo "would set clock to @$saved"
      else
        date -u -s "@$saved" >/dev/null
      fi
    fi ;;
  save)
    if [ "$now" -ge "$saved" ]; then write "$now"; fi ;;
  force-save)
    write "$now" ;;
  *)
    echo "usage: clock-keeper restore|save|force-save" >&2; exit 2 ;;
esac
EOF
chmod 0755 /usr/local/sbin/clock-keeper

cat > /etc/systemd/system/clock-keeper.service <<'EOF'
[Unit]
Description=Restore the clock at boot, save it at shutdown (no RTC)
DefaultDependencies=no
RequiresMountsFor=/var/lib/clock-keeper
After=local-fs.target
Before=sysinit.target time-sync.target shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/clock-keeper restore
ExecStop=/usr/local/sbin/clock-keeper save

[Install]
WantedBy=sysinit.target
EOF

cat > /etc/systemd/system/clock-keeper-save.service <<'EOF'
[Unit]
Description=Save the current time (periodic)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/clock-keeper save
EOF

cat > /etc/systemd/system/clock-keeper-save.timer <<'EOF'
[Unit]
Description=Save the current time every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF

if command -v restorecon >/dev/null; then
  restorecon -RF /usr/local/sbin/clock-keeper /var/lib/clock-keeper \
    /etc/systemd/system/clock-keeper.service \
    /etc/systemd/system/clock-keeper-save.service \
    /etc/systemd/system/clock-keeper-save.timer || true
fi

systemctl daemon-reload
/usr/local/sbin/clock-keeper force-save
systemctl enable --now clock-keeper.service clock-keeper-save.timer

echo
echo "Done. Saved: $(date -u -d "@$(cat /var/lib/clock-keeper/time)")  (UTC)"
echo "Test: sudo reboot, then run: date"
echo "If you ever set the clock EARLIER than the saved time, run: sudo clock-keeper force-save"
