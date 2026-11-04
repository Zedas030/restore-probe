"""Plugin registry. New probe modules: add a file, subclass Plugin, register it here."""

from __future__ import annotations

from .base import Plugin, ServiceResult, StageResult  # noqa: F401


def get_plugin_class(name: str) -> type[Plugin]:
    from . import databases, generic, grafana, homeassistant, influxdb, nginx, prometheus

    registry: dict[str, type[Plugin]] = {
        "homeassistant": homeassistant.HomeAssistantPlugin,
        "grafana": grafana.GrafanaPlugin,
        "prometheus": prometheus.PrometheusPlugin,
        "nginx": nginx.NginxPlugin,
        "influxdb": influxdb.InfluxDBPlugin,
        "http": generic.HttpPlugin,
        "files": generic.FilesPlugin,
        "sqlite": databases.SQLitePlugin,
        "postgres": databases.PostgresPlugin,
        "mariadb": databases.MariaDBPlugin,
    }
    if name not in registry:
        raise KeyError(f"Unknown plugin '{name}'. Known: {', '.join(sorted(registry))}")
    return registry[name]
