"""Probe module for Grafana (native installation, data in /var/lib/grafana, configuration in /etc/grafana).

Source: restic snapshot tagged 'grafana' (grafana.db as SQLite online backup, plugins, /etc/grafana).
Sandbox: official image in production's version, anonymous viewer access so the API can count without a
password. Stages: 1 /api/health · 2 /login · 3 dashboards and data sources vs. the production DB.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import requests

from ..util import Deadline, ProbeError, run
from .base import Plugin


def count_grafana_db(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return {
            "dashboards": con.execute("SELECT COUNT(*) FROM dashboard WHERE is_folder=0").fetchone()[0],
            "folders": con.execute("SELECT COUNT(*) FROM dashboard WHERE is_folder=1").fetchone()[0],
            "datasources": con.execute("SELECT COUNT(*) FROM data_source").fetchone()[0],
            "users": con.execute("SELECT COUNT(*) FROM user").fetchone()[0],
            "quick_check": con.execute("PRAGMA quick_check").fetchone()[0],
        }
    finally:
        con.close()


def detect_grafana_version() -> str | None:
    for cmd in (["grafana", "server", "-v"], ["grafana-server", "-v"]):
        res = run(cmd, check=False, quiet=True, timeout=30)
        if res.ok and res.stdout:
            parts = res.stdout.split()
            if len(parts) >= 2:
                return parts[1]
    return None


class GrafanaPlugin(Plugin):
    port = 3000

    def prepare(self) -> None:
        db = Path(self.opt.get("production_db", "/var/lib/grafana/grafana.db"))
        if not db.exists():
            raise ProbeError(f"Production DB missing: {db}")
        self.production = count_grafana_db(db)
        if not self.opt.get("image") or self.opt.get("image") == "auto":
            v = detect_grafana_version()
            if not v:
                raise ProbeError("Cannot detect the Grafana version (set options.image, e.g. grafana/grafana:13.3.0)")
            self.opt["image"] = f"grafana/grafana:{v}"

    def after_restore(self, info) -> None:
        # Assemble the sandbox data directory from the restored pieces
        db = self.find_file("grafana.db", option="db_path")
        self.data = self.work / "_grafana_data"
        self.data.mkdir()
        shutil.move(str(db), self.data / "grafana.db")
        plugins = self.find_dir("grafana/plugins", option="plugins_path", required=False)
        if plugins:
            shutil.move(str(plugins), self.data / "plugins")
        else:
            self.warn("Backup contains no plugins directory (plugins are missing in the sandbox)")
        self.provisioning = self.find_dir("grafana/provisioning", option="provisioning_path", required=False)
        # The image runs as UID 472 – the data must belong to it
        run(["chown", "-R", "472:0", str(self.data)])
        if self.provisioning:
            run(["chown", "-R", "472:0", str(self.provisioning)])
        self.restored_counts = count_grafana_db(self.data / "grafana.db")

    def start(self, deadline: Deadline) -> None:
        mounts = [(self.data, "/var/lib/grafana", "rw")]
        env = {
            "GF_SECURITY_ADMIN_PASSWORD": "restore-probe",  # only takes effect if there were no DB
            "GF_AUTH_ANONYMOUS_ENABLED": "true",
            "GF_AUTH_ANONYMOUS_ORG_ROLE": self.opt.get("anonymous_role", "Viewer"),
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
            "GF_SERVER_HTTP_PORT": str(self.port),
            "GF_PATHS_PLUGINS": "/var/lib/grafana/plugins",
            "GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS": self.opt.get("allow_unsigned_plugins", ""),
        }
        if self.provisioning:
            mounts.append((self.provisioning, "/etc/grafana/provisioning", "ro"))
        self.container = self.sandbox.run_container(self.svc.name, self.image(), mounts=mounts, env=env)
        self.sandbox.wait_running(self.container, deadline, "Grafana")
        self.ip = self.sandbox.container_ip(self.container)

    def _url(self, p: str) -> str:
        return f"http://{self.ip}:{self.port}{p}"

    def stage1(self, deadline: Deadline) -> str:
        r = self.wait_http("/api/health", deadline, what="Grafana /api/health")
        j = r.json()
        if j.get("database") != "ok":
            raise ProbeError(f"/api/health reports database={j.get('database')}")
        return f"health ok, version {j.get('version')}"

    def stage2(self, deadline: Deadline) -> str:
        r = self.wait_http("/login", deadline, ok_status=(200, 302), what="Grafana /login")
        return f"login page HTTP {r.status_code}"

    def stage3(self, deadline: Deadline) -> str:
        r = requests.get(self._url("/api/search"), params={"type": "dash-db", "limit": 5000}, timeout=30)
        if r.status_code != 200:
            raise ProbeError(f"/api/search: HTTP {r.status_code} {r.text[:100]}")
        live = len(r.json())
        rc = self.restored_counts
        ok = True
        ok &= self.compare("dashboards_api", "dashboards (via API)", self.production["dashboards"], live)
        ok &= self.compare("dashboards_db", "dashboards (in DB)", self.production["dashboards"], rc["dashboards"])
        ok &= self.compare("datasources", "data sources", self.production["datasources"], rc["datasources"])
        ok &= self.compare("users", "users", self.production["users"], rc["users"])
        ok &= self.compare("quick_check", "SQLite quick_check", 1, 1 if rc["quick_check"] == "ok" else 0, tolerance=0)
        self.fail_on_mismatch(ok)
        return f"{live} dashboards via API, {rc['datasources']} data sources, DB ok"
