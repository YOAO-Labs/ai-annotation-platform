"""Owned historical database and deterministic inputs for role-audit acceptance."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_asset_storage_cleanup_runtime import _verify_postgres

ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def historical_audit_database(test_engine, *, initialize=True):
    _verify_postgres(test_engine.url)
    name = "aap_role_audit_" + uuid.uuid4().hex[:16] + "_test"
    marker = "identity-role-audit:" + uuid.uuid4().hex
    evidence = {"database": name, "revision": "0171", "removed": False}
    engine = None
    async with test_engine.connect() as connection:
        db = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await db.execute(text(f'CREATE DATABASE "{name}"'))
        await db.execute(text(f"COMMENT ON DATABASE {name} IS '{marker}'"))
        oid = await db.scalar(
            text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": name}
        )
    try:
        url = test_engine.url.set(database=name)
        if not initialize:
            engine = create_async_engine(url, hide_parameters=True)
            evidence["revision"] = None
            yield engine, evidence
            return
        environment = os.environ.copy()
        environment["AAP_ROLE_AUDIT_HISTORICAL_URL"] = url.render_as_string(
            hide_password=False
        )
        code = "from alembic import command; from alembic.config import Config; import os; c=Config('alembic.ini'); c.set_main_option('sqlalchemy.url',os.environ['AAP_ROLE_AUDIT_HISTORICAL_URL']); command.upgrade(c,'0171')"
        command = [sys.executable, "-c", code]
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=ROOT / "apps/api",
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        log = (
            ROOT / ".worktree" / f"identity-role-audit-historical-{name}.migration.log"
        )
        log.write_text(result.stdout + result.stderr)
        evidence.update(
            migration_exit_code=result.returncode,
            migration_log=str(log.relative_to(ROOT)),
        )
        assert result.returncode == 0, log.read_text()[-4000:]
        engine = create_async_engine(url, hide_parameters=True)
        async with engine.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0171"
            )
            assert (
                await db.scalar(text("SELECT to_regnamespace('aap_identity_v1')"))
                is None
            )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='users' AND column_name='avatar_ref')"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO users(id,email,name,password_hash,role,is_active) VALUES ('10000000-0000-0000-0000-000000000001','admin@audit.test','admin','private-hash-sentinel','project_admin',true),('10000000-0000-0000-0000-000000000002','legacy@audit.test','legacy','private-hash-sentinel','annotator',false),('10000000-0000-0000-0000-000000000003','odd@audit.test','odd','private-hash-sentinel','mystery',true)"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO projects(id,display_id,name,type_label,type_key,owner_id,status) VALUES ('20000000-0000-0000-0000-000000000001','P-AUDIT-OLD','role audit historical','Image','image-det','10000000-0000-0000-0000-000000000001','in_progress')"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO project_members(id,project_id,user_id,role,assigned_by) VALUES ('30000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000002','viewer','10000000-0000-0000-0000-000000000001')"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO tasks(id,project_id,display_id,file_name,file_path,status,assignee_id,reviewer_id,review_round_id) VALUES ('40000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','T-AUDIT-OLD','old.png','old.png','review','10000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000002','50000000-0000-0000-0000-000000000001')"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO user_invitations(id,email,role,project_id,token,expires_at,invited_by) VALUES ('60000000-0000-0000-0000-000000000001','invite@audit.test','annotator','20000000-0000-0000-0000-000000000001','private-token-sentinel','2100-01-01'::timestamptz,'10000000-0000-0000-0000-000000000001')"
                )
            )
        yield engine, evidence
    finally:
        if engine is not None:
            await engine.dispose()
        async with test_engine.connect() as connection:
            db = await connection.execution_options(isolation_level="AUTOCOMMIT")
            observed = (
                await db.execute(
                    text(
                        "SELECT oid,shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                    ),
                    {"name": name},
                )
            ).one()
            assert observed == (oid, marker), observed
            clients = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                ),
                {"name": name},
            )
            assert clients == 0, clients
            await db.execute(text(f'DROP DATABASE "{name}"'))
            evidence["removed"] = True


@asynccontextmanager
async def audit_reader(test_engine, *, cli_role=False):
    """Compose explicit owner audit profiles with the remaining Labeling reads."""
    from tests.test_workspace_consumer_profile_native import _grants

    _verify_postgres(test_engine.url)
    role = (
        str(test_engine.url.database) + "_app"
        if cli_role
        else "identity_audit_" + uuid.uuid4().hex[:12]
    )
    password = uuid.uuid4().hex
    engine = None
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
            )
        )
    try:
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f'GRANT CONNECT ON DATABASE "{test_engine.url.database}" TO {role}'
                )
            )
            await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await db.execute(
                text(
                    f"GRANT SELECT ON public.tasks,public.task_batches,public.annotations,public.task_locks,public.alembic_version TO {role}"
                )
            )
            # Prove the owner profile removes existing table and column grants.
            await db.execute(text(f"GRANT SELECT ON public.users TO {role}"))
            await db.execute(text(f"GRANT SELECT (id,role) ON public.users TO {role}"))
            await db.execute(
                text(f"GRANT SELECT (id,token) ON public.user_invitations TO {role}")
            )
        async with test_engine.begin() as db:
            # Existing broad and column grants must both be removed by the installer.
            await db.execute(
                text(
                    f"GRANT SELECT ON public.projects,public.project_members TO {role}"
                )
            )
            await db.execute(
                text(f"GRANT SELECT (id,owner_id) ON public.projects TO {role}")
            )
            await db.execute(
                text(f"GRANT SELECT (id,role) ON public.project_members TO {role}")
            )
        for installer in (
            "identity-role-audit-grants.sql",
            "workspace-role-audit-grants.sql",
        ):
            result = await _grants(test_engine, ROOT / "infra/docker" / installer, role)
            assert result.returncode == 0, result.stderr
        engine = create_async_engine(
            test_engine.url.set(username=role, password=password), hide_parameters=True
        )
        yield engine, role
    finally:
        if engine is not None:
            await engine.dispose()
        async with test_engine.begin() as db:
            await db.execute(text(f"DROP OWNED BY {role}"))
            await db.execute(text(f"DROP ROLE {role}"))
