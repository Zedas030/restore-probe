#!/bin/bash
# Called by systemd (OnFailure=) when restore-probe.service or restic-backup.service itself crashes,
# i.e. before the tool could report anything. Sends the last log lines to Home Assistant.
set -u
UNIT="${1:-restore-probe.service}"
LOG="$(journalctl -u "$UNIT" -n 15 --no-pager -o cat 2>/dev/null | tail -c 1500)"
/opt/restore-probe/venv/bin/restore-probe alert \
  --title "Restore Probe: $UNIT crashed" \
  --message "The unit exited with an error without reporting itself. Last log lines:
$LOG" || {
  # Last resort if even the tool does not run: plain curl (reads the token from the .env)
  set -a; . /etc/restore-probe/.env 2>/dev/null; set +a
  curl -s -m 15 -H "Authorization: Bearer ${HA_TOKEN:-}" -H "Content-Type: application/json" \
    -X POST "http://127.0.0.1:8123/api/services/persistent_notification/create" \
    -d "{\"notification_id\":\"restore_probe_crash\",\"title\":\"Restore Probe: $UNIT crashed\",\"message\":\"The notifier did not run either. Please check journalctl -u $UNIT.\"}" >/dev/null
}
exit 0
