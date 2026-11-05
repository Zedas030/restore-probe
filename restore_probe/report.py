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




# ---------------------------------------------------------------- HTML page
# The shared homelab theme ships verbatim in restore_probe/assets/ and is inlined, so the page works offline.
# Tool-specific additions below use only the theme's CSS variables.

THEME_FILE = Path(__file__).resolve().parent / "assets" / "homelab-theme.css"

CSS = """
.top .live{max-width:46vw;overflow:hidden;text-overflow:ellipsis}
.sec,article.card{scroll-margin-top:90px}
.hero{margin-bottom:16px}.hero h2{overflow-wrap:anywhere}.hero p{overflow-wrap:anywhere}
.hero.none .h-ico{background:var(--surface-3);color:var(--muted)}
.tile-val{overflow-wrap:anywhere}
.card-h .ico.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 14%,transparent)}
.card-h .ico.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 16%,transparent)}
.card-h .ico.bad{color:var(--bad);background:color-mix(in srgb,var(--bad) 14%,transparent)}
.card-h .ico.none{color:var(--muted)}
.svc-msg{margin:0 0 14px;color:var(--text-2);overflow-wrap:anywhere}
.stages{list-style:none;margin:0 0 16px;padding:12px 14px;display:grid;gap:12px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm)}
.stages li{display:grid;grid-template-columns:28px minmax(0,1fr) auto;column-gap:12px;row-gap:2px;align-items:start}
.st-ico{grid-column:1;grid-row:1 / span 2;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--surface-3);color:var(--faint)}
.st-ico .i{width:15px;height:15px;stroke-width:2.4}
.st-ico.ok{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.st-ico.bad{background:color-mix(in srgb,var(--bad) 15%,transparent);color:var(--bad)}
.st-name{grid-column:2;grid-row:1;min-width:0;font-weight:600;color:var(--text);line-height:28px;overflow-wrap:anywhere}
.st-n{margin-right:8px;font-size:11.5px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);white-space:nowrap}
.st-time{grid-column:3;grid-row:1;line-height:28px;font-size:12px;color:var(--faint);white-space:nowrap}
.st-detail{grid-column:2 / 4;grid-row:2;min-width:0;font-size:13.5px;color:var(--muted);overflow-wrap:anywhere}
.stages li.none .st-name{color:var(--muted);font-weight:500}
.card-b .kv{margin-bottom:14px}
.card-b details pre{margin:0 0 12px;max-height:420px}
.card-b details .tbl-wrap{margin-bottom:12px}
.card-b details ul,.card-b > ul{margin:0 0 12px;padding-left:20px;color:var(--text-2);font-size:13.5px}
.card-b details li{overflow-wrap:anywhere}
.t td.num,.t th.num{text-align:right;white-space:nowrap}
.t td .pill{margin-right:6px}
.t.hist{min-width:560px}.t.hist td:first-child{white-space:nowrap}
.hist-svcs{display:flex;flex-wrap:wrap;gap:4px 14px}
.hist-svc{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,230px),1fr));gap:8px;margin:14px 0}
.chk{display:flex;align-items:center;gap:10px;min-width:0;padding:8px 10px;border-radius:var(--r-sm);background:var(--surface-2);border:1px solid var(--line)}
.chk .mono{min-width:0;overflow-wrap:anywhere;color:var(--text-2)}
@media (max-width:640px){.top .live{display:none}.sec,article.card{scroll-margin-top:140px}.card-b .t th,.card-b .t td{padding:8px 10px}.hero{align-items:flex-start;padding:18px}.hero h2{font-size:20px}.hero .h-ico{width:44px;height:44px}}
"""

FAVICON_COLOR = {"ok": "#16a34a", "warn": "#d97706", "fail": "#dc2626"}
FAVICON_MARK = {
    "ok": "<path d='M12 16.5l3 3 5.5-6' stroke-width='2.6'/>",
    "warn": "<path d='M16 11.5v5' stroke-width='2.8'/><circle cx='16' cy='20.6' r='.4' stroke-width='2.8'/>",
    "fail": "<path d='M13 13l6 6M19 13l-6 6' stroke-width='2.6'/>",
}

TONE = {"ok": "ok", "warn": "warn", "fail": "bad"}          # run/service status → theme tone class
STAGE_NAMES = {1: "service answers", 2: "login/API reachable", 3: "data plausible"}

