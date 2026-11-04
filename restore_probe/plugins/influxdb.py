"""Probe module for InfluxDB 1.x (container).

Source: restic snapshot tagged 'influxdb' containing an `influxd backup -portable` directory.
Sandbox: empty InfluxDB container with the same image, then `influxd restore -portable`.
Stages: 1 /ping + restore · 2 SHOW DATABASES · 3 measurements and series cardinality per database vs. production.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from ..util import Deadline, ProbeError, run
from .base import Metric, Plugin

SYSTEM_DBS = {"_internal"}


def influx_query(base: str, q: str, db: str | None = None) -> list:
    params = {"q": q}
    if db:
        params["db"] = db
    r = requests.get(f"{base}/query", params=params, timeout=60)
    if r.status_code != 200:
        raise ProbeError(f"InfluxDB query '{q}': HTTP {r.status_code} {r.text[:100]}")
    res = r.json()["results"][0]
    if "error" in res:
        raise ProbeError(f"InfluxDB query '{q}': {res['error']}")
    return res.get("series", [])


def databases(base: str) -> list[str]:
    series = influx_query(base, "SHOW DATABASES")
    return [v[0] for s in series for v in s.get("values", []) if v[0] not in SYSTEM_DBS]


def db_stats(base: str, db: str, window: tuple[float, float] | None = None) -> dict:
    """Measurements, exact series cardinality and – for a fixed time window – the number of stored points.

    Series counts keep growing in production (e.g. collectd creates a series per new network interface), so the
    data check compares points inside a window that ends before the backup was taken: those must be identical.
    """
    meas = influx_query(base, "SHOW MEASUREMENTS", db)
    n_meas = sum(len(s.get("values", [])) for s in meas)
    card = influx_query(base, f'SHOW SERIES EXACT CARDINALITY ON "{db}"')
    n_series = sum(int(v[-1]) for s in card for v in s.get("values", []))
    out = {"measurements": n_meas, "series": n_series}
    if window:
        start, end = (int(t * 1e9) for t in window)
        rows = influx_query(base, f"SELECT count(*) FROM /.*/ WHERE time >= {start} AND time < {end}", db)
        out["points"] = sum(int(v) for s in rows for row in s.get("values", []) for v in row[1:] if isinstance(v, (int, float)))
    return out


class InfluxDBPlugin(Plugin):
    port = 8086

    def prepare(self) -> None:
        self.prod_url = self.opt.get("production_url", "http://127.0.0.1:8086").rstrip("/")
        try:
            dbs = databases(self.prod_url)
        except requests.RequestException as e:
            raise ProbeError(f"Production InfluxDB not reachable: {e}")
        self.production = {db: db_stats(self.prod_url, db) for db in dbs}
        if not self.production:
            raise ProbeError("Production InfluxDB has no user databases")

    def _backup_dir(self) -> Path:
        sub = self.opt.get("backup_path")
        if sub:
            return self.work / sub.lstrip("/")
        for p in self.work.rglob("*.manifest"):  # first directory containing a *.manifest file
            return p.parent
        raise ProbeError("No portable InfluxDB backup (*.manifest) in the snapshot")

    def after_restore(self, info) -> None:
        # Window for the point comparison: the 24 h before the backup, ending 15 min before it
        end = info.timestamp - 15 * 60
        self.window = (end - float(self.opt.get("window_hours", 24)) * 3600, end)
        self.prod_points = {db: db_stats(self.prod_url, db, self.window).get("points") for db in self.production}
        self.bdir = self._backup_dir()
        self.data_dir = self.work / "_influx_data"
        self.data_dir.mkdir()
        self.chown_for_image(self.data_dir, self.bdir)

    def start(self, deadline: Deadline) -> None:
        self.container = self.sandbox.run_container(
            self.svc.name, self.image(),
            mounts=[(self.data_dir, "/var/lib/influxdb", "rw"), (self.bdir, "/restore", "ro")],
            env={"INFLUXDB_HTTP_AUTH_ENABLED": "false", "INFLUXDB_REPORTING_DISABLED": "true"},
            extra_args=[a for c in self.opt.get("cap_add", []) for a in ("--cap-add", c)])
        self.sandbox.wait_running(self.container, deadline, "InfluxDB")
        self.ip = self.sandbox.container_ip(self.container)

    def _url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def stage1(self, deadline: Deadline) -> str:
        self.wait_http(self._url() + "/ping", deadline, ok_status=(204,), what="InfluxDB /ping")
        # Restore now (the server has to be running for that)
        res = self.sandbox.exec(self.container, ["influxd", "restore", "-portable", "/restore"],
                                timeout=max(60, deadline.remaining()))
        lines = (res.stdout + res.stderr).strip().splitlines()
        self.restore_detail = lines[-1] if lines else ""
        # InfluxDB 1.x keeps shards imported by an online restore disabled until the next start
        run(["docker", "restart", "-t", "20", self.container], timeout=120)
        self.wait_http(self._url() + "/ping", deadline, ok_status=(204,), what="InfluxDB /ping after restart")
        return "answers; restore -portable completed, server restarted"

    def stage2(self, deadline: Deadline) -> str:
        dbs: list[str] = []
        while not deadline.expired():
            try:
                dbs = databases(self._url())
                if dbs:
                    break
            except (ProbeError, requests.RequestException):
                pass
            time.sleep(2)
        if not dbs:
            raise ProbeError(f"No databases visible after the restore ({self.restore_detail})")
        self.restored_dbs = dbs
        return "databases: " + ", ".join(dbs)

    def stage3(self, deadline: Deadline) -> str:
        ok = True
        parts = []
        for db, prod in self.production.items():
            if db not in self.restored_dbs:
                ok &= self.compare(f"{db}_present", f"{db}: present", 1, 0, tolerance=0)
                continue
            st = None
            while st is None:
                try:
                    st = db_stats(self._url(), db, self.window)
                except ProbeError as e:  # shards may still be opening right after the start
                    if "shard is disabled" not in str(e) or deadline.remaining() < 10:
                        raise
                    time.sleep(3)
            ok &= self.compare(f"{db}_measurements", f"{db}: measurements", prod["measurements"], st["measurements"])
            # exact: every point production holds for the window must be in the restore as well
            ok &= self.compare(f"{db}_points", f"{db}: points in the 24 h before the backup", self.prod_points.get(db),
                               st.get("points"), tolerance=float(self.opt.get("points_tolerance_percent", 0.5)), absolute=0)
            self.result.metrics.append(Metric(f"{db}_series", f"{db}: series (info, production keeps growing)",
                                              prod["series"], st["series"], True))
            parts.append(f"{db}: {st['measurements']} measurements, {st.get('points', 0):,} points in the 24 h window")
        self.fail_on_mismatch(ok)
        return "; ".join(parts)
