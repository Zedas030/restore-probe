"""Load the configuration: commented YAML, secrets only from the .env."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("/etc/restore-probe/config.yaml")
DEFAULT_ENV = Path("/etc/restore-probe/.env")


class ConfigError(Exception):
    pass


def load_env(path: Path) -> dict[str, str]:
    """Read a .env (KEY=VALUE, comments with #). Quoted values are unquoted."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def check_env_permissions(path: Path) -> list[str]:
    """Warn when the .env is readable by others. Secrets belong to root only."""
    warnings = []
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            warnings.append(f"{path} has mode {oct(mode)}, expected 600 (chmod 600 {path})")
    return warnings


@dataclass
class SandboxConfig:
    network: str = "restore-probe"
    subnet: str = "172.30.99.0/24"
    bridge_name: str = "rp-sandbox"
    memory: str = "1536m"
    cpus: str = "2"
    pids_limit: int = 512
    isolation_image: str = "alpine:3.20"
    isolation_must_fail: list[str] = field(default_factory=list)
    block_host_access: bool = True
    timezone: str = ""          # empty = host timezone


@dataclass
class HAConfig:
    url: str = "http://127.0.0.1:8123"
    token: str = ""
    notify_service: str | None = None
    persistent_notification: bool = True
    sensor_prefix: str = "restore_probe"
    heartbeat_entity: str | None = "input_datetime.restore_probe_last_run"
    enabled: bool = True


@dataclass
class ResticConfig:
    repository: str = ""
    password: str = ""
    binary: str = "restic"
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class UIConfig:
    home_url: str = ""          # empty = no dashboard button in the report header
    home_label: str = "Dashboard"


@dataclass
class ServiceConfig:
    name: str
    plugin: str
    title: str
    source: dict[str, Any]
    options: dict[str, Any]
    timeout_min: float
    tolerance_percent: float
    backup_max_age_hours: float
    enabled: bool = True


@dataclass
class Config:
    config_path: Path
    work_dir: Path
    state_dir: Path
    www_dir: Path
    min_free_gb: float
    default_timeout_min: float
    backup_max_age_hours: float
    tolerance_percent: float
    data_drop_alert_percent: float
    history_keep: int
    report_title: str
    prometheus_textfile: Path | None
    sandbox: SandboxConfig
    home_assistant: HAConfig
    restic: ResticConfig
    services: list[ServiceConfig]
    env: dict[str, str]
    warnings: list[str]
    ui: UIConfig = field(default_factory=UIConfig)


_ENV_REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def _resolve_env(value: Any, env: dict[str, str], where: str) -> Any:
    """Allow `${NAME}` in the YAML as a reference to a .env value (never the value itself in the YAML)."""
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            name = m.group(1)
            if name not in env:
                raise ConfigError(f"{where}: ${{{name}}} is not set in the .env")
            return env[name]
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v, env, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v, env, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def load_config(path: Path = DEFAULT_CONFIG, env_path: Path | None = None) -> Config:
    if not path.exists():
        raise ConfigError(f"Configuration missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    env_path = env_path or Path(raw.get("env_file", DEFAULT_ENV))
    env = {**load_env(env_path)}
    warnings = check_env_permissions(env_path)
    raw = _resolve_env(raw, env, "config")

    g = raw.get("general", {})
    state_dir = Path(g.get("state_dir", "/var/lib/restore-probe"))
    sb = raw.get("sandbox", {})
    ha = raw.get("home_assistant", {})
    rc = raw.get("restic", {})
    ui = raw.get("ui") or {}

    services: list[ServiceConfig] = []
    names = set()
    for s in raw.get("services", []):
        if "name" not in s or "plugin" not in s:
            raise ConfigError(f"Service without name/plugin: {s}")
        if not re.fullmatch(r"[a-z0-9_]+", s["name"]):
            raise ConfigError(f"Service name '{s['name']}' may only contain a-z, 0-9 and _")
        if s["name"] in names:
            raise ConfigError(f"Duplicate service name: {s['name']}")
        names.add(s["name"])
        if "source" not in s or "type" not in s["source"]:
            raise ConfigError(f"Service {s['name']}: source.type missing")
        services.append(ServiceConfig(
            name=s["name"], plugin=s["plugin"], title=s.get("title", s["name"]),
            source=s["source"], options=s.get("options", {}),
            timeout_min=float(s.get("timeout_min", g.get("default_timeout_min", 15))),
            tolerance_percent=float(s.get("tolerance_percent", g.get("tolerance_percent", 5))),
            backup_max_age_hours=float(s.get("backup_max_age_hours", g.get("backup_max_age_hours", 26))),
            enabled=bool(s.get("enabled", True)),
        ))
    if not services:
        raise ConfigError("No services configured (services: [])")

    textfile = g.get("prometheus_textfile")
    return Config(
        config_path=path,
        work_dir=Path(g.get("work_dir", state_dir / "work")),
        state_dir=state_dir,
        www_dir=Path(g.get("www_dir", state_dir / "www")),
        min_free_gb=float(g.get("min_free_gb", 4)),
        default_timeout_min=float(g.get("default_timeout_min", 15)),
        backup_max_age_hours=float(g.get("backup_max_age_hours", 26)),
        tolerance_percent=float(g.get("tolerance_percent", 5)),
        data_drop_alert_percent=float(g.get("data_drop_alert_percent", 20)),
        history_keep=int(g.get("history_keep", 90)),
        report_title=g.get("report_title", "Restore Probe"),
        prometheus_textfile=Path(textfile) if textfile else None,
        sandbox=SandboxConfig(**{k: v for k, v in sb.items() if k in SandboxConfig.__dataclass_fields__}),
        home_assistant=HAConfig(
            url=ha.get("url", "http://127.0.0.1:8123").rstrip("/"),
            token=ha.get("token", ""),
            notify_service=ha.get("notify_service"),
            persistent_notification=bool(ha.get("persistent_notification", True)),
            sensor_prefix=ha.get("sensor_prefix", "restore_probe"),
            heartbeat_entity=ha.get("heartbeat_entity", "input_datetime.restore_probe_last_run") or None,
            enabled=bool(ha.get("enabled", True)),
        ),
        restic=ResticConfig(
            repository=rc.get("repository", ""), password=rc.get("password", ""),
            binary=rc.get("binary", "restic"), extra_env=rc.get("extra_env", {}),
        ),
        services=services,
        env=env,
        warnings=warnings,
        ui=UIConfig(
            home_url=str(ui.get("home_url") or "").strip(),
            home_label=str(ui.get("home_label") or "Dashboard").strip() or "Dashboard",
        ),
    )
