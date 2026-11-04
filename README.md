# Restore Probe

**Proves every night that your backups can actually be restored.**

Most backup tools verify that the files are intact. Restore Probe goes all the way: it restores the latest backup,
starts the service in an isolated throw-away container with the same image version as production, and checks
whether it answers, whether you can log in, and whether the data is plausible. The result is a sentence like:

> **Home Assistant** ✅ backup from 04 Nov 04:46, restorable in 2:14 min, 18 automations loaded, 920 entities in the registry, recorder DB ok

Built for a home lab (Raspberry Pi, Docker, Home Assistant, Grafana, Prometheus, nginx, InfluxDB), but portable to
any service and host via YAML.

## Contents

- [What it does](#what-it-does)
- [Security: the sandbox](#security-the-sandbox)
- [Installation](#installation)
- [Secrets](#secrets)
- [Operation](#operation)
- [Notifications, sensors, metrics](#notifications-sensors-metrics)
- [Adding services](#adding-services)
- [restic backup](#restic-backup)
- [Update](#update)
- [Moving to another host](#moving-to-another-host)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## What it does

Every run (by default 08:00 via systemd timer):

1. **Preflight**: lock against parallel runs, wait for Docker (a full-system backup may have stopped it), remove leftovers from earlier runs, check free disk space.
2. **Isolation proof**: a tiny container inside the sandbox network tries to reach your router, Zigbee coordinator, MQTT broker, NAS, DNS and the host itself. If even one connection succeeds, no service is started.
3. **Per service** (order from the YAML, time limit per service):
   - find the latest backup, check its age (default: alert above 26 h)
   - restore it into a temporary directory (Home Assistant backups are decrypted)
   - neutralise it for the sandbox (Home Assistant: disable hardware integrations, drop `shell_command`/`wake_on_lan`)
   - start a throw-away container with production's image version
   - **stage 1** service answers · **stage 2** login/API reachable · **stage 3** data plausible (compared with production, tolerance configurable)
   - clean up everything, even on abort (`finally`, plus `ExecStopPost` in the systemd unit)
4. **Report**: JSON per run, `latest.json`, HTML page (nginx, port 8096), Prometheus metrics, Home Assistant sensors.
5. **Alert** via a Home Assistant notify service on failure, stale backup, or a clear drop in data compared with the previous run.

| Service | Source | Stage 1 | Stage 2 | Stage 3 (data) |
|---|---|---|---|---|
| Home Assistant | HA's own backup (encrypted) | frontend HTTP 200 | login flow via API yields a token | state RUNNING, automations loaded vs. backup and vs. production, entity registry, recorder DB `quick_check` + rows |
| Grafana | restic | `/api/health` | `/login` | dashboards via API and in the DB, data sources, users, SQLite `quick_check` |
| Prometheus | restic (TSDB snapshot) | `/-/ready` | TSDB status | series at snapshot time, scrape jobs, `promtool check config` |
| nginx | restic | `nginx -t`, port open | every site answers | file count and size of the web root |
| InfluxDB 1.x | restic (`backup -portable`) | `/ping`, `restore -portable` | `SHOW DATABASES` | measurements and series cardinality per database |
| generic `http` | any | HTTP status | login path | JSON count or files |
| generic `files` | any | files present | required files | count/size vs. production |
| `sqlite` | any | header valid | `integrity_check` | rows per table vs. production |
| `postgres` / `mariadb` | dump | server ready | dump imported | rows per table |
| `archive` | full-system image (`restore: false`) | archive + companion files present | whole archive reads back (gzip CRC, tar structure; each image is read only once) | required paths inside, entry count |

**Watching other backup jobs:** any systemd unit can report its own failure through Restore Probe's notifier.
Add a drop-in (`sudo systemctl edit <unit>`) with `[Unit]` → `OnFailure=restore-probe-failed@%n.service`. Combined
with an `archive` (or `files`) service for its output, a silently broken weekly job is noticed within a day.

## Security: the sandbox

The test container must **never** switch real devices or talk to the LAN. Several layers make sure of that:

- **Internal Docker network** (`--internal`): no gateway, no route to the LAN, no internet, DNS pointed at nothing.
- **iptables rule** on the host: new connections from the sandbox network to the host are dropped. Without it the real MQTT broker would be reachable via the bridge IP. The prober still reaches the containers because it initiates the connection (conntrack `ESTABLISHED`).
- **Container hardening**: no published ports, no devices (no USB, no Zigbee stick, no D-Bus), `--cap-drop ALL`, `no-new-privileges`, own hostname `rp-sandbox-<service>`, memory/CPU/PID limits.
- **Home Assistant in addition**: in the restored `.storage/core.config_entries` every integration outside an allow-list (`sun`, `backup`, `template`, …) is set to `disabled_by: user`. ZHA, TP-Link, Tuya, Sonos, Alexa, MQTT, Fritz!Box etc. never start. `shell_command`, `wake_on_lan`, `rest_command`, `notify` are removed from the sandbox `configuration.yaml`.
- **Proof, not promise**: every run starts with the isolation test and records it in the report, together with the security-relevant fields from `docker inspect` of every service container. `restore-probe selftest` runs the test on its own at any time.

Production data is only read. The only writes outside Restore Probe's own directories are Prometheus snapshots
(create/delete via the admin API) and the temporary `influxd backup` directory inside the InfluxDB data path, both
done by the restic backup job only.

## Installation

Requirements: Debian/Raspberry Pi OS, Docker, Python ≥ 3.11, root. Paths: program `/opt/restore-probe`,
configuration `/etc/restore-probe`, state `/var/lib/restore-probe`, logs `/var/log/restore-probe`.

```bash
git clone https://github.com/Zedas030/restore-probe.git
cd restore-probe
sudo ./scripts/install.sh --nginx --prometheus-admin-api
```

Options of `install.sh` (all idempotent; an existing `config.yaml`/`.env` is never overwritten):

| Option | Effect |
|---|---|
| *(none)* | packages (`restic`, `sqlite3`, `python3-venv`), venv, units, timers, logrotate, pull `alpine:3.20` |
| `--nginx` | site `/etc/nginx/sites-available/restore-probe` (port 8096) serving the HTML report |
| `--prometheus-admin-api` | adds `--web.enable-admin-api` to `/etc/default/prometheus` (backup next to it), restarts Prometheus. Needed for consistent TSDB snapshots |
| `--ha-setup` | creates `input_datetime.restore_probe_last_run` in Home Assistant's `configuration.yaml` and the dead-man's-switch automation (needs `HA_TOKEN`) |

Then:

1. **Enter the secrets** (see [Secrets](#secrets)).
2. **Adjust the configuration**: `/etc/restore-probe/config.yaml` is commented; check paths, isolation-test targets, notify service and services.
3. `sudo restore-probe check-config` · `sudo restore-probe selftest` · `sudo restore-probe notify-test`
4. `sudo ./scripts/install.sh --ha-setup`
5. First backup and first run: `sudo systemctl start restic-backup.service` then `sudo systemctl start restore-probe.service`

## Secrets

All secrets live in **one file**: `/etc/restore-probe/.env` (owner root, mode 600; `install.sh` creates it from
`config/env.example`). The YAML never contains a secret; it references them as `${NAME}`.

| Variable | What it is | Where to get it | How to rotate |
|---|---|---|---|
| `HA_TOKEN` | Home Assistant long-lived access token of an admin user | HA → your profile → *Security* → *Long-lived access tokens* → create one named "Restore Probe" | create a new token in HA, replace the value, delete the old token in HA; `sudo restore-probe notify-test` |
| `HA_BACKUP_KEY` | encryption key of Home Assistant's backups | HA → Settings → System → Backups → ⚙ → *Encryption key* → download the emergency kit | if you change the key in HA, update it here; older backups keep the old key, so keep the old kit too |
| `RESTIC_PASSWORD` | password of the restic repository | choose it yourself, e.g. `openssl rand -base64 24`; **store it in your password manager as well**, without it the repository is worthless | `restic key add` with the new password, then `restic key remove` for the old key, then update `.env` |
| `RESTIC_REPOSITORY` | repository location (not secret, kept here so `restic` CLI and the tool agree) | e.g. `/mnt/nas/system-backups/restic` or any restic backend URL | edit; a new location needs `restic init` (the backup job does that when the repository is empty) |
| `DOCKER_WAIT_MIN` | optional: minutes to wait for Docker if it is stopped (default 45) | – | – |

Edit with `sudoedit /etc/restore-probe/.env` and verify with `sudo restore-probe check-config`. Keep the mode at 600:
the tool warns on every run if the file is readable by others. `service.env` (next to it) holds non-secret values
for the systemd units, currently only `RESTORE_PROBE_REPORT_URL`.

## Operation

| Command | Purpose |
|---|---|
| `sudo restore-probe run` | full run, same as the timer |
| `sudo restore-probe run --only grafana nginx` | selected services only |
| `sudo restore-probe run --no-notify --keep` | debugging: no HA notifications, keep the work directory |
| `sudo restore-probe list-backups` | latest backup per service with its age |
| `sudo restore-probe selftest` | isolation proof + HA reachability |
| `sudo restore-probe backup [--only set]` | restic backup of all/selected sets |
| `sudo restore-probe report` | regenerate the HTML report from history |
| `sudo restore-probe cleanup` | remove sandbox leftovers and the work directory |
| `systemctl list-timers restore-probe.timer restic-backup.timer` | next runs |
| `journalctl -u restore-probe -n 100` · `/var/log/restore-probe/restore-probe.log` | logs (rotated, 14 days) |
| http://&lt;host&gt;:8096/ | HTML report with history, `latest.json` next to it |

Change the schedule with `sudo systemctl edit restore-probe.timer` and override `OnCalendar=` (same for `restic-backup.timer`).

Resources: the systemd units limit the tool itself (`MemoryMax=2G`, `CPUQuota=200%`, `Nice=10`, idle I/O); the
sandbox containers get `sandbox.memory`/`sandbox.cpus` from the YAML (default 1.5 GB / 2 CPUs). Before each
restore the tool checks that `min_free_gb` will still be free afterwards.

Result status: `ok` · `warn` (checks passed but with a note: stale backup, data drop vs. the previous run, setup errors in the log) · `fail` · `skipped`.

## Notifications, sensors, metrics

**Home Assistant** (REST API, `home_assistant:` in the YAML):

| Entity | Content |
|---|---|
| `sensor.restore_probe_<service>_status` | `ok`/`warn`/`fail`, attributes: message, backup, image, report URL |
| `sensor.restore_probe_<service>_backup_age` | age of the checked backup in hours |
| `sensor.restore_probe_<service>_restore_duration` | restore duration in seconds |
| `sensor.restore_probe_status` | overall status, attributes: number of problems, affected services |
| `sensor.restore_probe_last_run` | timestamp of the last run |
| `sensor.restore_probe_backup_status` | result of the last restic backup per set |
| `input_datetime.restore_probe_last_run` | heartbeat that survives HA restarts (dead-man's switch) |

Alerts go to `notify_service` (e.g. `notify.mobile_app_phone`) and to a `persistent_notification` (dismissed again
after a successful run).

**Dead-man's switch** in two parts: (1) `OnFailure=` in the systemd units reports crashes of the tool itself with the
last log lines; (2) the HA automation "Restore Probe: no report for 36 h" checks the heartbeat at 10:00 and 20:00
and reports when the timer, the token or the host has died.

**Prometheus** via the node_exporter textfile collector (`general.prometheus_textfile`), no extra port:

```
restore_probe_service_status{service}          0 ok · 1 warning · 2 failure · 3 skipped
restore_probe_service_ok{service}              1 if restorable
restore_probe_backup_age_hours{service}
restore_probe_backup_timestamp_seconds{service}
restore_probe_restore_duration_seconds{service}
restore_probe_service_duration_seconds{service}
restore_probe_stages_passed{service}           0–3
restore_probe_metric_production{service,metric} / restore_probe_metric_restored{…}
restore_probe_last_run_timestamp_seconds · restore_probe_run_duration_seconds · restore_probe_run_status · restore_probe_isolation_ok
restore_probe_backup_run_ok · restore_probe_backup_set_ok{set} · restore_probe_backup_set_bytes{set} · restore_probe_backup_last_run_timestamp_seconds
```

Example alerts: `restore_probe_service_ok == 0`, `time() - restore_probe_last_run_timestamp_seconds > 36*3600`, `restore_probe_isolation_ok == 0`.

## Adding services

Usually a YAML entry under `services:` with an existing plugin is enough. Decide three things:

1. **Source** (`source.type`): `ha_native` (Home Assistant backups), `restic` (snapshot with a `tag`), `path` (newest file matching a `glob`; tar archives are extracted).
2. **Plugin** (`plugin`): see the table above. Services with a container: `http`. Databases: `sqlite`, `postgres`, `mariadb`. Plain files: `files`.
3. **Production values** for the comparison (`options`), e.g. `compare_dir`, `production_file`, `production_url`.

Example: Uptime Kuma (SQLite in a Docker volume, backed up by the restic set `kuma`):

```yaml
restic:
  backup_sets:
    - name: kuma
      type: sqlite
      db: /var/lib/docker/volumes/uptime-kuma/_data/kuma.db
services:
  - name: uptime_kuma
    title: Uptime Kuma
    plugin: sqlite
    source: { type: restic, tag: kuma }
    options:
      file: var/lib/restore-probe/staging/kuma/kuma.db       # path as restic restores it
      production_file: /var/lib/docker/volumes/uptime-kuma/_data/kuma.db
      tables: [monitor, notification, user]
```

Example: any web service with its own image (`http` plugin):

```yaml
  - name: wiki
    title: Wiki
    plugin: http
    source: { type: restic, tag: wiki }
    options:
      image: ghcr.io/example/wiki:2.4.1
      mounts: [{ path: srv/wiki/data, target: /data, mode: rw, chown: "1000:1000" }]
      env: { WIKI_DB: sqlite:///data/wiki.db }
      port: 8080
      path: /healthz                      # stage 1
      login_path: /login                  # stage 2
      data_url: /api/pages                # stage 3: count a list …
      json_count_path: items
      production_data_url: http://127.0.0.1:8080/api/pages   # … and compare with production
      count_label: pages
```

If no plugin fits: add a file in `restore_probe/plugins/`, subclass `Plugin` (`prepare`, `after_restore`, `start`,
`stage1..3`), register it in `plugins/__init__.py`. `self.compare(...)` records comparison values,
`self.find_file()`/`self.find_dir()` locate restored content regardless of its path, and
`self.sandbox.run_container()` always applies every security setting. The existing plugins are short and serve as templates.

## restic backup

For services without a backup of their own Restore Probe ships a restic job (`restore-probe backup`, timer 02:30).
Sets are defined under `restic.backup_sets` and tagged with their name:

| Type | What happens |
|---|---|
| `files` | back up paths as they are (`exclude` supported) |
| `sqlite` | consistent copy via the SQLite online-backup API into a staging directory |
| `prometheus_snapshot` | TSDB snapshot via the admin API, deleted afterwards |
| `influxdb_portable` | `influxd backup -portable` inside the production container, deleted afterwards |
| `command` | any command that fills an output directory |

Every set accepts `extra_paths` (e.g. configuration directories). `require_mount` prevents silently backing up to the
SD card when the NAS is missing. Retention via `keep` (default 7 daily / 4 weekly / 6 monthly),
`restic check --read-data-subset=5%` once a week. Repository and password come from `.env`.

Manual restore (without Restore Probe), e.g. Grafana:

```bash
export RESTIC_REPOSITORY=… RESTIC_PASSWORD=…     # values from /etc/restore-probe/.env
restic snapshots --tag grafana
restic restore latest --tag grafana --target /tmp/restored
```

## Update

```bash
cd restore-probe && git pull
sudo ./scripts/install.sh            # the flags from the installation are not needed again
sudo restore-probe check-config && sudo restore-probe selftest
```

`install.sh` copies the code to `/opt/restore-probe/app`, refreshes the venv and the units; configuration, `.env`
and history stay untouched. New options show up in `config/config.example.yaml`.

## Moving to another host

Everything host-specific lives in `/etc/restore-probe/config.yaml`, `.env` and `service.env`.

1. Install on the new host (see above) and copy the three files from `/etc/restore-probe`.
2. Adjust in `config.yaml`: production paths (`production_config_dir`, `production_db`, …), container names,
   `isolation_must_fail` (gateway, hardware addresses, the old host), `require_mount`.
3. `sudo restore-probe check-config && sudo restore-probe selftest && sudo restore-probe list-backups`
4. Disable the timers on the old host: `sudo systemctl disable --now restore-probe.timer restic-backup.timer`.

The restic repository is host-independent; `restore-probe backup` on the new host writes into the same repository
(snapshots carry the hostname, Restore Probe searches by tag, not by host).

## Uninstall

```bash
sudo /opt/restore-probe/app/scripts/uninstall.sh          # keeps configuration and history
sudo /opt/restore-probe/app/scripts/uninstall.sh --purge  # also removes /etc/restore-probe and /var/lib/restore-probe
```

Removes units, timers, nginx site, sandbox network, iptables rule, Prometheus textfiles and `/opt/restore-probe`.
Not reverted automatically: `--web.enable-admin-api` in `/etc/default/prometheus` (backup next to it), the HA
automation and `input_datetime.restore_probe_last_run`, the restic repository.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ISOLATION BROKEN: reachable from the sandbox: …` | network was created without `--internal` or the iptables rule is missing. `docker network rm restore-probe`, then `sudo restore-probe selftest`. Check: `iptables -S INPUT \| grep rp-sandbox` |
| `HA backup is encrypted but HA_BACKUP_KEY is missing` / `Decryption failed` | check the key in `.env` (emergency kit, the dashes belong to it) |
| HA stage 2: `login_flow HTTP 400/403` | `trusted_networks` does not match: `sandbox.subnet` must contain the bridge IP; the backup's `configuration.yaml` may set `http: use_x_forwarded_for` |
| HA never reaches RUNNING | raise `timeout_min`, read the log in the report; often a custom component hanging without network → remove its domain from `allow_integrations` (default already does) |
| `container exited immediately` | image needs capabilities: `options.cap_add: [CHOWN, DAC_OVERRIDE, SETUID, SETGID]` |
| Prometheus backup: `HTTP 405` or `admin APIs disabled` | run `install.sh --prometheus-admin-api` |
| `… is not mounted; backup aborted` | NAS mount missing (`mount -a`, check automount). Intentional: never back up locally in silence |
| `Another Restore Probe run is active` | another run is active (`ps aux \| grep restore-probe`); after a power loss the lock in `/run` is gone automatically |
| `Not enough disk space` | lower `general.min_free_gb` or move `work_dir` to a bigger disk |
| Grafana shows fewer dashboards via API than in the DB | dashboards in folders with restricted permissions are invisible anonymously → the "dashboards (in DB)" metric still counts; if needed set `GF_AUTH_ANONYMOUS_ORG_ROLE` to `Admin` (sandbox only) |
| No HA notification despite failure | `sudo restore-probe notify-test`; token expired? `journalctl -u restore-probe \| grep "Home Assistant"` |
| Sundays "Docker has been unreachable for 45 min" | a full-system backup stops Docker for longer; raise `DOCKER_WAIT_MIN` in `.env` or move the timer |
| `Memory limits are NOT enforced` (run notes, `selftest`) / `docker info`: "No memory limit support" | the kernel's memory cgroup is off – the default on Raspberry Pi OS. Add `cgroup_enable=memory` to the single line in `/boot/firmware/cmdline.txt` (keep a backup), reboot, then `sudo restore-probe selftest` must say "enforced". Without it neither the sandbox `--memory` limit nor `MemoryMax=` of the units has any effect |
| Leftovers after an abort | `sudo restore-probe cleanup` (also happens automatically at the next start and via `ExecStopPost`) |

All containers carry the label `restore-probe=1`: `docker ps -a --filter label=restore-probe=1`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q                       # unit tests, run without Docker
sudo restore-probe run --only grafana --no-notify --keep -v   # end-to-end against the real system
```

Layout: `cli.py` (commands) · `config.py` (YAML + .env) · `runner.py` (flow, lock, time limits) ·
`sandbox.py` (network, iptables, containers, isolation test) · `sources.py` (ha_native, restic, path) ·
`plugins/` (one module per service) · `report.py` (JSON/HTML) · `notify.py` (HA, Prometheus) · `backup.py` (restic sets).

License: MIT.
