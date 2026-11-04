"""restic backup for services that would otherwise have none.

Backup sets live in the YAML under restic.backup_sets. Every set becomes a restic snapshot tagged with the set's
name, so Restore Probe can find "the latest backup of X". Types:

  files:               paths: [...]                         back up files/directories as they are
  sqlite:              db: /path/x.db                       consistent copy via SQLite online backup (staging)
  prometheus_snapshot: url: http://127.0.0.1:9090, dir: /var/lib/prometheus/metrics2   TSDB snapshot via admin API
  influxdb_portable:   container: influxdb, host_path: /srv/influxdb, inner_path: /var/lib/influxdb   influxd backup -portable
  command:             cmd: [...], output: /path            any command that fills a directory

Every set may also list 'extra_paths' (backed up alongside, e.g. /etc/grafana next to grafana.db).
After all sets: restic forget with retention + prune; weekly restic check with a data sample.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

from .config import Config
from .util import ProbeError, fmt_bytes, fmt_duration, rmtree_force, run

log = logging.getLogger("restore_probe.backup")


class ResticBackup:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        raw = yaml.safe_load(cfg.config_path.read_text(encoding="utf-8")) or {}
        self.spec = raw.get("restic", {})
        self.sets: list[dict] = self.spec.get("backup_sets", [])
        self.keep = self.spec.get("keep", {"daily": 7, "weekly": 4, "monthly": 6})
        self.staging = cfg.state_dir / "staging"
        self.results: list[dict] = []

    def env(self) -> dict[str, str]:
        r = self.cfg.restic
        if not r.repository or not r.password:
            raise ProbeError("restic: RESTIC_REPOSITORY/RESTIC_PASSWORD missing in the .env")
        return {"RESTIC_REPOSITORY": r.repository, "RESTIC_PASSWORD": r.password,
                "RESTIC_CACHE_DIR": str(self.cfg.state_dir / "restic-cache"), **r.extra_env}

    def restic(self, *args: str, timeout: float = 7200, check: bool = True):
        return run([self.cfg.restic.binary, *args], env=self.env(), timeout=timeout, check=check)

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        req = self.spec.get("require_mount")
        if req:
            run(["ls", req], check=False, quiet=True, timeout=30)  # trigger an automount
            if not run(["mountpoint", "-q", req], check=False, quiet=True).ok:
                raise ProbeError(f"{req} is not mounted; backup aborted (no local fallback)")
        if not self.restic("cat", "config", check=False, timeout=300).ok:
            repo = self.cfg.restic.repository
            if repo.startswith("/") and not Path(repo).exists():
                Path(repo).mkdir(parents=True, exist_ok=True)
            log.info("Initialising restic repository %s", repo)
            self.restic("init", timeout=300)
        self.restic("unlock", check=False, timeout=300)

    def run_all(self, only: list[str] | None = None) -> bool:
        ok_all = True
        self.staging.mkdir(parents=True, exist_ok=True)
        for s in self.sets:
            if only and s["name"] not in only:
                continue
            t0 = time.monotonic()
            res = {"name": s["name"], "ok": False, "message": "", "seconds": 0, "bytes": None, "snapshot": None}
            try:
                paths, cleanup = self._prepare(s)
                try:
                    snap = self._backup(s, paths)
                    res.update(ok=True, snapshot=snap.get("snapshot_id", "")[:8], bytes=snap.get("total_bytes_processed"),
                               message=f"{snap.get('files_new', 0)} new, {snap.get('files_changed', 0)} changed, "
                                       f"{fmt_bytes(snap.get('data_added', 0))} added")
                finally:
                    for c in cleanup:
                        try:
                            c()
                        except Exception as e:
                            log.warning("[%s] cleanup: %s", s["name"], e)
            except ProbeError as e:
                res["message"] = str(e)
                ok_all = False
                log.error("[%s] %s", s["name"], e)
            except Exception as e:
                res["message"] = f"{type(e).__name__}: {e}"
                ok_all = False
                log.exception("[%s] internal error", s["name"])
            res["seconds"] = round(time.monotonic() - t0, 1)
            log.info("[%s] %s – %s (%s)", s["name"], "OK" if res["ok"] else "FAILED", res["message"], fmt_duration(res["seconds"]))
            self.results.append(res)
        if not only:
            self._forget()
            self._maybe_check()
        return ok_all

    # ------------------------------------------------------------------ preparation per type
    def _prepare(self, s: dict) -> tuple[list[str], list]:
        t = s.get("type", "files")
        cleanup: list = []
        paths: list[str] = []
        if t == "files":
            paths = list(s.get("paths", []))
        elif t == "sqlite":
            src = Path(s["db"])
            if not src.exists():
                raise ProbeError(f"Database missing: {src}")
            stage = self.staging / s["name"]
            rmtree_force(stage)
            stage.mkdir(parents=True)
            dst = stage / src.name
            con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
            try:
                out = sqlite3.connect(dst)
                with out:
                    con.backup(out, pages=4096, sleep=0.05)
                out.close()
            finally:
                con.close()
            paths = [str(dst)]
            cleanup.append(lambda: rmtree_force(stage))
        elif t == "prometheus_snapshot":
            url = s.get("url", "http://127.0.0.1:9090").rstrip("/")
            r = requests.post(f"{url}/api/v1/admin/tsdb/snapshot", timeout=600)
            if r.status_code != 200:
                raise ProbeError(f"Prometheus snapshot failed: HTTP {r.status_code} {r.text[:150]} "
                                 f"(is --web.enable-admin-api set?)")
            name = r.json()["data"]["name"]
            snap = Path(s.get("dir", "/var/lib/prometheus/metrics2")) / "snapshots" / name
            if not snap.is_dir():
                raise ProbeError(f"Snapshot directory missing: {snap}")
            paths = [str(snap)]
            cleanup.append(lambda: rmtree_force(snap))
        elif t == "influxdb_portable":
            inner = s.get("inner_path", "/var/lib/influxdb").rstrip("/") + "/.restore-probe-backup"
            host = Path(s["host_path"]) / ".restore-probe-backup"
            rmtree_force(host)
            run(["docker", "exec", s["container"], "influxd", "backup", "-portable", inner], timeout=3600)
            if not host.is_dir() or not any(host.glob("*.manifest")):
                raise ProbeError(f"influxd backup left nothing in {host}")
            paths = [str(host)]
            cleanup.append(lambda: rmtree_force(host))
        elif t == "command":
            out = Path(s["output"])
            rmtree_force(out)
            out.mkdir(parents=True)
            run(list(s["cmd"]), timeout=3600)
            paths = [str(out)]
            cleanup.append(lambda: rmtree_force(out))
        else:
            raise ProbeError(f"Unknown set type: {t}")
        paths += [p for p in s.get("extra_paths", []) if Path(p).exists()]
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            raise ProbeError("Paths missing: " + ", ".join(missing))
        if not paths:
            raise ProbeError("Set without paths")
        return paths, cleanup

    def _backup(self, s: dict, paths: list[str]) -> dict:
        args = ["backup", "--tag", s["name"], "--json", "--one-file-system", "--exclude-caches"]
        for ex in s.get("exclude", []):
            args += ["--exclude", ex]
        args += paths
        res = self.restic(*args, check=False)
        summary = {}
        for line in res.stdout.splitlines():
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            if j.get("message_type") == "summary":
                summary = j
        if res.returncode not in (0, 3):  # 3 = some files unreadable, snapshot created anyway
            tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
            raise ProbeError("restic backup failed: " + " | ".join(tail))
        if res.returncode == 3:
            log.warning("[%s] restic: some files could not be read", s["name"])
        if not summary:
            raise ProbeError("restic returned no summary")
        return summary

    def _forget(self) -> None:
        args = ["forget", "--group-by", "tags", "--prune"]
        for k in ("last", "hourly", "daily", "weekly", "monthly", "yearly"):
            if k in self.keep:
                args += [f"--keep-{k}", str(self.keep[k])]
        self.restic(*args, timeout=7200)

    def _maybe_check(self) -> None:
        marker = self.cfg.state_dir / "restic-last-check"
        every_days = float(self.spec.get("check_every_days", 7))
        if marker.exists() and time.time() - marker.stat().st_mtime < every_days * 86400:
            return
        subset = str(self.spec.get("check_read_data_subset", "5%"))
        res = self.restic("check", f"--read-data-subset={subset}", check=False, timeout=14400)
        if not res.ok:
            raise ProbeError("restic check reports errors: " + (res.stderr or res.stdout).strip().splitlines()[-1])
        marker.touch()
        log.info("restic check (%s of the data) found no problems", subset)


def write_backup_textfile(path: Path, results: list[dict], ok: bool, started: float) -> None:
    lines = ["# HELP restore_probe_backup_last_run_timestamp_seconds End of the last restic backup run",
             "# TYPE restore_probe_backup_last_run_timestamp_seconds gauge",
             f"restore_probe_backup_last_run_timestamp_seconds {int(time.time())}",
             "# HELP restore_probe_backup_run_ok 1 if every set succeeded",
             "# TYPE restore_probe_backup_run_ok gauge",
             f"restore_probe_backup_run_ok {1 if ok else 0}",
             "# TYPE restore_probe_backup_run_duration_seconds gauge",
             f"restore_probe_backup_run_duration_seconds {round(time.time() - started, 1)}",
             "# TYPE restore_probe_backup_set_ok gauge"]
    for r in results:
        lines.append(f'restore_probe_backup_set_ok{{set="{r["name"]}"}} {1 if r["ok"] else 0}')
        lines.append(f'restore_probe_backup_set_duration_seconds{{set="{r["name"]}"}} {r["seconds"]}')
        if r.get("bytes") is not None:
            lines.append(f'restore_probe_backup_set_bytes{{set="{r["name"]}"}} {r["bytes"]}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".prom.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def run_backup(cfg: Config, only: list[str] | None = None, notify: bool = True) -> int:
    from .notify import HomeAssistantNotifier

    started = time.time()
    lock_path = Path("/run/restic-backup.lock") if os.access("/run", os.W_OK) else cfg.state_dir / "backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("A restic backup is already running", file=sys.stderr)
        return 1
    b = ResticBackup(cfg)
    ok = False
    error = ""
    try:
        if not b.sets:
            raise ProbeError("No backup_sets in the configuration")
        b.preflight()
        ok = b.run_all(only)
    except ProbeError as e:
        error = str(e)
        log.error("%s", e)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.exception("Backup run aborted")
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    if cfg.prometheus_textfile:
        try:
            write_backup_textfile(cfg.prometheus_textfile.with_name("restore_probe_backup.prom"), b.results, ok and not error, started)
        except Exception:
            log.exception("Backup textfile not written")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "backup-latest.json").write_text(json.dumps(
        {"started": started, "finished": time.time(), "ok": ok and not error, "error": error, "sets": b.results},
        ensure_ascii=False, indent=1), encoding="utf-8")

    for r in b.results:
        print(f"{'OK    ' if r['ok'] else 'FAILED'} {r['name']:14} {fmt_duration(r['seconds']):>10}  {r['message']}")
    if error:
        print("Error:", error)

    if notify:
        ha = HomeAssistantNotifier(cfg)
        p = cfg.home_assistant.sensor_prefix
        ha.set_state(f"sensor.{p}_backup_status", "ok" if (ok and not error) else "fail", {
            "friendly_name": "Restore Probe restic backup", "icon": "mdi:cloud-upload",
            "last_run": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sets": {r["name"]: ("ok" if r["ok"] else r["message"]) for r in b.results}, "error": error})
        if error or not ok:
            failed = [f"{r['name']}: {r['message']}" for r in b.results if not r["ok"]]
            ha.alert("Restore Probe: restic backup failed", "\n".join(failed) or error)
        for e in ha.errors:
            log.warning("Report to Home Assistant failed: %s", e)
    return 0 if (ok and not error) else 1
