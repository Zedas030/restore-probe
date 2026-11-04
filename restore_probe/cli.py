"""Command line: restore-probe run | backup | check-config | list-backups | selftest | notify-test | alert | report | cleanup"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG, ConfigError, load_config
from .util import ProbeError, fmt_bytes


def setup_logging(verbose: bool, logfile: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(sh)
    if logfile:
        try:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.WatchedFileHandler(logfile, encoding="utf-8")  # logrotate-friendly
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError as e:
            root.warning("Log file %s not writable: %s", logfile, e)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args, cfg) -> int:
    from .notify import HomeAssistantNotifier, compose_alert, compose_summary, write_prometheus_textfile
    from .report import write_html, write_json
    from .runner import Runner, load_previous

    log = logging.getLogger("restore_probe")
    if os.geteuid() != 0:
        log.warning("Not started as root; reading production data and iptables will probably fail")
    runner = Runner(cfg, only=args.only, keep_work=args.keep, previous=load_previous(cfg.state_dir))
    run = runner.run().to_dict()

    write_json(cfg, run)
    try:
        log.info("Report: %s", write_html(cfg))
    except Exception:
        log.exception("HTML report could not be written")
    if cfg.prometheus_textfile:
        try:
            write_prometheus_textfile(cfg.prometheus_textfile, run)
        except Exception:
            log.exception("Prometheus textfile could not be written")

    if not args.no_notify:
        ha = HomeAssistantNotifier(cfg)
        ha.publish_run(run)
        alert = compose_alert(run)
        if alert:
            url = os.environ.get("RESTORE_PROBE_REPORT_URL")
            ha.alert(alert[0], alert[1] + (f"\n{url}" if url else ""))
        else:
            ha.clear_alert()
        for e in ha.errors:
            log.warning("Report to Home Assistant failed: %s", e)
    print()
    print(f"Restore Probe {run['run_id']}: {run['status'].upper()}")
    print(compose_summary(run))
    if run.get("error"):
        print("Error:", run["error"])
        return 2
    return 0


def cmd_backup(args, cfg) -> int:
    from .backup import run_backup

    return run_backup(cfg, only=args.only, notify=not args.no_notify)


def cmd_check_config(args, cfg) -> int:
    from .plugins import get_plugin_class
    from .sources import SOURCES

    problems = []
    for s in cfg.services:
        try:
            get_plugin_class(s.plugin)
        except KeyError as e:
            problems.append(str(e))
        if s.source.get("type") not in SOURCES:
            problems.append(f"{s.name}: unknown source type {s.source.get('type')}")
    for w in cfg.warnings:
        print("WARNING:", w)
    for p in problems:
        print("ERROR:", p)
    print(f"{len(cfg.services)} services: " + ", ".join(f"{s.name} ({s.plugin}/{s.source['type']})" for s in cfg.services))
    print("Home Assistant reports:", "on" if (cfg.home_assistant.enabled and cfg.home_assistant.token) else "OFF (no token)")
    print("Prometheus textfile:", cfg.prometheus_textfile or "off")
    print("restic repository:", cfg.restic.repository or "not configured")
    return 1 if problems else 0


def cmd_list_backups(args, cfg) -> int:
    from .sources import make_source

    rc = 0
    for s in cfg.services:
        if args.only and s.name not in args.only:
            continue
        try:
            info = make_source(cfg, s.source).latest()
            flag = "OLD!" if info.age_hours > s.backup_max_age_hours else "ok"
            print(f"{s.name:16} {info.date_str:>13}  {info.age_hours:6.1f} h  {fmt_bytes(info.size_bytes or 0):>10}  {flag:4}  {info.label}")
        except ProbeError as e:
            print(f"{s.name:16} ERROR: {e}")
            rc = 1
    return rc


def cmd_selftest(args, cfg) -> int:
    """Isolation proof without services, plus Home Assistant reachability. For acceptance tests and debugging."""
    from datetime import datetime

    from .notify import HomeAssistantNotifier
    from .sandbox import Sandbox, default_isolation_targets
    from .util import Deadline

    rc = 0
    sb = Sandbox(cfg.sandbox, datetime.now().strftime("selftest-%H%M%S"))
    host_ip = sb.ensure_network()
    targets = default_isolation_targets(host_ip, cfg.sandbox.isolation_must_fail)
    iso = sb.verify_isolation(targets, Deadline(180))
    print("Sandbox network:", cfg.sandbox.network, cfg.sandbox.subnet, "bridge IP", host_ip)
    print("iptables rule:", "active" if sb.host_block_rule_present() else "MISSING")
    mem = Sandbox.memory_limits_supported()
    print("Memory limits:", "enforced" if mem else "NOT enforced (memory cgroup disabled, see README → Troubleshooting)")
    for c in iso.checks:
        print(f"  {'REACHABLE ❌' if c['reachable'] else 'blocked   ✅'}  {c['target']}")
    print("Isolation:", "PROVEN" if iso.passed else "BROKEN")
    if not iso.passed:
        rc = 1
    ha = HomeAssistantNotifier(cfg)
    if ha.enabled:
        ok = ha.set_state(f"sensor.{cfg.home_assistant.sensor_prefix}_selftest",
                          datetime.now().astimezone().isoformat(timespec="seconds"),
                          {"friendly_name": "Restore Probe self-test", "device_class": "timestamp"})
        print("Home Assistant:", "reachable, sensor written" if ok else f"ERROR {ha.errors}")
        rc |= 0 if ok else 1
    else:
        print("Home Assistant: reports disabled (no token)")
    return rc


def cmd_notify_test(args, cfg) -> int:
    from .notify import HomeAssistantNotifier

    ha = HomeAssistantNotifier(cfg)
    if not ha.enabled:
        print("Home Assistant reports are not configured (HA_TOKEN missing)")
        return 1
    ha.alert("Restore Probe: test message", "If you can read this, the notification path works.")
    if ha.errors:
        print("ERROR:", ha.errors)
        return 1
    print("Test message sent via", cfg.home_assistant.notify_service or "persistent_notification")
    return 0


def cmd_alert(args, cfg) -> int:
    from .notify import HomeAssistantNotifier

    ha = HomeAssistantNotifier(cfg)
    if not ha.enabled:
        print("Home Assistant reports are not configured (HA_TOKEN missing)", file=sys.stderr)
        return 1
    ha.alert(args.title, args.message)
    if ha.errors:
        print("ERROR:", ha.errors, file=sys.stderr)
        return 1
    return 0


def cmd_report(args, cfg) -> int:
    from .report import write_html

    print("Report written:", write_html(cfg))
    return 0


def cmd_cleanup(args, cfg) -> int:
    from .sandbox import Sandbox
    from .util import rmtree_force

    ids = Sandbox.cleanup_stale()
    print(f"{len(ids)} sandbox container(s) removed")
    if cfg.work_dir.exists():
        for p in cfg.work_dir.iterdir():
            rmtree_force(p)
    print("Work directory emptied:", cfg.work_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="restore-probe", description="Proves that backups can actually be restored.")
    ap.add_argument("-c", "--config", type=Path, default=Path(os.environ.get("RESTORE_PROBE_CONFIG", DEFAULT_CONFIG)))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", type=Path, default=Path(os.environ.get("RESTORE_PROBE_LOG", "/var/log/restore-probe/restore-probe.log")))
    ap.add_argument("--version", action="version", version=f"restore-probe {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="start a run (all or selected services)")
    p.add_argument("--only", nargs="+", metavar="SERVICE")
    p.add_argument("--no-notify", action="store_true", help="no reports to Home Assistant")
    p.add_argument("--keep", action="store_true", help="keep the work directory after the run (debugging)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("backup", help="create the restic backup of all configured backup sets")
    p.add_argument("--only", nargs="+", metavar="SET")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(fn=cmd_backup)
    sub.add_parser("check-config", help="validate the configuration").set_defaults(fn=cmd_check_config)
    p = sub.add_parser("list-backups", help="show the latest backup per service")
    p.add_argument("--only", nargs="+", metavar="SERVICE")
    p.set_defaults(fn=cmd_list_backups)
    sub.add_parser("selftest", help="isolation proof and Home Assistant reachability").set_defaults(fn=cmd_selftest)
    sub.add_parser("notify-test", help="send a test notification").set_defaults(fn=cmd_notify_test)
    p = sub.add_parser("alert", help="send a notification through the configured channels (for OnFailure scripts)")
    p.add_argument("--title", required=True)
    p.add_argument("--message", required=True)
    p.set_defaults(fn=cmd_alert)
    sub.add_parser("report", help="regenerate the HTML report from history").set_defaults(fn=cmd_report)
    sub.add_parser("cleanup", help="remove leftovers of earlier runs").set_defaults(fn=cmd_cleanup)
    args = ap.parse_args(argv)

    setup_logging(args.verbose, args.log_file if args.cmd in ("run", "backup") else None)

    # SIGTERM (systemctl stop, kill) must still run every finally block so no sandbox container is left behind
    def _terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, _terminate)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("Configuration error:", e, file=sys.stderr)
        return 2
    try:
        return args.fn(args, cfg)
    except ProbeError as e:
        print("Error:", e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
