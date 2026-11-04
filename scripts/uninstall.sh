#!/bin/bash
# Remove Restore Probe completely. Configuration and history stay unless --purge is given.
#   sudo ./scripts/uninstall.sh [--purge]
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Please run as root (sudo)."; exit 1; }
PURGE=0; [[ "${1:-}" == "--purge" ]] && PURGE=1

systemctl disable --now restore-probe.timer restic-backup.timer 2>/dev/null || true
systemctl stop restore-probe.service restic-backup.service 2>/dev/null || true
/opt/restore-probe/venv/bin/restore-probe cleanup 2>/dev/null || true
rm -f /etc/systemd/system/restore-probe.service /etc/systemd/system/restore-probe.timer \
      /etc/systemd/system/restore-probe-failed@.service /etc/systemd/system/restic-backup.service \
      /etc/systemd/system/restic-backup.timer /etc/logrotate.d/restore-probe /usr/local/bin/restore-probe
rm -rf /etc/systemd/system/restic-backup.service.d
systemctl daemon-reload

# Sandbox network and iptables rule
docker network rm restore-probe 2>/dev/null || true
iptables -S INPUT | grep -- '-i rp-sandbox' | grep -- '-j DROP' | sed 's/^-A /-D /' | while read -r spec; do
  eval "iptables $spec" 2>/dev/null || true
done

# nginx site
if [[ -e /etc/nginx/sites-enabled/restore-probe ]]; then
  rm -f /etc/nginx/sites-enabled/restore-probe /etc/nginx/sites-available/restore-probe
  nginx -t && systemctl reload nginx
fi

rm -f /var/lib/prometheus/node-exporter/restore_probe.prom /var/lib/prometheus/node-exporter/restore_probe_backup.prom
rm -rf /opt/restore-probe
if [[ $PURGE -eq 1 ]]; then
  rm -rf /etc/restore-probe /var/lib/restore-probe /var/log/restore-probe
  echo "Removed including configuration and history. The restic repository is kept."
else
  echo "Removed. Configuration (/etc/restore-probe) and history (/var/lib/restore-probe) are kept; --purge removes them too."
fi
echo "Not reverted: --web.enable-admin-api in /etc/default/prometheus (backup next to it), the HA automation 'Restore Probe: no report for 36 h' and input_datetime.restore_probe_last_run."
