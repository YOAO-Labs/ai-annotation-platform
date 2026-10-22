"""Cold Identity-only acceptance for installed media seed consumers."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
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
    _assert_empty_owned_rows,
    _create_datasets_storage_scope,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MEDIA_SEED_IDENTITY_LIVE") != "1",
        reason="requires the explicitly enabled owned media-seed Identity verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "apps/api/tests/fixtures/media_seed_identity_probe.py"
_READ_PROVISIONING = "aap_identity_v1.read_provisioning_account(text)"
_CREATE_PERSONA = "aap_identity_v1.create_seed_persona_account(jsonb)"
_IDENTITY_OPERATOR_FUNCTIONS = {
    *_IDENTITY_CONSUMER_FUNCTIONS,
    _READ_PROVISIONING,
    _CREATE_PERSONA,
}
_LARGE_PROJECT_DISPLAY_ID = "P-LARGE-IMG"
_LARGE_DATASET_DISPLAY_ID = "DS-LARGE-IMG"
_NULL_USER_FIELDS = (
    "disabled_kind",
    "disabled_at",
    "disabled_by",
    "disabled_reason",
    "email_verified_at",
    "last_login_at",
    "last_seen_at",
    "password_admin_reset_at",
    "deactivation_requested_at",
    "deactivation_reason",
    "deactivation_scheduled_at",
    "weekly_target_default",
    "avatar_ref",
)


def _probe_result(run) -> dict:
    prefix = "AAP_MEDIA_SEED_IDENTITY_RESULT="
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


async def _assert_media_identity_profile(test_engine) -> None:
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
    runtime = create_async_engine(runtime_url, hide_parameters=True)
    try:
        async with runtime.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
            role_flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls "
                        "FROM pg_catalog.pg_roles WHERE rolname=current_user"
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
            assert await _function_set(db, "legacy_runtime", "aap_identity_v1") == (
                _IDENTITY_OPERATOR_FUNCTIONS
            )
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'aap_identity_v1.account_profiles', 'SELECT')"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user,'public.users',"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user,'public.users',"
                    "'SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
            for table in (
                "public.projects",
                "public.datasets",
                "public.project_datasets",
            ):
                for privilege in ("SELECT", "INSERT"):
                    assert await db.scalar(
                        text(
                            "SELECT pg_catalog.has_table_privilege(current_user, "
                            ":table_name, :privilege)"
                        ),
                        {"table_name": table, "privilege": privilege},
                    ), (table, privilege)
    finally:
        await runtime.dispose()

    async with test_engine.connect() as db:
        assert await _function_set(
            db, "identity_api_consumer", "aap_identity_v1"
        ) == set(_IDENTITY_CONSUMER_FUNCTIONS)
        for signature in (_READ_PROVISIONING, _CREATE_PERSONA):
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege('identity_api_consumer', "
                    "CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
        assert not await db.scalar(
            text(
                "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                "'public.users','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
            )
        )


async def _create_screenshot_users(test_engine) -> dict[str, uuid.UUID]:
    from app.core.security import hash_password
    from app.services.screenshot_seed_spec import USER_SPECS

    ids = {email: uuid.uuid4() for email, _role in USER_SPECS.values()}
    password_hash = hash_password("Screenshot-Seed-Fixture-1234")
    async with test_engine.begin() as db:
        for email, role in USER_SPECS.values():
            await db.execute(
                text(
                    "INSERT INTO public.users "
                    "(id,email,name,password_hash,credential_generation,role,group_name,"
                    "group_id,status,is_active,preferences) "
                    "VALUES (:id,:email,:name,:password_hash,0,:role,NULL,NULL,"
                    "'offline',true,'{}'::jsonb)"
                ),
                {
                    "id": ids[email],
                    "email": email,
                    "name": email,
                    "password_hash": password_hash,
                    "role": role,
                },
            )
    return ids


async def _user_state(test_engine, email: str) -> dict:
    from app.core.security import verify_password

    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT id,email,name,role,group_name,group_id,status,is_active,"
                        "credential_generation,disabled_kind,disabled_at,disabled_by,"
                        "disabled_reason,email_verified_at,last_login_at,last_seen_at,"
                        "password_admin_reset_at,deactivation_requested_at,deactivation_reason,"
                        "deactivation_scheduled_at,weekly_target_default,avatar_ref,"
                        "preferences,password_hash,created_at FROM public.users WHERE email=:email"
                    ),
                    {"email": email},
                )
            )
            .mappings()
            .one()
        )
    state = dict(row)
    password_hash = state.pop("password_hash")
    created_at = state.pop("created_at")
    state["id"] = str(state["id"])
    state["password_matches_seed"] = verify_password("123456", password_hash)
    state["created_at_present"] = created_at is not None
    return state


async def _assert_admin_defaults(
    test_engine,
    *,
    email: str,
    account_id: str,
    name: str,
) -> dict:
    state = await _user_state(test_engine, email)
    assert state["id"] == account_id
    assert state["name"] == name
    assert state["role"] == "super_admin"
    assert state["group_name"] is None and state["group_id"] is None
    assert state["status"] == "offline" and state["is_active"] is True
    assert state["credential_generation"] == 0
    assert state["preferences"] == {}
    assert all(state[field] is None for field in _NULL_USER_FIELDS)
    assert state["password_matches_seed"] is True
    assert state["created_at_present"] is True
    return {
        "email": email,
        "id": account_id,
        "name": name,
        "role": state["role"],
        "group_name": state["group_name"],
        "group_id": state["group_id"],
        "status": state["status"],
        "is_active": state["is_active"],
        "credential_generation": state["credential_generation"],
        "preferences": state["preferences"],
        "lifecycle_fields_null": True,
        "password_matches_seed": state["password_matches_seed"],
    }


async def _run_probe(cli: WorkspaceImportCli, operation: str, *arguments: str):
    return await asyncio.to_thread(
        cli.run_command,
        ("python", f"/fixture/{PROBE.name}", operation, *arguments),
    )


async def _install_user_trigger(
    test_engine, *, email: str, mode: str
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    function_name = f"aap_test_media_identity_{suffix}"
    trigger_name = f"aap_test_media_identity_{suffix}"
    if mode == "raise":
        action = (
            "RAISE EXCEPTION USING ERRCODE = 'P0001', "
            "MESSAGE = 'controlled media seed user insert failure';"
        )
    elif mode == "sleep":
        action = "PERFORM pg_catalog.pg_sleep(10);"
    else:
        raise ValueError("unsupported media seed trigger mode")
    body = f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trigger$
BEGIN
    IF NEW.email = '{email}' THEN
        {action}
    END IF;
    RETURN NEW;
END
$trigger$"""
    async with test_engine.begin() as db:
        await db.execute(text(body))
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.users "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
            )
        )
    return function_name, trigger_name


