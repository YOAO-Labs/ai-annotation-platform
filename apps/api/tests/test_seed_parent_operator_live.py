"""Cold qualification of the parent seed's Identity and Workspace operations."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from datetime import UTC, datetime
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_workspace_import_cli_live import (
    _IDENTITY_CONSUMER_FUNCTIONS,
    _PROVISIONING_FUNCTIONS,
    _assert_empty_owned_rows,
    _create_datasets_storage_scope,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_SEED_PARENT_OPERATOR_LIVE") != "1",
        reason="requires the explicitly enabled owned seed-parent verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "apps/api/tests/fixtures/seed_parent_operator_probe.py"
_PERSONAS = {
    "admin": ("超级管理员", "super_admin", None),
    "pm": ("张明轩", "project_admin", "管理组"),
    "qa": ("李晓华", "employee", "质检组"),
    "anno": ("王芳", "employee", "标注组A"),
    "viewer": ("赵观察", "viewer", None),
    "anno2": ("刘洋", "employee", "标注组A"),
    "anno3": ("陈思远", "employee", "标注组B"),
}
_PROJECTS = ("P-COCO8", "P-VIDEO-DEV", "P-PC-DEV", "P-PC-MULTI", "P-OCR")
_MEMBERS = {
    "anno": "annotator",
    "anno2": "annotator",
    "anno3": "annotator",
    "qa": "reviewer",
    "viewer": "viewer",
}
_IDENTITY_SEED_FUNCTION = "aap_identity_v1.create_seed_persona_account(jsonb)"
_WORKSPACE_SEED_FUNCTIONS = (
    "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
    "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
    "aap_workspace_v1.write_seed_members(jsonb)",
)
_WRITE_MEMBERS = _WORKSPACE_SEED_FUNCTIONS[-1]
_PRIVATE_TABLES = ("users", "projects", "project_members", "project_templates")
_OLD = datetime(2000, 1, 1, tzinfo=UTC)


def _probe_result(run) -> dict:
    prefix = "AAP_SEED_PARENT_RESULT="
    matches = [
        line[len(prefix) :]
        for line in run.stdout.splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) == 1, run.evidence
    return json.loads(matches[0])


def _bucket_versions(client, bucket: str) -> list[tuple[str, str, str, bool]]:
    state = []
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
        state.extend(
            ("version", item["Key"], item["VersionId"], item["IsLatest"])
            for item in page.get("Versions", [])
        )
        state.extend(
            ("delete-marker", item["Key"], item["VersionId"], item["IsLatest"])
            for item in page.get("DeleteMarkers", [])
        )
    return sorted(state)


async def _assert_parent_operator_profile(test_engine) -> None:
    _verify_postgres(test_engine.url)
    runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime_url.drivername == "postgresql+asyncpg"
    assert runtime_url.username == "legacy_runtime"
    assert (runtime_url.host, runtime_url.port, runtime_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert str(runtime_url.database).endswith("_test")
    engine = create_async_engine(runtime_url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
            role_flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
                        "rolbypassrls FROM pg_catalog.pg_roles "
                        "WHERE rolname=current_user"
                    )
                )
            ).one()
            assert role_flags == (False, False, False, False, False)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname=current_user)"
                    )
                )
                == 0
            )

            identity_functions = set(
                (
                    await db.scalars(
                        text(
                            "SELECT ns.nspname || '.' || proc.proname || '(' || "
                            "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                            "FROM pg_catalog.pg_proc AS proc "
                            "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                            "WHERE ns.nspname='aap_identity_v1' "
                            "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                        )
                    )
                ).all()
            )
            assert identity_functions == {
                *_IDENTITY_CONSUMER_FUNCTIONS,
                *_PROVISIONING_FUNCTIONS,
                _IDENTITY_SEED_FUNCTION,
            }

            workspace_functions = set(
                (
                    await db.scalars(
                        text(
                            "SELECT ns.nspname || '.' || proc.proname || '(' || "
                            "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                            "FROM pg_catalog.pg_proc AS proc "
                            "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                            "WHERE ns.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$' "
                            "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                        )
                    )
                ).all()
            )
            assert len(workspace_functions) == 35
            assert set(_WORKSPACE_SEED_FUNCTIONS) <= workspace_functions
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'aap_identity_v1.account_profiles', 'SELECT')"
                )
            )
            for relation in (
                "aap_workspace_v1.project_facts",
                "aap_workspace_v1.project_member_facts",
            ):
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, "
                        ":relation, 'SELECT')"
                    ),
                    {"relation": relation},
                ), relation

            for table in _PRIVATE_TABLES:
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, "
                        ":table_name, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"table_name": f"public.{table}"},
                ), table
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user, "
                        ":table_name, 'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"table_name": f"public.{table}"},
                ), table
    finally:
        await engine.dispose()

    async with test_engine.connect() as db:
        for signature in (_IDENTITY_SEED_FUNCTION, _WRITE_MEMBERS):
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'identity_api_consumer', CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
        ordinary_identity = set(
            (
                await db.scalars(
                    text(
                        "SELECT ns.nspname || '.' || proc.proname || '(' || "
                        "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                        "FROM pg_catalog.pg_proc AS proc "
                        "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                        "WHERE ns.nspname='aap_identity_v1' "
                        "AND pg_catalog.has_function_privilege('identity_api_consumer', proc.oid, 'EXECUTE')"
                    )
                )
            ).all()
        )
        assert ordinary_identity == set(_IDENTITY_CONSUMER_FUNCTIONS)
        for table in _PRIVATE_TABLES:
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                    ":table_name, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"table_name": f"public.{table}"},
            ), table


async def _create_demo_projects(
    test_engine, owner_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    projects = {display_id: uuid.uuid4() for display_id in _PROJECTS}
    async with test_engine.begin() as db:
        for display_id, project_id in projects.items():
            await db.execute(
                text(
                    "INSERT INTO public.projects "
                    "(id,display_id,name,owner_id,type_label,type_key,data_type) "
                    "VALUES (:id,:display_id,:name,:owner,'图像目标检测','image-det','image')"
                ),
                {
                    "id": project_id,
                    "display_id": display_id,
                    "name": f"seed parent fixture {display_id}",
                    "owner": owner_id,
                },
            )
    return projects


async def _account_states(test_engine, account_ids: dict[str, str]) -> list[dict]:
    from app.core.security import verify_password

    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id,email,name,role,group_name,group_id,status,is_active,"
                        "credential_generation,disabled_kind,disabled_at,disabled_by,"
                        "disabled_reason,email_verified_at,last_login_at,last_seen_at,"
                        "password_admin_reset_at,deactivation_requested_at,deactivation_reason,"
                        "deactivation_scheduled_at,weekly_target_default,avatar_ref,"
                        "preferences,password_hash FROM public.users "
                        "WHERE email=ANY(CAST(:emails AS text[])) ORDER BY email"
                    ),
                    {"emails": list(_PERSONAS)},
                )
            )
            .mappings()
            .all()
        )
        groups = await db.scalar(text("SELECT count(*) FROM public.groups"))

    assert len(rows) == len(_PERSONAS)
    assert groups == 0
    actual = []
    for row in rows:
        state = dict(row)
        password_hash = state.pop("password_hash")
        state["id"] = str(state["id"])
        state["password_matches_seed"] = verify_password("123456", password_hash)
        actual.append(state)

    expected = []
    for email, (name, role, group_name) in sorted(_PERSONAS.items()):
        expected.append(
            {
                "id": account_ids[email],
                "email": email,
                "name": name,
                "role": role,
                "group_name": group_name,
                "group_id": None,
                "status": "offline",
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
                "disabled_at": None,
                "disabled_by": None,
                "disabled_reason": None,
                "email_verified_at": None,
                "last_login_at": None,
                "last_seen_at": None,
                "password_admin_reset_at": None,
                "deactivation_requested_at": None,
                "deactivation_reason": None,
                "deactivation_scheduled_at": None,
                "weekly_target_default": None,
                "avatar_ref": None,
                "preferences": {},
                "password_matches_seed": True,
            }
        )
    assert actual == expected
    return actual


async def _membership_rows(test_engine) -> list[dict]:
    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT project.display_id, persona.email, member.role, "
                        "assigner.email AS assigned_by, member.version, member.weekly_target, "
                        "member.assigned_at, member.updated_at, member.id "
                        "FROM public.project_members AS member "
                        "JOIN public.projects AS project ON project.id=member.project_id "
                        "JOIN public.users AS persona ON persona.id=member.user_id "
                        "JOIN public.users AS assigner ON assigner.id=member.assigned_by "
                        "WHERE project.display_id=ANY(CAST(:display_ids AS text[])) "
                        "ORDER BY project.display_id,persona.email"
                    ),
                    {"display_ids": list(_PROJECTS)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _assert_pending_dataset_absent(test_engine, display_id: str) -> None:
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text("SELECT id FROM public.datasets WHERE display_id=:display_id"),
                {"display_id": display_id},
            )
            is None
        )


async def _install_member_trigger(
    test_engine,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    mode: str,
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    function_name = f"aap_test_seed_parent_{suffix}"
    trigger_name = f"aap_test_seed_parent_{suffix}"
    if mode == "raise":
        action = (
            "RAISE EXCEPTION USING ERRCODE = 'P0001', "
            "MESSAGE = 'controlled seed-parent late membership failure';"
        )
    elif mode == "sleep":
        action = "PERFORM pg_catalog.pg_sleep(10);"
    else:
        raise ValueError("unsupported test trigger mode")

    body = f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trigger$
BEGIN
    IF NEW.user_id = '{user_id}'::uuid AND NEW.project_id = '{project_id}'::uuid THEN
        {action}
    END IF;
    RETURN NEW;
END
$trigger$"""
    async with test_engine.begin() as db:
        await db.execute(text(body))
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.project_members "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
            )
        )
    return function_name, trigger_name


