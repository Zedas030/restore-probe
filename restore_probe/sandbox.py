"""The sandbox: isolated Docker network, throw-away containers, isolation proof.

Security rules (not negotiable):
- Network with --internal: no gateway, no route to the LAN, no internet.
- In addition an iptables rule that drops NEW connections from the sandbox network to the host.
  Otherwise services on the host (e.g. the real MQTT broker) would be reachable via the bridge IP.
  The prober itself still reaches the containers because it opens the connection (ESTABLISHED).
- No published ports, no devices, all capabilities dropped, no privilege escalation.
- Every container carries the label restore-probe=1 so leftovers are found and removed by the next run.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import SandboxConfig
from .util import Deadline, ProbeError, run

log = logging.getLogger("restore_probe.sandbox")

LABEL = "restore-probe=1"
RULE_COMMENT = "restore-probe: sandbox must not reach the host"


@dataclass
class IsolationResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)  # {"target": "192.0.2.1:80", "reachable": False}
    inspect: dict = field(default_factory=dict)


class Sandbox:
    def __init__(self, cfg: SandboxConfig, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.containers: list[str] = []

    # ---------- network ----------
    def ensure_network(self) -> str:
        """Create the internal network (idempotent) and return the host's bridge IP."""
        res = run(["docker", "network", "inspect", self.cfg.network], check=False, quiet=True)
        if res.ok:
            info = json.loads(res.stdout)[0]
            if not info.get("Internal"):
                raise ProbeError(f"Docker network {self.cfg.network} exists but is not 'internal'. "
                                 f"Remove it: docker network rm {self.cfg.network}")
        else:
            run(["docker", "network", "create", "--internal", "--driver", "bridge",
                 "--subnet", self.cfg.subnet,
                 "--opt", f"com.docker.network.bridge.name={self.cfg.bridge_name}",
                 "--opt", "com.docker.network.bridge.enable_ip_masquerade=false",
                 "--label", LABEL, self.cfg.network])
            log.info("Sandbox network %s (%s) created", self.cfg.network, self.cfg.subnet)
        if self.cfg.block_host_access:
            self._ensure_host_block_rule()
        return self.host_bridge_ip()

    def host_bridge_ip(self) -> str:
        info = json.loads(run(["docker", "network", "inspect", self.cfg.network], quiet=True).stdout)[0]
        gw = info["IPAM"]["Config"][0].get("Gateway")
        if not gw:
            gw = str(list(ipaddress.ip_network(self.cfg.subnet).hosts())[0])
        return gw

    def _rule(self) -> list[str]:
        return ["INPUT", "-i", self.cfg.bridge_name, "-m", "conntrack", "--ctstate", "NEW",
                "-m", "comment", "--comment", RULE_COMMENT, "-j", "DROP"]

    def _ensure_host_block_rule(self) -> None:
        rule = self._rule()
        if not run(["iptables", "-C", *rule], check=False, quiet=True).ok:
            run(["iptables", "-I", *rule])
            log.info("iptables rule set: new connections from %s to the host are dropped", self.cfg.bridge_name)
        self._remove_legacy_rules()

    def _remove_legacy_rules(self) -> None:
        """Remove older DROP rules for the same bridge with a different comment (e.g. from earlier versions)."""
        res = run(["iptables", "-S", "INPUT"], check=False, quiet=True)
        for line in res.stdout.splitlines():
            if f"-i {self.cfg.bridge_name}" in line and "-j DROP" in line and RULE_COMMENT not in line:
                spec = line.replace("-A INPUT", "INPUT", 1)
                run(["sh", "-c", f"iptables -D {spec}"], check=False, quiet=True)

    def host_block_rule_present(self) -> bool:
        return run(["iptables", "-C", *self._rule()], check=False, quiet=True).ok

    @staticmethod
    def memory_limits_supported() -> bool:
        """Can Docker enforce --memory? Raspberry Pi kernels ship with the memory cgroup disabled by default."""
        try:
            return "memory" in Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
        except OSError:
            info = run(["docker", "info"], check=False, quiet=True)
            return "No memory limit support" not in (info.stdout + info.stderr)

    def timezone(self) -> str:
        """Timezone for the containers: sandbox.timezone, otherwise the host's (/etc/timezone), otherwise UTC."""
        if self.cfg.timezone:
            return self.cfg.timezone
        try:
            return Path("/etc/timezone").read_text().strip() or "UTC"
        except OSError:
            return "UTC"

    # ---------- containers ----------
    @staticmethod
    def ensure_image(image: str, timeout: float = 1800) -> None:
        """Pull the image up front: an implicit pull inside 'docker run' can take many minutes on small hosts."""
        if run(["docker", "image", "inspect", image], check=False, quiet=True).ok:
            return
        if image.startswith("sha256:"):
            raise ProbeError(f"Image {image[:19]} is not present locally any more (production container updated?)")
        log.info("Pulling image %s …", image)
        run(["docker", "pull", "-q", image], timeout=timeout)

    def run_container(self, name: str, image: str, *, mounts: list[tuple[Path, str, str]] = (),
                      env: dict[str, str] | None = None, cmd: list[str] | None = None,
                      user: str | None = None, entrypoint: str | None = None,
                      extra_args: list[str] | None = None) -> str:
        """Start a throw-away container in the sandbox network. mounts: (host path, target, 'ro'|'rw')."""
        cname = f"rp-{name}-{self.run_id}"
        self.remove_container(cname)
        self.ensure_image(image)
        args = ["docker", "run", "-d", "--name", cname, "--hostname", f"rp-sandbox-{name}",
                "--label", LABEL, "--label", f"restore-probe.run={self.run_id}",
                "--network", self.cfg.network,
                "--memory", self.cfg.memory, "--memory-swap", self.cfg.memory,
                "--cpus", self.cfg.cpus, "--pids-limit", str(self.cfg.pids_limit),
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--restart", "no",
                "--dns", "0.0.0.0",  # DNS into nothing: no resolver reachable
                "--add-host", f"rp-sandbox-{name}:127.0.1.1",
                "-e", f"TZ={self.timezone()}"]
        for host, target, mode in mounts:
            args += ["-v", f"{host}:{target}:{mode}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        if user:
            args += ["--user", user]
        if entrypoint is not None:
            args += ["--entrypoint", entrypoint]
        if extra_args:
            args += extra_args
        args.append(image)
        if cmd:
            args += cmd
        self.containers.append(cname)  # register first so a failed start is cleaned up as well
        try:
            run(args, timeout=120)
        except ProbeError:
            self.remove_container(cname)
            raise
        return cname

    def container_ip(self, cname: str) -> str:
        fmt = "{{(index .NetworkSettings.Networks \"" + self.cfg.network + "\").IPAddress}}"
        ip = run(["docker", "inspect", "-f", fmt, cname], quiet=True).stdout.strip()
        if not ip:
            raise ProbeError(f"Container {cname} has no IP in the sandbox network")
        return ip

    def container_running(self, cname: str) -> bool:
        res = run(["docker", "inspect", "-f", "{{.State.Running}}", cname], check=False, quiet=True)
        return res.ok and res.stdout.strip() == "true"

    def exec(self, cname: str, cmd: list[str], *, timeout: float = 300, check: bool = True, user: str | None = None):
        args = ["docker", "exec"]
        if user:
            args += ["--user", user]
        return run([*args, cname, *cmd], timeout=timeout, check=check)

    def logs(self, cname: str, tail: int = 60) -> str:
        res = run(["docker", "logs", "--tail", str(tail), cname], check=False, quiet=True)
        return (res.stdout + res.stderr).strip()

    def wait_running(self, cname: str, deadline: Deadline, what: str) -> None:
        while not deadline.expired():
            if self.container_running(cname):
                return
            state = run(["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", cname],
                        check=False, quiet=True).stdout.strip()
            if state.startswith("exited"):
                raise ProbeError(f"{what}: container exited immediately ({state}). Log: {self.logs(cname, 15)}")
            time.sleep(1)
        raise ProbeError(f"{what}: container does not start")

    def wait_port(self, ip: str, port: int, deadline: Deadline, what: str) -> None:
        last = ""
        while not deadline.expired():
            try:
                with socket.create_connection((ip, port), timeout=3):
                    return
            except OSError as e:
                last = str(e)
            time.sleep(2)
        raise ProbeError(f"{what}: port {port} on {ip} does not answer ({last})")

    def remove_container(self, cname: str) -> None:
        run(["docker", "rm", "-f", "-v", cname], check=False, quiet=True)
        if cname in self.containers:
            self.containers.remove(cname)

    def cleanup(self) -> None:
        for c in list(self.containers):
            self.remove_container(c)

    @staticmethod
    def cleanup_stale() -> list[str]:
        """Remove containers of earlier runs (e.g. after a power loss in the middle of a run)."""
        res = run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], check=False, quiet=True)
        ids = res.stdout.split()
        for cid in ids:
            run(["docker", "rm", "-f", "-v", cid], check=False, quiet=True)
        return ids

    # ---------- proof ----------
    def inspect_security(self, cname: str) -> dict:
        """Read the security-relevant fields from docker inspect for the report."""
        info = json.loads(run(["docker", "inspect", cname], quiet=True).stdout)[0]
        hc = info["HostConfig"]
        nets = info["NetworkSettings"]["Networks"]
        return {
            "networks": list(nets.keys()),
            "internal_network": self.cfg.network in nets,
            "published_ports": hc.get("PortBindings") or {},
            "devices": hc.get("Devices") or [],
            "cap_drop": hc.get("CapDrop") or [],
            "cap_add": hc.get("CapAdd") or [],
            "privileged": hc.get("Privileged", False),
            "security_opt": hc.get("SecurityOpt") or [],
            "memory": hc.get("Memory"),
            "nano_cpus": hc.get("NanoCpus"),
            "hostname": info["Config"].get("Hostname"),
            "network_mode": hc.get("NetworkMode"),
        }

    def verify_isolation(self, targets: list[str], deadline: Deadline) -> IsolationResult:
        """Start a tiny container in the sandbox network and prove that it reaches NOTHING real.

        Every target (host:port) must be unreachable. DNS must fail and there must be no default route.
        If a single connection succeeds, the isolation counts as broken and no service container is started.
        """
        script_lines = ["set +e"]
        for t in targets:
            host, port = t.rsplit(":", 1)
            script_lines.append(f"nc -z -w 3 {host} {port} >/dev/null 2>&1 && echo \"REACH {t}\" || echo \"BLOCK {t}\"")
        script_lines.append("nslookup example.com >/dev/null 2>&1 && echo 'REACH dns' || echo 'BLOCK dns'")
        script_lines.append("ip route 2>/dev/null | grep -q default && echo 'REACH defaultroute' || echo 'BLOCK defaultroute'")
        cname = self.run_container("isolation", self.cfg.isolation_image, entrypoint="/bin/sh",
                                   cmd=["-c", "\n".join(script_lines)])
        try:
            while not deadline.expired():
                st = run(["docker", "inspect", "-f", "{{.State.Status}}", cname], quiet=True).stdout.strip()
                if st == "exited":
                    break
                time.sleep(1)
            out = self.logs(cname, 200)
            inspect = self.inspect_security(cname)
        finally:
            self.remove_container(cname)
        checks = []
        passed = True
        for line in out.splitlines():
            if line.startswith(("REACH ", "BLOCK ")):
                state, target = line.split(" ", 1)
                reachable = state == "REACH"
                checks.append({"target": target, "reachable": reachable})
                if reachable:
                    passed = False
        if not checks:
            raise ProbeError(f"Isolation test produced no results. Log: {out[:300]}")
        if inspect["published_ports"] or inspect["devices"] or inspect["privileged"] or not inspect["internal_network"]:
            passed = False
        return IsolationResult(passed=passed, checks=checks, inspect=inspect)


def default_isolation_targets(host_bridge_ip: str, configured: list[str]) -> list[str]:
    """Replace placeholders: @host_bridge → host's bridge IP, @gateway → host's default gateway."""
    out = []
    gw = _default_gateway()
    for t in configured:
        t = t.replace("@host_bridge", host_bridge_ip)
        if "@gateway" in t:
            if not gw:
                continue
            t = t.replace("@gateway", gw)
        out.append(t)
    # Always test the host via the bridge as well (MQTT, Home Assistant, SSH are typical candidates)
    for p in ("1883", "8123", "22"):
        t = f"{host_bridge_ip}:{p}"
        if t not in out:
            out.append(t)
    return out


def _default_gateway() -> str | None:
    res = run(["ip", "-4", "route", "show", "default"], check=False, quiet=True)
    parts = res.stdout.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    return None
