"""Probe module for Home Assistant (container installation without Supervisor).

Source: Home Assistant's own backup. Before the sandbox start the backup is neutralised:
- every integration outside an allow-list is disabled in .storage/core.config_entries
  (ZHA, TP-Link, Tuya, Sonos, Alexa, MQTT … never start);
- shell_command, wake_on_lan & co. are removed from configuration.yaml;
- a trusted_networks auth provider for the sandbox network lets the prober log in via the API
  without knowing any password.

Stages: 1 frontend answers · 2 login flow yields a token · 3 state RUNNING, automations loaded,
entity registry, recorder DB (quick_check, freshness of the newest entry) compared with production.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import requests
import yaml

from ..sources import BackupInfo
from ..util import Deadline, ProbeError
from .base import Metric, Plugin

DEFAULT_ALLOWLIST = ["sun", "backup", "template", "utility_meter", "integration", "shopping_list",
                     "analytics", "radio_browser", "google_translate", "met"]
REMOVE_TOP_LEVEL = ["shell_command", "wake_on_lan", "rest_command", "telegram_bot", "notify"]


# --- read and write YAML with HA tags (!include, !secret …) without losing them ---
class _Tagged:
    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value


class _HALoader(yaml.SafeLoader):
    pass


class _HADumper(yaml.SafeDumper):
    pass


def _construct_tagged(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return _Tagged(node.tag, loader.construct_scalar(node))
    if isinstance(node, yaml.SequenceNode):
        return _Tagged(node.tag, loader.construct_sequence(node))
    return _Tagged(node.tag, loader.construct_mapping(node))


def _represent_tagged(dumper, data: _Tagged):
    if isinstance(data.value, list):
        return dumper.represent_sequence(data.tag, data.value)
    if isinstance(data.value, dict):
        return dumper.represent_mapping(data.tag, data.value)
    return dumper.represent_scalar(data.tag, str(data.value), style="")


_HALoader.add_multi_constructor("!", _construct_tagged)
_HADumper.add_representer(_Tagged, _represent_tagged)


def load_ha_yaml(text: str):
    return yaml.load(text, Loader=_HALoader) or {}


def dump_ha_yaml(data) -> str:
    return yaml.dump(data, Dumper=_HADumper, allow_unicode=True, sort_keys=False, default_flow_style=False)


def neutralize_configuration(text: str, trusted_cidr: str, remove_keys=REMOVE_TOP_LEVEL) -> str:
    """Sandbox configuration.yaml: auth via trusted_networks, dangerous domains removed."""
    data = load_ha_yaml(text)
    if not isinstance(data, dict):
        raise ProbeError("configuration.yaml is not a mapping")
    for k in remove_keys:
        data.pop(k, None)
    ha = data.get("homeassistant")
    if not isinstance(ha, dict):
        ha = {}
    ha["auth_providers"] = [
        {"type": "trusted_networks", "trusted_networks": [trusted_cidr], "allow_bypass_login": True},
        {"type": "homeassistant"},
    ]
    data["homeassistant"] = ha
    return "# Adjusted by Restore Probe for the sandbox.\n" + dump_ha_yaml(data)


def disable_config_entries(path: Path, allowlist: list[str]) -> dict[str, int]:
    """Disable every integration outside the allow-list. Returns counters for the report."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    entries = doc["data"]["entries"]
    disabled = kept = 0
    for e in entries:
        if e.get("domain") in allowlist:
            kept += 1
            continue
        if e.get("disabled_by") is None:
            e["disabled_by"] = "user"
            disabled += 1
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"disabled": disabled, "kept": kept, "total": len(entries)}


def count_automations_yaml(text: str) -> int:
    try:
        data = load_ha_yaml(text)
        if isinstance(data, list):
            return len(data)
    except yaml.YAMLError:
        pass
    return len(re.findall(r"^- (?:id|alias):", text, flags=re.M))


def count_entities(storage: Path) -> int:
    f = storage / "core.entity_registry"
    return len(json.loads(f.read_text(encoding="utf-8"))["data"]["entities"])