async def _drop_member_trigger(test_engine, names: tuple[str, str]) -> None:
    function_name, trigger_name = names
    async with test_engine.begin() as db:
        await db.execute(
            text(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.project_members")
        )
        await db.execute(text(f"DROP FUNCTION IF EXISTS public.{function_name}()"))


async def _membership_gate_activity(
    test_engine, *, application_name: str, client_address: str | None = None
) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid, usename, application_name, host(client_addr) AS client_addr, "
                        "state, wait_event_type, wait_event, "
                        "position('write_seed_members' in query) > 0 AS seed_writer "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND usename='legacy_runtime' "
                        "AND application_name=:application_name "
                        "AND (CAST(:client_address AS inet) IS NULL "
                        "OR client_addr=CAST(:client_address AS inet))"
                    ),
                    {
                        "application_name": application_name,
                        "client_address": client_address,
                    },
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_membership_sleep(
    test_engine, cli: WorkspaceImportCli, run_task: asyncio.Task, application_name: str
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        activity = await _membership_gate_activity(
            test_engine,
            application_name=application_name,
        )
        if (
            activity is not None
            and activity["state"] == "active"
            and activity["wait_event"] == "PgSleep"
            and activity["seed_writer"] is True
        ):
            client_address = await asyncio.to_thread(
                cli.running_database_client_address
            )
            assert activity["client_addr"] == client_address
            return client_address, activity
        if run_task.done():
            pytest.fail(
                f"seed membership one-shot exited before the SQL gate: {run_task.result()}"
            )
        await asyncio.sleep(0.04)
    pytest.fail("seed membership did not reach the owned pg_sleep trigger")


async def _wait_for_membership_disconnect(
    test_engine, *, backend_pid: int, client_address: str
) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            present = await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
                    "WHERE datname=current_database() AND pid=:pid "
                    "AND usename='legacy_runtime' "
                    "AND client_addr=CAST(:client_address AS inet))"
                ),
                {"pid": backend_pid, "client_address": client_address},
            )
        if not present:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped seed-parent client still owns its PostgreSQL session")