async def _drop_user_trigger(test_engine, names: tuple[str, str]) -> None:
    function_name, trigger_name = names
    async with test_engine.begin() as db:
        await db.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.users"))
        await db.execute(text(f"DROP FUNCTION IF EXISTS public.{function_name}()"))


async def _media_gate_activity(test_engine, *, application_name: str) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('create_seed_persona_account' in query)>0 AS seed_writer "
                        "FROM pg_catalog.pg_stat_activity WHERE datname=current_database() "
                        "AND usename='legacy_runtime' AND application_name=:application_name"
                    ),
                    {"application_name": application_name},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_media_insert_gate(
    test_engine,
    cli: WorkspaceImportCli,
    run_task: asyncio.Task,
    application_name: str,
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + 20
    while cli.container_id is None and not run_task.done():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("media seed one-shot did not create its owned container")
        await asyncio.sleep(0.02)
    if run_task.done():
        pytest.fail(
            f"media seed one-shot exited before its SQL gate: {run_task.result()}"
        )

    while asyncio.get_running_loop().time() < deadline:
        activity = await _media_gate_activity(
            test_engine, application_name=application_name
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
            assert activity["client_host"] == client_address
            return client_address, activity
        if run_task.done():
            pytest.fail(
                f"media seed one-shot exited before its SQL gate: {run_task.result()}"
            )
        await asyncio.sleep(0.04)
    pytest.fail("media seed did not reach its owned pg_sleep trigger")


async def _wait_for_backend_exit(test_engine, backend_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            alive = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=:pid)"
                ),
                {"pid": backend_pid},
            )
        if not alive:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped media seed backend is still present in pg_stat_activity")