_SVG = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{}</svg>'
ICON = {
    "brand": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M5.5 2.5v4h4"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
    "ok": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "warn": '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    "bad": '<path d="M6 6l12 12M18 6L6 18"/>',
    "none": '<path d="M6 12h12"/>',
    "grid": '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "timer": '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9.5 2.5h5"/>',
    "archive": '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9M10 13h4"/>',
    "home": '<path d="M3 11l9-8 9 8M5 10v10h14V10"/>',
    "moon": '<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z"/>',
    "file": '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>',
}

BOOT_JS = ("(function(){var t;try{t=localStorage.getItem('hl-theme')}catch(e){}"
           "if(t!=='light'&&t!=='dark')t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';"
           "document.documentElement.dataset.theme=t})()")
TOGGLE_JS = ("(function(){var r=document.documentElement,t=r.dataset.theme==='dark'?'light':'dark';"
             "r.dataset.theme=t;try{localStorage.setItem('hl-theme',t)}catch(e){}})()")


def _icon(name: str) -> str:
    return _SVG.format(ICON[name])


def _tone(status: str | None) -> str:
    return TONE.get(status or "", "none")


def _pill(status: str | None) -> str:
    t = _tone(status)
    cls = f"pill {t}" if t != "none" else "pill"
    return f"<span class='{cls}'><span class='d'></span>{_e(STATUS_TEXT.get(status, status))}</span>"


def _when(iso: str | None, fmt: str = "%d %b %Y %H:%M") -> str:
    if not iso:
        return "–"
    try:
        return datetime.fromisoformat(iso).strftime(fmt)
    except ValueError:
        return str(iso).replace("T", " ")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _theme_css() -> str:
    try:
        return THEME_FILE.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Theme stylesheet missing (%s), report renders unstyled", e)
        return ""


def favicon_href(status: str | None) -> str:
    """Inline SVG favicon: a restore arrow around a mark, colored by the overall result."""
    color = FAVICON_COLOR.get(status or "", "#64748b")
    mark = FAVICON_MARK.get(status or "", "")
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           f"<rect width='32' height='32' rx='7' fill='{color}'/>"
           "<g fill='none' stroke='#fff' stroke-linecap='round' stroke-linejoin='round'>"
           "<path d='M7.2 18.5A9 9 0 1 0 9.6 9.4' stroke-width='2.4'/>"
           "<path d='M8.8 4.8l.8 4.6-4.6.8' stroke-width='2.4'/>"
           f"{mark}</g></svg>")
    return "data:image/svg+xml," + svg.replace("<", "%3C").replace(">", "%3E").replace("#", "%23")


def _header(cfg: Config, latest: dict | None) -> str:
    ui = getattr(cfg, "ui", None)
    home_url = (getattr(ui, "home_url", "") or "").strip()
    home_label = getattr(ui, "home_label", "") or "Dashboard"
    tabs = [("#services", "grid", "Overview", True), ("#isolation", "shield", "Isolation", False),
            ("#history", "clock", "History", False)]
    on_attr = " class='on' aria-current='page'"
    nav = "".join(f"<a{on_attr if on else ''} href='{href}'>{_icon(ic)}{label}</a>" for href, ic, label, on in tabs)
    if latest:
        st = latest["status"]
        live = (f"<span class='live' title='Result of the last run'><i class='sd {_tone(st)}'></i>"
                f"{_e(STATUS_TEXT.get(st, st))} · {_e(_when(latest.get('started_iso'), '%d %b %H:%M'))}</span>")
    else:
        live = "<span class='live'><i class='sd'></i>no run yet</span>"
    home = (f"<a class='btn cross home-btn' href='{_e(home_url)}' title='{_e(home_label)}'>{_icon('home')}"
            f"<span class='lbl'>{_e(home_label)}</span></a>") if home_url else ""
    return ("<header class='top'>"
            f"<a class='brand' href='./'><span class='brand-mark'>{_icon('brand')}</span>"
            f"<span><span class='brand-name'>{_e(cfg.report_title)}</span>"
            "<span class='brand-sub'>Nightly proof that backups restore</span></span></a>"
            f"<nav class='tabs' aria-label='Sections'>{nav}</nav>"
            f"<div class='top-right'>{live}{home}"
            f"<button class='icon-btn' type='button' onclick=\"{TOGGLE_JS}\" aria-label='Toggle light/dark'>{_icon('moon')}</button>"
            "</div></header>")


