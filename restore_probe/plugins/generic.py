"""Generic probe modules.

http:  start any image with the restored directory and check it over HTTP.
       options: image, mounts: [{path: "var/lib/x", target: "/data", mode: rw, chown: "1000:1000"}], cmd: [...],
                env: {...}, user, cap_add: [...], port, path (stage 1), login_path (stage 2),
                expect_status, data_url + json_count_path (stage 3) or compare_dir (files vs. production).
files: check files only, no container (e.g. an archive of configuration files).
       options: compare_dir (production path), restored_path (sub-path in the backup), required: [relative paths]
"""

from __future__ import annotations

from pathlib import Path

import requests

from ..util import Deadline, ProbeError, dir_size, fmt_bytes, run
from .base import Plugin


def json_path(doc, path: str):
    """Tiny JSON path: 'data.items' or 'results.0.series'. Ending in a list/dict means: count it."""
    cur = doc
    for part in path.split("."):
        if part == "":
            continue
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


class FilesPlugin(Plugin):
    def prepare(self) -> None:
        cmp = self.opt.get("compare_dir")
        if cmp:
            self.production["files"], self.production["bytes"] = dir_size(Path(cmp))

    def start(self, deadline: Deadline) -> None:
        pass  # no container needed

    def _restored(self) -> Path:
        sub = self.opt.get("restored_path")
        return self.work / sub.lstrip("/") if sub else self.work

    def stage1(self, deadline: Deadline) -> str:
        r = self._restored()
        if not r.exists():
            raise ProbeError(f"Restored path missing: {r}")
        files, size = dir_size(r)
        if files == 0:
            raise ProbeError("Backup is empty")
        self.restored_files, self.restored_bytes = files, size
        return f"{files} files, {fmt_bytes(size)}"

    def stage2(self, deadline: Deadline) -> str:
        missing = [p for p in self.opt.get("required", []) if not (self._restored() / p).exists()]
        if missing:
            raise ProbeError("Required files missing: " + ", ".join(missing))
        return f"{len(self.opt.get('required', []))} required files present"

    def stage3(self, deadline: Deadline) -> str:
        if "files" in self.production:
            ok = self.compare("files", "files", self.production["files"], self.restored_files)
            ok &= self.compare("bytes", "size (bytes)", self.production["bytes"], self.restored_bytes)
            if not ok:
                raise ProbeError(f"Less data than in production ({self.restored_files} files instead of {self.production['files']})")
            return f"{self.restored_files} files, same as production"
        mn = int(self.opt.get("min_files", 1))
        if not self.compare("files", "files", mn, self.restored_files, tolerance=0):
            raise ProbeError(f"Only {self.restored_files} files, at least {mn} expected")
        return f"{self.restored_files} files"


class HttpPlugin(Plugin):
    def prepare(self) -> None:
        if not self.opt.get("image"):
            raise ProbeError("The http plugin needs options.image")
        self.port = int(self.opt.get("port", 80))
        cmp = self.opt.get("compare_dir")
        if cmp:
            self.production["files"], self.production["bytes"] = dir_size(Path(cmp))
        pu = self.opt.get("production_data_url")
        if pu:
            r = requests.get(pu, timeout=30, headers=self.opt.get("production_headers", {}))
            self.production["count"] = self._count(r)

    def _count(self, r: requests.Response) -> float:
        p = self.opt.get("json_count_path")
        if not p:
            return float(len(r.content))
        v = json_path(r.json(), p)
        return float(len(v)) if isinstance(v, (list, dict)) else float(v)

    def after_restore(self, info) -> None:
        for m in self.opt.get("mounts", []):
            host = self.work / m["path"].lstrip("/")
            if not host.exists():
                raise ProbeError(f"Path missing in the backup: {m['path']}")
            if m.get("chown"):
                run(["chown", "-R", str(m["chown"]), str(host)])

    def start(self, deadline: Deadline) -> None:
        mounts = [(self.work / m["path"].lstrip("/"), m["target"], m.get("mode", "rw")) for m in self.opt.get("mounts", [])]
        self.container = self.sandbox.run_container(
            self.svc.name, self.opt["image"], mounts=mounts, env=self.opt.get("env", {}),
            cmd=self.opt.get("cmd"), user=self.opt.get("user"),
            extra_args=[a for c in self.opt.get("cap_add", []) for a in ("--cap-add", c)])
        self.sandbox.wait_running(self.container, deadline, self.svc.title)
        self.ip = self.sandbox.container_ip(self.container)

    def _url(self, p: str) -> str:
        return f"http://{self.ip}:{self.port}{p}"

    def stage1(self, deadline: Deadline) -> str:
        st = tuple(self.opt.get("expect_status", [200]))
        r = self.wait_http(self.opt.get("path", "/"), deadline, ok_status=st, what=self.svc.title)
        return f"HTTP {r.status_code} on {self.opt.get('path', '/')}"

    def stage2(self, deadline: Deadline) -> str:
        lp = self.opt.get("login_path")
        if not lp:
            return "skipped (no login_path configured)"
        r = self.wait_http(lp, deadline, ok_status=tuple(self.opt.get("login_expect_status", [200, 302])),
                      what=f"{self.svc.title} {lp}")
        return f"HTTP {r.status_code} on {lp}"

    def stage3(self, deadline: Deadline) -> str:
        parts = []
        ok = True
        du = self.opt.get("data_url")
        label = self.opt.get("count_label", "records")
        if du:
            r = requests.get(self._url(du), timeout=30, headers=self.opt.get("headers", {}))
            if r.status_code != 200:
                raise ProbeError(f"{du}: HTTP {r.status_code}")
            cnt = self._count(r)
            exp = self.production.get("count", self.opt.get("min_count"))
            ok &= self.compare("count", label, exp, cnt)
            parts.append(f"{int(cnt)} {label}")
        if "files" in self.production:
            files, _size = dir_size(self.work / self.opt["mounts"][0]["path"].lstrip("/"))
            ok &= self.compare("files", "files", self.production["files"], files)
            parts.append(f"{files} files")
        self.fail_on_mismatch(ok)
        return ", ".join(parts) or "no data check configured"
