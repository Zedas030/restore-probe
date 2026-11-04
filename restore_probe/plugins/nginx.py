"""Probe module for nginx (native installation: /etc/nginx + /var/www).

Source: restic snapshot tagged 'nginx'. Sandbox: official nginx image in production's version.
Debian-specific module loaders (modules-enabled) are emptied in the sandbox copy because those module paths do not
exist in the image. Stages: 1 nginx -t and first port · 2 every site answers · 3 file count and size of the web
root vs. production.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from ..util import Deadline, ProbeError, dir_size, fmt_bytes, run
from .base import Plugin

# nginx' master process starts as root and switches the workers to an unprivileged user
DEFAULT_CAPS = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"]


def detect_nginx_version() -> str | None:
    for binary in ("nginx", "/usr/sbin/nginx"):
        res = run([binary, "-v"], check=False, quiet=True, timeout=30)
        m = re.search(r"nginx/(\d+\.\d+\.\d+)", res.stderr + res.stdout)
        if m:
            return m.group(1)
    return None


def listen_ports(sites_dir: Path) -> dict[str, list[int]]:
    """Read the listen ports per site file (comments ignored, IPv4/IPv6 forms understood)."""
    out: dict[str, list[int]] = {}
    if not sites_dir.exists():
        return out
    for f in sorted(sites_dir.iterdir()):
        if not f.is_file():
            continue
        ports: list[int] = []
        text = re.sub(r"#.*", "", f.read_text(encoding="utf-8", errors="replace"))
        for m in re.finditer(r"\blisten\s+(?:\[[^\]]*\]:|[\d.]+:)?(\d+)", text):
            p = int(m.group(1))
            if p not in ports:
                ports.append(p)
        if ports:
            out[f.name] = ports
    return out


def site_roots(sites_dir: Path) -> dict[str, list[str]]:
    """Absolute `root` directives per site file (used to tell whether a site's content is in the backup)."""
    out: dict[str, list[str]] = {}
    if not sites_dir.exists():
        return out
    for f in sorted(sites_dir.iterdir()):
        if f.is_file():
            text = re.sub(r"#.*", "", f.read_text(encoding="utf-8", errors="replace"))
            roots = [m.group(1).rstrip(";") for m in re.finditer(r"\broot\s+([^\s;]+)", text)]
            out[f.name] = [r for r in roots if r.startswith("/")]
    return out


class NginxPlugin(Plugin):
    def prepare(self) -> None:
        if not self.opt.get("image") or self.opt.get("image") == "auto":
            v = detect_nginx_version()
            if not v:
                raise ProbeError("Cannot detect the nginx version (set options.image, e.g. nginx:1.26.3)")
            self.opt["image"] = f"nginx:{v}"
        self.prod_www = Path(self.opt.get("production_www", "/var/www"))
        self.production["files"], self.production["bytes"] = dir_size(self.prod_www)
        self.expect_status = tuple(self.opt.get("expect_status", [200, 301, 302, 403]))
        self.caps = self.opt.get("cap_add", DEFAULT_CAPS)

    def _cap_args(self) -> list[str]:
        out: list[str] = []
        for c in self.caps:
            out += ["--cap-add", c]
        return out

    def after_restore(self, info) -> None:
        self.etc = self.find_file("nginx.conf", parent_name="nginx", option="config_path").parent
        self.www = self.find_dir(self.opt.get("www_suffix", "var/www"), option="www_path")
        mods = self.etc / "modules-enabled"
        if mods.exists():
            for f in mods.iterdir():
                f.unlink()
        self.sites = listen_ports(self.etc / "sites-enabled")
        if not self.sites:
            raise ProbeError("No site with a listen port in sites-enabled")
        # Sites whose web root is not part of this backup cannot answer in the sandbox: skip them with a note
        roots = site_roots(self.etc / "sites-enabled")
        www_abs = "/" + str(self.www.relative_to(self.work)).replace("\\", "/")
        self.skipped: dict[str, str] = {}
        for site in list(self.sites):
            for r in roots.get(site, []):
                inside = r == www_abs or r.startswith(www_abs.rstrip("/") + "/")
                if not inside and not (self.work / r.lstrip("/")).exists():
                    self.skipped[site] = r
                    del self.sites[site]
                    break
        for site in self.opt.get("skip_sites", []):
            if self.sites.pop(site, None) is not None:
                self.skipped[site] = "options.skip_sites"
        if not self.sites:
            raise ProbeError("No site left to check (every site serves content from outside the backup)")
        # What production answers on each port: the sandbox has to answer the same
        self.prod_status: dict[int, int | None] = {}
        prod_host = self.opt.get("production_host", "127.0.0.1")
        for ports in self.sites.values():
            for p in ports:
                try:
                    self.prod_status[p] = requests.get(f"http://{prod_host}:{p}/", timeout=10, allow_redirects=False).status_code
                except requests.RequestException:
                    self.prod_status[p] = None
        self.restored_files, self.restored_bytes = dir_size(self.www)
        # Test the configuration without network
        res = run(["docker", "run", "--rm", "--network", "none", *self._cap_args(),
                   "-v", f"{self.etc}:/etc/nginx:ro", "-v", f"{self.www}:/var/www:ro",
                   self.image(), "nginx", "-t"], check=False, timeout=120)
        out = (res.stderr or res.stdout).strip().splitlines()
        if not res.ok:
            raise ProbeError("nginx -t fails: " + (out[-1] if out else f"exit {res.returncode}"))
        self.config_test = out[-1] if out else "nginx -t ok"

    def start(self, deadline: Deadline) -> None:
        self.container = self.sandbox.run_container(
            self.svc.name, self.image(), mounts=[(self.etc, "/etc/nginx", "ro"), (self.www, "/var/www", "ro")],
            extra_args=self._cap_args())
        self.sandbox.wait_running(self.container, deadline, "nginx")
        self.ip = self.sandbox.container_ip(self.container)

    def stage1(self, deadline: Deadline) -> str:
        first = next(iter(self.sites.values()))[0]
        self.sandbox.wait_port(self.ip, first, deadline, "nginx")
        return f"{self.config_test}; port {first} open"

    def stage2(self, deadline: Deadline) -> str:
        bad = []
        okc = 0
        for site, ports in self.sites.items():
            for p in ports:
                try:
                    r = requests.get(f"http://{self.ip}:{p}/", timeout=15, allow_redirects=False)
                    want = self.prod_status.get(p)
                    if (want is not None and r.status_code == want) or (want is None and r.status_code in self.expect_status):
                        okc += 1
                    else:
                        bad.append(f"{site}:{p} → HTTP {r.status_code} (production: {want or 'unreachable'})")
                except requests.RequestException as e:
                    bad.append(f"{site}:{p} → {type(e).__name__}")
        if bad:
            raise ProbeError("Sites do not answer: " + "; ".join(bad))
        info = f"{okc} site ports answer like production ({', '.join(self.sites)})"
        if self.skipped:  # information, not a warning: these sites are backed up elsewhere (or not meant to be)
            info += "; not part of this backup: " + ", ".join(f"{s} (root {r})" for s, r in self.skipped.items())
        return info

    def stage3(self, deadline: Deadline) -> str:
        ok = self.compare("files", "files in the web root", self.production["files"], self.restored_files)
        ok &= self.compare("bytes", "size of the web root (bytes)", self.production["bytes"], self.restored_bytes)
        if not ok:
            raise ProbeError(f"Web root smaller than production: {self.restored_files} files / {fmt_bytes(self.restored_bytes)} "
                             f"instead of {self.production['files']} / {fmt_bytes(self.production['bytes'])}")
        return f"{self.restored_files} files, {fmt_bytes(self.restored_bytes)}"