def _hero(latest: dict) -> str:
    svcs = [s for s in latest.get("services", []) if s.get("status") != "skipped"]
    n = len(svcs)
    failed = [s for s in svcs if s.get("status") == "fail"]
    noted = [s for s in svcs if s.get("status") == "warn"]
    when = (f"Last run {_e(_when(latest.get('started_iso')))} on {_e(latest.get('host'))}, "
            f"took {_e(fmt_duration(latest.get('duration_seconds') or 0))}.")
    if latest.get("error"):
        tone, ic, title = "bad", "bad", "Run aborted"
        text = f"{_e(latest['error'])} {when}"
    elif failed:
        tone, ic = "bad", "bad"
        title = (f"{len(failed)} of {n} backups not restorable" if n > 1 else "The backup is not restorable")
        text = "Failed: " + "; ".join(f"<b>{_e(s['title'])}</b> ({_e(s.get('message'))})" for s in failed) + f". {when}"
    elif not n:
        tone, ic, title = "none", "none", "No service was checked"
        text = f"All services are disabled or were skipped. {when}"
    elif noted:
        tone, ic = "warn", "warn"
        title = f"All {_plural(n, 'backup')} restorable, {len(noted)} with notes" if n > 1 else "The backup is restorable, with notes"
        text = "Notes for " + ", ".join(f"<b>{_e(s['title'])}</b>" for s in noted) + f". {when}"
    else:
        tone, ic = "ok", "ok"
        title = f"All {_plural(n, 'backup')} restorable" if n > 1 else "The backup is restorable"
        text = f"Every service was restored into the sandbox, started and matched production. {when}"
    return (f"<section class='card hero {tone}'><div class='h-ico'>{_icon(ic)}</div>"
            f"<div><h2>{_e(title)}</h2><p>{text}</p></div></section>")


def _tiles(cfg: Config, latest: dict) -> str:
    svcs = latest.get("services", [])
    checked = [s for s in svcs if s.get("status") != "skipped"]
    counts = {k: sum(1 for s in svcs if s.get("status") == k) for k in ("ok", "warn", "fail", "skipped")}
    tone_ok = "bad" if counts["fail"] or latest.get("error") else ("warn" if counts["warn"] else "ok")
    sub_ok = " · ".join(f"{counts[k]} {label}" for k, label in
                        (("warn", "with notes"), ("fail", "failed"), ("skipped", "skipped")) if counts[k]) or "no notes, no failures"

    limits = {s.name: s.backup_max_age_hours for s in cfg.services}
    aged = [(s, (s.get("backup") or {}).get("age_hours")) for s in svcs]
    aged = [(s, a) for s, a in aged if isinstance(a, (int, float))]
    if aged:
        s_old, age = max(aged, key=lambda x: x[1])
        limit = limits.get(s_old.get("name"))
        age_tone = "warn" if limit is not None and age > limit else ""
        age_val = f"{age:.1f} h" if age < 72 else f"{age / 24:.1f} d"
        age_sub = f"{_e(s_old.get('title'))}" + (f" · limit {_fmt_num(limit)} h" if limit is not None else "")
    else:
        age_tone, age_val, age_sub = "", "–", "no backup information"

    def tile(ic: str, label: str, val: str, sub: str, tone: str = "") -> str:
        return (f"<div class='card tile'><div class='tile-top'>{_icon(ic)}{label}</div>"
                f"<div class='tile-val {tone}'>{val}</div><div class='tile-sub'>{sub}</div></div>")

    return ("<div class='grid cols-4'>"
            + tile("check", "Services ok", f"{counts['ok']}/{len(checked)}", sub_ok, tone_ok)
            + tile("calendar", "Last run", _e(_when(latest.get("started_iso"), "%d %b %H:%M")),
                   f"{_e(_when(latest.get('started_iso'), '%Y'))} · host {_e(latest.get('host'))}")
            + tile("timer", "Duration", _e(fmt_duration(latest.get("duration_seconds") or 0)),
                   "whole run incl. isolation test")
            + tile("archive", "Oldest backup", _e(age_val), age_sub, age_tone)
            + "</div>")


