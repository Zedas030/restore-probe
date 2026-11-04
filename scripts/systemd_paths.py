#!/usr/bin/env python3
"""Print the extra ReadWritePaths the restic-backup unit needs, derived from config.yaml.

install.sh writes them into a systemd drop-in so the unit file itself stays generic:
- the restic repository (when it is a local path) and require_mount
- host paths of influxdb_portable sets and the output of command sets
- directories of prometheus_snapshot sets
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from restore_probe.config import DEFAULT_CONFIG, load_config  # noqa: E402


def main() -> int:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    cfg = load_config(cfg_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    spec = raw.get("restic", {})
    paths: list[str] = []
    repo = cfg.restic.repository
    if repo.startswith("/"):
        paths.append(repo)
    if spec.get("require_mount"):
        paths.append(spec["require_mount"])
    for s in spec.get("backup_sets", []):
        t = s.get("type")
        if t == "influxdb_portable" and s.get("host_path"):
            paths.append(s["host_path"])
        elif t == "command" and s.get("output"):
            paths.append(s["output"])
        elif t == "prometheus_snapshot":
            paths.append(s.get("dir", "/var/lib/prometheus/metrics2"))
    seen = []
    for p in paths:
        p = str(Path(p))
        if p not in seen and not any(p.startswith(q.rstrip("/") + "/") for q in seen):
            seen.append(p)
    print(" ".join(seen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
