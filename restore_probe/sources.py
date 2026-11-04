"""Backup sources: where the latest backup comes from and how it lands in the work directory.

Three types:
- ha_native: Home Assistant's own backups (.tar with backup.json + encrypted homeassistant.tar.gz)
- restic:    latest snapshot with a given tag from the restic repository
- path:      newest file matching a glob pattern (tar archives are extracted)
"""

from __future__ import annotations

import glob
import json
import logging
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .util import ProbeError, fmt_date, run

log = logging.getLogger("restore_probe.sources")


@dataclass
class BackupInfo:
    label: str            # e.g. file name or snapshot ID
    timestamp: float      # Unix time of the backup
    size_bytes: int | None
    location: str
    meta: dict

    @property
    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600.0

    @property
    def date_str(self) -> str:
        return fmt_date(self.timestamp)


class Source:
    def __init__(self, cfg: Config, spec: dict):
        self.cfg = cfg
        self.spec = spec

    def latest(self) -> BackupInfo:
        raise NotImplementedError

    def restore(self, info: BackupInfo, target: Path) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------- ha_native
class HANativeSource(Source):
    """Home Assistant backup (format 2): outer tar with backup.json and homeassistant.tar.gz.

    With 'protected: true' the inner archive is SecureTar-encrypted; the password is the encryption key from
    Home Assistant's emergency kit. It comes from the .env (key), never from the YAML.
    """

    def _dirs(self) -> list[Path]:
        dirs = self.spec.get("dirs") or [self.spec.get("dir")]
        return [Path(d) for d in dirs if d]

    def latest(self) -> BackupInfo:
        pattern = self.spec.get("pattern", "*.tar")
        best: BackupInfo | None = None
        for d in self._dirs():
            for f in d.glob(pattern):
                try:
                    with tarfile.open(f, "r:") as tar:
                        names = tar.getnames()
                        member = tar.getmember("./backup.json" if "./backup.json" in names else "backup.json")
                        meta = json.load(tar.extractfile(member))
                except (tarfile.TarError, KeyError, json.JSONDecodeError, OSError) as e:
                    log.warning("HA backup %s unreadable: %s", f, e)
                    continue
                ts = datetime.fromisoformat(meta["date"]).timestamp()
                if self.spec.get("only_automatic", False) and not meta.get("extra", {}).get("with_automatic_settings"):
                    continue
                info = BackupInfo(label=f.name, timestamp=ts, size_bytes=f.stat().st_size, location=str(f), meta=meta)
                if best is None or info.timestamp > best.timestamp:
                    best = info
        if best is None:
            raise ProbeError(f"No HA backup found in {', '.join(map(str, self._dirs()))}")
        return best

    def restore(self, info: BackupInfo, target: Path) -> None:
        from securetar import SecureTarFile  # imported here: unit tests do not need it

        key = self.spec.get("key")
        protected = bool(info.meta.get("protected"))
        if protected and not key:
            raise ProbeError("HA backup is encrypted but HA_BACKUP_KEY is missing in the .env")
        target.mkdir(parents=True, exist_ok=True)
        inner_name = "homeassistant.tar.gz"
        with tarfile.open(info.location, "r:") as outer:
            names = outer.getnames()
            member_name = next((n for n in names if n.endswith(inner_name)), None)
            if not member_name:
                raise ProbeError(f"{info.label}: {inner_name} missing in the backup ({names})")
            inner_path = target / inner_name
            # Stream the inner archive to disk (can be ~1 GB; never into RAM)
            with outer.extractfile(outer.getmember(member_name)) as src, open(inner_path, "wb") as dst:
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
        try:
            if protected:
                # securetar ≥ 2025: pass the password, the library handles key derivation (v2/v3)
                with SecureTarFile(inner_path, password=key, gzip=True) as stf:
                    _safe_extract(stf, target)
            else:
                with tarfile.open(inner_path, "r:gz") as tf:
                    _safe_extract(tf, target)
        except ProbeError:
            raise
        except Exception as e:  # SecureTar raises unspecific errors for a wrong key
            if protected:
                raise ProbeError(f"Decryption failed (wrong key?): {type(e).__name__}: {e}") from e
            raise ProbeError(f"Extraction failed: {type(e).__name__}: {e}") from e
        finally:
            inner_path.unlink(missing_ok=True)
        if not (target / "data").is_dir():
            raise ProbeError("Extracted HA backup contains no data/ directory")


