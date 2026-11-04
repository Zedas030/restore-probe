"""Probe module for large full-system archives (e.g. raspiBackup .tgz images) that are too big to restore nightly.

The source must set `restore: false`; nothing is extracted and no container is started.
Stages: 1 archive and companion files present, size plausible · 2 the whole archive reads back without errors
(gzip CRC + tar structure) · 3 required paths are inside and the entry count did not shrink.

Reading a 10+ GB archive takes minutes, so stage 2 remembers what it already verified (file name, size, mtime)
in the state directory and only reads an archive once.

options: companions: ["*.img", "*.mbr", "*.sfdisk"]   (glob patterns next to the archive)
         required: ["etc/fstab", "etc/passwd", …]   (paths inside the archive, without leading ./)
         min_size_gb: 1
         min_entries: 10000
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..util import Deadline, ProbeError, fmt_bytes
from .base import Plugin


class ArchivePlugin(Plugin):
    def _state_file(self) -> Path:
        return self.cfg.state_dir / f"archive-verified-{self.svc.name}.json"

    def after_restore(self, info) -> None:
        self.archive = Path(info.location)
        st = self.archive.stat()
        self.fingerprint = {"name": self.archive.name, "size": st.st_size, "mtime": int(st.st_mtime)}

    def start(self, deadline: Deadline) -> None:
        pass  # nothing to start

    def stage1(self, deadline: Deadline) -> str:
        size = self.archive.stat().st_size
        min_gb = float(self.opt.get("min_size_gb", 0.1))
        if size < min_gb * 1e9:
            raise ProbeError(f"Archive is only {fmt_bytes(size)}, at least {min_gb:g} GB expected")
        missing = [pat for pat in self.opt.get("companions", []) if not list(self.archive.parent.glob(pat))]
        if missing:
            raise ProbeError("Companion files missing next to the archive: " + ", ".join(missing))
        return f"{self.archive.name}: {fmt_bytes(size)}" + (f", companions {', '.join(self.opt['companions'])} present"
                                                             if self.opt.get("companions") else "")

    def stage2(self, deadline: Deadline) -> str:
        cache = {}
        try:
            cache = json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if cache.get("fingerprint") == self.fingerprint and cache.get("ok"):
            self.entries, self.found = cache["entries"], set(cache.get("found", []))
            return f"already verified on {cache.get('verified_at', '?')}: {self.entries:,} entries, readable"

        required = [r.lstrip("./") for r in self.opt.get("required", [])]
        found: set[str] = set()
        entries = 0
        errors = ""
        # Stream the listing: never hold millions of names in memory
        proc = subprocess.Popen(["tar", "-tzf", str(self.archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                entries += 1
                name = line.rstrip("\n").lstrip("./")
                if name in required:
                    found.add(name)
                if entries % 20000 == 0 and deadline.expired():
                    proc.kill()
                    raise ProbeError(f"Time limit exceeded while reading the archive ({entries:,} entries so far)")
            proc.wait(timeout=max(10, deadline.remaining()))
            errors = proc.stderr.read().strip()
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode != 0:
            raise ProbeError(f"Archive is damaged (tar exit {proc.returncode} after {entries:,} entries): "
                             + (errors.splitlines()[-1] if errors else "no message"))
        self.entries, self.found = entries, found
        from datetime import datetime
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file().write_text(json.dumps({
            "fingerprint": self.fingerprint, "ok": True, "entries": entries, "found": sorted(found),
            "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
        return f"read completely, {entries:,} entries, gzip and tar structure intact"

    def stage3(self, deadline: Deadline) -> str:
        required = [r.lstrip("./") for r in self.opt.get("required", [])]
        missing = [r for r in required if r not in self.found]
        ok = self.compare("entries", "entries in the archive", float(self.opt.get("min_entries", 1)), self.entries, tolerance=0)
        ok &= self.compare("required", "required paths present", len(required), len(required) - len(missing), tolerance=0,
                           note=("missing: " + ", ".join(missing)) if missing else "")
        self.fail_on_mismatch(ok, "Archive incomplete")
        return f"{self.entries:,} entries, {len(required)} required paths present"
