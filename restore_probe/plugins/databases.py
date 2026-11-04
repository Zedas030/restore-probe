"""Probe modules for databases.

sqlite:   file from the backup, no container. integrity_check + rows per table vs. the production file.
          options: file (path inside the backup), production_file, tables: [name, …] (empty = all tables)
postgres: import a dump (pg_dump -Fc or .sql) into a throw-away PostgreSQL, count rows.
          options: image (e.g. postgres:16), dump (path inside the backup), database, tables: ["schema.table", …],
                   production: {host, port, user, password, database} (optional, for comparison values),
                   min_rows: {table: n} (alternative to production)
mariadb:  import a dump (.sql / .sql.gz) into a throw-away MariaDB, count rows.
          options: image (e.g. mariadb:11), dump, database, tables, production: {...}, min_rows
Passwords for production comparisons belong in the .env and are referenced as ${NAME} in the YAML.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from pathlib import Path

from ..util import Deadline, ProbeError, run
from .base import Plugin

DB_CAPS = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"]


def _caps(caps: list[str]) -> list[str]:
    return [a for c in caps for a in ("--cap-add", c)]


# ------------------------------------------------------------------ SQLite
def sqlite_counts(db: Path, tables: list[str] | None, integrity: bool) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        out: dict = {"tables": {}}
        if integrity:
            out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        if not tables:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            try:
                out["tables"][t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error as e:
                out["tables"][t] = None
                out.setdefault("errors", []).append(f"{t}: {e}")
        return out
    finally:
        con.close()


class SQLitePlugin(Plugin):
    def prepare(self) -> None:
        pf = self.opt.get("production_file")
        if pf:
            if not Path(pf).exists():
                raise ProbeError(f"Production file missing: {pf}")
            self.production = sqlite_counts(Path(pf), self.opt.get("tables"), integrity=False)

    def start(self, deadline: Deadline) -> None:
        pass

    def _file(self) -> Path:
        f = self.opt.get("file")
        if not f:
            raise ProbeError("The sqlite plugin needs options.file")
        p = self.work / f.lstrip("/")
        if not p.exists():
            # restic restores with the full original path; fall back to searching by file name
            return self.find_file(Path(f).name)
        return p

    def stage1(self, deadline: Deadline) -> str:
        p = self._file()
        with open(p, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                raise ProbeError("File is not an SQLite database")
        return f"{p.name}: SQLite header valid, {p.stat().st_size} bytes"

    def stage2(self, deadline: Deadline) -> str:
        try:
            self.restored = sqlite_counts(self._file(), self.opt.get("tables"), integrity=True)
        except sqlite3.DatabaseError as e:
            raise ProbeError(f"Database unreadable: {e}") from e
        if self.restored.get("integrity") != "ok":
            raise ProbeError(f"integrity_check: {self.restored.get('integrity')}")
        return f"integrity_check ok, {len(self.restored['tables'])} tables"

    def stage3(self, deadline: Deadline) -> str:
        ok = True
        for t, n in self.restored["tables"].items():
            exp = (self.production.get("tables") or {}).get(t)
            ok &= self.compare(f"rows_{t}", f"rows in {t}", exp, n)
        self.fail_on_mismatch(ok, "Row counts differ")
        total = sum(v or 0 for v in self.restored["tables"].values())
        return f"{total} rows in {len(self.restored['tables'])} tables"


# ------------------------------------------------------------------ dump helpers
def find_dump(work: Path, spec: str | None, patterns: tuple[str, ...]) -> Path:
    if spec:
        p = work / spec.lstrip("/")
        if not p.exists():
            raise ProbeError(f"Dump missing in the backup: {spec}")
        return p
    for pat in patterns:
        files = sorted(work.rglob(pat), key=lambda f: f.stat().st_mtime)
        if files:
            return files[-1]
    raise ProbeError(f"No dump found ({', '.join(patterns)}); set options.dump")


def maybe_gunzip(p: Path) -> Path:
    if p.suffix == ".gz":
        out = p.with_suffix("")
        with gzip.open(p, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return out
    return p


# ------------------------------------------------------------------ PostgreSQL
class PostgresPlugin(Plugin):
    port = 5432

    def prepare(self) -> None:
        if not self.opt.get("image"):
            raise ProbeError("The postgres plugin needs options.image (e.g. postgres:16)")
        self.db = self.opt.get("database", "app")
        self.tables = self.opt.get("tables", [])
        prod = self.opt.get("production")
        if prod and self.tables:
            env = {"PGPASSWORD": prod.get("password", "")}
            for t in self.tables:
                res = run(["psql", "-h", prod.get("host", "127.0.0.1"), "-p", str(prod.get("port", 5432)),
                           "-U", prod.get("user", "postgres"), "-d", prod.get("database", self.db), "-tAc",
                           f"SELECT COUNT(*) FROM {t}"], env=env, check=False, timeout=120)
                if res.ok:
                    self.production[t] = int(res.stdout.strip() or 0)

    def after_restore(self, info) -> None:
        self.dump = maybe_gunzip(find_dump(self.work, self.opt.get("dump"), ("*.dump", "*.pgdump", "*.sql.gz", "*.sql")))
        run(["chmod", "644", str(self.dump)])

    def start(self, deadline: Deadline) -> None:
        self.container = self.sandbox.run_container(
            self.svc.name, self.opt["image"], mounts=[(self.dump.parent, "/dump", "ro")],
            env={"POSTGRES_PASSWORD": "restore-probe", "POSTGRES_DB": self.db},
            extra_args=_caps(self.opt.get("cap_add", DB_CAPS)))
        self.sandbox.wait_running(self.container, deadline, "PostgreSQL")
        self.ip = self.sandbox.container_ip(self.container)

    def _psql(self, sql: str, deadline: Deadline) -> str:
        res = self.sandbox.exec(self.container, ["psql", "-U", "postgres", "-d", self.db, "-tAc", sql],
                                timeout=max(30, deadline.remaining()))
        return res.stdout.strip()

    def stage1(self, deadline: Deadline) -> str:
        while not deadline.expired():
            r = self.sandbox.exec(self.container, ["pg_isready", "-U", "postgres"], check=False, timeout=15)
            if r.ok:
                return r.stdout.strip()
            time.sleep(2)
        raise ProbeError("pg_isready does not report readiness")

    def stage2(self, deadline: Deadline) -> str:
        name = f"/dump/{self.dump.name}"
        if self.dump.suffix == ".sql":
            cmd = ["psql", "-U", "postgres", "-d", self.db, "-v", "ON_ERROR_STOP=1", "-f", name]
        else:
            cmd = ["pg_restore", "-U", "postgres", "-d", self.db, "--no-owner", "--no-privileges", name]
        res = self.sandbox.exec(self.container, cmd, timeout=max(60, deadline.remaining()), check=False)
        if not res.ok:
            tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
            raise ProbeError("Importing the dump failed: " + " | ".join(tail))
        n = self._psql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')", deadline)
        return f"dump imported, {n} tables"

    def stage3(self, deadline: Deadline) -> str:
        ok = True
        parts = []
        tables = self.tables or [t for t in self._psql(
            "SELECT table_schema||'.'||table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema')", deadline).splitlines() if t]
        for t in tables:
            n = int(self._psql(f"SELECT COUNT(*) FROM {t}", deadline) or 0)
            ok &= self.compare(f"rows_{t}", f"rows in {t}", self.production.get(t, self.opt.get("min_rows", {}).get(t)), n)
            parts.append(f"{t}={n}")
        self.fail_on_mismatch(ok, "Row counts differ")
        return ", ".join(parts)


# ------------------------------------------------------------------ MariaDB / MySQL
class MariaDBPlugin(Plugin):
    port = 3306

    def prepare(self) -> None:
        if not self.opt.get("image"):
            raise ProbeError("The mariadb plugin needs options.image (e.g. mariadb:11)")
        self.db = self.opt.get("database", "app")
        self.tables = self.opt.get("tables", [])
        prod = self.opt.get("production")
        if prod and self.tables:
            for t in self.tables:
                res = run(["mariadb", "-h", prod.get("host", "127.0.0.1"), "-P", str(prod.get("port", 3306)),
                           "-u", prod.get("user", "root"), "-N", "-B", "-e", f"SELECT COUNT(*) FROM {t}",
                           prod.get("database", self.db)],
                          env={"MYSQL_PWD": prod.get("password", "")}, check=False, timeout=120)
                if res.ok:
                    self.production[t] = int(res.stdout.strip() or 0)

    def after_restore(self, info) -> None:
        self.dump = maybe_gunzip(find_dump(self.work, self.opt.get("dump"), ("*.sql.gz", "*.sql")))
        run(["chmod", "644", str(self.dump)])

    def start(self, deadline: Deadline) -> None:
        self.container = self.sandbox.run_container(
            self.svc.name, self.opt["image"], mounts=[(self.dump.parent, "/dump", "ro")],
            env={"MARIADB_ROOT_PASSWORD": "restore-probe", "MARIADB_DATABASE": self.db,
                 "MYSQL_ROOT_PASSWORD": "restore-probe", "MYSQL_DATABASE": self.db},
            extra_args=_caps(self.opt.get("cap_add", DB_CAPS)))
        self.sandbox.wait_running(self.container, deadline, "MariaDB")
        self.ip = self.sandbox.container_ip(self.container)

    def _cli(self) -> str:
        r = self.sandbox.exec(self.container, ["sh", "-c", "command -v mariadb || command -v mysql"], check=False, timeout=15)
        return r.stdout.strip() or "mariadb"

    def _sql(self, sql: str, deadline: Deadline) -> str:
        res = self.sandbox.exec(self.container, [self._cli(), "-uroot", "-prestore-probe", "-N", "-B", "-e", sql, self.db],
                                timeout=max(30, deadline.remaining()))
        return res.stdout.strip()

    def stage1(self, deadline: Deadline) -> str:
        while not deadline.expired():
            r = self.sandbox.exec(self.container, ["sh", "-c",
                                  f"{self._cli()}-admin -uroot -prestore-probe ping 2>/dev/null || mysqladmin -uroot -prestore-probe ping"],
                                  check=False, timeout=15)
            if r.ok and "alive" in r.stdout:
                return "server answers (ping alive)"
            time.sleep(2)
        raise ProbeError("MariaDB does not report readiness")

    def stage2(self, deadline: Deadline) -> str:
        res = self.sandbox.exec(self.container, ["sh", "-c", f"{self._cli()} -uroot -prestore-probe {self.db} < /dump/{self.dump.name}"],
                                timeout=max(60, deadline.remaining()), check=False)
        if not res.ok:
            tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
            raise ProbeError("Importing the dump failed: " + " | ".join(tail))
        n = self._sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()", deadline)
        return f"dump imported, {n} tables"

    def stage3(self, deadline: Deadline) -> str:
        ok = True
        parts = []
        tables = self.tables or [t for t in self._sql("SHOW TABLES", deadline).splitlines() if t]
        for t in tables:
            n = int(self._sql(f"SELECT COUNT(*) FROM `{t}`", deadline) or 0)
            ok &= self.compare(f"rows_{t}", f"rows in {t}", self.production.get(t, self.opt.get("min_rows", {}).get(t)), n)
            parts.append(f"{t}={n}")
        self.fail_on_mismatch(ok, "Row counts differ")
        return ", ".join(parts)
