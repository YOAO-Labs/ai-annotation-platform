"""Run the real role-audit CLI against a fenced historical PostgreSQL database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from scripts.audit_project_roles import (
    _json_default,
    collect_report,
    read_only_audit_session,
)
from tests.identity_role_audit_helpers import audit_reader, historical_audit_database
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_read_relations_native import _grants
from tests.test_identity_recovery import _postgres_tool
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_ROLE_AUDIT_LIVE") != "1",
        reason="requires the explicitly enabled owned Identity role-audit verifier",
    ),
]

_AUDIT_VIEWS = {
    "aap_identity_audit_v1.account_roles",
    "aap_identity_audit_v1.invitation_facts",
    "aap_identity_audit_v1.schema_capabilities",
    "aap_workspace_audit_v1.project_facts",
    "aap_workspace_audit_v1.project_member_facts",
    "aap_workspace_audit_v1.schema_capabilities",
    "aap_labeling_audit_v1.task_facts",
    "aap_labeling_audit_v1.task_batch_facts",
    "aap_labeling_audit_v1.annotation_facts",
    "aap_labeling_audit_v1.task_lock_facts",
    "aap_labeling_audit_v1.schema_capabilities",
}
_READ_ONLY_TABLES = {"alembic_version"}
_IDENTITY_API_FUNCTIONS = {
    "read_actor_facts(uuid)",
    "lock_accounts_share(uuid[],boolean)",
    "lock_accounts_update(uuid[],boolean)",
    "lock_accounts_key_share(uuid[],boolean)",
    "check_accepted_api_key(uuid,uuid,text[])",
}
_IDENTITY_API_VIEWS = {
    "aap_identity_v1.account_profiles",
    "aap_identity_v1.invitation_audit_targets",
}


async def _function_set(db, role: str, schema: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT ns.nspname || '.' || proc.proname || '(' || "
                    "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                    "WHERE ns.nspname=:schema "
                    "AND pg_catalog.has_function_privilege(:role, proc.oid, 'EXECUTE')"
                ),
                {"schema": schema, "role": role},
            )
        ).all()
    )


async def _identity_api_profile(engine) -> tuple[set[str], set[str]]:
    async with engine.connect() as db:
        functions = await _function_set(db, "identity_api_consumer", "aap_identity_v1")
        views = {
            view
            for view in _IDENTITY_API_VIEWS
            if await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                    "CAST(:relation AS regclass), 'SELECT')"
                ),
                {"relation": view},
            )
        }
    assert functions == {
        f"aap_identity_v1.{signature}" for signature in _IDENTITY_API_FUNCTIONS
    }
    assert views == _IDENTITY_API_VIEWS
    return functions, views


async def _active_runtime_count(engine, *, database: str | None = None) -> int:
    async with engine.connect() as db:
        predicate = (
            "WHERE usename='legacy_runtime' AND pid<>pg_backend_pid()"
            if database is None
            else "WHERE usename='legacy_runtime' AND datname=:database "
            "AND pid<>pg_backend_pid()"
        )
        return await db.scalar(
            text(f"SELECT count(*) FROM pg_catalog.pg_stat_activity {predicate}"),
            {} if database is None else {"database": database},
        )


@asynccontextmanager
async def _temporary_runtime_role(test_engine, historical, monkeypatch):
    """Replace only this verifier's idle legacy LOGIN with a clean read-only role."""
    runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime_url.drivername == "postgresql+asyncpg"
    assert runtime_url.username == "legacy_runtime"
    assert runtime_url.password
    assert runtime_url.host == historical.url.host
    assert runtime_url.port == historical.url.port
    assert str(historical.url.database).startswith("aap_role_audit_")
    assert str(historical.url.database).endswith("_test")
    assert await _active_runtime_count(test_engine) == 0

    backup_role = f"legacy_runtime_audit_backup_{uuid.uuid4().hex[:10]}"
    database_name = str(historical.url.database)
    assert re.fullmatch(r"aap_role_audit_[0-9a-f]{16}_test", database_name)
    created = False
    try:
        async with test_engine.connect() as db:
            password_literal = await db.scalar(
                text("SELECT pg_catalog.quote_literal(:password)"),
                {"password": runtime_url.password},
            )
        assert isinstance(password_literal, str) and password_literal
        role_setup = (
            f"ALTER ROLE legacy_runtime RENAME TO {backup_role};\n"
            "CREATE ROLE legacy_runtime WITH LOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
            f"PASSWORD {password_literal};\n"
            f'GRANT CONNECT ON DATABASE "{database_name}" TO legacy_runtime;\n'
        )
        _postgres_tool(
            test_engine,
            [
                "psql",
                "-U",
                "owner",
                "-d",
                test_engine.url.database,
                "-v",
                "ON_ERROR_STOP=1",
                "--single-transaction",
            ],
            body=role_setup.encode("utf-8"),
        )
        created = True

        async with historical.begin() as db:
            await db.execute(text("GRANT USAGE ON SCHEMA public TO legacy_runtime"))
            await db.execute(
                text(
                    "GRANT SELECT ON TABLE public.alembic_version TO legacy_runtime"
                )
            )

        historical_runtime_url = runtime_url.set(database=database_name)
        monkeypatch.setenv(
            "AAP_MODULE_DATA_LEGACY_RUNTIME_URL",
            historical_runtime_url.render_as_string(hide_password=False),
        )
        async with test_engine.connect() as db:
            flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,"
                        "rolreplication,rolbypassrls "
                        "FROM pg_catalog.pg_roles WHERE rolname='legacy_runtime'"
                    )
                )
            ).one()
            assert flags == (False, False, False, False, False, False)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname='legacy_runtime')"
                    )
                )
                == 0
            )
        yield historical_runtime_url, backup_role
    finally:
        if created:
            assert await _active_runtime_count(test_engine) == 0
            async with historical.begin() as db:
                await db.execute(text("DROP OWNED BY legacy_runtime"))
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f'REVOKE CONNECT ON DATABASE "{database_name}" '
                        "FROM legacy_runtime"
                    )
                )
                await db.execute(text("DROP ROLE legacy_runtime"))
                await db.execute(
                    text(f"ALTER ROLE {backup_role} RENAME TO legacy_runtime")
                )


