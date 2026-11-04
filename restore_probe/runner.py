"""The run: lock, preflight, isolation proof, then restore and check service after service.

Principle: whatever happens, at the end every sandbox container is gone and the work directory is empty.
A failing service is a result, not a crash; the run continues with the next service.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config, ServiceConfig
from .plugins import ServiceResult, get_plugin_class
from .sandbox import Sandbox, default_isolation_targets
from .sources import make_source
from .util import Deadline, ProbeError, fmt_bytes, fmt_duration, free_gb, rmtree_force, run

log = logging.getLogger("restore_probe.runner")

STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2, "skipped": 3}


@dataclass
class RunResult:
    run_id: str
    started: float
    finished: float | None = None
    host: str = field(default_factory=socket.gethostname)
    version: str = __version__
    status: str = "running"
    isolation: dict = field(default_factory=dict)
    services: list[ServiceResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.started

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "started": self.started, "finished": self.finished,
            "started_iso": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "duration_seconds": round(self.duration, 1), "host": self.host, "version": self.version,
            "status": self.status, "isolation": self.isolation, "notes": self.notes, "error": self.error,
            "services": [s.to_dict() for s in self.services],
        }


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProbeError(f"Another Restore Probe run is active (lock {self.path})")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


def wait_for_docker(max_minutes: float) -> None:
    """A full-system backup may stop Docker for a while: better wait than fail pointlessly."""
    dl = Deadline(max_minutes * 60)
    while True:
        if run(["docker", "info"], check=False, quiet=True, timeout=60).ok:
            return
        if dl.expired():
            raise ProbeError(f"Docker has been unreachable for {max_minutes:.0f} min")
        log.info("Docker not reachable, waiting …")
        time.sleep(30)


class Runner:
    def __init__(self, cfg: Config, *, only: list[str] | None = None, keep_work: bool = False,
                 previous: dict | None = None):
        self.cfg = cfg
        self.only = only
        self.keep_work = keep_work
        self.previous = previous or {}
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.result = RunResult(run_id=self.run_id, started=time.time())
        self.sandbox = Sandbox(cfg.sandbox, self.run_id)

    # ------------------------------------------------------------------ flow
    def run(self) -> RunResult:
        lock_path = Path("/run/restore-probe.lock") if os.access("/run", os.W_OK) else self.cfg.state_dir / "run.lock"
        try:
            with Lock(lock_path):
                self._run_locked()
        except ProbeError as e:
            self.result.status = "fail"
            self.result.error = str(e)
            log.error("Run aborted: %s", e)
        except Exception as e:  # last line of defence: never die silently
            self.result.status = "fail"
            self.result.error = f"{type(e).__name__}: {e}"
            log.exception("Unexpected error in the run")
        finally:
            self.result.finished = time.time()
            self._final_cleanup()
        return self.result

    def _run_locked(self) -> None:
        for w in self.cfg.warnings:
            self.result.notes.append(w)
            log.warning(w)
        wait_for_docker(float(self.cfg.env.get("DOCKER_WAIT_MIN", 45)))
        if not Sandbox.memory_limits_supported():
            msg = ("Memory limits are NOT enforced: the kernel's memory cgroup is disabled "
                   "(Raspberry Pi: add 'cgroup_enable=memory' to /boot/firmware/cmdline.txt and reboot)")
            self.result.notes.append(msg)
            log.warning(msg)
        stale = Sandbox.cleanup_stale()
        if stale:
            self.result.notes.append(f"{len(stale)} container(s) of an earlier run removed")
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        for old in self.cfg.work_dir.iterdir():
            rmtree_force(old)

        if self.only:
            unknown = set(self.only) - {s.name for s in self.cfg.services}
            if unknown:
                raise ProbeError(f"Unknown services: {', '.join(sorted(unknown))}")

        host_ip = self.sandbox.ensure_network()
        targets = default_isolation_targets(host_ip, self.cfg.sandbox.isolation_must_fail)
        iso = self.sandbox.verify_isolation(targets, Deadline(180))
        self.result.isolation = {"passed": iso.passed, "checks": iso.checks, "inspect": iso.inspect,
                                 "host_block_rule": self.sandbox.host_block_rule_present()}
        if not iso.passed:
            reached = [c["target"] for c in iso.checks if c["reachable"]]
            raise ProbeError("ISOLATION BROKEN: reachable from the sandbox: " + ", ".join(reached or ["(container properties)"]) +
                             ". No service is started.")
        log.info("Isolation proven: %d targets unreachable", len(iso.checks))

        services = [s for s in self.cfg.services if s.enabled and (not self.only or s.name in self.only)]
        for svc in services:
            self.result.services.append(self._probe_service(svc))
        if not self.only:
            for svc in self.cfg.services:
                if not svc.enabled:
                    self.result.services.append(ServiceResult(svc.name, svc.title, status="skipped", message="disabled"))

        worst = max((STATUS_ORDER.get(s.status, 2) for s in self.result.services if s.status != "skipped"), default=0)
        self.result.status = {0: "ok", 1: "warn", 2: "fail"}[min(worst, 2)]

    # ------------------------------------------------------------------ one service
    def _probe_service(self, svc: ServiceConfig) -> ServiceResult:
        log.info("=== %s (%s) ===", svc.title, svc.plugin)
        deadline = Deadline(svc.timeout_min * 60)
        work = self.cfg.work_dir / svc.name
        try:
            cls = get_plugin_class(svc.plugin)
        except KeyError as e:
            return ServiceResult(svc.name, svc.title, status="fail", message=str(e), error=str(e))
        plugin = cls(self.cfg, svc, self.sandbox, work)
        res = plugin.result
        t0 = time.monotonic()
        try:
            source = make_source(self.cfg, svc.source)
            plugin.prepare()
            info = source.latest()
            res.backup = {"label": info.label, "timestamp": info.timestamp, "date": info.date_str,
                          "age_hours": round(info.age_hours, 1), "size_bytes": info.size_bytes,
                          "location": info.location, "meta": info.meta}
            log.info("[%s] backup %s from %s (%.1f h old, %s)", svc.name, info.label, info.date_str, info.age_hours,
                     fmt_bytes(info.size_bytes or 0))
            if info.age_hours > svc.backup_max_age_hours:
                plugin.warn(f"Backup is {info.age_hours:.0f} h old (allowed: {svc.backup_max_age_hours:.0f} h)")

            restore = svc.source.get("restore", True)  # false: check the archive in place (huge system images)
            if restore:
                need = plugin.estimated_space_bytes(info) / 1e9 + self.cfg.min_free_gb
                have = free_gb(self.cfg.work_dir)
                if have < need:
                    raise ProbeError(f"Not enough disk space: {have:.1f} GB free, {need:.1f} GB needed")
                rmtree_force(work)
                work.mkdir(parents=True)
            tr = time.monotonic()
            if restore:
                source.restore(info, work)
            plugin.after_restore(info)
            res.restore_seconds = round(time.monotonic() - tr, 1)
            log.info("[%s] restored in %s", svc.name, fmt_duration(res.restore_seconds))
            deadline.check("restore")

            plugin.start(deadline)
            if plugin.container:
                res.isolation = {**res.isolation, **self.sandbox.inspect_security(plugin.container)}
                if not res.image:
                    try:
                        res.image = plugin.image()
                    except ProbeError:
                        pass

            ok = plugin.run_stage(1, plugin.stage1, deadline)
            ok = ok and plugin.run_stage(2, plugin.stage2, deadline)
            ok = ok and plugin.run_stage(3, plugin.stage3, deadline)

            self._check_data_drop(plugin)
            if ok:
                res.status = "warn" if res.warnings else "ok"
                res.message = self._success_message(res)
            else:
                res.status = "fail"
                failed = next(s for s in res.stages if not s.ok)
                res.message = f"Stage {failed.stage} ({failed.name}) failed: {failed.detail}"
                res.error = failed.detail
                logs = plugin.container_logs(30)
                if logs:
                    res.error += "\n--- container log ---\n" + logs
        except ProbeError as e:
            res.status = "fail"
            res.message = str(e)
            res.error = str(e)
            log.error("[%s] %s", svc.name, e)
        except Exception as e:
            res.status = "fail"
            res.message = f"Internal error: {type(e).__name__}: {e}"
            res.error = res.message
            log.exception("[%s] internal error", svc.name)
        finally:
            res.total_seconds = round(time.monotonic() - t0, 1)
            try:
                if plugin.container:
                    self.sandbox.remove_container(plugin.container)
                    plugin.container = None
                if not self.keep_work:
                    rmtree_force(work)
            except Exception:
                log.exception("[%s] cleanup failed", svc.name)
        log.info("[%s] result: %s – %s (%s)", svc.name, res.status.upper(), res.message, fmt_duration(res.total_seconds))
        return res

    def _success_message(self, res: ServiceResult) -> str:
        parts = [f"backup from {res.backup.get('date')}"]
        if res.restore_seconds is not None:
            parts.append(f"restorable in {fmt_duration(res.restore_seconds)}")
        st3 = next((s for s in res.stages if s.stage == 3), None)
        if st3 and st3.detail:
            parts.append(st3.detail)
        msg = ", ".join(parts)
        msg = msg[0].upper() + msg[1:]
        if res.warnings:
            msg += " – note: " + "; ".join(res.warnings)
        return msg

    def _check_data_drop(self, plugin) -> None:
        """Compare with the previous run: clearly less data than yesterday raises a warning, even when sandbox
        and production agree today (e.g. after something was deleted in production by accident)."""
        prev = next((s for s in self.previous.get("services", []) if s["name"] == plugin.svc.name), None)
        if not prev:
            return
        prev_metrics = {m["key"]: m for m in prev.get("metrics", [])}
        limit = self.cfg.data_drop_alert_percent
        for m in plugin.result.metrics:
            if m.key.endswith(("_hours", "_check", "_integrity", "_loaded")):
                continue  # not a quantity
            pm = prev_metrics.get(m.key)
            if not pm or pm.get("restored") in (None, 0) or m.restored is None:
                continue
            if m.restored < pm["restored"] * (1 - limit / 100.0):
                plugin.warn(f"{m.label}: {m.restored:g} instead of {pm['restored']:g} in the previous run "
                            f"(−{100 - m.restored / pm['restored'] * 100:.0f} %)")

    def _final_cleanup(self) -> None:
        try:
            self.sandbox.cleanup()
            Sandbox.cleanup_stale()
            if not self.keep_work and self.cfg.work_dir.exists():
                for p in self.cfg.work_dir.iterdir():
                    rmtree_force(p)
        except Exception:
            log.exception("Final cleanup failed")


def load_previous(state_dir: Path) -> dict:
    p = state_dir / "latest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}
