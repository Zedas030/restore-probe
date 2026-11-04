"""Base class for probe modules.

Flow per service (driven by the runner):
    prepare()        → read production values (image version, counts)
    restore()        → the source restores the backup into the work directory
    after_restore()  → the plugin prepares it for the sandbox
    start()          → start the throw-away container
    stage1()         → service answers (port/HTTP)
    stage2()         → login page or API reachable
    stage3()         → data plausible (compared with production)
    cleanup          → container gone, work directory gone (always, also on errors)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, ServiceConfig
from ..sandbox import Sandbox
from ..sources import BackupInfo
from ..util import Deadline, ProbeError, rmtree_force, run, within_tolerance

log = logging.getLogger("restore_probe.plugin")

STAGE_NAMES = {1: "service answers", 2: "login/API reachable", 3: "data plausible"}


@dataclass
class StageResult:
    stage: int
    name: str
    ok: bool
    detail: str = ""
    duration_s: float = 0.0


@dataclass
class Metric:
    """One comparison value: production vs. sandbox."""
    key: str
    label: str
    production: float | None
    restored: float | None
    ok: bool
    note: str = ""


@dataclass
class ServiceResult:
    name: str
    title: str
    status: str = "pending"          # ok | warn | fail | skipped
    message: str = ""
    backup: dict = field(default_factory=dict)
    stages: list[StageResult] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    isolation: dict = field(default_factory=dict)
    restore_seconds: float | None = None
    total_seconds: float | None = None
    image: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "status": self.status, "message": self.message,
            "backup": self.backup, "image": self.image, "error": self.error, "warnings": self.warnings,
            "restore_seconds": self.restore_seconds, "total_seconds": self.total_seconds,
            "stages": [s.__dict__ for s in self.stages],
            "metrics": [m.__dict__ for m in self.metrics],
            "isolation": self.isolation,
        }


class Plugin:
    """A probe module. Subclasses fill in the stages; everything shared lives here."""

    work: Path  #: directory the source fills; plugins work below it

    def __init__(self, cfg: Config, svc: ServiceConfig, sandbox: Sandbox, work: Path):
        self.cfg = cfg
        self.svc = svc
        self.sandbox = sandbox
        self.work = work
        self.opt = svc.options
        self.result = ServiceResult(name=svc.name, title=svc.title)
        self.container: str | None = None
        self.ip: str | None = None
        self.production: dict[str, Any] = {}

    # ---- hooks ----
    def prepare(self) -> None:
        """Collect production values. Errors here are expected failures (ProbeError)."""

    def estimated_space_bytes(self, info: BackupInfo) -> int:
        """Roughly how much space does the restore need? Default: 3× the backup size."""
        return int((info.size_bytes or 0) * 3)

    def after_restore(self, info: BackupInfo) -> None:
        """Prepare the restored backup for the sandbox (ownership, neutralising integrations …)."""

    def start(self, deadline: Deadline) -> None:
        raise NotImplementedError

    def stage1(self, deadline: Deadline) -> str:
        raise NotImplementedError

    def stage2(self, deadline: Deadline) -> str:
        raise NotImplementedError

    def stage3(self, deadline: Deadline) -> str:
        raise NotImplementedError

    def cleanup(self) -> None:
        if self.container:
            self.sandbox.remove_container(self.container)
            self.container = None
        rmtree_force(self.work)

    # ---- helpers for subclasses ----
    def image(self) -> str:
        """Image for the sandbox: options.image, otherwise the image of the production container.

        The image is pulled once if it is missing locally (see Sandbox.ensure_image)."""
        if getattr(self, "_image", None):
            return self._image
        img = self.opt.get("image")
        if not img or img == "auto":
            img = None
            container = self.opt.get("production_container")
            if container:
                res = run(["docker", "inspect", "-f", "{{.Config.Image}}@@{{.Image}}", container], check=False, quiet=True)
                if res.ok:
                    tag, digest = res.stdout.strip().split("@@")
                    # Use the image ID (digest) so exactly the same version runs, even for 'latest'/'stable'
                    self.result.image = f"{tag} ({digest[7:19]})"
                    img = digest
            if not img:
                raise ProbeError(f"{self.svc.name}: cannot determine the image (set options.image)")
        else:
            self.result.image = img
        Sandbox.ensure_image(img)
        self._image = img
        return img

    def compare(self, key: str, label: str, production: float | None, restored: float | None,
                tolerance: float | None = None, note: str = "", absolute: float | None = None) -> bool:
        """Record a comparison value. OK when restored is at most `tolerance` % below production.

        For small numbers (e.g. 20 automations) 5 % is stricter than intended, so `absolute` items of
        difference are allowed as well (default options.tolerance_absolute, otherwise 2), because production
        keeps growing between the backup and the check. tolerance=0 means: exact.
        """
        tol = self.svc.tolerance_percent if tolerance is None else tolerance
        if absolute is None:
            absolute = 0 if tol == 0 else float(self.opt.get("tolerance_absolute", 2))
        if production is None or restored is None:
            ok = restored is not None
        else:
            ok = within_tolerance(production, restored, tol) or restored >= production - absolute
        self.result.metrics.append(Metric(key, label, production, restored, ok, note))
        return ok

    def fail_on_mismatch(self, ok: bool, what: str = "Data differs") -> None:
        """Raise a ProbeError listing every failed metric when ok is False."""
        if ok:
            return
        bad = [m for m in self.result.metrics if not m.ok]
        raise ProbeError(f"{what}: " + "; ".join(f"{m.label} {_n(m.restored)} instead of {_n(m.production)}" for m in bad))

    def warn(self, text: str) -> None:
        log.warning("[%s] %s", self.svc.name, text)
        self.result.warnings.append(text)

    def run_stage(self, n: int, fn, deadline: Deadline) -> bool:
        name = STAGE_NAMES[n]
        t0 = time.monotonic()
        try:
            deadline.check(name)
            detail = fn(deadline)
            self.result.stages.append(StageResult(n, name, True, detail or "", time.monotonic() - t0))
            log.info("[%s] stage %d %s: OK %s", self.svc.name, n, name, detail or "")
            return True
        except ProbeError as e:
            self.result.stages.append(StageResult(n, name, False, str(e), time.monotonic() - t0))
            log.error("[%s] stage %d %s: FAILED %s", self.svc.name, n, name, e)
            return False
        except Exception as e:  # unexpected error in the plugin: record it as a failure as well
            log.exception("[%s] stage %d %s: exception", self.svc.name, n, name)
            self.result.stages.append(StageResult(n, name, False, f"{type(e).__name__}: {e}", time.monotonic() - t0))
            return False

    def container_logs(self, tail: int = 40) -> str:
        return self.sandbox.logs(self.container, tail) if self.container else ""

    def alive(self) -> bool:
        """Is the sandbox container still running? Passed to wait loops so they stop early."""
        return bool(self.container) and self.sandbox.container_running(self.container)

    def wait_http(self, path_or_url: str, deadline: Deadline, **kw):
        """wait_http bound to this plugin's container: stops as soon as the container dies."""
        from ..util import wait_http
        url = path_or_url if path_or_url.startswith("http") else f"http://{self.ip}:{self.port}{path_or_url}"
        try:
            return wait_http(url, deadline, alive=self.alive, **kw)
        except ProbeError as e:
            if not self.alive():
                raise ProbeError(f"{e}. Container log: {self.container_logs(8)}") from e
            raise

    def chown_for_image(self, *paths: Path, image: str | None = None) -> str:
        """Hand restored data to the user the image runs as (root when the image sets no USER)."""
        img = image or self.image()
        user = run(["docker", "image", "inspect", "-f", "{{.Config.User}}", img], check=False, quiet=True).stdout.strip()
        if not user or user in ("root", "0", "0:0"):
            owner = "0:0"
        elif all(part.isdigit() for part in user.split(":")):
            owner = user if ":" in user else f"{user}:{user}"
        else:
            name = user.split(":")[0]
            ids = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", img, "-c",
                       f"echo $(id -u {name}):$(id -g {name})"], check=False, quiet=True, timeout=60).stdout.strip()
            owner = ids if ids and ":" in ids and ids[0].isdigit() else "0:0"
        for p in paths:
            run(["chown", "-R", owner, str(p)])
        return owner

    # ---- locating restored content (restic restores with the full original path) ----
    def find_file(self, name: str, parent_name: str | None = None, option: str | None = None) -> Path:
        """First file `name` below the work directory; optionally fixed via options[option]."""
        if option and self.opt.get(option):
            p = self.work / str(self.opt[option]).lstrip("/")
            if not p.exists():
                raise ProbeError(f"{self.opt[option]} missing in the restored backup")
            return p
        hits = [p for p in self.work.rglob(name) if p.is_file() and (parent_name is None or p.parent.name == parent_name)]
        if not hits:
            raise ProbeError(f"{name} missing in the restored backup")
        hits.sort(key=lambda p: len(p.parts))
        return hits[0]

    def find_dir(self, suffix: str, option: str | None = None, required: bool = True) -> Path | None:
        """Directory whose path ends with `suffix` (e.g. 'var/www' or 'grafana/plugins')."""
        if option and self.opt.get(option):
            p = self.work / str(self.opt[option]).lstrip("/")
            if p.is_dir():
                return p
            if required:
                raise ProbeError(f"{self.opt[option]} missing in the restored backup")
            return None
        parts = Path(suffix).parts
        hits = [p for p in self.work.rglob(parts[-1]) if p.is_dir() and p.parts[-len(parts):] == parts]
        if not hits:
            if required:
                raise ProbeError(f"Directory …/{suffix} missing in the restored backup")
            return None
        hits.sort(key=lambda p: len(p.parts))
        return hits[0]


def _n(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return f"{v:g}" if isinstance(v, float) else str(v)
