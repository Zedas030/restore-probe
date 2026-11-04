import io
import tarfile
from types import SimpleNamespace

import pytest

from restore_probe.config import ServiceConfig
from restore_probe.plugins.archive import ArchivePlugin
from restore_probe.sandbox import Sandbox
from restore_probe.util import Deadline, ProbeError


def _svc(opts):
    return ServiceConfig(name="system", plugin="archive", title="System image", source={"type": "path", "restore": False},
                         options=opts, timeout_min=1, tolerance_percent=5, backup_max_age_hours=192)


def _make_tgz(path, names):
    with tarfile.open(path, "w:gz") as tf:
        for n in names:
            data = b"x" * 10
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def _plugin(cfg, tmp_path, archive, opts):
    p = ArchivePlugin(cfg, _svc(opts), Sandbox(cfg.sandbox, "t"), tmp_path / "work")
    p.after_restore(SimpleNamespace(location=str(archive)))
    return p


def test_archive_ok_and_cached(cfg, tmp_path):
    a = tmp_path / "img" / "host-backup.tgz"
    a.parent.mkdir()
    _make_tgz(a, ["./etc/fstab", "./boot/firmware/cmdline.txt", "./usr/bin/x"])
    (a.parent / "host.img").write_bytes(b"\0")
    opts = {"companions": ["*.img"], "required": ["etc/fstab", "boot/firmware/cmdline.txt"], "min_size_gb": 0, "min_entries": 3}
    p = _plugin(cfg, tmp_path, a, opts)
    d = Deadline(30)
    assert "companions" in p.stage1(d)
    assert "read completely, 3 entries" in p.stage2(d)
    assert "3 entries" in p.stage3(d)
    p2 = _plugin(cfg, tmp_path, a, opts)
    assert p2.stage2(d).startswith("already verified")


def test_archive_missing_required_and_companion(cfg, tmp_path):
    a = tmp_path / "b.tgz"
    _make_tgz(a, ["./usr/bin/x"])
    p = _plugin(cfg, tmp_path, a, {"companions": ["*.mbr"], "min_size_gb": 0})
    with pytest.raises(ProbeError, match="Companion"):
        p.stage1(Deadline(30))
    p = _plugin(cfg, tmp_path, a, {"required": ["etc/fstab"], "min_size_gb": 0})
    d = Deadline(30)
    p.stage2(d)
    with pytest.raises(ProbeError, match="incomplete"):
        p.stage3(d)


def test_archive_corrupted(cfg, tmp_path):
    a = tmp_path / "c.tgz"
    _make_tgz(a, [f"./f{i}" for i in range(2000)])
    raw = bytearray(a.read_bytes())
    mid = len(raw) // 2
    raw[mid:mid + 200] = b"\x00" * 200
    a.write_bytes(raw)
    p = _plugin(cfg, tmp_path, a, {"min_size_gb": 0})
    with pytest.raises(ProbeError, match="damaged"):
        p.stage2(Deadline(30))


def test_archive_plain_tar(cfg, tmp_path):
    a = tmp_path / "host-tar-backup.tar"
    with tarfile.open(a, "w") as tf:
        for n in ["./etc/fstab", "./usr/bin/x"]:
            ti = tarfile.TarInfo(n)
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    p = _plugin(cfg, tmp_path, a, {"required": ["etc/fstab"], "min_size_gb": 0, "min_entries": 2})
    d = Deadline(30)
    assert "tar headers intact" in p.stage2(d)
    assert "2 entries" in p.stage3(d)