async def _source_snapshot(historical) -> dict:
    async with historical.connect() as db:
        users = (
            await db.execute(
                text(
                    "SELECT id,email,name,md5(password_hash) AS password_digest,"
                    "role,is_active,created_at FROM public.users ORDER BY id"
                )
            )
        ).all()
        invitations = (
            await db.execute(
                text(
                    "SELECT id,email,role,project_id,md5(token) AS token_digest,"
                    "expires_at,accepted_at,revoked_at "
                    "FROM public.user_invitations ORDER BY id"
                )
            )
        ).all()
        revision = await db.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )
        projects = (
            await db.execute(
                text("SELECT id,owner_id,status FROM public.projects ORDER BY id")
            )
        ).all()
        members = (
            await db.execute(
                text(
                    "SELECT id,project_id,user_id,role FROM public.project_members ORDER BY id"
                )
            )
        ).all()
    return {
        "projects": tuple(tuple(row) for row in projects),
        "members": tuple(tuple(row) for row in members),
        "users": tuple(tuple(row) for row in users),
        "invitations": tuple(tuple(row) for row in invitations),
        "revision": revision,
    }


async def _assert_empty_audit_views(historical) -> None:
    async with historical.connect() as db:
        for relation in _AUDIT_VIEWS:
            assert (
                await db.scalar(
                    text("SELECT to_regclass(:relation)"), {"relation": relation}
                )
                is None
            )
        assert (
            await db.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "0171"
        )