async def _finish_pending_cli_task(task: asyncio.Task) -> None:
    # The SQL gate lasts at most ten seconds. Join its lifecycle before close(),
    # including failures before Docker starts, so two threads never remove it.
    await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def _run_probe(cli: WorkspaceImportCli, operation: str, *arguments: str):
    return await asyncio.to_thread(
        cli.run_command,
        ("python", f"/fixture/{PROBE.name}", operation, *arguments),
    )


async def test_seed_parent_helpers_run_with_operator_only_capabilities(
    test_engine, tmp_path, record_property
):
    from app.config import settings
    from app.core.security import hash_password, verify_password

    _verify_postgres(test_engine.url)
    await _assert_empty_owned_rows(test_engine)
    await _assert_parent_operator_profile(test_engine)

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    bucket_before = _bucket_versions(owner_s3, bucket)
    assert bucket_before == []

    source_root = tmp_path / "seed-parent-source"
    source_root.mkdir()
    shutil.copyfile(PROBE, source_root / PROBE.name)
    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    scope, destroy_scope = _create_datasets_storage_scope(
        resources, owner_s3, bucket, settings
    )
    cli = None
    sleep_task = None
    sleep_trigger = None
    try:
        cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source_root,
            buckets=resources["buckets"],
            minio_authority=f"{authority_host}:{endpoint.port or 9000}",
            minio_use_ssl=endpoint.scheme == "https",
            s3_access_key=scope["access_key"],
            s3_secret_key=scope["secret_key"],
        )
        await asyncio.to_thread(cli.start)

        accounts_run = await _run_probe(cli, "accounts")
        record_property("seed_parent_account_create", accounts_run.evidence)
        assert accounts_run.exit_code == 0, accounts_run.stderr
        accounts = _probe_result(accounts_run)
        account_ids = accounts["account_ids"]
        assert set(account_ids) == set(_PERSONAS)
        assert accounts["commit_attempts"] == 1
        initial_accounts = await _account_states(test_engine, account_ids)
        record_property("seed_parent_initial_account_facts", initial_accounts)

        project_ids = await _create_demo_projects(
            test_engine, uuid.UUID(account_ids["pm"])
        )
        record_property(
            "seed_parent_demo_project_ids",
            {
                display_id: str(project_id)
                for display_id, project_id in project_ids.items()
            },
        )
        async with test_engine.connect() as db:
            assert await db.scalar(
                text(
                    "SELECT count(*) FROM public.projects "
                    "WHERE display_id=ANY(CAST(:ids AS text[]))"
                ),
                {"ids": list(_PROJECTS)},
            ) == len(_PROJECTS)
            assert (
                await db.scalar(text("SELECT count(*) FROM public.project_members"))
                == 0
            )

        pm_existing_password = "Keep-Existing-1234"
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET name='legacy pm', role='viewer', "
                    "group_name='legacy group', is_active=false,password_hash=:password_hash "
                    "WHERE id=:id"
                ),
                {
                    "id": uuid.UUID(account_ids["pm"]),
                    "password_hash": hash_password(pm_existing_password),
                },
            )
        replay_run = await _run_probe(cli, "accounts")
        record_property("seed_parent_account_reuse", replay_run.evidence)
        assert replay_run.exit_code == 0, replay_run.stderr
        replay = _probe_result(replay_run)
        assert replay["account_ids"] == account_ids
        assert replay["commit_attempts"] == 1
        async with test_engine.connect() as db:
            pm = (
                await db.execute(
                    text(
                        "SELECT name,role,group_name,is_active,password_hash "
                        "FROM public.users "
                        "WHERE id=:id"
                    ),
                    {"id": uuid.UUID(account_ids["pm"])},
                )
            ).one()
        assert pm[:4] == ("legacy pm", "viewer", "legacy group", False)
        pm_password_preserved = verify_password(pm_existing_password, pm[4])
        assert pm_password_preserved is True
        record_property(
            "seed_parent_reused_pm_state",
            {
                "id": account_ids["pm"],
                "name": pm[0],
                "role": pm[1],
                "group_name": pm[2],
                "is_active": pm[3],
                "password_preserved": pm_password_preserved,
            },
        )

        missing_dataset = f"SP-MISS-{uuid.uuid4().hex[:8]}"
        async with test_engine.begin() as db:
            await db.execute(
                text(f"REVOKE EXECUTE ON FUNCTION {_WRITE_MEMBERS} FROM legacy_runtime")
            )
        try:
            missing_run = await _run_probe(
                cli,
                "memberships_fail",
                account_ids["pm"],
                missing_dataset,
            )
            record_property(
                "seed_parent_membership_capability_denied", missing_run.evidence
            )
            assert missing_run.exit_code != 0
            missing = _probe_result(missing_run)
            assert (
                "permission denied for function write_seed_members"
                in missing["error"].lower()
            ), missing
            assert missing["pending_id_materialized"] is True
            assert missing["commit_attempts"] == 0
            await _assert_pending_dataset_absent(test_engine, missing_dataset)
            assert await _membership_rows(test_engine) == []
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION {_WRITE_MEMBERS} TO legacy_runtime"
                    )
                )

        viewer_id = uuid.UUID(account_ids["viewer"])
        first_project_id = project_ids[_PROJECTS[0]]
        late_names = await _install_member_trigger(
            test_engine,
            user_id=viewer_id,
            project_id=first_project_id,
            mode="raise",
        )
        late_dataset = f"SP-LATE-{uuid.uuid4().hex[:8]}"
        try:
            late_run = await _run_probe(
                cli,
                "memberships_fail",
                account_ids["pm"],
                late_dataset,
            )
            record_property("seed_parent_membership_late_rollback", late_run.evidence)
            assert late_run.exit_code != 0
            late = _probe_result(late_run)
            assert "controlled seed-parent late membership failure" in late["error"]
            assert late["pending_id_materialized"] is True
            assert late["commit_attempts"] == 0
            await _assert_pending_dataset_absent(test_engine, late_dataset)
            assert await _membership_rows(test_engine) == []
        finally:
            await _drop_member_trigger(test_engine, late_names)

        sleep_names = await _install_member_trigger(
            test_engine,
            user_id=viewer_id,
            project_id=first_project_id,
            mode="sleep",
        )
        sleep_trigger = sleep_names
        sleep_dataset = f"SP-KILL-{uuid.uuid4().hex[:8]}"
        application_name = f"aap-seed-parent-{uuid.uuid4().hex}"
        sleep_task = asyncio.create_task(
            asyncio.to_thread(
                cli.run_command,
                (
                    "python",
                    f"/fixture/{PROBE.name}",
                    "memberships_sleep",
                    account_ids["pm"],
                    sleep_dataset,
                    application_name,
                ),
            )
        )
        try:
            client_address, gate = await _wait_for_membership_sleep(
                test_engine, cli, sleep_task, application_name
            )
            assert gate["usename"] == "legacy_runtime"
            assert gate["application_name"] == application_name
            assert gate["client_addr"] == client_address
            assert gate["seed_writer"] is True
            record_property(
                "seed_parent_physical_cancellation_gate",
                {
                    "backend_pid": gate["pid"],
                    "role": gate["usename"],
                    "application_name": gate["application_name"],
                    "client_addr": gate["client_addr"],
                    "state": gate["state"],
                    "wait_event_type": gate["wait_event_type"],
                    "wait_event": gate["wait_event"],
                    "seed_writer": gate["seed_writer"],
                },
            )
            stopped = await asyncio.to_thread(cli.stop_current_container)
            assert stopped is True
            sleep_run = await asyncio.wait_for(asyncio.shield(sleep_task), timeout=35)
            record_property("seed_parent_physical_cancellation", sleep_run.evidence)
            assert sleep_run.exit_code in {137, 143}, sleep_run.evidence
            assert sleep_run.externally_stopped is True
            assert sleep_run.timed_out is False
            await _wait_for_membership_disconnect(
                test_engine,
                backend_pid=gate["pid"],
                client_address=client_address,
            )
            await _assert_pending_dataset_absent(test_engine, sleep_dataset)
            assert await _membership_rows(test_engine) == []
        finally:
            await _finish_pending_cli_task(sleep_task)
            if sleep_trigger is not None:
                await _drop_member_trigger(test_engine, sleep_trigger)
                sleep_trigger = None

        recovered_run = await _run_probe(cli, "memberships", account_ids["pm"])
        record_property("seed_parent_membership_recovery", recovered_run.evidence)
        assert recovered_run.exit_code == 0, recovered_run.stderr
        recovered = _probe_result(recovered_run)
        assert recovered["changed"] == 25
        assert recovered["commit_attempts"] == 1
        rows = await _membership_rows(test_engine)
        expected_pairs = {
            (display_id, email, role)
            for display_id in _PROJECTS
            for email, role in _MEMBERS.items()
        }
        assert len(rows) == 25
        assert {
            (row["display_id"], row["email"], row["role"]) for row in rows
        } == expected_pairs
        assert {row["assigned_by"] for row in rows} == {"pm"}
        assert all(
            row["version"] == 1
            and row["weekly_target"] is None
            and row["assigned_at"] is not None
            and row["updated_at"] is not None
            for row in rows
        )
        assert not any(row["email"] in {"admin", "pm"} for row in rows)
        record_property(
            "seed_parent_membership_recovery_state",
            {
                "rows": len(rows),
                "assigned_by": sorted({row["assigned_by"] for row in rows}),
                "version": sorted({row["version"] for row in rows}),
                "weekly_target": sorted(
                    {row["weekly_target"] for row in rows}, key=str
                ),
            },
        )

        anno_noop_id = rows[0]["id"]
        admin_id = uuid.UUID(account_ids["admin"])
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.project_members SET assigned_by=:admin, "
                    "updated_at=:old WHERE id=:id"
                ),
                {"admin": admin_id, "old": _OLD, "id": anno_noop_id},
            )
        async with test_engine.connect() as db:
            unchanged_before = (
                (
                    await db.execute(
                        text(
                            "SELECT role,assigned_by,version,weekly_target,assigned_at,updated_at "
                            "FROM public.project_members WHERE id=:id"
                        ),
                        {"id": anno_noop_id},
                    )
                )
                .mappings()
                .one()
            )

        pending_noop_dataset = f"SP-NOOP-{uuid.uuid4().hex[:8]}"
        noop_run = await _run_probe(
            cli,
            "memberships_pending",
            account_ids["pm"],
            pending_noop_dataset,
        )
        record_property("seed_parent_membership_noop", noop_run.evidence)
        assert noop_run.exit_code == 0, noop_run.stderr
        noop = _probe_result(noop_run)
        assert noop["changed"] == 0
        assert noop["commit_attempts"] == 0
        assert noop["pending_id_materialized"] is True
        await _assert_pending_dataset_absent(test_engine, pending_noop_dataset)
        async with test_engine.connect() as db:
            unchanged_after = (
                (
                    await db.execute(
                        text(
                            "SELECT role,assigned_by,version,weekly_target,assigned_at,updated_at "
                            "FROM public.project_members WHERE id=:id"
                        ),
                        {"id": anno_noop_id},
                    )
                )
                .mappings()
                .one()
            )
        assert unchanged_after == unchanged_before
        assert unchanged_after["assigned_by"] == admin_id
        assert unchanged_after["updated_at"] == unchanged_before["updated_at"]
        record_property(
            "seed_parent_noop_member_state",
            {
                "role": unchanged_after["role"],
                "assigned_by": str(unchanged_after["assigned_by"]),
                "updated_at": unchanged_after["updated_at"].isoformat(),
                "pending_dataset_id": noop["pending_id"],
                "pending_dataset_committed": False,
            },
        )

        qa_row = next(
            row
            for row in rows
            if row["display_id"] == _PROJECTS[0] and row["email"] == "qa"
        )
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.project_members SET role='viewer',assigned_by=:admin, "
                    "version=9,weekly_target=222,assigned_at=:old,updated_at=:old "
                    "WHERE id=:id"
                ),
                {"admin": admin_id, "old": _OLD, "id": qa_row["id"]},
            )
        repair_run = await _run_probe(cli, "memberships", account_ids["pm"])
        record_property("seed_parent_membership_repair", repair_run.evidence)
        assert repair_run.exit_code == 0, repair_run.stderr
        repair = _probe_result(repair_run)
        assert repair["changed"] == 1
        assert repair["commit_attempts"] == 1
        async with test_engine.connect() as db:
            qa = (
                (
                    await db.execute(
                        text(
                            "SELECT member.role,member.assigned_by,member.version, "
                            "member.weekly_target,member.assigned_at,member.updated_at, "
                            "assigner.email AS assigned_by_email "
                            "FROM public.project_members AS member "
                            "JOIN public.users AS assigner ON assigner.id=member.assigned_by "
                            "WHERE member.id=:id"
                        ),
                        {"id": qa_row["id"]},
                    )
                )
                .mappings()
                .one()
            )
        assert qa["role"] == "reviewer"
        assert qa["assigned_by"] == uuid.UUID(account_ids["pm"])
        assert qa["assigned_by_email"] == "pm"
        assert qa["version"] == 9
        assert qa["weekly_target"] == 222
        assert qa["assigned_at"].isoformat().startswith("2000-01-01T00:00:00")
        assert qa["updated_at"].isoformat().startswith("2000-") is False
        record_property(
            "seed_parent_repaired_member_state",
            {
                "role": qa["role"],
                "assigned_by": qa["assigned_by_email"],
                "version": qa["version"],
                "weekly_target": qa["weekly_target"],
                "assigned_at": qa["assigned_at"].isoformat(),
                "updated_at": qa["updated_at"].isoformat(),
            },
        )

        final_run = await _run_probe(cli, "memberships", account_ids["pm"])
        record_property("seed_parent_membership_idempotent", final_run.evidence)
        assert final_run.exit_code == 0, final_run.stderr
        final = _probe_result(final_run)
        assert final["changed"] == 0
        assert final["commit_attempts"] == 0
        assert len(await _membership_rows(test_engine)) == 25
        assert _bucket_versions(owner_s3, bucket) == bucket_before
        record_property(
            "seed_parent_dataset_bucket_unchanged",
            {"version_count_before": len(bucket_before), "version_count_after": 0},
        )
        record_property(
            "seed_parent_operator_scope",
            "Only _ensure_seed_accounts and _ensure_demo_memberships were invoked; "
            "parent media child entrypoints were not part of this qualification.",
        )
    finally:
        try:
            if sleep_task is not None and cli is not None:
                await _finish_pending_cli_task(sleep_task)
        finally:
            try:
                if sleep_trigger is not None:
                    await _drop_member_trigger(test_engine, sleep_trigger)
            finally:
                try:
                    if cli is not None:
                        await asyncio.to_thread(cli.close)
                finally:
                    await asyncio.to_thread(destroy_scope, scope)
