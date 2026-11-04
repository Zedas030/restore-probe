"""Probe module for Prometheus (native installation).

Source: restic snapshot tagged 'prometheus', made from a TSDB snapshot (admin API) plus /etc/prometheus.
Sandbox: prom/prometheus in production's version with the restored configuration.
Stages: 1 /-/ready · 2 /api/v1/status/tsdb · 3 jobs and series at snapshot time vs. production.
"""

from __future__ import annotations

import calendar
import re
import time

import requests

from ..util import Deadline, ProbeError, run
from .base import Plugin

SNAPSHOT_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]+")


def detect_prometheus_version() -> str | None:
    res = run(["prometheus", "--version"], check=False, quiet=True, timeout=30)
    text = (res.stdout or "") + (res.stderr or "")
    for tok in text.split():
        if tok[0].isdigit() and tok.count(".") >= 2:
            return tok.split("+")[0]  # 2.53.3+ds1 → 2.53.3
    return None


def _query(base: str, expr: str, at: float | None = None) -> float:
    params = {"query": expr}
    if at:
        params["time"] = at
    r = requests.get(f"{base}/api/v1/query", params=params, timeout=60)
    if r.status_code != 200:
        raise ProbeError(f"Query {expr}: HTTP {r.status_code} {r.text[:100]}")
    res = r.json()["data"]["result"]
    if not res:
        return 0.0
    return float(res[0]["value"][1])


def snapshot_time(name: str) -> float | None:
    """'20261105T013001Z-5a86…' → Unix time (UTC)."""
    try:
        return float(calendar.timegm(time.strptime(name.split("-")[0], "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


class PrometheusPlugin(Plugin):
    port = 9090

    def prepare(self) -> None:
        if not self.opt.get("image") or self.opt.get("image") == "auto":
            v = detect_prometheus_version()
            if not v:
                raise ProbeError("Cannot detect the Prometheus version (set options.image, e.g. prom/prometheus:v2.53.3)")
            self.opt["image"] = f"prom/prometheus:v{v}"
        self.prod_url = self.opt.get("production_url", "http://127.0.0.1:9090").rstrip("/")

    def after_restore(self, info) -> None:
        self.etc = self.find_file("prometheus.yml", option="config_path").parent
        snaps = sorted(p for p in self.work.rglob("*") if p.is_dir() and SNAPSHOT_RE.fullmatch(p.name))
        if not snaps:
            raise ProbeError("No TSDB snapshot (directory YYYYMMDDThhmmssZ-…) in the backup")
        self.tsdb = snaps[-1]
        self.snapshot_time = snapshot_time(self.tsdb.name) or info.timestamp
        run(["chown", "-R", "65534:65534", str(self.tsdb)])
        # Check the configuration with promtool from the same image, without network
        res = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "promtool",
                   "-v", f"{self.etc}:/etc/prometheus:ro", self.image(), "check", "config", "/etc/prometheus/prometheus.yml"],
                  check=False, timeout=120)
        out = (res.stdout + res.stderr).strip()
        self.config_check = res.ok
        self.config_check_text = out.splitlines()[-1] if out else ""

    def start(self, deadline: Deadline) -> None:
        self.container = self.sandbox.run_container(
            self.svc.name, self.image(),
            mounts=[(self.tsdb, "/prometheus", "rw"), (self.etc, "/etc/prometheus", "ro")],
            cmd=["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus",
                 "--web.listen-address=0.0.0.0:9090", "--storage.tsdb.retention.time=10y"])
        self.sandbox.wait_running(self.container, deadline, "Prometheus")
        self.ip = self.sandbox.container_ip(self.container)

    def _url(self, p: str = "") -> str:
        return f"http://{self.ip}:{self.port}{p}"

    def stage1(self, deadline: Deadline) -> str:
        self.wait_http("/-/ready", deadline, what="Prometheus /-/ready")
        return "ready" + ("" if self.config_check else f" (promtool: {self.config_check_text})")

    def stage2(self, deadline: Deadline) -> str:
        r = self.wait_http("/api/v1/status/tsdb", deadline, what="Prometheus TSDB status")
        head = r.json()["data"]["headStats"]
        return f"TSDB status: {head.get('numSeries')} series in the head block"

    def stage3(self, deadline: Deadline) -> str:
        at = self.snapshot_time - 2 * 3600  # safely inside both data sets
        sandbox, prod = self._url(), self.prod_url
        ok = True
        s_series = _query(sandbox, 'count({__name__=~".+"})', at)
        p_series = _query(prod, 'count({__name__=~".+"})', at)
        ok &= self.compare("series", "series at snapshot time", p_series, s_series)
        s_jobs = set(requests.get(sandbox + "/api/v1/label/job/values", timeout=30).json()["data"])
        p_jobs = set(requests.get(prod + "/api/v1/label/job/values", timeout=30).json()["data"])
        missing = sorted(p_jobs - s_jobs)
        ok &= self.compare("jobs", "scrape jobs with data", len(p_jobs), len(s_jobs),
                           note=("missing: " + ", ".join(missing)) if missing else "")
        ok &= self.compare("config_check", "promtool check config", 1, 1 if self.config_check else 0,
                           tolerance=0, note=self.config_check_text)
        self.fail_on_mismatch(ok)
        return f"{int(s_series)} series, {len(s_jobs)} jobs, configuration valid"
