"""Small helpers: running commands, deadlines, disk space, HTTP with retries."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("restore_probe")


class ProbeError(Exception):
    """Expected failure: the check failed, the tool itself keeps running."""


class Deadline:
    """Time limit for one service. Every wait loop asks it how much time is left."""

    def __init__(self, seconds: float):
        self.start = time.monotonic()
        self.end = self.start + seconds

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.end

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def check(self, what: str = "step") -> None:
        if self.expired():
            raise ProbeError(f"Time limit exceeded during: {what}")


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(cmd: list[str], *, timeout: float | None = 600, check: bool = True, input_text: str | None = None,
        env: dict | None = None, quiet: bool = False) -> CmdResult:
    """Run a command and return its output. With check=True a failure becomes a ProbeError."""
    if not quiet:
        log.debug("$ %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"Command exceeded {timeout}s: {' '.join(cmd[:3])} …") from e
    except FileNotFoundError as e:
        raise ProbeError(f"Program not found: {cmd[0]}") from e
    res = CmdResult(p.returncode, p.stdout, p.stderr)
    if check and not res.ok:
        tail = (res.stderr or res.stdout).strip().splitlines()[-5:]
        raise ProbeError(f"Command failed ({p.returncode}): {' '.join(cmd[:4])} … → {' | '.join(tail)}")
    return res


def free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1e9


def dir_size(path: Path) -> tuple[int, int]:
    """Number of files and total size in bytes (does not follow symlinks)."""
    files = 0
    size = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            p = Path(root) / n
            try:
                if p.is_symlink():
                    continue
                size += p.stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def rmtree_force(path: Path) -> None:
    """Delete a directory even when containers left files owned by other users."""
    if not path.exists():
        return

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)
    if path.exists():  # last attempt as root
        run(["rm", "-rf", str(path)], check=False, quiet=True)


def wait_http(url: str, deadline: Deadline, *, ok_status: tuple[int, ...] = (200,), interval: float = 3.0,
              what: str | None = None, headers: dict | None = None, alive=None) -> requests.Response:
    """Poll a URL until it answers with an expected status or the deadline passes.

    `alive` is an optional callable; when it returns False (container stopped) waiting ends immediately.
    """
    what = what or url
    last = "no answer yet"
    while not deadline.expired():
        if alive is not None and not alive():
            raise ProbeError(f"{what}: container stopped while waiting (last: {last})")
        try:
            r = requests.get(url, timeout=10, headers=headers, allow_redirects=False)
            if r.status_code in ok_status:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(interval)
    raise ProbeError(f"{what}: no usable answer before the deadline (last: {last})")


def within_tolerance(expected: float, actual: float, percent: float) -> bool:
    """Is actual at most `percent` percent below expected? More is always fine."""
    if expected <= 0:
        return actual >= 0
    return actual >= expected * (1 - percent / 100.0)


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d} h"
    return f"{m}:{s:02d} min"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_date(ts: float) -> str:
    """Short, unambiguous date for messages: '04 Nov 04:46'."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def hours_since(ts: float) -> float:
    return (time.time() - ts) / 3600.0