def recorder_stats(db: Path, quick_check: bool = True) -> dict:
    """Open the recorder DB read-only: integrity, highest state_id and timestamp of the newest state."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        con.execute("PRAGMA query_only = 1")
        out = {}
        if quick_check:
            out["quick_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
        out["max_state_id"] = con.execute("SELECT COALESCE(MAX(state_id),0) FROM states").fetchone()[0]
        out["max_event_id"] = con.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
        out["schema_version"] = con.execute(
            "SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1").fetchone()[0]
        try:
            out["newest_state_ts"] = con.execute("SELECT MAX(last_updated_ts) FROM states").fetchone()[0]
        except sqlite3.OperationalError:  # very old schema without *_ts columns
            out["newest_state_ts"] = None
        return out
    finally:
        con.close()


class HomeAssistantPlugin(Plugin):
    port = 8123

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.token: str | None = None
        self.restored_db: dict = {}
        self.data_dir: Path = self.work / "data"

    # ---- production ----
    def prepare(self) -> None:
        prod = Path(self.opt.get("production_config_dir", "/srv/homeassistant"))
        if not prod.is_dir():
            raise ProbeError(f"Production config directory missing: {prod}")
        self.production["version"] = (prod / ".HA_VERSION").read_text().strip() if (prod / ".HA_VERSION").exists() else None
        self.production["entities"] = count_entities(prod / ".storage")
        self.production["automations"] = count_automations_yaml((prod / "automations.yaml").read_text(encoding="utf-8")) \
            if (prod / "automations.yaml").exists() else 0

    def estimated_space_bytes(self, info: BackupInfo) -> int:
        return int((info.size_bytes or 0) * 3.5)  # archive + extracted + WAL growth at start

    # ---- prepare the backup ----
    def after_restore(self, info: BackupInfo) -> None:
        d = self.data_dir
        if not (d / "configuration.yaml").exists():
            raise ProbeError("configuration.yaml missing in the backup")
        storage = d / ".storage"
        if not (storage / "core.config_entries").exists():
            raise ProbeError(".storage/core.config_entries missing in the backup")

        allow = self.opt.get("allow_integrations", DEFAULT_ALLOWLIST)
        counts = disable_config_entries(storage / "core.config_entries", allow)
        self.result.isolation["config_entries"] = counts

        cfg_text = (d / "configuration.yaml").read_text(encoding="utf-8")
        (d / "configuration.yaml").write_text(
            neutralize_configuration(cfg_text, self.cfg.sandbox.subnet, self.opt.get("remove_domains", REMOVE_TOP_LEVEL)),
            encoding="utf-8")

        # Leftovers that only get in the way inside the sandbox
        for name in (".ha_run.lock", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault"):
            (d / name).unlink(missing_ok=True)

        db = d / self.opt.get("db_file", "home-assistant_v2.db")
        if db.exists():
            try:
                self.restored_db = recorder_stats(db, quick_check=True)
            except sqlite3.DatabaseError as e:
                self.restored_db = {"quick_check": f"unreadable: {e}"}
        else:
            self.restored_db = {}
            self.warn("Backup contains no recorder database")
        self.backup_ts = info.timestamp

    # ---- sandbox ----
    def start(self, deadline: Deadline) -> None:
        img = self.image()
        extra = []
        for cap in self.opt.get("cap_add", []):
            extra += ["--cap-add", cap]
        self.container = self.sandbox.run_container(
            self.svc.name, img, mounts=[(self.data_dir, "/config", "rw")],
            env={"PYTHONUNBUFFERED": "1"}, extra_args=extra)
        self.sandbox.wait_running(self.container, deadline, "Home Assistant")
        self.ip = self.sandbox.container_ip(self.container)

    def _url(self, path: str) -> str:
        return f"http://{self.ip}:{self.port}{path}"

    def stage1(self, deadline: Deadline) -> str:
        r = self.wait_http("/", deadline, ok_status=(200,), what="HA frontend")
        return f"HTTP {r.status_code} on /"

    def stage2(self, deadline: Deadline) -> str:
        client_id = f"http://{self.ip}:{self.port}/"
        last = ""
        while not deadline.expired():
            try:
                r = requests.post(self._url("/auth/login_flow"), json={
                    "client_id": client_id, "handler": ["trusted_networks", None], "redirect_uri": client_id}, timeout=15)
                if r.status_code != 200:
                    last = f"login_flow HTTP {r.status_code}: {r.text[:120]}"
                    time.sleep(3)
                    continue
                flow = r.json()
                if flow.get("type") == "form":
                    users = {}
                    for fld in flow.get("data_schema", []):
                        if fld.get("name") == "user":
                            users = fld.get("options") or {}
                    if isinstance(users, list):
                        users = {u[0] if isinstance(u, (list, tuple)) else u.get("value"): 1 for u in users}
                    if not users:
                        raise ProbeError(f"Login flow offers no user: {flow}")
                    user_id = next(iter(users))
                    r = requests.post(self._url(f"/auth/login_flow/{flow['flow_id']}"),
                                      json={"client_id": client_id, "user": user_id}, timeout=15)
                    flow = r.json()
                if flow.get("type") != "create_entry":
                    raise ProbeError(f"Login flow did not end with create_entry: {flow}")
                r = requests.post(self._url("/auth/token"), data={
                    "grant_type": "authorization_code", "code": flow["result"], "client_id": client_id}, timeout=15)
                if r.status_code != 200:
                    raise ProbeError(f"Token exchange failed: HTTP {r.status_code} {r.text[:120]}")
                self.token = r.json()["access_token"]
                return "logged in via trusted_networks, access token received"
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(3)
        raise ProbeError(f"Login flow did not succeed before the deadline ({last})")

    def _api(self, path: str, deadline: Deadline):
        r = requests.get(self._url(path), headers={"Authorization": f"Bearer {self.token}"},
                         timeout=max(5, min(30, deadline.remaining())))
        if r.status_code != 200:
            raise ProbeError(f"{path}: HTTP {r.status_code}")
        return r.json()

    def stage3(self, deadline: Deadline) -> str:
        # Wait until HA has fully started
        state = version = ""
        while not deadline.expired():
            try:
                cfg = self._api("/api/config", deadline)
                state, version = cfg.get("state", ""), cfg.get("version", "")
                if state == "RUNNING":
                    break
            except (ProbeError, requests.RequestException):
                pass
            time.sleep(5)
        if state != "RUNNING":
            raise ProbeError(f"HA does not reach state RUNNING (last: {state or 'no answer'}). "
                             f"Log: {self.container_logs(10)}")
        ok = True
        backup_version = (self.result.backup.get("meta") or {}).get("homeassistant", {}).get("version")
        if backup_version and version != backup_version:
            self.warn(f"Sandbox runs HA {version}, backup was made by {backup_version}")

        states = self._api("/api/states", deadline)
        live_autom = sum(1 for s in states if s["entity_id"].startswith("automation."))
        autom_file = self.data_dir / "automations.yaml"
        in_backup = count_automations_yaml(autom_file.read_text(encoding="utf-8")) if autom_file.exists() else 0
        # Strong: everything in the backup is actually loaded (exact)
        ok &= self.compare("automations_loaded", "automations loaded (of those in the backup)", in_backup, live_autom, tolerance=0)
        # Weaker: the backup is not far behind production
        ok &= self.compare("automations", "automations in backup vs. production", self.production.get("automations"), in_backup)

        restored_entities = count_entities(self.data_dir / ".storage")
        ok &= self.compare("entities", "entities in the registry", self.production.get("entities"), restored_entities)

        if self.restored_db:
            qc = self.restored_db.get("quick_check")
            ok &= self.compare("recorder_integrity", "recorder DB quick_check", 1, 1 if qc == "ok" else 0,
                               tolerance=0, note=str(qc)[:80])
            newest = self.restored_db.get("newest_state_ts")
            if newest:
                lag_h = max(0.0, (self.backup_ts - newest) / 3600)
                max_lag = float(self.opt.get("recorder_max_lag_hours", 3))
                fresh = lag_h <= max_lag
                self.result.metrics.append(Metric(
                    "recorder_lag_hours", "recorder: newest entry before backup time (h)", max_lag, round(lag_h, 2), fresh,
                    f"allowed ≤ {max_lag:g} h"))
                ok &= fresh
        # Count configuration errors in the log (note only)
        try:
            r = requests.get(self._url("/api/error_log"), headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            bad = [ln for ln in r.text.splitlines() if "Invalid config" in ln or "Setup failed for" in ln]
            if bad:
                self.warn(f"{len(bad)} setup errors in the sandbox log (e.g. {bad[0][:100]})")
        except requests.RequestException:
            pass
        self.fail_on_mismatch(ok)
        return (f"HA {version}, {live_autom} automations loaded, {restored_entities} entities in the registry, "
                f"recorder DB {'ok' if self.restored_db.get('quick_check') == 'ok' else 'not checked'}")
