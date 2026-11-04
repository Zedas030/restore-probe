"""Plugin logic that can be tested without Docker (comparison, path lookup, file and SQLite checks)."""

import sqlite3
from pathlib import Path

import pytest

from restore_probe.config import ServiceConfig
from restore_probe.plugins import get_plugin_class
from restore_probe.plugins.databases import SQLitePlugin, sqlite_counts
from restore_probe.plugins.generic import FilesPlugin, json_path
from restore_probe.plugins.grafana import count_grafana_db
from restore_probe.plugins.nginx import listen_ports
from restore_probe.sandbox import Sandbox
from restore_probe.util import Deadline, ProbeError


def _svc(name, plugin, options=None, tol=5):
    return ServiceConfig(name=name, plugin=plugin, title=name, source={"type": "path"}, options=options or {},
                         timeout_min=1, tolerance_percent=tol, backup_max_age_hours=26)


def test_registry_knows_all_plugins():
    for n in ("homeassistant", "grafana", "prometheus", "nginx", "influxdb", "http", "files", "sqlite", "postgres", "mariadb"):
        assert get_plugin_class(n)
    with pytest.raises(KeyError):
        get_plugin_class("gibtsnicht")


def test_compare_and_tolerance(cfg, tmp_path):
    p = FilesPlugin(cfg, _svc("x", "files"), Sandbox(cfg.sandbox, "t"), tmp_path / "w")
    assert p.compare("a", "A", 100, 96)
    assert not p.compare("b", "B", 100, 90)
    assert p.compare("c", "C", None, 5)          # no production value: only presence counts
    assert not p.compare("d", "D", 10, None)
    assert [m.ok for m in p.result.metrics] == [True, False, True, False]


def test_find_file_and_dir(cfg, tmp_path):
    work = tmp_path / "w"
    (work / "var/lib/grafana/plugins").mkdir(parents=True)
    (work / "var/lib/restore-probe/staging/grafana").mkdir(parents=True)
    (work / "var/lib/restore-probe/staging/grafana/grafana.db").write_bytes(b"")
    (work / "etc/grafana/provisioning").mkdir(parents=True)
    p = FilesPlugin(cfg, _svc("g", "files"), Sandbox(cfg.sandbox, "t"), work)
    assert p.find_file("grafana.db").name == "grafana.db"
    assert p.find_dir("grafana/plugins") == work / "var/lib/grafana/plugins"
    assert p.find_dir("grafana/provisioning") == work / "etc/grafana/provisioning"
    assert p.find_dir("nix/da", required=False) is None
    with pytest.raises(ProbeError):
        p.find_file("fehlt.txt")


def test_files_plugin_stages(cfg, tmp_path):
    prod = tmp_path / "prod"
    (prod / "a").mkdir(parents=True)
    (prod / "a/index.html").write_text("x" * 100)
    (prod / "b.txt").write_text("y" * 50)
    work = tmp_path / "w"
    (work / "var/www/a").mkdir(parents=True)
    (work / "var/www/a/index.html").write_text("x" * 100)
    (work / "var/www/b.txt").write_text("y" * 50)
    p = FilesPlugin(cfg, _svc("w", "files", {"restored_path": "var/www", "compare_dir": str(prod), "required": ["a/index.html"]}),
                    Sandbox(cfg.sandbox, "t"), work)
    p.prepare()
    d = Deadline(10)
    assert "2 files" in p.stage1(d)
    assert p.stage2(d).startswith("1 required")
    assert "same as production" in p.stage3(d)
    (work / "var/www/b.txt").unlink()
    p2 = FilesPlugin(cfg, _svc("w", "files", {"restored_path": "var/www", "compare_dir": str(prod)}), Sandbox(cfg.sandbox, "t"), work)
    p2.prepare()
    p2.stage1(d)
    with pytest.raises(ProbeError, match="Less data"):
        p2.stage3(d)


def _mkdb(path: Path, rows: int):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * rows)
    con.commit()
    con.close()