def _safe_extract(tf, target: Path) -> None:
    """Extract without path escapes (../) and without device files; modes and owners are kept.

    Works as a stream (member by member) because encrypted SecureTar archives are not seekable.
    """
    base = target.resolve()
    for m in tf:
        dest = (base / m.name).resolve()
        if base not in dest.parents and dest != base:
            raise ProbeError(f"Archive contains a forbidden path: {m.name}")
        if m.isdev():
            continue
        tf.extract(m, base, filter="fully_trusted")


# ---------------------------------------------------------------- restic
class ResticSource(Source):
    def _env(self) -> dict[str, str]:
        r = self.cfg.restic
        if not r.repository or not r.password:
            raise ProbeError("restic: RESTIC_REPOSITORY or RESTIC_PASSWORD missing")
        return {"RESTIC_REPOSITORY": r.repository, "RESTIC_PASSWORD": r.password,
                "RESTIC_CACHE_DIR": str(self.cfg.state_dir / "restic-cache"), **r.extra_env}

    def latest(self) -> BackupInfo:
        tag = self.spec.get("tag")
        if not tag:
            raise ProbeError("restic source without tag")
        res = run([self.cfg.restic.binary, "snapshots", "--tag", tag, "--latest", "1", "--json", "--no-lock"],
                  env=self._env(), timeout=300)
        snaps = json.loads(res.stdout or "[]")
        if not snaps:
            raise ProbeError(f"restic: no snapshot with tag '{tag}'")
        s = snaps[-1]
        summary = s.get("summary") or {}
        return BackupInfo(label=s["short_id"], timestamp=_parse_restic_time(s["time"]),
                          size_bytes=summary.get("total_bytes_processed"),
                          location=f"restic:{tag}:{s['short_id']}", meta={"paths": s.get("paths"), "hostname": s.get("hostname")})

    def restore(self, info: BackupInfo, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        args = [self.cfg.restic.binary, "restore", info.label, "--target", str(target), "--no-lock"]
        for inc in self.spec.get("include", []):
            args += ["--include", inc]
        run(args, env=self._env(), timeout=3600)


def _parse_restic_time(s: str) -> float:
    # restic writes e.g. 2026-11-05T02:30:01.123456789+01:00 (9 fractional digits, Python handles 6)
    main, _, rest = s.partition(".")
    if rest:
        frac, tz = rest, ""
        for i, ch in enumerate(rest):
            if ch in "+-Z":
                frac, tz = rest[:i], rest[i:]
                break
        s = f"{main}.{frac[:6].ljust(6, '0')}{tz}"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- path
class PathSource(Source):
    """Newest file matching a glob pattern, e.g. /mnt/nas/backups/websites-*.tar.gz."""

    def latest(self) -> BackupInfo:
        pattern = self.spec.get("glob")
        if not pattern:
            raise ProbeError("path source without glob")
        files = [Path(p) for p in glob.glob(pattern)]
        files = [f for f in files if f.is_file()]
        if not files:
            raise ProbeError(f"No file matches {pattern}")
        f = max(files, key=lambda p: p.stat().st_mtime)
        st = f.stat()
        return BackupInfo(label=f.name, timestamp=st.st_mtime, size_bytes=st.st_size, location=str(f), meta={})

    def restore(self, info: BackupInfo, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        src = Path(info.location)
        if self.spec.get("extract", True) and tarfile.is_tarfile(src):
            with tarfile.open(src) as tf:
                _safe_extract(tf, target)
        else:
            shutil.copy2(src, target / src.name)


SOURCES = {"ha_native": HANativeSource, "restic": ResticSource, "path": PathSource}


def make_source(cfg: Config, spec: dict) -> Source:
    t = spec.get("type")
    if t not in SOURCES:
        raise ProbeError(f"Unknown source type: {t} (known: {', '.join(SOURCES)})")
    return SOURCES[t](cfg, spec)