async def _finish_pending_cli_task(task: asyncio.Task) -> None:
    # Join the bounded SQL gate before close(), including failed startup, so
    # separate threads never race to remove the same owned container.
    await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def _create_screenshot_fixture_users(test_engine) -> dict[str, uuid.UUID]:
    from app.core.security import hash_password
    from app.services.screenshot_seed_spec import USER_SPECS

    ids = {email: uuid.uuid4() for email, _role in USER_SPECS.values()}
    password_hash = hash_password("Screenshot-Seed-Fixture-1234")
    async with test_engine.begin() as db:
        for email, role in USER_SPECS.values():
            await db.execute(
                text(
                    "INSERT INTO public.users "
                    "(id,email,name,password_hash,credential_generation,role,group_name,"
                    "group_id,status,is_active,preferences) "
                    "VALUES (:id,:email,:name,:password_hash,0,:role,NULL,NULL,"
                    "'offline',true,'{}'::jsonb)"
                ),
                {
                    "id": ids[email],
                    "email": email,
                    "name": email,
                    "password_hash": password_hash,
                    "role": role,
                },
            )
    return ids


async def _large_graph_state(test_engine) -> dict:
    async with test_engine.connect() as db:
        project = (
            (
                await db.execute(
                    text(
                        "SELECT id,owner_id FROM public.projects WHERE display_id=:display_id"
                    ),
                    {"display_id": _LARGE_PROJECT_DISPLAY_ID},
                )
            )
            .mappings()
            .first()
        )
        dataset = (
            (
                await db.execute(
                    text(
                        "SELECT id,created_by FROM public.datasets WHERE display_id=:display_id"
                    ),
                    {"display_id": _LARGE_DATASET_DISPLAY_ID},
                )
            )
            .mappings()
            .first()
        )
    return {
        "project": None
        if project is None
        else {key: str(value) for key, value in project.items()},
        "dataset": None
        if dataset is None
        else {key: str(value) for key, value in dataset.items()},
    }


