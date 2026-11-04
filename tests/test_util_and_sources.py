import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from restore_probe.sources import HANativeSource, PathSource, _parse_restic_time, _safe_extract
from restore_probe.util import Deadline, ProbeError, fmt_bytes, fmt_duration, within_tolerance


def test_within_tolerance():
    assert within_tolerance(100, 96, 5)
    assert not within_tolerance(100, 94, 5)
    assert within_tolerance(100, 150, 5)      # mehr ist immer erlaubt
    assert within_tolerance(0, 0, 5)
    assert within_tolerance(100, 100, 0)
    assert not within_tolerance(100, 99, 0)


def test_formatting():
    assert fmt_duration(134) == "2:14 min"
    assert fmt_duration(3725) == "1:02:05 h"
    assert fmt_bytes(980224000).endswith("MB")


def test_deadline():
    d = Deadline(0.01)
    time.sleep(0.02)
    assert d.expired()
    with pytest.raises(ProbeError, match="Time limit"):
        d.check("x")


def test_restic_time_parsing():
    ts = _parse_restic_time("2026-11-05T02:30:01.123456789+01:00")
    assert abs(ts - 1793842201.123456) < 0.001          # = 2026-11-05 01:30:01 UTC
    assert _parse_restic_time("2026-11-05T01:30:01Z") == 1793842201.0


def _tar_with(entries: dict[str, bytes], mode="w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in entries.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_safe_extract_blocks_path_escape(tmp_path):
    data = _tar_with({"../evil.txt": b"x"})
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        with pytest.raises(ProbeError, match="forbidden path"):
            _safe_extract(tf, tmp_path)


def test_ha_native_latest_and_unencrypted_restore(tmp_path, cfg):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i, date in enumerate(["2026-11-02T05:00:00+01:00", "2026-11-04T05:00:00+01:00", "2026-11-03T05:00:00+01:00"]):
        inner = _tar_with({"data/configuration.yaml": b"homeassistant:\n", "data/.storage/core.config_entries": b"{}"})
        meta = json.dumps({"date": date, "protected": False, "homeassistant": {"version": "2026.10.4"}, "name": f"b{i}"}).encode()
        outer = _tar_with({"./backup.json": meta, "./homeassistant.tar.gz": inner}, mode="w:")
        (bdir / f"Automatic_backup_{i}.tar").write_bytes(outer)
    src = HANativeSource(cfg, {"type": "ha_native", "dir": str(bdir), "pattern": "Automatic_backup_*.tar"})
    info = src.latest()
    assert info.label == "Automatic_backup_1.tar"
    assert info.meta["homeassistant"]["version"] == "2026.10.4"
    target = tmp_path / "work"
    src.restore(info, target)
    assert (target / "data" / "configuration.yaml").read_text() == "homeassistant:\n"
    assert not (target / "homeassistant.tar.gz").exists()


def test_ha_native_protected_without_key(tmp_path, cfg):
    bdir = tmp_path / "b"
    bdir.mkdir()
    meta = json.dumps({"date": "2026-11-04T05:00:00+01:00", "protected": True}).encode()
    (bdir / "x.tar").write_bytes(_tar_with({"./backup.json": meta, "./homeassistant.tar.gz": b"enc"}, mode="w:"))
    src = HANativeSource(cfg, {"type": "ha_native", "dir": str(bdir), "pattern": "*.tar"})
    with pytest.raises(ProbeError, match="HA_BACKUP_KEY"):
        src.restore(src.latest(), tmp_path / "w")


def test_path_source_picks_newest(tmp_path, cfg):
    a = tmp_path / "websites-1.tar.gz"
    b = tmp_path / "websites-2.tar.gz"
    a.write_bytes(_tar_with({"var/www/index.html": b"alt"}))
    b.write_bytes(_tar_with({"var/www/index.html": b"neu"}))
    import os
    os.utime(a, (1, 1))
    src = PathSource(cfg, {"type": "path", "glob": str(tmp_path / "websites-*.tar.gz")})
    info = src.latest()
    assert info.label == "websites-2.tar.gz"
    src.restore(info, tmp_path / "w")
    assert (tmp_path / "w/var/www/index.html").read_text() == "neu"
