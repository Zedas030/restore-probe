"""Outgoing reports: Home Assistant sensors and notifications, Prometheus textfile.

Everything here is best effort: an unreachable Home Assistant must not prevent the report, but it is logged so
that the dead-man's switch in Home Assistant (stale heartbeat) fires.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import requests

from .config import Config
from .util import fmt_duration

log = logging.getLogger("restore_probe.notify")

STATUS_NUM = {"ok": 0, "warn": 1, "fail": 2, "skipped": 3}
ICON = {"ok": "mdi:backup-restore", "warn": "mdi:alert", "fail": "mdi:alert-octagon", "skipped": "mdi:minus-circle-outline"}


class HomeAssistantNotifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg.home_assistant
        self.enabled = bool(self.cfg.enabled and self.cfg.token)
        self.errors: list[str] = []

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = requests.post(f"{self.cfg.url}{path}", json=payload, headers=self._headers(), timeout=20)
            if r.status_code >= 300:
                self.errors.append(f"Home Assistant {path}: HTTP {r.status_code} {r.text[:100]}")
                return False
            return True
        except requests.RequestException as e:
            self.errors.append(f"Home Assistant {path}: {type(e).__name__}")
            return False

    def set_state(self, entity: str, state, attributes: dict) -> bool:
        return self._post(f"/api/states/{entity}", {"state": state, "attributes": attributes})

    def publish_run(self, run: dict) -> None:
        if not self.enabled:
            return
        p = self.cfg.sensor_prefix
        url = os.environ.get("RESTORE_PROBE_REPORT_URL", "")
        for s in run["services"]:
            b = s.get("backup") or {}
            base = {"friendly_name": f"Restore Probe {s['title']}", "run_id": run["run_id"], "message": s.get("message", ""),
                    "backup": b.get("label"), "backup_date": b.get("date"),
                    "stages_ok": sum(1 for x in s.get("stages", []) if x["ok"]),
                    "image": s.get("image", ""), "report_url": url}
            self.set_state(f"sensor.{p}_{s['name']}_status", s["status"], {**base, "icon": ICON.get(s["status"], "mdi:help")})
            self.set_state(f"sensor.{p}_{s['name']}_backup_age",
                           round(b["age_hours"], 1) if b.get("age_hours") is not None else "unknown",
                           {"friendly_name": f"Restore Probe {s['title']} backup age", "unit_of_measurement": "h",
                            "state_class": "measurement", "icon": "mdi:clock-outline"})
            self.set_state(f"sensor.{p}_{s['name']}_restore_duration",
                           s.get("restore_seconds") if s.get("restore_seconds") is not None else "unknown",
                           {"friendly_name": f"Restore Probe {s['title']} restore duration", "unit_of_measurement": "s",
                            "device_class": "duration", "state_class": "measurement", "icon": "mdi:timer-outline"})
        problems = [s for s in run["services"] if s["status"] in ("warn", "fail")]
        self.set_state(f"sensor.{p}_status", run["status"], {
            "friendly_name": "Restore Probe overall status", "icon": ICON.get(run["status"], "mdi:help"),
            "run_id": run["run_id"], "problems": len(problems), "problem_services": [s["title"] for s in problems],
            "error": run.get("error", ""), "duration": fmt_duration(run.get("duration_seconds", 0)), "report_url": url})
        finished = datetime.fromtimestamp(run["finished"] or run["started"]).astimezone()
        self.set_state(f"sensor.{p}_last_run", finished.isoformat(timespec="seconds"), {
            "friendly_name": "Restore Probe last run", "device_class": "timestamp", "icon": "mdi:calendar-clock"})
        # Heartbeat for the dead-man's switch: input_datetime survives HA restarts, API-set sensors do not
        if self.cfg.heartbeat_entity:
            self._post("/api/services/input_datetime/set_datetime",
                       {"entity_id": self.cfg.heartbeat_entity, "datetime": finished.strftime("%Y-%m-%d %H:%M:%S")})

    def alert(self, title: str, message: str) -> None:
        if not self.enabled:
            return
        if self.cfg.persistent_notification:
            self._post("/api/services/persistent_notification/create",
                       {"notification_id": "restore_probe", "title": title, "message": message})
        if self.cfg.notify_service:
            domain, _, service = self.cfg.notify_service.partition(".")
            self._post(f"/api/services/{domain}/{service}", {"title": title, "message": message,
                                                              "data": {"tag": "restore-probe", "group": "restore-probe"}})

    def clear_alert(self) -> None:
        if self.enabled and self.cfg.persistent_notification:
            self._post("/api/services/persistent_notification/dismiss", {"notification_id": "restore_probe"})


def compose_alert(run: dict) -> tuple[str, str] | None:
    """Text for the notification; None when everything is fine."""
    if run.get("error"):
        return "Restore Probe: run aborted", run["error"]
    problems = [s for s in run["services"] if s["status"] in ("warn", "fail")]
    if not problems:
        return None
    fails = [s for s in problems if s["status"] == "fail"]
    title = f"Restore Probe: {len(fails)} backup(s) not restorable" if fails else "Restore Probe: notes"
    lines = []
    for s in problems:
        icon = "❌" if s["status"] == "fail" else "⚠️"
        lines.append(f"{icon} {s['title']}: {s.get('message', '')[:300]}")
    return title, "\n".join(lines)


def compose_summary(run: dict) -> str:
    lines = []
    for s in run["services"]:
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(s["status"], "⏭️")
        lines.append(f"{icon} {s['title']}: {s.get('message', '')}")
    return "\n".join(lines)


def write_prometheus_textfile(path: Path, run: dict) -> None:
    """Metrics for the node_exporter textfile collector. Written atomically, otherwise it reads half files."""
    out: list[str] = []

    def m(name, value, labels=None, help_=None):
        if help_:
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} gauge")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{str(v).replace(chr(34), chr(39))}"' for k, v in labels.items()) + "}"
        out.append(f"{name}{lab} {value}")

    m("restore_probe_last_run_timestamp_seconds", int(run["finished"] or run["started"]), help_="End of the last run (Unix time)")
    m("restore_probe_run_duration_seconds", run.get("duration_seconds", 0), help_="Duration of the last run")
    m("restore_probe_run_status", STATUS_NUM.get(run["status"], 2), help_="Overall status: 0 ok, 1 warning, 2 failure")
    m("restore_probe_isolation_ok", 1 if (run.get("isolation") or {}).get("passed") else 0,
      help_="Sandbox isolation proven (1) or not (0)")
    first = True
    for s in run["services"]:
        b = s.get("backup") or {}
        lbl = {"service": s["name"]}
        m("restore_probe_service_status", STATUS_NUM.get(s["status"], 2), lbl,
          help_="Status per service: 0 ok, 1 warning, 2 failure, 3 skipped" if first else None)
        if s["status"] == "skipped":  # disabled services must not look like failures in alerts
            first = False
            continue
        m("restore_probe_service_ok", 1 if s["status"] in ("ok", "warn") else 0, lbl,
          help_="1 if the backup is restorable (status ok or warn)" if first else None)
        if b.get("age_hours") is not None:
            m("restore_probe_backup_age_hours", b["age_hours"], lbl, help_="Age of the checked backup in hours" if first else None)
        if b.get("timestamp"):
            m("restore_probe_backup_timestamp_seconds", int(b["timestamp"]), lbl, help_="Timestamp of the checked backup" if first else None)
        if s.get("restore_seconds") is not None:
            m("restore_probe_restore_duration_seconds", s["restore_seconds"], lbl, help_="Restore duration" if first else None)
        if s.get("total_seconds") is not None:
            m("restore_probe_service_duration_seconds", s["total_seconds"], lbl, help_="Total check duration per service" if first else None)
        m("restore_probe_stages_passed", sum(1 for x in s.get("stages", []) if x["ok"]), lbl, help_="Stages passed (0-3)" if first else None)
        for mt in s.get("metrics", []):
            if mt.get("production") is not None:
                m("restore_probe_metric_production", mt["production"], {**lbl, "metric": mt["key"]})
            if mt.get("restored") is not None:
                m("restore_probe_metric_restored", mt["restored"], {**lbl, "metric": mt["key"]})
        first = False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".prom.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
