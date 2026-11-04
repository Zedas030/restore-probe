"""Reports: JSON history (one file per run + latest.json) and a static HTML page."""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .util import fmt_bytes, fmt_duration

log = logging.getLogger("restore_probe.report")

STATUS_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skipped": "⏭️", "running": "⏳"}
STATUS_TEXT = {"ok": "ok", "warn": "with notes", "fail": "failed", "skipped": "skipped"}


def write_json(cfg: Config, run: dict) -> Path:
    hist = cfg.state_dir / "history"
    hist.mkdir(parents=True, exist_ok=True)
    p = hist / f"{run['run_id']}.json"
    _atomic_write(p, json.dumps(run, ensure_ascii=False, indent=1))
    _atomic_write(cfg.state_dir / "latest.json", json.dumps(run, ensure_ascii=False, indent=1))
    files = sorted(hist.glob("*.json"))
    for old in files[:-cfg.history_keep] if cfg.history_keep > 0 else []:
        old.unlink(missing_ok=True)
    return p


def load_history(cfg: Config, limit: int = 60) -> list[dict]:
    hist = cfg.state_dir / "history"
    if not hist.exists():
        return []
    out = []
    for f in sorted(hist.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def write_html(cfg: Config) -> Path:
    runs = load_history(cfg)
    cfg.www_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.www_dir / "index.html"
    _atomic_write(out, render_html(cfg, runs))
    os.chmod(out, 0o644)
    os.chmod(cfg.www_dir, 0o755)
    if (cfg.state_dir / "latest.json").exists():  # JSON next to it for other tools
        shutil.copyfile(cfg.state_dir / "latest.json", cfg.www_dir / "latest.json")
        os.chmod(cfg.www_dir / "latest.json", 0o644)
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_num(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else f"{v:g}"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e5e7eb;--ok:#15803d;--warn:#b45309;--fail:#b91c1c;--okbg:#dcfce7;--warnbg:#fef3c7;--failbg:#fee2e2;--skipbg:#e5e7eb}
@media(prefers-color-scheme:dark){:root{--bg:#0f141b;--card:#171e28;--fg:#e6e9ef;--muted:#9aa3b2;--line:#2a3340;--okbg:#14532d;--warnbg:#78350f;--failbg:#7f1d1d;--skipbg:#334155;--ok:#86efac;--warn:#fcd34d;--fail:#fca5a5}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.15rem;margin:28px 0 10px}
.sub{color:var(--muted);margin-bottom:18px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.card h3{margin:0 0 6px;font-size:1.05rem;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.badge{font-size:.75rem;padding:2px 8px;border-radius:999px;font-weight:600}
.ok{background:var(--okbg);color:var(--ok)}.warn{background:var(--warnbg);color:var(--warn)}.fail{background:var(--failbg);color:var(--fail)}.skipped{background:var(--skipbg);color:var(--muted)}
.msg{margin:4px 0 8px;overflow-wrap:anywhere}.kv{color:var(--muted);font-size:.85rem;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:600}details{margin-top:8px}summary{cursor:pointer;color:var(--muted);font-size:.9rem}pre{white-space:pre-wrap;font-size:.8rem;background:var(--bg);padding:8px;border-radius:6px;overflow:auto}
.hist td:first-child{white-space:nowrap}.dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.fail{background:var(--fail)}.dot.skipped{background:var(--muted)}
footer{color:var(--muted);font-size:.8rem;margin-top:32px}
"""


def render_html(cfg: Config, runs: list[dict]) -> str:
    latest = runs[0] if runs else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{_e(cfg.report_title)}</title><style>{CSS}</style></head><body><main>",
             f"<h1>{_e(cfg.report_title)}</h1>"]
    if not latest:
        parts.append("<p class='sub'>No run recorded yet.</p></main></body></html>")
        return "".join(parts)

    st = latest["status"]
    parts.append(f"<div class='sub'>Last run {_e(latest['started_iso'].replace('T', ' '))} on {_e(latest['host'])} · "
                 f"duration {_e(fmt_duration(latest['duration_seconds']))} · overall result "
                 f"<span class='badge {st}'>{STATUS_ICON.get(st, '')} {STATUS_TEXT.get(st, st)}</span></div>")
    if latest.get("error"):
        parts.append(f"<div class='card fail'><strong>Run aborted:</strong> {_e(latest['error'])}</div>")

    parts.append("<div class='cards'>")
    for s in latest["services"]:
        b = s.get("backup") or {}
        parts.append(f"<div class='card'><h3>{STATUS_ICON.get(s['status'], '')} {_e(s['title'])} "
                     f"<span class='badge {s['status']}'>{STATUS_TEXT.get(s['status'], s['status'])}</span></h3>")
        parts.append(f"<div class='msg'>{_e(s.get('message'))}</div>")
        kv = []
        if b:
            kv.append(f"Backup {_e(b.get('label'))} from {_e(b.get('date'))} ({_e(b.get('age_hours'))} h old"
                      + (f", {_e(fmt_bytes(b['size_bytes']))}" if b.get("size_bytes") else "") + ")")
        if s.get("image"):
            kv.append(f"Image {_e(s['image'])}")
        if s.get("restore_seconds") is not None:
            kv.append(f"Restore {_e(fmt_duration(s['restore_seconds']))}, total {_e(fmt_duration(s['total_seconds'] or 0))}")
        parts.append("<div class='kv'>" + "<br>".join(kv) + "</div>")
        if s.get("stages"):
            parts.append("<table>" + "".join(
                f"<tr><td>{'✅' if x['ok'] else '❌'} Stage {x['stage']}</td><td>{_e(x['name'])}</td><td>{_e(x['detail'])}</td></tr>"
                for x in s["stages"]) + "</table>")
        if s.get("metrics"):
            parts.append("<details><summary>Comparison values</summary><table><tr><th>Value</th><th>Production</th><th>Sandbox</th><th></th></tr>" + "".join(
                f"<tr><td>{_e(m['label'])}</td><td>{_fmt_num(m['production'])}</td><td>{_fmt_num(m['restored'])}</td>"
                f"<td>{'✅' if m['ok'] else '❌'} {_e(m.get('note', ''))}</td></tr>" for m in s["metrics"]) + "</table></details>")
        if s.get("warnings"):
            parts.append("<details open><summary>Notes</summary><ul>" + "".join(f"<li>{_e(w)}</li>" for w in s["warnings"]) + "</ul></details>")
        iso = s.get("isolation") or {}
        if iso:
            parts.append("<details><summary>Sandbox proof (docker inspect)</summary><pre>" + _e(json.dumps(iso, ensure_ascii=False, indent=1)) + "</pre></details>")
        if s.get("error") and s["status"] == "fail":
            parts.append("<details><summary>Error details</summary><pre>" + _e(s["error"]) + "</pre></details>")
        parts.append("</div>")
    parts.append("</div>")

    iso = latest.get("isolation") or {}
    if iso:
        parts.append("<h2>Isolation proof</h2><div class='card'>")
        parts.append(f"<div class='msg'>{'✅ sandbox network is sealed' if iso.get('passed') else '❌ ISOLATION BROKEN'} · "
                     f"iptables rule {'active' if iso.get('host_block_rule') else 'MISSING'}</div><table>")
        for c in iso.get("checks", []):
            parts.append(f"<tr><td>{'❌ reachable' if c['reachable'] else '✅ blocked'}</td><td>{_e(c['target'])}</td></tr>")
        parts.append("</table><details><summary>Container properties</summary><pre>" + _e(json.dumps(iso.get("inspect", {}), indent=1)) + "</pre></details></div>")

    if latest.get("notes"):
        parts.append("<h2>Notes on this run</h2><div class='card'><ul>" + "".join(f"<li>{_e(n)}</li>" for n in latest["notes"]) + "</ul></div>")

    parts.append("<h2>History</h2><div class='card'><table class='hist'><tr><th>Run</th><th>Result</th><th>Services</th><th>Duration</th></tr>")
    for r in runs:
        dots = " ".join(f"<span title='{_e(s['title'])}: {_e(s.get('message'))}'><span class='dot {s['status']}'></span>{_e(s['title'])}</span>"
                        for s in r.get("services", []))
        parts.append(f"<tr><td>{_e(r['started_iso'].replace('T', ' '))}</td><td><span class='badge {r['status']}'>{STATUS_TEXT.get(r['status'], r['status'])}</span></td>"
                     f"<td>{dots}</td><td>{_e(fmt_duration(r.get('duration_seconds', 0)))}</td></tr>")
    parts.append("</table></div>")
    parts.append(f"<footer>Restore Probe {_e(latest.get('version'))} · page generated {now} · <a href='latest.json'>latest.json</a></footer></main></body></html>")
    return "".join(parts)