def _stages(s: dict) -> str:
    stages = s.get("stages") or []
    if not stages:
        return ""
    items = []
    for x in stages:
        t = "ok" if x.get("ok") else "bad"
        dur = x.get("duration_s")
        time_s = f"<span class='st-time'>{dur:.1f} s</span>" if isinstance(dur, (int, float)) else ""
        detail = f"<span class='st-detail'>{_e(x.get('detail'))}</span>" if x.get("detail") else ""
        items.append(f"<li class='{t}'><span class='st-ico {t}' role='img' aria-label='{'passed' if x.get('ok') else 'failed'}'>"
                     f"{_icon('ok' if x.get('ok') else 'bad')}</span>"
                     f"<span class='st-name'><span class='st-n'>Stage {_e(x.get('stage'))}</span>{_e(x.get('name'))}</span>"
                     f"{time_s}{detail}</li>")
    last = max((x.get("stage") or 0 for x in stages), default=0)
    for n in range(last + 1, 4):          # stages the run did not reach after a failure
        items.append(f"<li class='none'><span class='st-ico' role='img' aria-label='not reached'>{_icon('none')}</span>"
                     f"<span class='st-name'><span class='st-n'>Stage {n}</span>{_e(STAGE_NAMES.get(n, ''))}</span>"
                     "<span class='st-detail'>not reached</span></li>")
    return "<ol class='stages'>" + "".join(items) + "</ol>"


