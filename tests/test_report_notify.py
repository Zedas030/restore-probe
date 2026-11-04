import json
import time

from restore_probe.notify import compose_alert, write_prometheus_textfile
from restore_probe.plugins.base import Metric, ServiceResult, StageResult
from restore_probe.report import load_history, render_html, write_html, write_json
from restore_probe.runner import RunResult


def _run(status_a="ok", status_b="fail") -> dict:
    a = ServiceResult("homeassistant", "Home Assistant", status=status_a, message="Backup from 04 Nov 04:46, restorable in 2:14 min",
                      backup={"label": "x.tar", "date": "04 Nov 04:46", "age_hours": 3.2, "timestamp": time.time() - 3.2 * 3600, "size_bytes": 980224000},
                      stages=[StageResult(1, "service answers", True, "HTTP 200"), StageResult(2, "Login", True), StageResult(3, "data", True, "412 entities")],
                      metrics=[Metric("entities", "entities", 920, 918, True)], restore_seconds=134, total_seconds=300, image="ha:2026.10.4")
    b = ServiceResult("grafana", "Grafana", status=status_b, message="Stage 3 failed: dashboards 3 instead of 12",
                      backup={"label": "abc123", "date": "04 Nov 02:30", "age_hours": 5.0, "timestamp": time.time() - 18000},
                      stages=[StageResult(1, "a", True), StageResult(2, "b", True), StageResult(3, "c", False, "dashboards 3 instead of 12")],
                      metrics=[Metric("dashboards", "Dashboards", 12, 3, False)], error="dashboards 3 instead of 12 <script>")
    r = RunResult(run_id="20261105-080000", started=time.time() - 600, finished=time.time(), status="fail",
                  isolation={"passed": True, "checks": [{"target": "192.0.2.1:80", "reachable": False}], "inspect": {}, "host_block_rule": True},
                  services=[a, b])
    return r.to_dict()


def test_compose_alert():
    run = _run()
    title, msg = compose_alert(run)
    assert "1 backup(s) not restorable" in title
    assert "Grafana" in msg and "Home Assistant" not in msg
    assert compose_alert(_run("ok", "ok")) is None
    t, m = compose_alert(_run("ok", "warn"))
    assert "notes" in t
    run["error"] = "ISOLATION BROKEN"
    assert compose_alert(run)[0].endswith("aborted")


def test_json_history_and_html(cfg):
    run = _run()
    write_json(cfg, run)
    assert (cfg.state_dir / "latest.json").exists()
    for i in range(7):
        r = dict(run)
        r["run_id"] = f"2026110{i}-080000"
        write_json(cfg, r)
    assert len(list((cfg.state_dir / "history").glob("*.json"))) == cfg.history_keep
    out = write_html(cfg)
    html = out.read_text(encoding="utf-8")
    assert "Home Assistant" in html and "failed" in html
    assert "<script>" not in html and "&lt;script&gt;" in html    # error texts are escaped
    assert "Isolation proof" in html and "192.0.2.1:80" in html
    assert (cfg.www_dir / "latest.json").exists()
    assert load_history(cfg)[0]["run_id"].startswith("2026110")


def test_render_html_without_runs(cfg):
    assert "No run recorded yet" in render_html(cfg, [])


def test_prometheus_textfile(cfg):
    run = _run()
    write_prometheus_textfile(cfg.prometheus_textfile, run)
    text = cfg.prometheus_textfile.read_text()
    assert 'restore_probe_service_status{service="grafana"} 2' in text
    assert 'restore_probe_service_ok{service="homeassistant"} 1' in text
    assert 'restore_probe_metric_restored{service="grafana",metric="dashboards"} 3' in text
    assert "restore_probe_isolation_ok 1" in text
    run["services"].append({"name": "off", "title": "Off", "status": "skipped", "stages": [], "metrics": []})
    write_prometheus_textfile(cfg.prometheus_textfile, run)
    text = cfg.prometheus_textfile.read_text()
    assert 'restore_probe_service_status{service="off"} 3' in text
    assert 'restore_probe_service_ok{service="off"}' not in text
    assert "restore_probe_run_status 2" in text
    assert text.endswith("\n")
    assert not cfg.prometheus_textfile.with_suffix(".prom.tmp").exists()
