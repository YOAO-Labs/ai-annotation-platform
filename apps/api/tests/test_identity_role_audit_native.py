"""Replay the actual role audit across the Identity and Workspace read boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import hashlib
import json
import subprocess
import sys
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.test_identity_recovery import _postgres_tool
from unittest.mock import patch
from tests.identity_role_audit_helpers import historical_audit_database, audit_reader

from app.db.base import Base
from scripts.audit_project_roles import collect_report, read_only_audit_session
from tests.test_project_role_audit import _seed_full, _seed_manager_cases, _cleanup

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = os.getenv("AAP_IDENTITY_ROLE_AUDIT_CAPTURE") == "1"
GOLDEN_PATH = ROOT / "apps/api/tests/fixtures/identity_role_audit_legacy.json"
GOLDEN = {} if CAPTURE else json.loads(GOLDEN_PATH.read_text())["snapshots"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def _canonical(value):
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        result = {k: _canonical(v) for k, v in value.items()}
        if "script_heads" in result:
            result["script_heads"] = ["<head>"]
            if result["schema_in_sync"]:
                result["database_revisions"] = ["<head>"]
        return result
    return value


@pytest.mark.parametrize("scenario", ["empty", "full", "managers"])
@pytest.mark.parametrize("max_rows", [1, 1000])
async def test_role_audit_report_replay(
    test_engine, scenario, max_rows, record_property
):
    sequence = {}

    def fixed_fixture_ids(_mapper, _connection, target):
        table = target.__tablename__
        sequence[table] = sequence.get(table, 0) + 1
        if hasattr(target, "id"):
            target.id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aap-role-audit:{scenario}:{table}:{sequence[table]}",
            )

    ctx = {}
    try:
        event.listen(Base, "before_insert", fixed_fixture_ids, propagate=True)
        counter = 0

        def fixed_uuid():
            nonlocal counter
            counter += 1
            return uuid.uuid5(
                uuid.NAMESPACE_URL, f"aap-role-audit-input:{scenario}:{counter}"
            )

        try:
            with patch.object(uuid, "uuid4", fixed_uuid):
                if scenario == "full":
                    ctx = await _seed_full(test_engine)
                elif scenario == "managers":
                    ctx = await _seed_manager_cases(test_engine)
        finally:
            event.remove(Base, "before_insert", fixed_fixture_ids)
        async with audit_reader(test_engine) as (reader, _role):
            async with read_only_audit_session(reader) as db:
                report = await collect_report(
                    db,
                    run_id="identity-role-audit-replay",
                    generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    max_rows=max_rows,
                )
        report = _canonical(report)
        name = f"{scenario}-{max_rows}"
        if CAPTURE:
            directory = ROOT / ".worktree/identity-role-audit-baseline-cases"
            directory.mkdir(exist_ok=True)
            (directory / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n")
        else:
            assert report == GOLDEN[name]
        record_property("identity_role_audit_replay", name)
    finally:
        if ctx:
            await _cleanup(test_engine, ctx)


@pytest.mark.parametrize("max_rows", [1, 1000])
async def test_real_0171_role_audit_replay(test_engine, max_rows, record_property):
    async with historical_audit_database(test_engine) as (historical, evidence):
        async with audit_reader(historical) as (reader, _role):
            async with read_only_audit_session(reader) as db:
                report = _canonical(
                    await collect_report(
                        db,
                        run_id="identity-role-audit-replay",
                        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        max_rows=max_rows,
                    )
                )
        name = f"historical-0171-{max_rows}"
        if CAPTURE:
            directory = ROOT / ".worktree/identity-role-audit-baseline-cases"
            directory.mkdir(exist_ok=True)
            (directory / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n")
        else:
            assert report == GOLDEN[name]
    record_property("identity_role_audit_historical", evidence)


async def _denied(engine, statement):
    with pytest.raises(DBAPIError) as caught:
        async with engine.connect() as db:
            await db.execute(text(statement))
    assert caught.value.orig.sqlstate == "42501"


async def test_role_audit_profile_and_repeatable_snapshot(test_engine):
    from tests.test_workspace_consumer_profile_native import _grants

    ctx = await _seed_full(test_engine)
    try:
        async with audit_reader(test_engine) as (reader, role):
            for table in (
                "users",
                "user_invitations",
                "projects",
                "project_members",
                "tasks",
                "task_batches",
                "annotations",
                "task_locks",
            ):
                await _denied(reader, f"SELECT id FROM public.{table}")
            await _denied(
                reader,
                "UPDATE aap_identity_audit_v1.account_roles SET role='viewer' WHERE false",
            )
            await _denied(
                reader,
                "UPDATE aap_labeling_audit_v1.task_facts "
                "SET status='pending' WHERE false",
            )
            async with reader.connect() as db:
                view_ids = (
                    (
                        await db.execute(
                            text(
                                "SELECT oid FROM pg_class WHERE relnamespace IN ('aap_identity_audit_v1'::regnamespace,'aap_workspace_audit_v1'::regnamespace,'aap_labeling_audit_v1'::regnamespace) ORDER BY oid"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            for installer, variables in (
                ("identity-role-audit-grants.sql", {}),
                ("workspace-role-audit-grants.sql", {}),
                ("labeling-role-audit-grants.sql", {"labeling_owner": "labeling_code"}),
            ):
                result = await _grants(
                    test_engine,
                    ROOT / "infra/docker" / installer,
                    role,
                    **variables,
                )
                assert result.returncode == 0, result.stderr
            async with reader.connect() as db:
                assert (
                    await db.execute(
                        text(
                            "SELECT oid FROM pg_class WHERE relnamespace IN ('aap_identity_audit_v1'::regnamespace,'aap_workspace_audit_v1'::regnamespace,'aap_labeling_audit_v1'::regnamespace) ORDER BY oid"
                        )
                    )
                ).scalars().all() == view_ids
            rejected = await _grants(
                test_engine,
                ROOT / "infra/docker/identity-role-audit-grants.sql",
                role,
                consumer_role="identity_api_consumer",
            )
            assert rejected.returncode != 0
            async with test_engine.connect() as db:
                allowed = (
                    await db.scalars(
                        text(
                            "SELECT n.nspname||'.'||c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' AND c.relkind='v' AND has_table_privilege('identity_api_consumer',c.oid,'SELECT')"
                        )
                    )
                ).all()
                assert set(allowed) == {
                    "aap_identity_v1.account_profiles",
                    "aap_identity_v1.invitation_audit_targets",
                }
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' AND has_function_privilege('identity_api_consumer',p.oid,'EXECUTE')"
                        )
                    )
                    == 5
                )
            arguments = {
                "run_id": "snapshot",
                "generated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
            async with read_only_audit_session(reader) as db:
                before = await collect_report(db, **arguments)
                assert (
                    await db.scalar(text("SHOW transaction_isolation"))
                    == "repeatable read"
                )
                assert await db.scalar(text("SHOW transaction_read_only")) == "on"
                async with test_engine.begin() as writer:
                    await writer.execute(
                        text(
                            "UPDATE public.users SET role='viewer',is_active=false WHERE id=:id"
                        ),
                        {"id": ctx["ids"]["unknown_user"]},
                    )
                    await writer.execute(
                        text(
                            "UPDATE public.user_invitations SET project_role='reviewer' WHERE id=:id"
                        ),
                        {"id": ctx["ids"]["legacy_project_invitation"]},
                    )
                    await writer.execute(
                        text(
                            "UPDATE public.projects SET status='audit_diagnostic' WHERE id=:id"
                        ),
                        {"id": ctx["projects"][0]},
                    )
                    await writer.execute(
                        text(
                            "UPDATE public.project_members SET role='audit_diagnostic' WHERE user_id=:id"
                        ),
                        {"id": ctx["ids"]["unknown_user"]},
                    )
                assert await collect_report(db, **arguments) == before
            async with read_only_audit_session(reader) as db:
                after = await collect_report(db, **arguments)
            assert (
                after["reconciliation"]["projects"]["by_status"]["audit_diagnostic"]
                == 1
            )
            assert (
                after["reconciliation"]["memberships"]["by_role"]["audit_diagnostic"]
                == 1
            )
            assert (
                after["summary"]["unknown_platform_roles"]
                == before["summary"]["unknown_platform_roles"] - 1
            )
            assert (
                next(
                    row
                    for row in after["findings"]["invitations"]["pending"]["items"]
                    if row["id"] == ctx["ids"]["legacy_project_invitation"]
                )["project_role"]
                == "reviewer"
            )
    finally:
        await _cleanup(test_engine, ctx)


async def _node_audit_reader(reader, *, prepared, project_role):
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_role_audit_peer.mjs")],
        input=json.dumps(
            {
                "database_url": reader.url.set(
                    drivername="postgresql"
                ).render_as_string(hide_password=False),
                "prepared": prepared,
                "project_role": project_role,
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "read_only": True,
        "private_identity_denied": True,
        "private_workspace_denied": True,
        "prepared": prepared,
    }


@pytest.mark.parametrize(
    "invalid", ["database", "owner", "definition", "view_owner", "extra_relation"]
)
async def test_workspace_audit_installer_rejects_drift_atomically(test_engine, invalid):
    from tests.test_workspace_consumer_profile_native import _grants

    installer = ROOT / "infra/docker/workspace-role-audit-grants.sql"
    relation = "aap_workspace_audit_v1.project_facts"
    async with audit_reader(test_engine) as (reader, role):
        async with test_engine.begin() as db:
            definition = await db.scalar(
                text(f"SELECT pg_get_viewdef('{relation}'::regclass)")
            )
            await db.execute(text(f"REVOKE SELECT ON {relation} FROM {role}"))
            if invalid == "definition":
                await db.execute(
                    text(
                        f"CREATE OR REPLACE VIEW {relation} WITH (security_barrier=true) AS SELECT id,owner_id,status FROM public.projects WHERE false"
                    )
                )
            elif invalid == "view_owner":
                await db.execute(text(f"ALTER VIEW {relation} OWNER TO identity_code"))
            elif invalid == "extra_relation":
                await db.execute(
                    text("CREATE TABLE aap_workspace_audit_v1.unexpected(id uuid)")
                )
        try:
            options = (
                {"expected_database": "wrong_test"} if invalid == "database" else {}
            )
            if invalid == "owner":
                options["workspace_owner"] = "identity_code"
            result = await _grants(test_engine, installer, role, **options)
            assert result.returncode != 0
            async with test_engine.connect() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(:role,CAST(:relation AS regclass),'SELECT')"
                    ),
                    {"role": role, "relation": relation},
                )
            await _denied(reader, f"SELECT id FROM {relation}")
        finally:
            async with test_engine.begin() as db:
                if invalid == "view_owner":
                    await db.execute(
                        text(f"ALTER VIEW {relation} OWNER TO workspace_code")
                    )
                if invalid == "definition":
                    await db.execute(
                        text(
                            f"CREATE OR REPLACE VIEW {relation} WITH (security_barrier=true) AS {definition}"
                        )
                    )
                if invalid == "extra_relation":
                    await db.execute(
                        text("DROP TABLE aap_workspace_audit_v1.unexpected")
                    )
            recovered = await _grants(test_engine, installer, role)
            assert recovered.returncode == 0, recovered.stderr
        async with read_only_audit_session(reader) as db:
            await collect_report(db)


@pytest.mark.parametrize(
    "invalid", ["database", "owner", "definition", "view_owner", "extra_relation"]
)
async def test_labeling_audit_installer_rejects_drift_atomically(test_engine, invalid):
    from tests.test_workspace_consumer_profile_native import _grants

    installer = ROOT / "infra/docker/labeling-role-audit-grants.sql"
    relation = "aap_labeling_audit_v1.task_facts"
    async with audit_reader(test_engine) as (reader, role):
        async with test_engine.begin() as db:
            definition = await db.scalar(
                text(f"SELECT pg_get_viewdef('{relation}'::regclass)")
            )
            await db.execute(text(f"REVOKE SELECT ON {relation} FROM {role}"))
            if invalid == "definition":
                await db.execute(
                    text(
                        f"CREATE OR REPLACE VIEW {relation} WITH (security_barrier=true) AS "
                        "SELECT id,project_id,status,assignee_id,reviewer_id,batch_id,"
                        "review_round_id,reviewer_claimed_at,reviewed_at,first_reviewed_at,"
                        "first_review_result,annotation_contributor_ids,"
                        "review_contributor_ids,review_submitter_id "
                        "FROM public.tasks WHERE false"
                    )
                )
            elif invalid == "view_owner":
                await db.execute(text(f"ALTER VIEW {relation} OWNER TO identity_code"))
            elif invalid == "extra_relation":
                await db.execute(
                    text("CREATE TABLE aap_labeling_audit_v1.unexpected(id uuid)")
                )
        try:
            options = (
                {"expected_database": "wrong_test"} if invalid == "database" else {}
            )
            if invalid == "owner":
                options["labeling_owner"] = "identity_code"
            result = await _grants(test_engine, installer, role, **options)
            assert result.returncode != 0
            async with test_engine.connect() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(:role,CAST(:relation AS regclass),'SELECT')"
                    ),
                    {"role": role, "relation": relation},
                )
            await _denied(reader, f"SELECT id FROM {relation}")
        finally:
            async with test_engine.begin() as db:
                if invalid == "view_owner":
                    await db.execute(
                        text(f"ALTER VIEW {relation} OWNER TO labeling_code")
                    )
                if invalid == "definition":
                    await db.execute(
                        text(
                            f"CREATE OR REPLACE VIEW {relation} "
                            f"WITH (security_barrier=true) AS {definition}"
                        )
                    )
                if invalid == "extra_relation":
                    await db.execute(
                        text("DROP TABLE aap_labeling_audit_v1.unexpected")
                    )
            recovered = await _grants(test_engine, installer, role)
            assert recovered.returncode == 0, recovered.stderr
        async with read_only_audit_session(reader) as db:
            await collect_report(db)


async def test_normal_workspace_profile_revokes_explicit_audit_grants(test_engine):
    from tests.test_workspace_consumer_profile_native import _grants

    async with audit_reader(test_engine) as (reader, role):
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"GRANT SELECT (id) ON aap_workspace_audit_v1.project_facts TO {role}"
                )
            )
        applied = await _grants(
            test_engine, ROOT / "infra/docker/workspace-consumer-grants.sql", role
        )
        assert applied.returncode == 0, applied.stderr
        async with test_engine.connect() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_schema_privilege(:role,'aap_workspace_audit_v1','USAGE')"
                ),
                {"role": role},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_any_column_privilege(:role,'aap_workspace_audit_v1.project_facts','SELECT')"
                ),
                {"role": role},
            )
        await _denied(reader, "SELECT id FROM aap_workspace_audit_v1.project_facts")


async def test_historical_workspace_audit_preserves_diagnostic_nulls(test_engine):
    async with historical_audit_database(test_engine) as (historical, _evidence):
        async with historical.begin() as db:
            await db.execute(
                text(
                    "ALTER TABLE public.projects ALTER COLUMN owner_id DROP NOT NULL, ALTER COLUMN status DROP NOT NULL"
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE public.project_members ALTER COLUMN project_id DROP NOT NULL, ALTER COLUMN user_id DROP NOT NULL, ALTER COLUMN role DROP NOT NULL"
                )
            )
            await db.execute(
                text("UPDATE public.projects SET owner_id=NULL,status=NULL")
            )
            await db.execute(
                text(
                    "UPDATE public.project_members SET project_id=NULL,user_id=NULL,role=NULL"
                )
            )
        async with audit_reader(historical) as (reader, _role):
            async with read_only_audit_session(reader) as db:
                assert (
                    await db.execute(
                        text(
                            "SELECT owner_id,status FROM aap_workspace_audit_v1.project_facts"
                        )
                    )
                ).one() == (None, None)
                assert (
                    await db.execute(
                        text(
                            "SELECT project_id,user_id,role FROM aap_workspace_audit_v1.project_member_facts"
                        )
                    )
                ).one() == (None, None, None)
                report = await collect_report(db)
                assert report["reconciliation"]["projects"]["by_status"] == {
                    "unknown": 1
                }
                assert report["reconciliation"]["memberships"]["by_role"] == {
                    "unknown": 1
                }
                assert report["baseline"]["database_revisions"] == ["0171"]


async def test_historical_profile_tracks_preparation_and_node(
    test_engine, record_property
):
    from tests.test_workspace_consumer_profile_native import _grants

    async with historical_audit_database(test_engine) as (historical, evidence):
        async with audit_reader(historical) as (reader, _role):
            async with reader.connect() as db:
                view_ids = (
                    await db.scalars(
                        text(
                            "SELECT oid FROM pg_class WHERE relnamespace IN ('aap_identity_audit_v1'::regnamespace,'aap_workspace_audit_v1'::regnamespace,'aap_labeling_audit_v1'::regnamespace) ORDER BY oid"
                        )
                    )
                ).all()
            await _node_audit_reader(reader, prepared=False, project_role=None)
            environment = os.environ.copy()
            environment["AAP_ROLE_AUDIT_HISTORICAL_URL"] = (
                historical.url.render_as_string(hide_password=False)
            )
            code = "from alembic import command; from alembic.config import Config; import os; c=Config('alembic.ini'); c.set_main_option('sqlalchemy.url',os.environ['AAP_ROLE_AUDIT_HISTORICAL_URL']); command.upgrade(c,'0173')"
            upgraded = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", code],
                env=environment,
                cwd=ROOT / "apps/api",
                capture_output=True,
                text=True,
                timeout=180,
            )
            log = (
                ROOT
                / ".worktree"
                / f"identity-role-audit-historical-{evidence['database']}.0173.log"
            )
            log.write_text(upgraded.stdout + upgraded.stderr)
            assert upgraded.returncode == 0, log.read_text()[-4000:]
            async with historical.begin() as db:
                await db.execute(
                    text("UPDATE public.user_invitations SET project_role='reviewer'")
                )
            await _node_audit_reader(reader, prepared=True, project_role="reviewer")
            installed = _grants(
                historical,
                "labeling-role-audit-grants.sql",
                labeling_owner="labeling_code",
                consumer_role=_role,
            )
            assert installed.returncode == 0, installed.stderr
            async with read_only_audit_session(reader) as db:
                report = await collect_report(db)
                assert all(report["baseline"]["schema_capabilities"].values())
                assert report["baseline"]["database_revisions"] == ["0173"]
                assert (
                    report["findings"]["invitations"]["pending"]["items"][0][
                        "project_role"
                    ]
                    == "reviewer"
                )
                assert (
                    await db.scalars(
                        text(
                            "SELECT oid FROM pg_class WHERE relnamespace IN ('aap_identity_audit_v1'::regnamespace,'aap_workspace_audit_v1'::regnamespace,'aap_labeling_audit_v1'::regnamespace) ORDER BY oid"
                        )
                    )
                ).all() == view_ids
            await _denied(reader, "SELECT project_role FROM public.user_invitations")
            dump = await asyncio.to_thread(
                _postgres_tool,
                historical,
                ["pg_dump", "-U", "owner", "-d", historical.url.database, "-Fc"],
            )
            backup = (
                ROOT / ".worktree" / f"identity-role-audit-{evidence['database']}.dump"
            )
            with os.fdopen(
                os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as stream:
                stream.write(dump)
            async with historical_audit_database(test_engine, initialize=False) as (
                restored,
                restore_evidence,
            ):
                await asyncio.to_thread(
                    _postgres_tool,
                    restored,
                    [
                        "pg_restore",
                        "-U",
                        "owner",
                        "-d",
                        restored.url.database,
                        "--clean",
                        "--if-exists",
                        "--exit-on-error",
                    ],
                    body=dump,
                )
                restored_reader = create_async_engine(
                    reader.url.set(database=restored.url.database), hide_parameters=True
                )
                try:
                    await _node_audit_reader(
                        restored_reader, prepared=True, project_role="reviewer"
                    )
                    async with read_only_audit_session(restored_reader) as db:
                        restored_report = await collect_report(db)
                    restore_evidence["revision"] = restored_report["baseline"][
                        "database_revisions"
                    ][0]
                    assert _canonical(
                        {
                            k: v
                            for k, v in restored_report.items()
                            if k not in ("run_id", "generated_at")
                        }
                    ) == _canonical(
                        {
                            k: v
                            for k, v in report.items()
                            if k not in ("run_id", "generated_at")
                        }
                    )
                finally:
                    await restored_reader.dispose()
            record_property(
                "identity_role_audit_backup_restore",
                {
                    "backup": str(backup.relative_to(ROOT)),
                    "sha256": hashlib.sha256(dump).hexdigest(),
                    "bytes": len(dump),
                    "source_revision": "0173",
                    "restore": restore_evidence,
                    "node_read_after_restore": True,
                },
            )
    record_property("identity_role_audit_no_reinstall_transition", evidence)