def test_sqlite_plugin_detects_missing_rows_and_corruption(cfg, tmp_path):
    prod = tmp_path / "prod.db"
    _mkdb(prod, 100)
    work = tmp_path / "w"
    work.mkdir()
    _mkdb(work / "copy.db", 100)
    opts = {"file": "copy.db", "production_file": str(prod)}
    p = SQLitePlugin(cfg, _svc("s", "sqlite", opts), Sandbox(cfg.sandbox, "t"), work)
    p.prepare()
    d = Deadline(10)
    p.stage1(d)
    assert "integrity_check ok" in p.stage2(d)
    assert "100 rows" in p.stage3(d)

    _mkdb(tmp_path / "w2.db", 80)
    (work / "copy.db").write_bytes((tmp_path / "w2.db").read_bytes())
    p = SQLitePlugin(cfg, _svc("s", "sqlite", opts), Sandbox(cfg.sandbox, "t"), work)
    p.prepare()
    p.stage1(d)
    p.stage2(d)
    with pytest.raises(ProbeError, match="Row counts differ"):
        p.stage3(d)

    raw = bytearray((work / "copy.db").read_bytes())
    raw[100:4000] = b"\xff" * 3900
    (work / "copy.db").write_bytes(raw)
    p = SQLitePlugin(cfg, _svc("s", "sqlite", opts), Sandbox(cfg.sandbox, "t"), work)
    p.prepare()
    p.stage1(d)
    with pytest.raises((ProbeError, sqlite3.DatabaseError)):
        p.stage2(d)


def test_sqlite_counts_and_grafana_counts(tmp_path):
    db = tmp_path / "grafana.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE dashboard (id INTEGER PRIMARY KEY, is_folder INTEGER);
        CREATE TABLE data_source (id INTEGER PRIMARY KEY);
        CREATE TABLE user (id INTEGER PRIMARY KEY);
        INSERT INTO dashboard (is_folder) VALUES (0),(0),(0),(1);
        INSERT INTO data_source DEFAULT VALUES; INSERT INTO user DEFAULT VALUES;
    """)
    con.commit()
    con.close()
    c = count_grafana_db(db)
    assert c["dashboards"] == 3 and c["folders"] == 1 and c["datasources"] == 1 and c["quick_check"] == "ok"
    assert sqlite_counts(db, None, True)["tables"]["dashboard"] == 4
    # unified storage (Grafana >= 12): legacy table empty, dashboards in 'resource'
    con = sqlite3.connect(db)
    con.executescript("""
        DELETE FROM dashboard;
        CREATE TABLE resource ("group" TEXT, resource TEXT, name TEXT);
        INSERT INTO resource VALUES ('dashboard.grafana.app','dashboards','a'),('dashboard.grafana.app','dashboards','b'),
                                    ('folder.grafana.app','folders','f'),('rules.alerting.grafana.app','configs','x');
    """)
    con.commit()
    con.close()
    c = count_grafana_db(db)
    assert c["dashboards"] == 2 and c["folders"] == 1
    assert sqlite_counts(db, None, True)["tables"]["dashboard"] == 0   # emptied above (unified storage case)


def test_nginx_site_roots(tmp_path):
    from restore_probe.plugins.nginx import site_roots
    (tmp_path / "a").write_text("server {\n  root /var/www/a;   # comment root /x\n}\n")
    (tmp_path / "b").write_text("server { root relative/path; }\n")
    assert site_roots(tmp_path) == {"a": ["/var/www/a"], "b": []}


def test_nginx_listen_ports(tmp_path):
    (tmp_path / "a").write_text("server {\n    listen 8091;\n    listen [::]:8091;\n}\n")
    (tmp_path / "b").write_text("server { listen 127.0.0.1:8095; }\n")
    assert listen_ports(tmp_path) == {"a": [8091], "b": [8095]}


def test_json_path():
    doc = {"data": {"items": [1, 2, 3]}, "results": [{"series": [{"values": [[1], [2]]}]}]}
    assert json_path(doc, "data.items") == [1, 2, 3]
    assert json_path(doc, "results.0.series.0.values") == [[1], [2]]