async def _assert_runtime_acl(
    historical, *, installed: bool, excluded_view: str | None = None
) -> None:
    async with historical.connect() as db:
        role_flags = (
            await db.execute(
                text(
                    "SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,"
                    "rolreplication,rolbypassrls "
                    "FROM pg_catalog.pg_roles WHERE rolname='legacy_runtime'"
                )
            )
        ).one()
        assert role_flags == (False, False, False, False, False, False)
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_auth_members "
                    "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname='legacy_runtime')"
                )
            )
            == 0
        )
        public_relations = set(
            (
                await db.scalars(
                    text(
                        "SELECT relation.relname FROM pg_catalog.pg_class AS relation "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid=relation.relnamespace "
                        "WHERE namespace.nspname='public' "
                        "AND relation.relkind IN ('r','p','v','m') "
                        "AND pg_catalog.has_table_privilege("
                        "'legacy_runtime',relation.oid,'SELECT')"
                    )
                )
            ).all()
        )
        assert public_relations == _READ_ONLY_TABLES
        assert await db.scalar(
            text(
                "SELECT pg_catalog.has_schema_privilege("
                "'legacy_runtime','public','USAGE')"
            )
        )
        for relation in _READ_ONLY_TABLES:
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege('legacy_runtime',"
                    "CAST(:relation AS regclass),"
                    "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"relation": f"public.{relation}"},
            ), relation
        for private_relation in (
            "public.users",
            "public.user_invitations",
            "public.projects",
            "public.project_members",
            "public.tasks",
            "public.task_batches",
            "public.annotations",
            "public.task_locks",
        ):
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege('legacy_runtime',"
                    "CAST(:relation AS regclass),'SELECT')"
                ),
                {"relation": private_relation},
            )
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege('legacy_runtime',"
                    "CAST(:relation AS regclass),'SELECT')"
                ),
                {"relation": private_relation},
            )

        if installed:
            readable_views = set(
                (
                    await db.scalars(
                        text(
                            "SELECT namespace.nspname || '.' || relation.relname "
                            "FROM pg_catalog.pg_class AS relation "
                            "JOIN pg_catalog.pg_namespace AS namespace "
                            "ON namespace.oid=relation.relnamespace "
                            "WHERE namespace.nspname IN ("
                            "'aap_identity_audit_v1','aap_workspace_audit_v1',"
                            "'aap_labeling_audit_v1') "
                            "AND pg_catalog.has_table_privilege("
                            "'legacy_runtime',relation.oid,'SELECT')"
                        )
                    )
                ).all()
            )
            expected_views = (
                _AUDIT_VIEWS
                if excluded_view is None
                else _AUDIT_VIEWS - {excluded_view}
            )
            assert readable_views == expected_views
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege("
                    "'legacy_runtime','aap_identity_audit_v1','USAGE')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege("
                    "'legacy_runtime','aap_workspace_audit_v1','USAGE')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege("
                    "'legacy_runtime','aap_labeling_audit_v1','USAGE')"
                )
            )
            for view in _AUDIT_VIEWS:
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "'identity_api_consumer',CAST(:relation AS regclass),'SELECT')"
                    ),
                    {"relation": view},
                ), view
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege('legacy_runtime',"
                        "CAST(:relation AS regclass),"
                        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"relation": view},
                ), view


async def _audit_cli(cli: WorkspaceImportCli, *arguments: str):
    return await asyncio.to_thread(
        cli.run_command,
        ("python", "-m", "scripts.audit_project_roles", *arguments),
    )


def _report(run) -> dict:
    assert not run.timed_out and run.exit_code == 0, run.evidence
    return json.loads(run.stdout)


