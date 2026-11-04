#!/usr/bin/env python3
"""Set up the dead-man's switch in Home Assistant (part 2 of the self-monitoring).

1. input_datetime.restore_probe_last_run in configuration.yaml (if missing) + reload
2. automation 'Restore Probe: no report for 36 h' via the REST API (automations.yaml is backed up first)

Reads the HA URL/token from /etc/restore-probe/config.yaml and .env. Runs as root on the HA host.
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from restore_probe.config import load_config  # noqa: E402

AUTOMATION_ID = "restore_probe_dead_mans_switch"


def main() -> int:
    cfg = load_config()
    ha = cfg.home_assistant
    if not ha.token:
        print("HA_TOKEN is missing in the .env – enter it first.")
        return 1
    hdr = {"Authorization": f"Bearer {ha.token}", "Content-Type": "application/json"}
    svc = next((s for s in cfg.services if s.plugin == "homeassistant"), None)
    conf_dir = Path(svc.options.get("production_config_dir", "/srv/homeassistant")) if svc else Path("/srv/homeassistant")
    conf = conf_dir / "configuration.yaml"
    app = Path(__file__).resolve().parents[1]
    if not conf.exists():
        print(f"configuration.yaml not found at {conf} (set options.production_config_dir of the homeassistant service)")
        return 1

    # 1) input_datetime
    text = conf.read_text(encoding="utf-8")
    if "restore_probe_last_run" not in text:
        stamp = datetime.now().strftime("%Y%m%d")
        backup = conf.with_name(f"configuration.yaml.bak-{stamp}-restore-probe")
        shutil.copy2(conf, backup)
        snippet = (app / "config/ha-configuration-snippet.yaml").read_text(encoding="utf-8")
        body = "".join(ln for ln in snippet.splitlines(keepends=True) if not ln.startswith("#"))
        entry = body.split("input_datetime:", 1)[1].lstrip("\n")  # only the indented entry
        if "\ninput_datetime:" in text or text.startswith("input_datetime:"):
            # Section exists: insert the entry right below it
            out, inserted = [], False
            for ln in text.splitlines(keepends=True):
                out.append(ln)
                if not inserted and ln.startswith("input_datetime:"):
                    out.append(entry)
                    inserted = True
            text = "".join(out)
        else:
            text = text.rstrip("\n") + "\n\n# Restore Probe: timestamp of the last run (dead-man's switch)\ninput_datetime:\n" + entry
        conf.write_text(text, encoding="utf-8")
        r = requests.post(f"{ha.url}/api/config/core/check_config", headers=hdr, timeout=120)
        if r.status_code != 200 or r.json().get("result") != "valid":
            print("check_config reports an error, reverting the change:", r.text[:300])
            shutil.copy2(backup, conf)
            return 1
        requests.post(f"{ha.url}/api/services/input_datetime/reload", headers=hdr, timeout=60)
        time.sleep(2)
        print("input_datetime.restore_probe_last_run created (configuration.yaml extended, backup next to it)")
    else:
        print("input_datetime.restore_probe_last_run is already configured")
    st = requests.get(f"{ha.url}/api/states/input_datetime.restore_probe_last_run", headers=hdr, timeout=30)
    if st.status_code != 200:
        print("Note: entity not visible yet – restart HA or reload 'Input datetime'.")

    # 2) automation
    auto = yaml.safe_load((app / "config/ha-automation.yaml").read_text(encoding="utf-8"))
    if ha.notify_service:
        for a in auto.get("actions", []):
            if str(a.get("action", "")).startswith("notify.") and a["action"] != "notify.persistent_notification":
                a["action"] = ha.notify_service
    autom_file = conf_dir / "automations.yaml"
    if autom_file.exists():
        shutil.copy2(autom_file, autom_file.with_name(f"automations.yaml.bak-{datetime.now():%Y%m%d}-restore-probe"))
    r = requests.post(f"{ha.url}/api/config/automation/config/{AUTOMATION_ID}", headers=hdr, json=auto, timeout=60)
    if r.status_code not in (200, 201):
        print("Automation could not be created:", r.status_code, r.text[:300])
        return 1
    print(f"Automation '{auto['alias']}' created/updated (id {AUTOMATION_ID})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
