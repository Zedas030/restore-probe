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
    assert "12 <script>" not in html and "12 &lt;script&gt;" in html    # error texts are escaped
    assert "Isolation proof" in html and "192.0.2.1:80" in html
    assert (cfg.www_dir / "latest.json").exists()
    assert load_history(cfg)[0]["run_id"].startswith("2026110")


def test_render_html_without_runs(cfg):
    assert "No run recorded yet" in render_html(cfg, [])


def test_html_theme_hero_and_home_button(cfg):
    page = render_html(cfg, [_run()])
    assert "--surface-3" in page and "data-theme" in page          # shared theme inlined (works offline)
    assert "hl-theme" in page and "Toggle light/dark" in page
    assert "1 of 2 backups not restorable" in page and "class='card hero bad'" in page
    assert "Services ok" in page and "Oldest backup" in page and "1/2" in page
    assert page.count("class='stages'") == 2 and "Stage 3" in page
    assert "class='btn cross home-btn'" not in page     # no dashboard link unless configured
    cfg.ui.home_url = "http://192.0.2.10:8092/"
    cfg.ui.home_label = "My <Dashboard>"
    page = render_html(cfg, [_run("ok", "ok")])
    assert "href='http://192.0.2.10:8092/'" in page and "My &lt;Dashboard&gt;" in page
    assert "All 2 backups restorable" in page and "class='card hero ok'" in page


def test_html_unreached_stages_and_warn(cfg):
    run = _run("warn", "fail")
    run["services"][1]["stages"] = run["services"][1]["stages"][:1]
    run["services"][1]["stages"][0]["ok"] = False
    page = render_html(cfg, [run])
    assert page.count("<span class='st-detail'>not reached</span>") == 2
    run = _run("warn", "ok")
    run["status"] = "warn"
    assert "All 2 backups restorable, 1 with notes" in render_html(cfg, [run])


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


def test_favicon_follows_overall_status():
    from restore_probe.report import favicon_href
    assert favicon_href("ok").startswith("data:image/svg+xml,")
    assert "%2316a34a" in favicon_href("ok")
    assert "%23dc2626" in favicon_href("fail")
    assert "%2364748b" in favicon_href(None)
    assert "<" not in favicon_href("warn") and "#" not in favicon_href("warn")