async def _activity(historical, tag: str) -> dict | None:
    async with historical.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('account_roles' in query)>0 AS reads_account_roles "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND usename='legacy_runtime' "
                        "AND application_name=:tag"
                    ),
                    {"tag": tag},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_report_gate(
    historical, cli: WorkspaceImportCli, run_task: asyncio.Task, tag: str
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + 25
    while cli.container_id is None and not run_task.done():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("role-audit CLI did not create its owned container")
        await asyncio.sleep(0.02)
    if run_task.done():
        pytest.fail(f"role-audit CLI exited before its SQL gate: {run_task.result()}")

    while asyncio.get_running_loop().time() < deadline:
        activity = await _activity(historical, tag)
        if (
            activity is not None
            and activity["state"] == "active"
            and activity["wait_event_type"] == "Timeout"
            and activity["wait_event"] == "PgSleep"
            and activity["reads_account_roles"] is True
        ):
            address = await asyncio.to_thread(cli.running_database_client_address)
            assert activity["client_host"] == address
            return address, activity
        if run_task.done():
            pytest.fail(
                f"role-audit CLI exited before its SQL gate: {run_task.result()}"
            )
        await asyncio.sleep(0.04)
    pytest.fail("role-audit CLI did not reach the owned account_roles view gate")


async def _wait_for_backend_exit(historical, backend_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        async with historical.connect() as db:
            alive = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=:pid)"
                ),
                {"pid": backend_pid},
            )
        if not alive:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped role-audit backend remains in pg_stat_activity")


async def _install_report_gate(historical, tag: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    original = f"aap_identity_audit_v1.account_roles_before_gate_{suffix}"
    function = f"aap_identity_audit_v1.role_audit_pause_once_{suffix}"
    body = f"""CREATE FUNCTION {function}() RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $role_audit_gate$
BEGIN
    IF pg_catalog.current_setting('application_name', true)
       IS DISTINCT FROM '{tag}' THEN
        PERFORM pg_catalog.set_config('application_name', '{tag}', true);
        PERFORM pg_catalog.pg_sleep(10);
    END IF;
    RETURN true;
END
$role_audit_gate$"""
    async with historical.begin() as db:
        await db.execute(
            text(
                "ALTER VIEW aap_identity_audit_v1.account_roles "
                f"RENAME TO {original.split('.')[-1]}"
            )
        )
        await db.execute(text(body))
        await db.execute(
            text(
                "CREATE VIEW aap_identity_audit_v1.account_roles AS "
                "SELECT original.id,original.role,original.is_active "
                f"FROM {original} AS original WHERE {function}()"
            )
        )
        await db.execute(
            text("REVOKE ALL ON aap_identity_audit_v1.account_roles FROM PUBLIC")
        )
        await db.execute(
            text(
                "GRANT SELECT ON aap_identity_audit_v1.account_roles TO legacy_runtime"
            )
        )
    return original, function


async def _remove_report_gate(historical, original: str, function: str) -> None:
    async with historical.begin() as db:
        await db.execute(text("DROP VIEW aap_identity_audit_v1.account_roles RESTRICT"))
        await db.execute(text(f"ALTER VIEW {original} RENAME TO account_roles"))
        await db.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))


async def _finish_cli_task(task: asyncio.Task) -> None:
    await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def _revision(historical) -> str:
    async with historical.connect() as db:
        return await db.scalar(text("SELECT version_num FROM public.alembic_version"))


async def _assert_report(report: dict, *, max_rows: int) -> None:
    assert report["report_version"] == "project-role-audit/2"
    assert report["read_only"] is True
    assert report["baseline"]["database_revisions"] == ["0171"]
    assert report["baseline"]["schema_in_sync"] is False
    assert report["limits"]["max_rows_per_section"] == max_rows
    assert all(
        value is False for value in report["baseline"]["schema_capabilities"].values()
    )
    assert (
        report["baseline"]["schema_capabilities"]["user_invitations.project_role"]
        is False
    )
    assert report["reconciliation"]["users"]["total"] == 3
    assert report["reconciliation"]["memberships"]["total"] == 1
    assert report["reconciliation"]["projects"]["total"] == 1
    assert report["reconciliation"]["tasks"]["total"] == 1
    assert report["findings"]["role_discrepancies"]["unknown_platform_roles"][
        "items"
    ] == [
        {
            "id": "10000000-0000-0000-0000-000000000003",
            "role": "mystery",
        }
    ]
    invitation = report["findings"]["invitations"]["pending"]["items"][0]
    assert invitation["id"] == "60000000-0000-0000-0000-000000000001"
    assert invitation["project_id"] == "20000000-0000-0000-0000-000000000001"
    assert invitation["project_role"] is None
    assert invitation["invited_by"] == "10000000-0000-0000-0000-000000000001"
    assert (
        report["findings"]["invitations"]["pending_project_role_to_backfill"]["total"]
        == 1
    )