async def test_media_seed_identity_consumers_use_owned_identity_capabilities(
    test_engine, tmp_path, record_property
):
    from app.config import settings
    from app.core.security import hash_password, verify_password
    from app.services.screenshot_seed_spec import USER_SPECS

    _verify_postgres(test_engine.url)
    await _assert_empty_owned_rows(test_engine)
    await _assert_media_identity_profile(test_engine)

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    bucket_before = _bucket_versions(owner_s3, bucket)
    assert bucket_before == []

    source_root = tmp_path / "media-seed-identity-source"
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
    gate_task = None
    gate_trigger = None
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

        suffix = uuid.uuid4().hex[:12]
        helper_emails = {
            "coco8": f"media-coco8-{suffix}@cold.test",
            "video": f"media-video-{suffix}@cold.test",
            "ocr": f"media-ocr-{suffix}@cold.test",
        }
        helper_names = {
            "coco8": "超级管理员",
            "video": "超级管理员",
            "ocr": "Admin",
        }
        account_ids: dict[str, str] = {}
        for kind in ("coco8", "video", "ocr"):
            run = await _run_probe(cli, "admin", kind, helper_emails[kind])
            record_property(f"media_seed_{kind}_admin_create", run.evidence)
            assert run.exit_code == 0, run.stderr
            result = _probe_result(run)
            assert result["commit_attempts"] == 1
            account_ids[kind] = result["owner_id"]
            facts = await _assert_admin_defaults(
                test_engine,
                email=helper_emails[kind],
                account_id=result["owner_id"],
                name=helper_names[kind],
            )
            record_property(f"media_seed_{kind}_admin_defaults", facts)
        async with test_engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM public.groups")) == 0

        kept_password = "Keep-Media-Seed-Owner-1234"
        kept_hash = hash_password(kept_password)
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET name='existing media owner',role='viewer',"
                    "group_name='legacy media group',is_active=false,password_hash=:hash "
                    "WHERE id=:id"
                ),
                {"id": uuid.UUID(account_ids["coco8"]), "hash": kept_hash},
            )
        reused_run = await _run_probe(cli, "admin", "coco8", helper_emails["coco8"])
        record_property("media_seed_existing_identity_reuse", reused_run.evidence)
        assert reused_run.exit_code == 0, reused_run.stderr
        reused = _probe_result(reused_run)
        assert reused["owner_id"] == account_ids["coco8"]
        async with test_engine.connect() as db:
            reused_pm = (
                await db.execute(
                    text(
                        "SELECT id,name,role,group_name,is_active,password_hash "
                        "FROM public.users WHERE email=:email"
                    ),
                    {"email": helper_emails["coco8"]},
                )
            ).one()
        assert reused_pm[:5] == (
            uuid.UUID(account_ids["coco8"]),
            "existing media owner",
            "viewer",
            "legacy media group",
            False,
        )
        assert verify_password(kept_password, reused_pm[5]) is True
        record_property(
            "media_seed_reused_identity_state",
            {
                "id": account_ids["coco8"],
                "name": reused_pm[1],
                "role": reused_pm[2],
                "group_name": reused_pm[3],
                "is_active": reused_pm[4],
                "password_preserved": True,
            },
        )

        pending_owner_id = uuid.UUID(account_ids["video"])
        missing_email = f"media-missing-{suffix}@cold.test"
        missing_dataset = f"MS-MISS-{suffix[:8]}"
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"REVOKE EXECUTE ON FUNCTION {_CREATE_PERSONA} FROM legacy_runtime"
                )
            )
        try:
            missing_run = await _run_probe(
                cli,
                "admin",
                "coco8",
                missing_email,
                missing_dataset,
                str(pending_owner_id),
            )
            record_property(
                "media_seed_identity_capability_denied", missing_run.evidence
            )
            assert missing_run.exit_code != 0
            missing = _probe_result(missing_run)
            assert (
                "permission denied for function create_seed_persona_account"
                in missing["error"].lower()
            ), missing
            assert missing["pending_id_materialized"] is True
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text("SELECT id FROM public.users WHERE email=:email"),
                        {"email": missing_email},
                    )
                    is None
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM public.datasets WHERE display_id=:display_id"
                        ),
                        {"display_id": missing_dataset},
                    )
                    is None
                )
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION {_CREATE_PERSONA} TO legacy_runtime"
                    )
                )

        trigger_email = f"media-late-{suffix}@cold.test"
        trigger_dataset = f"MS-LATE-{suffix[:8]}"
        late_trigger = await _install_user_trigger(
            test_engine, email=trigger_email, mode="raise"
        )
        try:
            late_run = await _run_probe(
                cli,
                "admin",
                "video",
                trigger_email,
                trigger_dataset,
                str(pending_owner_id),
            )
            record_property("media_seed_late_insert_rollback", late_run.evidence)
            assert late_run.exit_code != 0
            late = _probe_result(late_run)
            assert "controlled media seed user insert failure" in late["error"]
            assert late["pending_id_materialized"] is True
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text("SELECT id FROM public.users WHERE email=:email"),
                        {"email": trigger_email},
                    )
                    is None
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM public.datasets WHERE display_id=:display_id"
                        ),
                        {"display_id": trigger_dataset},
                    )
                    is None
                )
        finally:
            await _drop_user_trigger(test_engine, late_trigger)

        sleeping_email = f"media-sleep-{suffix}@cold.test"
        sleeping_dataset = f"MS-SLEEP-{suffix[:8]}"
        application_name = f"aap-media-seed-{uuid.uuid4().hex}"
        sleep_trigger = await _install_user_trigger(
            test_engine, email=sleeping_email, mode="sleep"
        )
        gate_trigger = sleep_trigger
        gate_task = asyncio.create_task(
            asyncio.to_thread(
                cli.run_command,
                (
                    "python",
                    f"/fixture/{PROBE.name}",
                    "admin",
                    "ocr",
                    sleeping_email,
                    sleeping_dataset,
                    str(pending_owner_id),
                    application_name,
                ),
            )
        )
        try:
            client_address, activity = await _wait_for_media_insert_gate(
                test_engine, cli, gate_task, application_name
            )
            backend_pid = activity["pid"]
            assert activity["usename"] == "legacy_runtime"
            assert activity["application_name"] == application_name
            assert activity["client_host"] == client_address
            assert activity["seed_writer"] is True
            record_property(
                "media_seed_physical_cancellation_gate",
                {
                    "role": activity["usename"],
                    "application_name": activity["application_name"],
                    "client_host": activity["client_host"],
                    "backend_pid": backend_pid,
                    "state": activity["state"],
                    "wait_event_type": activity["wait_event_type"],
                    "wait_event": activity["wait_event"],
                    "seed_writer": activity["seed_writer"],
                },
            )
            assert await asyncio.to_thread(cli.stop_current_container) is True
            stopped_run = await asyncio.wait_for(asyncio.shield(gate_task), timeout=35)
            record_property("media_seed_physical_cancellation", stopped_run.evidence)
            assert stopped_run.exit_code in {137, 143}, stopped_run.evidence
            assert stopped_run.externally_stopped is True
            assert stopped_run.timed_out is False
            await _wait_for_backend_exit(test_engine, backend_pid)
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text("SELECT id FROM public.users WHERE email=:email"),
                        {"email": sleeping_email},
                    )
                    is None
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM public.datasets WHERE display_id=:display_id"
                        ),
                        {"display_id": sleeping_dataset},
                    )
                    is None
                )
        finally:
            await _finish_pending_cli_task(gate_task)
            if gate_trigger is not None:
                await _drop_user_trigger(test_engine, gate_trigger)
                gate_trigger = None

        recovery_run = await _run_probe(cli, "admin", "ocr", sleeping_email)
        record_property("media_seed_same_email_recovery", recovery_run.evidence)
        assert recovery_run.exit_code == 0, recovery_run.stderr
        recovered = _probe_result(recovery_run)
        recovered_facts = await _assert_admin_defaults(
            test_engine,
            email=sleeping_email,
            account_id=recovered["owner_id"],
            name="Admin",
        )
        record_property("media_seed_recovered_admin_defaults", recovered_facts)
        recovered_reuse_run = await _run_probe(cli, "admin", "ocr", sleeping_email)
        record_property(
            "media_seed_recovered_same_email_reuse", recovered_reuse_run.evidence
        )
        assert recovered_reuse_run.exit_code == 0, recovered_reuse_run.stderr
        recovered_reuse = _probe_result(recovered_reuse_run)
        assert recovered_reuse["owner_id"] == recovered["owner_id"]
        await _assert_admin_defaults(
            test_engine,
            email=sleeping_email,
            account_id=recovered["owner_id"],
            name="Admin",
        )

        screenshot_ids = await _create_screenshot_fixture_users(test_engine)
        screenshot_run = await _run_probe(cli, "screenshot_users")
        record_property("media_seed_screenshot_users", screenshot_run.evidence)
        assert screenshot_run.exit_code == 0, screenshot_run.stderr
        screenshot = _probe_result(screenshot_run)
        assert screenshot["user_ids"] == {
            key: str(screenshot_ids[email])
            for key, (email, _role) in USER_SPECS.items()
        }

        baseline_catalog_run = await _run_probe(cli, "screenshot_catalog")
        record_property(
            "media_seed_catalog_without_projects", baseline_catalog_run.evidence
        )
        assert baseline_catalog_run.exit_code == 0, baseline_catalog_run.stderr
        baseline_catalog = _probe_result(baseline_catalog_run)
        assert baseline_catalog["ready"] is False
        assert not [
            issue for issue in baseline_catalog["issues"] if issue.startswith("user ")
        ]
        assert any(
            "project is missing or duplicated" in issue
            for issue in baseline_catalog["issues"]
        )

        screenshot_pm = USER_SPECS["project_admin"][0]
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET role='viewer',is_active=false "
                    "WHERE email=:email"
                ),
                {"email": screenshot_pm},
            )
        bad_users_run = await _run_probe(cli, "screenshot_users")
        record_property("media_seed_screenshot_wrong_role", bad_users_run.evidence)
        assert bad_users_run.exit_code != 0
        bad_users = _probe_result(bad_users_run)
        assert "seed user pm with role project_admin is required" in bad_users["error"]

        drift_catalog_run = await _run_probe(cli, "screenshot_catalog")
        record_property("media_seed_catalog_identity_drift", drift_catalog_run.evidence)
        assert drift_catalog_run.exit_code == 0, drift_catalog_run.stderr
        drift_catalog = _probe_result(drift_catalog_run)
        assert [
            issue for issue in drift_catalog["issues"] if issue.startswith("user ")
        ] == [
            "user pm must have role project_admin, got viewer",
            "user pm must be active",
        ]

        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET role='project_admin',is_active=true "
                    "WHERE email=:email"
                ),
                {"email": screenshot_pm},
            )
        recovered_screenshot_run = await _run_probe(cli, "screenshot_users")
        record_property(
            "media_seed_screenshot_users_recovery", recovered_screenshot_run.evidence
        )
        assert recovered_screenshot_run.exit_code == 0, recovered_screenshot_run.stderr
        recovered_screenshot = _probe_result(recovered_screenshot_run)
        assert recovered_screenshot["user_ids"] == screenshot["user_ids"]
        ready_catalog_run = await _run_probe(cli, "screenshot_catalog")
        record_property(
            "media_seed_catalog_identity_recovery", ready_catalog_run.evidence
        )
        assert ready_catalog_run.exit_code == 0, ready_catalog_run.stderr
        ready_catalog = _probe_result(ready_catalog_run)
        assert ready_catalog["ready"] is False
        assert not [
            issue for issue in ready_catalog["issues"] if issue.startswith("user ")
        ]
        assert any(
            "project is missing or duplicated" in issue
            for issue in ready_catalog["issues"]
        )

        manifest_version = 1
        graph_run = await _run_probe(
            cli,
            "large_graph",
            helper_emails["coco8"],
            str(manifest_version),
        )
        record_property("media_seed_large_graph_create", graph_run.evidence)
        assert graph_run.exit_code == 0, graph_run.stderr
        graph = _probe_result(graph_run)
        assert graph["project"]["owner_id"] == account_ids["coco8"]
        assert graph["dataset"]["created_by"] == account_ids["coco8"]
        graph_state = await _large_graph_state(test_engine)
        assert graph_state["project"]["id"] == graph["project"]["id"]
        assert graph_state["dataset"]["id"] == graph["dataset"]["id"]
        assert graph_state["project"]["owner_id"] == account_ids["coco8"]
        assert graph_state["dataset"]["created_by"] == account_ids["coco8"]
        record_property("media_seed_large_graph_identity_facts", graph_state)

        repeat_graph_run = await _run_probe(
            cli,
            "large_graph",
            helper_emails["coco8"],
            str(manifest_version),
        )
        record_property("media_seed_large_graph_repeat", repeat_graph_run.evidence)
        assert repeat_graph_run.exit_code == 0, repeat_graph_run.stderr
        repeat_graph = _probe_result(repeat_graph_run)
        assert repeat_graph["project"] == graph["project"]
        assert repeat_graph["dataset"] == graph["dataset"]
        assert await _large_graph_state(test_engine) == graph_state

        missing_owner = f"media-no-owner-{suffix}@cold.test"
        missing_graph_run = await _run_probe(
            cli,
            "large_graph",
            missing_owner,
            str(manifest_version),
        )
        record_property(
            "media_seed_large_graph_missing_owner", missing_graph_run.evidence
        )
        assert missing_graph_run.exit_code != 0
        missing_graph = _probe_result(missing_graph_run)
        assert f"owner {missing_owner!r} is missing" in missing_graph["error"]
        assert await _large_graph_state(test_engine) == graph_state
        assert _bucket_versions(owner_s3, bucket) == bucket_before
        record_property(
            "media_seed_identity_scope",
            "Only media-seed Identity lookups/creators, screenshot identity validation, "
            "and large-image owner resolution were qualified; media workflows and "
            "Project/Dataset policy remain outside this slice.",
        )
    finally:
        try:
            if gate_task is not None and cli is not None:
                await _finish_pending_cli_task(gate_task)
        finally:
            try:
                if gate_trigger is not None:
                    await _drop_user_trigger(test_engine, gate_trigger)
            finally:
                try:
                    if cli is not None:
                        await asyncio.to_thread(cli.close)
                finally:
                    await asyncio.to_thread(destroy_scope, scope)
