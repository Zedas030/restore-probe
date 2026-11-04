#!/usr/bin/env python3
"""End-to-end test: deliberately damaged backups must be detected and must raise an alert.

Builds two broken backups from the real latest ones and probes them with a temporary configuration
(own state directory, own sensor prefix, no heartbeat), so the production history and sensors stay untouched:

  1. ha_bitrot        – copy of the latest Home Assistant backup with 1 MiB of garbage in the middle
                        (simulates bit rot / a truncated upload). Expected: restore fails.
  2. grafana_partial  – the latest Grafana snapshot restored, every dashboard deleted from grafana.db, packed as
                        a tar archive (simulates a backup that "worked" but is silently incomplete).
                        Expected: stage 3 fails because dashboards are missing compared with production.

The alert goes through the configured Home Assistant notify service with the title prefix "[E2E TEST]".
Usage (as root): /opt/restore-probe/venv/bin/python scripts/e2e_corruption.py [--config /etc/restore-probe/config.yaml]
Exit code 0 means: both damaged backups were detected (= test passed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from restore_probe.config import DEFAULT_CONFIG, load_config  # noqa: E402
from restore_probe.sources import make_source  # noqa: E402

BASE = Path("/var/lib/restore-probe-e2e")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--keep", action="store_true", help="keep the damaged copies afterwards")
    args = ap.parse_args()

    cfg = load_config(args.config)
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    ha_svc = next(s for s in raw["services"] if s["plugin"] == "homeassistant")
    gf_svc = next(s for s in raw["services"] if s["plugin"] == "grafana")

    shutil.rmtree(BASE, ignore_errors=True)
    (BASE / "ha").mkdir(parents=True)
    (BASE / "grafana").mkdir(parents=True)

    # 1) Home Assistant: damaged copy of the newest backup
    ha_cfg = next(s for s in cfg.services if s.name == ha_svc["name"])
    info = make_source(cfg, ha_cfg.source).latest()
    dst = BASE / "ha" / info.label
    print(f"[1] copying {info.label} ({info.size_bytes / 1e6:.0f} MB) and damaging 1 MiB in the middle …")
    shutil.copyfile(info.location, dst)
    with open(dst, "r+b") as fh:
        fh.seek(info.size_bytes // 2)
        fh.write(os.urandom(1024 * 1024))

    # 2) Grafana: restore the latest snapshot, delete all dashboards, pack it as tar
    gf_cfg = next(s for s in cfg.services if s.name == gf_svc["name"])
    src = make_source(cfg, gf_cfg.source)
    ginfo = src.latest()
    tmp = BASE / "grafana-tmp"
    print(f"[2] restoring Grafana snapshot {ginfo.label} and deleting every dashboard …")
    src.restore(ginfo, tmp)
    dbs = list(tmp.rglob("grafana.db"))
    if not dbs:
        print("grafana.db not found in the snapshot")
        return 2
    con = sqlite3.connect(dbs[0])
    deleted = con.execute("DELETE FROM dashboard WHERE is_folder=0").rowcount
    con.commit()
    con.close()
    print(f"    {deleted} dashboards deleted")
    with tarfile.open(BASE / "grafana" / "grafana-partial.tar.gz", "w:gz") as tf:
        tf.add(tmp, arcname=".")
    shutil.rmtree(tmp)

    # Temporary configuration: same everything, but only the two damaged services and isolated state
    test = dict(raw)
    test["general"] = {**raw.get("general", {}), "state_dir": str(BASE / "state"), "work_dir": str(BASE / "work"),
                       "www_dir": str(BASE / "www"), "report_title": "Restore Probe – E2E corruption test",
                       "prometheus_textfile": None}
    test["home_assistant"] = {**raw.get("home_assistant", {}), "sensor_prefix": "restore_probe_e2e", "heartbeat_entity": ""}
    ha_copy = {**ha_svc, "name": "ha_bitrot", "title": "[E2E TEST] Home Assistant (bit rot)",
               "source": {**ha_svc["source"], "dirs": [str(BASE / "ha")], "dir": None, "pattern": "*.tar"}}
    gf_copy = {**gf_svc, "name": "grafana_partial", "title": "[E2E TEST] Grafana (incomplete backup)",
               "source": {"type": "path", "glob": str(BASE / "grafana" / "*.tar.gz")}}
    test["services"] = [ha_copy, gf_copy]
    test_cfg = BASE / "config.yaml"
    test_cfg.write_text(yaml.safe_dump(test, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(test_cfg, 0o600)

    print("[3] probing both damaged backups (alerts go to Home Assistant) …")
    rp = shutil.which("restore-probe") or "/opt/restore-probe/venv/bin/restore-probe"
    proc = subprocess.run([rp, "-c", str(test_cfg), "--log-file", str(BASE / "e2e.log"), "run"],
                          capture_output=True, text=True)
    print(proc.stdout[-3000:])
    latest = json.loads((BASE / "state" / "latest.json").read_text(encoding="utf-8"))
    results = {s["name"]: s for s in latest["services"]}
    ok_ha = results.get("ha_bitrot", {}).get("status") == "fail"
    gf = results.get("grafana_partial", {})
    ok_gf = gf.get("status") == "fail" and any(st["stage"] == 3 and not st["ok"] for st in gf.get("stages", []))
    print(f"ha_bitrot detected:       {'YES' if ok_ha else 'NO'} – {results.get('ha_bitrot', {}).get('message', '')[:200]}")
    print(f"grafana_partial detected: {'YES' if ok_gf else 'NO'} – {gf.get('message', '')[:200]}")
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "label=restore-probe=1"], capture_output=True, text=True).stdout.split()
    print(f"sandbox containers left behind: {len(leftovers)}")
    if not args.keep:
        for p in ("ha", "grafana", "work"):
            shutil.rmtree(BASE / p, ignore_errors=True)
    print("E2E corruption test:", "PASSED" if (ok_ha and ok_gf and not leftovers) else "FAILED")
    print(f"Report and JSON: {BASE / 'www' / 'index.html'}, {BASE / 'state' / 'latest.json'}")
    return 0 if (ok_ha and ok_gf and not leftovers) else 1


if __name__ == "__main__":
    raise SystemExit(main())