async def test_installed_role_audit_uses_owned_identity_views_on_real_0171(
    test_engine, tmp_path, monkeypatch, record_property
):
    _verify_postgres(test_engine.url)
    api_profile_before = await _identity_api_profile(test_engine)
    resources, _main_bucket, _owner_s3 = await assert_workspace_identity(test_engine)
    import_access_key = os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY"]
    import_secret_key = os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY"]
    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )

    async with historical_audit_database(test_engine) as (historical, evidence):
        before = await _source_snapshot(historical)
        assert before["revision"] == "0171"
        assert len(before["users"]) == 3
        assert len(before["invitations"]) == 1
        cli = None
        pending_gate = None
        gate_view = None
        async with _temporary_runtime_role(test_engine, historical, monkeypatch):
            try:
                cli_root = tmp_path / "identity-role-audit-cli"
                cli_root.mkdir()
                cli = WorkspaceImportCli(
                    historical,
                    tmp_path,
                    source_root=cli_root,
                    buckets=resources["buckets"],
                    minio_authority=f"{authority_host}:{endpoint.port or 9000}",
                    minio_use_ssl=endpoint.scheme == "https",
                    s3_access_key=import_access_key,
                    s3_secret_key=import_secret_key,
                )
                await asyncio.to_thread(cli.start)

                await _assert_runtime_acl(historical, installed=False)
                await _assert_empty_audit_views(historical)
                missing_view = await _audit_cli(cli, "--max-rows", "1", "--pretty")
                record_property(
                    "identity_role_audit_missing_view",
                    missing_view.evidence,
                )
                assert not missing_view.timed_out
                assert missing_view.exit_code == 2, missing_view.evidence
                assert missing_view.stderr.strip().startswith("audit failed: ")
                assert "Traceback" not in missing_view.stderr
                assert "private-hash-sentinel" not in missing_view.stderr
                assert "private-token-sentinel" not in missing_view.stderr
                await _assert_empty_audit_views(historical)
                assert await _source_snapshot(historical) == before

                _grants(
                    historical,
                    "identity-role-audit-grants.sql",
                    identity_owner="identity_code",
                    consumer_role="legacy_runtime",
                )
                missing_workspace = await _audit_cli(cli, "--max-rows", "1")
                record_property(
                    "workspace_role_audit_missing_profile", missing_workspace.evidence
                )
                assert (
                    missing_workspace.exit_code == 2 and not missing_workspace.timed_out
                )
                assert await _source_snapshot(historical) == before
                _grants(
                    historical,
                    "workspace-role-audit-grants.sql",
                    workspace_owner="workspace_code",
                    consumer_role="legacy_runtime",
                )
                missing_labeling = await _audit_cli(cli, "--max-rows", "1")
                record_property(
                    "labeling_role_audit_missing_profile", missing_labeling.evidence
                )
                assert (
                    missing_labeling.exit_code == 2
                    and not missing_labeling.timed_out
                )
                assert await _source_snapshot(historical) == before
                _grants(
                    historical,
                    "labeling-role-audit-grants.sql",
                    labeling_owner="labeling_code",
                    consumer_role="legacy_runtime",
                )
                await _assert_runtime_acl(historical, installed=True)
                await _identity_api_profile(test_engine)
                async with historical.connect() as db:
                    for view in _AUDIT_VIEWS:
                        assert not await db.scalar(
                            text(
                                "SELECT pg_catalog.has_table_privilege("
                                "'identity_api_consumer',CAST(:relation AS regclass),'SELECT')"
                            ),
                            {"relation": view},
                        ), view

                limited_run = await _audit_cli(cli, "--max-rows", "1")
                record_property(
                    "identity_role_audit_limited_report",
                    limited_run.evidence,
                )
                limited = _report(limited_run)
                await _assert_report(limited, max_rows=1)
                assert limited_run.stdout.strip().count("\n") == 0
                assert "private-hash-sentinel" not in limited_run.stdout
                assert "private-token-sentinel" not in limited_run.stdout

                pretty_run = await _audit_cli(cli, "--max-rows", "1000", "--pretty")
                record_property(
                    "identity_role_audit_pretty_report",
                    pretty_run.evidence,
                )
                pretty = _report(pretty_run)
                await _assert_report(pretty, max_rows=1000)
                assert pretty_run.stdout.startswith("{\n")
                assert pretty["summary"] == limited["summary"]
                assert "private-hash-sentinel" not in pretty_run.stdout
                assert "private-token-sentinel" not in pretty_run.stdout
                assert await _source_snapshot(historical) == before
                assert await _revision(historical) == "0171"

                async with historical.begin() as db:
                    await db.execute(
                        text(
                            "REVOKE SELECT ON aap_identity_audit_v1.account_roles "
                            "FROM legacy_runtime"
                        )
                    )
                try:
                    denied = await _audit_cli(cli, "--max-rows", "1")
                    record_property(
                        "identity_role_audit_view_select_denied",
                        denied.evidence,
                    )
                    assert not denied.timed_out and denied.exit_code == 2, (
                        denied.evidence
                    )
                    assert denied.stderr.strip().startswith("audit failed: ")
                    assert "Traceback" not in denied.stderr
                    assert "private-hash-sentinel" not in denied.stderr
                    assert "private-token-sentinel" not in denied.stderr
                    await _assert_runtime_acl(
                        historical,
                        installed=True,
                        excluded_view="aap_identity_audit_v1.account_roles",
                    )
                    assert await _source_snapshot(historical) == before
                    assert await _revision(historical) == "0171"
                finally:
                    async with historical.begin() as db:
                        await db.execute(
                            text(
                                "GRANT SELECT ON aap_identity_audit_v1.account_roles "
                                "TO legacy_runtime"
                            )
                        )

                recovered_run = await _audit_cli(cli, "--max-rows", "1")
                record_property(
                    "identity_role_audit_select_recovery",
                    recovered_run.evidence,
                )
                recovered = _report(recovered_run)
                await _assert_report(recovered, max_rows=1)
                assert recovered["summary"] == limited["summary"]
                assert await _source_snapshot(historical) == before

                gate_tag = f"aap-role-audit-{uuid.uuid4().hex[:24]}"
                gate_view = await _install_report_gate(historical, gate_tag)
                pending_gate = asyncio.create_task(
                    asyncio.to_thread(
                        cli.run_command,
                        (
                            "python",
                            "-m",
                            "scripts.audit_project_roles",
                            "--max-rows",
                            "1000",
                        ),
                    )
                )
                try:
                    address, activity = await _wait_for_report_gate(
                        historical, cli, pending_gate, gate_tag
                    )
                    backend_pid = activity["pid"]
                    assert activity["usename"] == "legacy_runtime"
                    assert activity["application_name"] == gate_tag
                    assert activity["client_host"] == address
                    assert activity["reads_account_roles"] is True
                    record_property(
                        "identity_role_audit_physical_cancellation_gate",
                        {
                            "role": activity["usename"],
                            "application_name": activity["application_name"],
                            "client_host": activity["client_host"],
                            "backend_pid": backend_pid,
                            "wait_event_type": activity["wait_event_type"],
                            "wait_event": activity["wait_event"],
                            "reads_account_roles": activity["reads_account_roles"],
                        },
                    )
                    assert await asyncio.to_thread(cli.stop_current_container) is True
                    stopped = await asyncio.wait_for(
                        asyncio.shield(pending_gate), timeout=35
                    )
                    record_property(
                        "identity_role_audit_physical_cancellation",
                        stopped.evidence,
                    )
                    assert stopped.exit_code in {137, 143}, stopped.evidence
                    assert stopped.externally_stopped is True
                    assert stopped.timed_out is False
                    await _wait_for_backend_exit(historical, backend_pid)
                    assert await _source_snapshot(historical) == before
                    assert await _revision(historical) == "0171"
                finally:
                    await _finish_cli_task(pending_gate)
                    if gate_view is not None:
                        await _remove_report_gate(
                            historical, gate_view[0], gate_view[1]
                        )
                        gate_view = None

                final_run = await _audit_cli(cli, "--max-rows", "1")
                record_property(
                    "identity_role_audit_physical_cancellation_recovery",
                    final_run.evidence,
                )
                final_report = _report(final_run)
                await _assert_report(final_report, max_rows=1)
                assert final_report["summary"] == limited["summary"]
                assert await _source_snapshot(historical) == before
                assert await _revision(historical) == "0171"
                record_property(
                    "identity_role_audit_scope",
                    "Qualified the real 0171 role-audit CLI against fenced "
                    "Identity and Workspace-owned audit views, including limit, "
                    "pretty output, permission denial, physical process recovery, "
                    "and unchanged historical rows. Broader Identity operations "
                    "remain outside this case.",
                )
            finally:
                try:
                    if pending_gate is not None:
                        await _finish_cli_task(pending_gate)
                finally:
                    try:
                        if gate_view is not None:
                            await _remove_report_gate(
                                historical, gate_view[0], gate_view[1]
                            )
                    finally:
                        if cli is not None:
                            await asyncio.to_thread(cli.close)
        assert await _source_snapshot(historical) == before

    assert await _identity_api_profile(test_engine) == api_profile_before
    record_property("identity_role_audit_owned_database", evidence)


