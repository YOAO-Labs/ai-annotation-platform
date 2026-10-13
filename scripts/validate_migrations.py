"""Real disposable-database migration validation (plan section 7.2).

The old CI round trip was ``upgrade head`` -> ``alembic stamp <floor>`` ->
``downgrade base`` -> ``upgrade head``.  ``stamp`` only rewrites the version
marker; it does not execute the skipped schema/data rollback, so the irreversible
segment was never validated.

This runner performs the four checks the plan asks for, each on its own freshly
created, ownership-tagged database derived from the current disposable target:

1. ``fresh``     - a brand new database upgraded straight to head.
2. ``reversible``- ``upgrade <floor>`` (a real schema, not a stamp), then a real
                   ``downgrade base`` and ``upgrade head``.  ``<floor>`` is the
                   revision below the unique irreversible revision, so the
                   irreversible ``downgrade()`` is never executed.
3. ``forward``   - seed authentic legacy rows at ``<floor>``, snapshot them, run
                   the irreversible ``upgrade`` and assert the forward data
                   semantics (converted roles, preserved history, new server
                   default, no invented memberships).  It also proves the
                   irreversible ``downgrade`` refuses.
4. ``restore``   - restore the pre-conversion ``pg_dump`` snapshot into an
                   independent database and assert the authentic pre-conversion
                   state is recoverable.

Safety: the runner never touches the base database's schema.  It creates only
``<base>__mv_<phase>`` databases, tags each with the run owner, refuses to adopt
or drop a database without a matching owner comment, refuses non-local or
``prod``/``annotation`` targets, and drops every database and dump it created in
a ``finally`` block (proven by ``--fail-after``).

Run it through the owned-worktree launcher, e.g.::

    pnpm dev:worktree -- init --mode test
    pnpm dev:worktree -- exec --mode test -- \
        uv run python ../../scripts/validate_migrations.py

CI (single disposable service database, no worktree launcher) must opt in with a
unique owner, e.g. ``AAP_MIGRATION_VALIDATION_OWNER="ci:$GITHUB_RUN_ID"``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO = Path(__file__).resolve().parent.parent
API = REPO / "apps/api"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
OWNED_DB_RE = re.compile(r"^[a-z0-9_]{1,63}$")
SENTINEL_TABLE_COLUMNS = {
    "project_members": {"version", "updated_at"},
    "user_invitations": {"project_role"},
    "tasks": {
        "annotation_contributor_ids",
        "review_contributor_ids",
        "review_submitter_id",
    },
}
IRREVERSIBLE_DEFAULT_FLOOR = "annotator"
HEAD_DEFAULT = "employee"

#: Set by ``--fail-after create`` to inject a failure between CREATE and COMMENT.
_INJECT_CREATE_FAILURE = False


class ValidationError(RuntimeError):
    """Refuse the run rather than touch an unverified database."""


@dataclass(frozen=True)
class Anchor:
    url: URL
    base_database: str
    owner: str
    mode: str | None
    dump_dir: Path
    manifest_postgres: tuple[str, int] | None = None


@dataclass
class RunState:
    created: list[str] = field(default_factory=list)
    dumps: list[Path] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Ownership guards
# --------------------------------------------------------------------------- #
def resolve_anchor(environ: dict | None = None) -> Anchor:
    environment = dict(os.environ if environ is None else environ)
    raw = environment.get("MIGRATION_DATABASE_URL") or environment.get("DATABASE_URL")
    if not raw:
        raise ValidationError(
            "MIGRATION_DATABASE_URL/DATABASE_URL is required; run through "
            "'pnpm dev:worktree -- exec --mode test' or export a disposable target"
        )
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise ValidationError(
            f"postgresql driver required, got {url.get_backend_name()!r}"
        )
    if url.host not in LOCAL_HOSTS:
        raise ValidationError("only a local PostgreSQL endpoint is allowed")
    if url.query:
        raise ValidationError("connection string must not carry query options")
    database = url.database or ""
    if not OWNED_DB_RE.match(database):
        raise ValidationError(f"unexpected database name {database!r}")

    mode = environment.get("AAP_WORKTREE_MODE")
    if mode:
        if mode not in {"test", "e2e"}:
            raise ValidationError(
                "worktree migration validation only runs in test/e2e mode, never dev"
            )
        state = REPO / ".worktree" / mode / "resources.json"
        if not state.exists():
            raise ValidationError(
                f"missing {state}; run 'pnpm dev:worktree -- init --mode {mode}' first"
            )
        document = json.loads(state.read_text())
        resources = document["resources"]
        if database != resources["database"]:
            raise ValidationError(
                "connection string database does not match the owned mode database"
            )
        owner = resources["owner"]
        if not owner.startswith("aap-worktree:"):
            raise ValidationError("worktree owner tag is malformed")
        postgres = document.get("postgres") or {}
        endpoint = (
            (str(postgres.get("host")), int(postgres.get("port", 5432)))
            if postgres.get("host")
            else None
        )
        dump_dir = Path(resources["directory"]) / "tmp" / "migration-validation"
    else:
        owner = environment.get("AAP_MIGRATION_VALIDATION_OWNER", "")
        if not owner:
            raise ValidationError(
                "set AAP_MIGRATION_VALIDATION_OWNER (unique per run) when not "
                "running inside an owned worktree"
            )
        if "prod" in database.lower() or database == "annotation":
            raise ValidationError(f"refusing shared/production target {database!r}")
        if not (database.endswith("_test") or database.endswith("_e2e")):
            raise ValidationError(
                f"disposable target database must end with _test/_e2e, got {database!r}"
            )
        endpoint = None
        dump_dir = Path(environment.get("TMPDIR", "/tmp")) / "aap-migration-validation"
    return Anchor(
        url=url,
        base_database=database,
        owner=owner,
        mode=mode,
        dump_dir=dump_dir,
        manifest_postgres=endpoint,
    )


def derived_database(anchor: Anchor, phase: str) -> str:
    name = f"{anchor.base_database}__mv_{phase}"
    if len(name) > 63 or not OWNED_DB_RE.match(name):
        raise ValidationError(f"derived database name is invalid: {name!r}")
    return name


async def verify_anchor(anchor: Anchor) -> None:
    """Prove the base target is the manifest-owned, connectable database."""
    exists, owner, _ = await database_state(anchor, anchor.base_database)
    if not exists:
        raise ValidationError(f"base database {anchor.base_database!r} does not exist")
    if anchor.mode:
        if owner != anchor.owner:
            raise ValidationError(
                f"base database owner marker {owner!r} does not match the manifest owner"
            )
        if anchor.manifest_postgres:
            actual = (anchor.url.host or "", anchor.url.port or 5432)
            if actual != anchor.manifest_postgres:
                raise ValidationError(
                    f"endpoint {actual} does not match the worktree manifest "
                    f"{anchor.manifest_postgres}"
                )
    current = await fetch_scalar(
        anchor, anchor.base_database, "SELECT current_database()"
    )
    if current != anchor.base_database:
        raise ValidationError("base database connection resolved elsewhere")


# --------------------------------------------------------------------------- #
# Owned database lifecycle
# --------------------------------------------------------------------------- #
def _admin_engine(anchor: Anchor):
    return create_async_engine(
        anchor.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 10, "command_timeout": 60},
    )


async def database_state(anchor: Anchor, name: str) -> tuple[bool, str | None, int]:
    """Return (exists, owner_comment, sessions) — existence is explicit."""
    engine = _admin_engine(anchor)
    try:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT shobj_description(oid, 'pg_database') AS owner, "
                            "(SELECT count(*) FROM pg_stat_activity WHERE datname = :name) AS sessions "
                            "FROM pg_database WHERE datname = :name"
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return (False, None, 0)
            return (True, row["owner"], int(row["sessions"]))
    finally:
        await engine.dispose()


def create_action(exists: bool, owner: str | None, expected: str) -> str:
    """``create`` a fresh DB, ``recreate`` our own leftover, else ``refuse``.

    An existing database without our ownership comment is an unknown-owner
    collision and is always refused, never adopted or dropped.
    """
    if not exists:
        return "create"
    if owner != expected:
        return "refuse"
    return "recreate"


def drop_action(
    exists: bool, owner: str | None, expected: str, *, created_by_run: bool
) -> str:
    if not exists:
        return "absent"
    if owner == expected:
        return "drop"
    if owner is None and created_by_run:
        # Allocated by this run but failed before the COMMENT was written.
        return "drop"
    return "refuse"


async def _drop_database(anchor: Anchor, name: str) -> None:
    engine = _admin_engine(anchor)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{name}"')
    finally:
        await engine.dispose()
    exists, _, _ = await database_state(anchor, name)
    if exists:
        raise ValidationError(f"database {name} drop did not take effect")


async def create_owned_database(anchor: Anchor, state: RunState, name: str) -> None:
    exists, owner, _ = await database_state(anchor, name)
    action = create_action(exists, owner, anchor.owner)
    if action == "refuse":
        raise ValidationError(f"refusing to adopt database {name} owned by {owner!r}")
    engine = _admin_engine(anchor)
    try:
        async with engine.connect() as connection:
            if action == "recreate":
                await connection.exec_driver_sql(f'DROP DATABASE "{name}"')
            await connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            # Register inside the live connection block, before the fallible
            # connection close / engine disposal: a dispose, COMMENT or verify
            # failure must still clean up the freshly created database.
            state.created.append(name)
    finally:
        await engine.dispose()
    if _INJECT_CREATE_FAILURE and name.endswith("__mv_fresh"):
        raise ValidationError("injected allocation failure after CREATE")
    engine = _admin_engine(anchor)
    try:
        async with engine.connect() as connection:
            escaped = anchor.owner.replace("'", "''")
            await connection.exec_driver_sql(
                f"COMMENT ON DATABASE \"{name}\" IS '{escaped}'"
            )
    finally:
        await engine.dispose()
    exists, verified, _ = await database_state(anchor, name)
    if not exists or verified != anchor.owner:
        raise ValidationError(f"database {name} ownership could not be verified")


async def drop_created_database(anchor: Anchor, name: str) -> None:
    exists, owner, sessions = await database_state(anchor, name)
    action = drop_action(exists, owner, anchor.owner, created_by_run=True)
    if action == "absent":
        return
    if action == "refuse":
        raise ValidationError(f"refusing to drop database {name} owned by {owner!r}")
    if sessions:
        raise ValidationError(
            f"database {name} still has {sessions} sessions; not forcing"
        )
    await _drop_database(anchor, name)


# --------------------------------------------------------------------------- #
# SQL / Alembic helpers
# --------------------------------------------------------------------------- #
async def fetch_scalar(
    anchor: Anchor, database: str, sql: str, params: dict | None = None
):
    engine = create_async_engine(
        anchor.url.set(database=database),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 10, "command_timeout": 120},
    )
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(sql), params or {})
    finally:
        await engine.dispose()


async def fetch_all(
    anchor: Anchor, database: str, sql: str, params: dict | None = None
):
    engine = create_async_engine(
        anchor.url.set(database=database), poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql), params or {})).mappings().all()
    finally:
        await engine.dispose()


async def execute_statements(
    anchor: Anchor, database: str, statements: list[tuple[str, dict]]
) -> None:
    engine = create_async_engine(
        anchor.url.set(database=database), poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.begin() as connection:
            for sql, params in statements:
                await connection.execute(text(sql), params)
    finally:
        await engine.dispose()


def run_alembic(
    anchor: Anchor, database: str, *arguments: str, check: bool = True
) -> str:
    target = anchor.url.set(database=database).render_as_string(hide_password=False)
    environment = dict(os.environ)
    environment["MIGRATION_DATABASE_URL"] = target
    environment["DATABASE_URL"] = target
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if check and result.returncode:
        raise ValidationError(
            f"alembic {' '.join(arguments)} failed (rc={result.returncode}) on {database}"
        )
    return result.stdout + result.stderr


def graph_policy():
    sys.path.insert(0, str(REPO / "scripts"))
    from alembic_reversible_floor import build_config, classify_chain, load_revisions

    return classify_chain(*load_revisions(build_config()))


async def current_revision(anchor: Anchor, database: str) -> list[str]:
    exists = await fetch_scalar(
        anchor, database, "SELECT to_regclass('public.alembic_version')"
    )
    if not exists:
        return []
    rows = await fetch_all(anchor, database, "SELECT version_num FROM alembic_version")
    return [row["version_num"] for row in rows]


async def column_default(
    anchor: Anchor, database: str, table: str, column: str
) -> str | None:
    return await fetch_scalar(
        anchor,
        database,
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column",
        {"table": table, "column": column},
    )


async def assert_sentinel_columns(anchor: Anchor, database: str) -> None:
    for table, columns in SENTINEL_TABLE_COLUMNS.items():
        rows = await fetch_all(
            anchor,
            database,
            "SELECT column_name FROM information_schema.columns WHERE table_name = :table",
            {"table": table},
        )
        present = {row["column_name"] for row in rows}
        if not columns.issubset(present):
            raise ValidationError(
                f"{database}: {table} missing columns {sorted(columns - present)}"
            )


# --------------------------------------------------------------------------- #
# pg_dump / pg_restore backends (host client when present, else the container)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DumpBackend:
    kind: str  # "host" | "docker"
    container: str | None = None


def docker(*arguments: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *arguments], input=stdin, capture_output=True, timeout=300
    )


def find_postgres_container(anchor: Anchor) -> str:
    result = docker("ps", "--format", "{{.Names}}\t{{.Ports}}")
    if result.returncode:
        raise ValidationError(
            "docker is unavailable; cannot locate the Postgres server"
        )
    port = str(anchor.url.port or 5432)
    for line in result.stdout.decode().splitlines():
        name, _, ports = line.partition("\t")
        for mapping in ports.split(","):
            mapping = mapping.strip()
            if "->" not in mapping:
                continue
            published, _, target = mapping.partition("->")
            if target.startswith("5432/") and published.endswith(f":{port}"):
                return name
    raise ValidationError(f"no local container publishes PostgreSQL port {port}")


def detect_dump_backend(anchor: Anchor) -> DumpBackend:
    if shutil.which("pg_dump") and shutil.which("pg_restore"):
        return DumpBackend("host")
    return DumpBackend("docker", find_postgres_container(anchor))


def _host_pg_env(anchor: Anchor) -> dict:
    environment = dict(os.environ)
    if anchor.url.password:
        environment["PGPASSWORD"] = anchor.url.password
    return environment


def _host_pg_common(anchor: Anchor) -> list[str]:
    return [
        "-h",
        anchor.url.host or "127.0.0.1",
        "-p",
        str(anchor.url.port or 5432),
        "-U",
        anchor.url.username or "postgres",
    ]


def pg_dump_database(
    backend: DumpBackend, anchor: Anchor, database: str, destination: Path
) -> None:
    if backend.kind == "host":
        result = subprocess.run(
            [
                "pg_dump",
                *_host_pg_common(anchor),
                "--no-owner",
                "--no-privileges",
                "-Fc",
                database,
            ],
            capture_output=True,
            env=_host_pg_env(anchor),
            timeout=600,
        )
    else:
        result = docker(
            "exec",
            "-i",
            backend.container or "",
            "pg_dump",
            "-U",
            anchor.url.username or "postgres",
            "--no-owner",
            "--no-privileges",
            "-Fc",
            database,
        )
    if result.returncode or not result.stdout:
        raise ValidationError(f"pg_dump failed for {database} (rc={result.returncode})")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(result.stdout)


def pg_restore_database(
    backend: DumpBackend, anchor: Anchor, database: str, source: Path
) -> None:
    payload = source.read_bytes()
    if backend.kind == "host":
        result = subprocess.run(
            [
                "pg_restore",
                *_host_pg_common(anchor),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--dbname",
                database,
            ],
            input=payload,
            capture_output=True,
            env=_host_pg_env(anchor),
            timeout=600,
        )
    else:
        result = docker(
            "exec",
            "-i",
            backend.container or "",
            "pg_restore",
            "-U",
            anchor.url.username or "postgres",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--dbname",
            database,
            stdin=payload,
        )
    if result.returncode:
        raise ValidationError(
            f"pg_restore failed for {database} (rc={result.returncode})"
        )


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
async def phase_fresh(anchor: Anchor, state: RunState) -> None:
    database = derived_database(anchor, "fresh")
    policy = graph_policy()
    await create_owned_database(anchor, state, database)
    upgrade_output = run_alembic(anchor, database, "upgrade", "head")
    upgrades = sum(
        1 for line in upgrade_output.splitlines() if "Running upgrade" in line
    )
    current = await current_revision(anchor, database)
    if current != [policy.head]:
        raise ValidationError(f"fresh database not at head: {current}")
    await assert_sentinel_columns(anchor, database)
    default = await column_default(anchor, database, "users", "role")
    if default is None or HEAD_DEFAULT not in default:
        raise ValidationError(
            f"users.role default is not {HEAD_DEFAULT!r}: {default!r}"
        )
    state.phases.append(f"fresh:{database}@{policy.head}(upgrades={upgrades})")


async def phase_reversible(anchor: Anchor, state: RunState) -> None:
    database = derived_database(anchor, "reversible")
    policy = graph_policy()
    floor = policy.reversible_floor
    if floor == policy.head:
        raise ValidationError("no irreversible revision present; nothing to validate")
    await create_owned_database(anchor, state, database)
    run_alembic(anchor, database, "upgrade", floor)
    if await current_revision(anchor, database) != [floor]:
        raise ValidationError(f"reversible start is not at floor {floor}")
    floor_default = await column_default(anchor, database, "users", "role")
    if floor_default is None or IRREVERSIBLE_DEFAULT_FLOOR not in floor_default:
        raise ValidationError(
            f"pre-conversion users.role default changed unexpectedly: {floor_default!r}"
        )
    await assert_sentinel_columns(anchor, database)
    downgrade_output = run_alembic(anchor, database, "downgrade", "base")
    downgrades = sum(
        1 for line in downgrade_output.splitlines() if "Running downgrade" in line
    )
    if downgrades == 0:
        raise ValidationError("downgrade base executed no real downgrade step")
    if await current_revision(anchor, database):
        raise ValidationError("downgrade base left an alembic_version row")
    run_alembic(anchor, database, "upgrade", "head")
    if await current_revision(anchor, database) != [policy.head]:
        raise ValidationError("re-upgrade did not reach head")
    await assert_sentinel_columns(anchor, database)
    state.phases.append(
        f"reversible:{database}@{floor}->base({downgrades} downgrades)->{policy.head}"
    )


async def seed_legacy_rows(anchor: Anchor, database: str) -> dict:
    now = datetime.now(timezone.utc)
    ids = {
        "owner": uuid.uuid4(),
        "anno": uuid.uuid4(),
        "rev": uuid.uuid4(),
        "project": uuid.uuid4(),
        "member": uuid.uuid4(),
        "pending": uuid.uuid4(),
        "populated": uuid.uuid4(),
        "accepted": uuid.uuid4(),
    }
    statements: list[tuple[str, dict]] = [
        (
            "INSERT INTO users (id, email, name, password_hash, role, is_active) "
            "VALUES (:id, :email, :name, 'x', :role, :active)",
            {
                "id": ids["owner"],
                "email": "mv-owner@test.local",
                "name": "owner",
                "role": "project_admin",
                "active": True,
            },
        ),
        (
            "INSERT INTO users (id, email, name, password_hash, role, is_active) "
            "VALUES (:id, :email, :name, 'x', :role, :active)",
            {
                "id": ids["anno"],
                "email": "mv-anno@test.local",
                "name": "anno",
                "role": "annotator",
                "active": True,
            },
        ),
        (
            "INSERT INTO users (id, email, name, password_hash, role, is_active) "
            "VALUES (:id, :email, :name, 'x', :role, :active)",
            {
                "id": ids["rev"],
                "email": "mv-reviewer@test.local",
                "name": "reviewer",
                "role": "reviewer",
                "active": False,
            },
        ),
        (
            "INSERT INTO projects (id, display_id, name, type_label, type_key, owner_id) "
            "VALUES (:id, :display_id, :name, 'detection', 'image-det', :owner)",
            {
                "id": ids["project"],
                "display_id": f"MV-{ids['project'].hex[:8]}",
                "name": "mv project",
                "owner": ids["owner"],
            },
        ),
        (
            "INSERT INTO project_members (id, project_id, user_id, role) "
            "VALUES (:id, :project, :user, 'annotator')",
            {"id": ids["member"], "project": ids["project"], "user": ids["anno"]},
        ),
        (
            "INSERT INTO user_invitations (id, email, role, token, expires_at, invited_by, project_id) "
            "VALUES (:id, :email, :role, :token, :expires, :invited_by, :project)",
            {
                "id": ids["pending"],
                "email": "mv-pending@test.local",
                "role": "annotator",
                "token": uuid.uuid4().hex,
                "expires": now + timedelta(days=3),
                "invited_by": ids["owner"],
                "project": ids["project"],
            },
        ),
        (
            "INSERT INTO user_invitations (id, email, role, token, expires_at, invited_by, project_id, project_role) "
            "VALUES (:id, :email, :role, :token, :expires, :invited_by, :project, 'annotator')",
            {
                "id": ids["populated"],
                "email": "mv-populated@test.local",
                "role": "reviewer",
                "token": uuid.uuid4().hex,
                "expires": now + timedelta(days=3),
                "invited_by": ids["owner"],
                "project": ids["project"],
            },
        ),
        (
            "INSERT INTO user_invitations (id, email, role, token, expires_at, invited_by, project_id, accepted_at) "
            "VALUES (:id, :email, :role, :token, :expires, :invited_by, :project, :accepted)",
            {
                "id": ids["accepted"],
                "email": "mv-accepted@test.local",
                "role": "annotator",
                "token": uuid.uuid4().hex,
                "expires": now + timedelta(days=3),
                "invited_by": ids["owner"],
                "project": ids["project"],
                "accepted": now,
            },
        ),
    ]
    await execute_statements(anchor, database, statements)
    return ids


async def assert_forward_conversion(anchor: Anchor, database: str, ids: dict) -> None:
    for key in ("anno", "rev"):
        role = await fetch_scalar(
            anchor, database, "SELECT role FROM users WHERE id = :id", {"id": ids[key]}
        )
        if role != HEAD_DEFAULT:
            raise ValidationError(
                f"user {key} was not converted to {HEAD_DEFAULT}: {role!r}"
            )
    active = await fetch_scalar(
        anchor,
        database,
        "SELECT is_active FROM users WHERE id = :id",
        {"id": ids["rev"]},
    )
    if active is not False:
        raise ValidationError("inactive account was reactivated")
    memberships = await fetch_scalar(
        anchor,
        database,
        "SELECT count(*) FROM project_members WHERE user_id = :id",
        {"id": ids["rev"]},
    )
    if memberships:
        raise ValidationError("conversion invented a project membership")
    preserved = await fetch_scalar(
        anchor,
        database,
        "SELECT role FROM project_members WHERE id = :id",
        {"id": ids["member"]},
    )
    if preserved != "annotator":
        raise ValidationError("existing membership role was rewritten")

    pending = (
        await fetch_all(
            anchor,
            database,
            "SELECT role, project_role FROM user_invitations WHERE id = :id",
            {"id": ids["pending"]},
        )
    )[0]
    if pending["project_role"] != "annotator" or pending["role"] != HEAD_DEFAULT:
        raise ValidationError(f"pending invitation not normalised: {dict(pending)}")
    populated = (
        await fetch_all(
            anchor,
            database,
            "SELECT role, project_role FROM user_invitations WHERE id = :id",
            {"id": ids["populated"]},
        )
    )[0]
    if populated["project_role"] != "annotator" or populated["role"] != HEAD_DEFAULT:
        raise ValidationError(
            f"populated project_role not preserved: {dict(populated)}"
        )
    accepted = (
        await fetch_all(
            anchor,
            database,
            "SELECT role, project_role FROM user_invitations WHERE id = :id",
            {"id": ids["accepted"]},
        )
    )[0]
    if accepted["role"] != "annotator" or accepted["project_role"] is not None:
        raise ValidationError(f"historical invitation was rewritten: {dict(accepted)}")

    default = await column_default(anchor, database, "users", "role")
    if default is None or HEAD_DEFAULT not in default:
        raise ValidationError(f"head users.role default is not {HEAD_DEFAULT!r}")


async def phase_forward(anchor: Anchor, state: RunState, backend: DumpBackend) -> Path:
    database = derived_database(anchor, "forward")
    policy = graph_policy()
    floor = policy.reversible_floor
    await create_owned_database(anchor, state, database)
    run_alembic(anchor, database, "upgrade", floor)
    ids = await seed_legacy_rows(anchor, database)
    snapshot = anchor.dump_dir / f"{database}__pre_conversion.dump"
    state.dumps.append(snapshot)
    pg_dump_database(backend, anchor, database, snapshot)
    run_alembic(anchor, database, "upgrade", "head")
    await assert_forward_conversion(anchor, database, ids)
    refusal = run_alembic(anchor, database, "downgrade", floor, check=False)
    if "not reversible" not in refusal.lower():
        raise ValidationError(
            "irreversible downgrade did not refuse with the documented error"
        )
    if await current_revision(anchor, database) != [policy.head]:
        raise ValidationError("refused downgrade mutated the version marker")
    state.phases.append(f"forward:{database}@base->{floor}->{policy.head}")
    return snapshot


def check_restored_history(rows: list) -> None:
    """Require exactly one untouched pre-conversion historical invitation row."""
    if len(rows) != 1:
        raise ValidationError(
            "restored snapshot historical invitation count is "
            f"{len(rows)}, expected exactly 1"
        )
    row = rows[0]
    if row["role"] != "annotator" or row["project_role"] is not None:
        raise ValidationError(f"restored historical invitation changed: {dict(row)}")


async def phase_restore(
    anchor: Anchor, state: RunState, snapshot: Path, backend: DumpBackend
) -> None:
    database = derived_database(anchor, "restore")
    policy = graph_policy()
    floor = policy.reversible_floor
    await create_owned_database(anchor, state, database)
    pg_restore_database(backend, anchor, database, snapshot)
    if await current_revision(anchor, database) != [floor]:
        raise ValidationError("restored snapshot is not at the pre-conversion floor")
    await assert_sentinel_columns(anchor, database)
    default = await column_default(anchor, database, "users", "role")
    if default is None or IRREVERSIBLE_DEFAULT_FLOOR not in default:
        raise ValidationError(
            f"restored snapshot lost the pre-conversion role default: {default!r}"
        )
    legacy = await fetch_all(
        anchor,
        database,
        "SELECT email, role FROM users WHERE email LIKE 'mv-%@test.local' ORDER BY email",
    )
    roles = sorted(row["role"] for row in legacy)
    if roles != ["annotator", "project_admin", "reviewer"]:
        raise ValidationError(f"restored snapshot lost legacy roles: {roles}")
    membership = await fetch_scalar(
        anchor,
        database,
        "SELECT pm.role FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE u.email = 'mv-anno@test.local'",
    )
    if membership != "annotator":
        raise ValidationError(f"restored project membership changed: {membership!r}")
    pre_conversion_project_role = await fetch_scalar(
        anchor,
        database,
        "SELECT project_role FROM user_invitations WHERE email = 'mv-pending@test.local'",
    )
    if pre_conversion_project_role is not None:
        raise ValidationError(
            "restored snapshot already contains the converted project_role"
        )
    accepted = await fetch_all(
        anchor,
        database,
        "SELECT role, project_role FROM user_invitations WHERE email = 'mv-accepted@test.local'",
    )
    check_restored_history(list(accepted))
    state.phases.append(f"restore:{database}@{floor}")


def cleanup(anchor: Anchor, state: RunState) -> list[str]:
    """Best-effort cleanup of everything this run created, aggregating errors."""
    errors: list[str] = []
    for database in reversed(state.created):
        try:
            asyncio.run(drop_created_database(anchor, database))
        except Exception as error:  # noqa: BLE001 - keep cleaning the rest
            errors.append(f"{database}: {type(error).__name__}: {error}")
    for dump in state.dumps:
        try:
            dump.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 - keep cleaning the rest
            errors.append(f"{dump}: {type(error).__name__}: {error}")
    return errors


async def list_owned_databases(anchor: Anchor) -> list[str]:
    rows = await fetch_all(
        anchor,
        "postgres",
        "SELECT datname FROM pg_database WHERE shobj_description(oid, 'pg_database') = :owner",
        {"owner": anchor.owner},
    )
    return sorted(row["datname"] for row in rows)


def main(argv: list[str] | None = None) -> int:
    global _INJECT_CREATE_FAILURE

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-after",
        choices=["create", "fresh", "reversible", "forward"],
        help="test hook: abort after the named phase to prove failure cleanup",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    arguments = parser.parse_args(argv)
    _INJECT_CREATE_FAILURE = arguments.fail_after == "create"

    anchor = resolve_anchor()
    policy = graph_policy()
    state = RunState()
    summary: dict = {
        "database": anchor.base_database,
        "owner": anchor.owner,
        "mode": anchor.mode,
    }
    try:
        asyncio.run(verify_anchor(anchor))
        backend = detect_dump_backend(anchor)
        asyncio.run(phase_fresh(anchor, state))
        if arguments.fail_after == "fresh":
            raise ValidationError("injected failure after fresh")
        asyncio.run(phase_reversible(anchor, state))
        if arguments.fail_after == "reversible":
            raise ValidationError("injected failure after reversible")
        snapshot = asyncio.run(phase_forward(anchor, state, backend))
        if arguments.fail_after == "forward":
            raise ValidationError("injected failure after forward")
        asyncio.run(phase_restore(anchor, state, snapshot, backend))
        summary.update(
            {
                "head": policy.head,
                "reversible_floor": policy.reversible_floor,
                "irreversible": list(policy.irreversible),
                "phases": state.phases,
                "result": "passed",
            }
        )
    except Exception as error:  # noqa: BLE001 - cleanup must run for any failure
        summary.update(
            {"result": "failed", "error": f"{type(error).__name__}: {error}"}
        )
    finally:
        cleanup_errors = cleanup(anchor, state)
        leftovers = [
            name
            for name in asyncio.run(list_owned_databases(anchor))
            if name != anchor.base_database
        ]
        summary["created"] = state.created
        summary["cleanup_errors"] = cleanup_errors
        summary["leftover_owned_databases"] = leftovers

    if arguments.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"migration-validation: {summary['result']} ({', '.join(state.phases)})")
        if summary.get("error"):
            print(f"  error: {summary['error']}", file=sys.stderr)
        if cleanup_errors or leftovers:
            print(
                f"  cleanup: errors={cleanup_errors} leftovers={leftovers}",
                file=sys.stderr,
            )
    failures = (
        summary["result"] != "passed"
        or summary["cleanup_errors"]
        or summary["leftover_owned_databases"]
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