def _service_card(s: dict) -> str:
    st = s.get("status")
    t = _tone(st)
    b = s.get("backup") or {}
    out = [f"<article class='card{' tone-' + t if t != 'none' else ''}' id='svc-{_e(s.get('name'))}'>"
           f"<div class='card-h'><span class='ico {t}'>{_icon(t)}</span><h3>{_e(s.get('title'))}</h3>"
           f"<span class='aside'>{_pill(st)}</span></div><div class='card-b'>"]
    if s.get("message"):
        out.append(f"<p class='svc-msg'>{_e(s['message'])}</p>")
    out.append(_stages(s))
    kv = []
    if b:
        kv.append(("Backup", _e(b.get("label"))))
        kv.append(("Date", _e(b.get("date"))))
        kv.append(("Age", f"{_e(b.get('age_hours'))} h"))
        if b.get("size_bytes"):
            kv.append(("Size", _e(fmt_bytes(b["size_bytes"]))))
    if s.get("image"):
        kv.append(("Image", f"<span class='mono'>{_e(s['image'])}</span>"))
    if s.get("restore_seconds") is not None:
        kv.append(("Restore", _e(fmt_duration(s["restore_seconds"]))))
        kv.append(("Total", _e(fmt_duration(s.get("total_seconds") or 0))))
    if kv:
        out.append("<dl class='kv'>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in kv) + "</dl>")
    if s.get("metrics"):
        rows = "".join(
            f"<tr><td>{_e(m.get('label'))}</td><td class='num'>{_fmt_num(m.get('production'))}</td>"
            f"<td class='num'>{_fmt_num(m.get('restored'))}</td>"
            f"<td><span class='pill {'ok' if m.get('ok') else 'bad'}'><span class='d'></span>{'ok' if m.get('ok') else 'differs'}</span>"
            f"{_e(m.get('note', ''))}</td></tr>" for m in s["metrics"])
        out.append("<details><summary>Comparison values</summary><div class='tbl-wrap'><table class='t'>"
                   "<thead><tr><th>Value</th><th class='num'>Production</th><th class='num'>Sandbox</th><th>Check</th></tr></thead>"
                   f"<tbody>{rows}</tbody></table></div></details>")
    if s.get("warnings"):
        out.append("<details open><summary>Notes</summary><ul>"
                   + "".join(f"<li>{_e(w)}</li>" for w in s["warnings"]) + "</ul></details>")
    iso = s.get("isolation") or {}
    if iso:
        out.append("<details><summary>Sandbox proof (docker inspect)</summary><pre>"
                   + _e(json.dumps(iso, ensure_ascii=False, indent=1)) + "</pre></details>")
    if s.get("error") and st == "fail":
        out.append("<details><summary>Error details</summary><pre>" + _e(s["error"]) + "</pre></details>")
    out.append("</div></article>")
    return "".join(out)


def _isolation(latest: dict) -> str:
    iso = latest.get("isolation") or {}
    if not iso:
        return ""
    ok = bool(iso.get("passed"))
    rule = bool(iso.get("host_block_rule"))
    checks = "".join(
        f"<div class='chk'><span class='pill {'bad' if c.get('reachable') else 'ok'}'><span class='d'></span>"
        f"{'reachable' if c.get('reachable') else 'blocked'}</span><span class='mono'>{_e(c.get('target'))}</span></div>"
        for c in iso.get("checks", []))
    return ("<section class='sec' id='isolation'><div class='sec-h'><h2>Isolation proof</h2>"
            "<span class='sub'>Before any service starts, these targets must be unreachable from the sandbox</span></div>"
            f"<div class='card tone-{'ok' if ok else 'bad'}'><div class='card-h'><span class='ico {'ok' if ok else 'bad'}'>{_icon('shield')}</span>"
            f"<h3>Sandbox network</h3><span class='aside'><span class='pill {'ok' if ok else 'bad'}'><span class='d'></span>"
            f"{'sealed' if ok else 'BROKEN'}</span></span></div><div class='card-b'>"
            f"<div class='note {'ok' if ok and rule else 'bad'}'>{'Sandbox network is sealed' if ok else 'ISOLATION BROKEN'} · "
            f"iptables rule {'active' if rule else 'MISSING'}</div>"
            f"<div class='checks'>{checks}</div>"
            "<details><summary>Container properties</summary><pre>"
            + _e(json.dumps(iso.get("inspect", {}), indent=1)) + "</pre></details></div></div></section>")


def _history(runs: list[dict]) -> str:
    rows = []
    for r in runs:
        dots = "".join(f"<span class='hist-svc' title='{_e(s.get('title'))}: {_e(s.get('message'))}'>"
                       f"<i class='sd {_tone(s.get('status'))}'></i>{_e(s.get('title'))}</span>"
                       for s in r.get("services", []))
        rows.append(f"<tr><td>{_e(_when(r.get('started_iso'), '%Y-%m-%d %H:%M'))}</td><td>{_pill(r.get('status'))}</td>"
                    f"<td><div class='hist-svcs'>{dots}</div></td>"
                    f"<td class='num'>{_e(fmt_duration(r.get('duration_seconds') or 0))}</td></tr>")
    return ("<section class='sec' id='history'><div class='sec-h'><h2>History</h2>"
            f"<span class='sub'>{_plural(len(runs), 'run')}, newest first</span></div>"
            "<div class='card'><div class='tbl-wrap'><table class='t hist'><thead><tr><th>Run</th><th>Result</th>"
            "<th>Services</th><th class='num'>Duration</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div></div></section>")


def render_html(cfg: Config, runs: list[dict]) -> str:
    latest = runs[0] if runs else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    icon = favicon_href(latest["status"] if latest else None)
    parts = ["<!doctype html>\n<html lang='en' data-theme='dark'><head><meta charset='utf-8'>"
             "<meta name='viewport' content='width=device-width,initial-scale=1'>"
             "<meta name='color-scheme' content='light dark'>"
             f"<link rel='icon' type='image/svg+xml' href=\"{icon}\">"
             f"<title>Overview · {_e(cfg.report_title)}</title>"
             f"<style>{_theme_css()}</style><style>{CSS}</style>"
             f"<script>{BOOT_JS}</script></head><body>",
             _header(cfg, latest),
             "<main class='content'>"
             "<div class='page-head'><div><div class='kicker'>Backup restore check</div>"
             f"<h1>{_e(cfg.report_title)}</h1>"
             "<p>Every night the latest backup of each service is restored into a sealed sandbox, started with the "
             "production image and compared with production.</p></div>"
             f"<div class='actions'><a class='btn sm' href='latest.json'>{_icon('file')}latest.json</a></div></div>"]
    if not latest:
        parts.append(f"<section class='card hero none'><div class='h-ico'>{_icon('none')}</div>"
                     "<div><h2>No run recorded yet.</h2><p>The report fills in after the first run "
                     "(<span class='mono'>restore-probe run</span> or the nightly timer).</p></div></section>"
                     f"<footer class='foot'>Restore Probe · page generated {now}</footer></main></body></html>")
        return "".join(parts)

    parts.append(_hero(latest))
    parts.append(_tiles(cfg, latest))

    svcs = latest.get("services", [])
    parts.append("<section class='sec' id='services'><div class='sec-h'><h2>Services</h2>"
                 "<span class='sub'>Stage 1 service answers · Stage 2 login/API reachable · Stage 3 data plausible</span></div>"
                 "<div class='grid cols-2'>" + "".join(_service_card(s) for s in svcs) + "</div></section>")
    parts.append(_isolation(latest))
    if latest.get("notes"):
        parts.append("<section class='sec' id='notes'><div class='sec-h'><h2>Notes on this run</h2></div>"
                     "<div class='card tone-warn'><div class='card-b'><ul>"
                     + "".join(f"<li>{_e(n)}</li>" for n in latest["notes"]) + "</ul></div></div></section>")
    parts.append(_history(runs))
    parts.append(f"<footer class='foot'>Restore Probe {_e(latest.get('version'))} · page generated {now} · "
                 "<a href='latest.json'>latest.json</a></footer></main></body></html>")
    return "".join(parts)