async def test_installed_role_audit_current_workspace_denial_and_recovery(
    test_engine, tmp_path, record_property
):
    from tests.test_project_role_audit import _cleanup, _seed_full

    resources, _bucket, _s3 = await assert_workspace_identity(test_engine)
    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    host = f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    ctx = await _seed_full(test_engine)
    cli = None
    try:
        async with audit_reader(test_engine, cli_role=True) as (reader, role):
            before = await _source_snapshot(test_engine)
            async with read_only_audit_session(reader) as db:
                expected = json.loads(
                    json.dumps(await collect_report(db), default=_json_default)
                )
            source = tmp_path / "workspace-role-audit-current-cli"
            source.mkdir()
            cli = WorkspaceImportCli(
                test_engine,
                tmp_path,
                source_root=source,
                buckets=resources["buckets"],
                minio_authority=f"{host}:{endpoint.port or 9000}",
                minio_use_ssl=endpoint.scheme == "https",
                s3_access_key=os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY"],
                s3_secret_key=os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY"],
                runtime_database_url=reader.url,
            )
            try:
                await asyncio.to_thread(cli.start)
                run = await _audit_cli(cli, "--max-rows", "1000")
                record_property("workspace_role_audit_current_report", run.evidence)
                report = _report(run)
                for key in ("generated_at", "run_id"):
                    expected.pop(key)
                    report.pop(key)
                assert report == expected
                async with test_engine.begin() as db:
                    await db.execute(
                        text(
                            f"REVOKE SELECT ON aap_workspace_audit_v1.project_member_facts FROM {role}"
                        )
                    )
                try:
                    denied = await _audit_cli(cli, "--max-rows", "1000")
                    record_property(
                        "workspace_role_audit_current_denied", denied.evidence
                    )
                    assert denied.exit_code == 2 and not denied.timed_out
                    assert denied.stderr.strip().startswith("audit failed: ")
                    assert "Traceback" not in denied.stderr
                finally:
                    async with test_engine.begin() as db:
                        await db.execute(
                            text(
                                f"GRANT SELECT ON aap_workspace_audit_v1.project_member_facts TO {role}"
                            )
                        )
                recovered = await _audit_cli(cli, "--max-rows", "1000")
                record_property(
                    "workspace_role_audit_current_recovered", recovered.evidence
                )
                report = _report(recovered)
                for key in ("generated_at", "run_id"):
                    report.pop(key)
                assert report == expected
                assert await _source_snapshot(test_engine) == before
            finally:
                if cli is not None:
                    await asyncio.to_thread(cli.close)
                    cli = None
    finally:
        await _cleanup(test_engine, ctx)
