"""Real standalone Workspace processes and public API bindings on owned state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.audit_log import AuditLog
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.workspace_membership import (
    add_member,
    change_role,
    preview_role_change,
)
from tests.factory import create_membership, create_project, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)
from tests.test_identity_admission_live import (
    Container,
    _port,
    _start,
    _start_api,
)
from tests.test_module_data_roles import _role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_MEMBERSHIP_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned Workspace membership verifier",
    ),
]


def _workspace_dsn(test_engine):
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_WORKSPACE_MEMBERSHIP_URL", "workspace_membership_client"
        )
    )
    assert (value.host, value.port, value.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert value.database.endswith("_test")
    _verify_postgres(value)
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


def _secret(path: Path, value: str, *, container=False):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o444 if container else 0o600)
    return path


async def _start_workspace(test_engine, directory, control):
    directory.mkdir(parents=True, exist_ok=True)
    dsn_file = _secret(directory / "membership-dsn", _workspace_dsn(test_engine))
    token_file = _secret(directory / "membership-control", control)
    interpreter = Path(os.environ["AAP_WORKSPACE_RUNTIME_PYTHON"])
    assert interpreter.is_file()
    port = _port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "WORKSPACE_HOST": "127.0.0.1",
        "WORKSPACE_PORT": str(port),
        "WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE": str(dsn_file),
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": str(token_file),
    }
    return await asyncio.to_thread(
        _start,
        [str(interpreter), "-I", "-m", "aap_workspace"],
        environment,
        directory,
        directory / "workspace.log",
        port,
        "/ready",
    )


async def _start_container_workspace(test_engine, directory, control):
    directory.mkdir(parents=True, exist_ok=True)
    identity = os.environ["AAP_WORKSPACE_RUNTIME_IDENTITY"]
    image = _inspect_docker(os.environ["AAP_WORKSPACE_RUNTIME_IMAGE"])
    assert image["Config"]["Labels"]["org.aap.acceptance"] == "workspace-membership"
    assert image["Config"]["Labels"]["org.aap.acceptance-id"] == identity
    dsn = (
        make_url(_workspace_dsn(test_engine))
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    dsn_file = _secret(directory / "membership-dsn", dsn, container=True)
    token_file = _secret(directory / "membership-control", control, container=True)
    owner = secrets.token_hex(12)
    container_id = _docker(
        "run",
        "--detach",
        "--name",
        f"aap-workspace-membership-{owner}",
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={owner}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=32m",
        "--publish",
        "127.0.0.1::8172",
        "--mount",
        f"type=bind,src={dsn_file},dst=/run/secrets/membership-dsn,readonly",
        "--mount",
        f"type=bind,src={token_file},dst=/run/secrets/membership-control,readonly",
        "--env",
        "WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE=/run/secrets/membership-dsn",
        "--env",
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/membership-control",
        image["Id"],
    ).strip()
    try:
        metadata = _inspect_docker(container_id)
        port = metadata["NetworkSettings"]["Ports"]["8172/tcp"][0]["HostPort"]
        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 35
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    if (await client.get(origin + "/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert _inspect_docker(container_id)["State"]["Running"], (
                    "Owned Workspace container exited"
                )
                assert time.monotonic() < deadline, (
                    "Owned Workspace container did not become ready"
                )
                await asyncio.sleep(0.1)
        return Container(container_id, owner, origin)
    except BaseException:
        _stop_remove_container(container_id, owner)
        raise


async def _start_workspace_api(
    test_engine, directory, *, workspace_url=None, workspace_dsn=None, control
):
    directory.mkdir(parents=True, exist_ok=True)
    _verify_postgres(test_engine.url)
    runtime = _role_url("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime")
    value = make_url(runtime)
    assert (value.host, value.port, value.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    configuration = {
        "WORKSPACE_MEMBERSHIP_URL": workspace_url or "",
        "WORKSPACE_MEMBERSHIP_DATABASE_URL": workspace_dsn or "",
        "WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE": "",
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN": control,
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "",
        "AUDIT_ASYNC": "false",
    }
    return await asyncio.to_thread(
        _start_api,
        directory,
        settings.secret_key,
        "",
        "",
        database_url=runtime,
        configuration=configuration,
    )


def _command(operation, actor_id, project_id, payload, *, operation_id=None):
    return {
        "schema": "aap-workspace-membership/v1",
        "operation_id": str(operation_id or uuid.uuid4()),
        "operation": operation,
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "payload": payload,
        "audit_context": {"method": None, "path": None, "ip": None, "request_id": None},
    }


async def _seed_owned_member_scope(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        suffix = uuid.uuid4().hex
        actor = await create_user(
            db, "project_admin", f"workspace-owner-{suffix}@example.test", "Owner"
        )
        employee = await create_user(
            db, "employee", f"workspace-member-{suffix}@example.test", "Member"
        )
        project = await create_project(db, owner_id=actor.id)
        return maker, actor.id, employee.id, project.id


@pytest.mark.parametrize("autoflush", [False, True])
async def test_rejected_local_command_never_commits_unrelated_pending_work(
    test_engine, monkeypatch, autoflush
):
    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    async with maker.begin() as db:
        await create_membership(
            db,
            project_id=project_id,
            user_id=employee_id,
            role="annotator",
            assigned_by=actor_id,
        )
    pending_id = uuid.uuid4()
    async with maker(autoflush=autoflush) as caller:
        pending = User(
            id=pending_id,
            email=f"pending-{pending_id}@example.test",
            name="Caller work",
            role="viewer",
            password_hash="fixture",
            is_active=True,
        )
        caller.add(pending)
        with pytest.raises(HTTPException) as rejected:
            await add_member(
                caller,
                project_id=project_id,
                actor_id=actor_id,
                target_user_id=employee_id,
                project_role="annotator",
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "该用户已在项目中"
        assert caller.in_transaction()
        if not autoflush:
            assert pending in caller.new
        async with maker() as observer:
            assert await observer.get(User, pending_id) is None
        await caller.rollback()


async def test_local_account_contention_rolls_back_unrelated_caller_work(
    test_engine, monkeypatch
):
    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    pending_id = uuid.uuid4()
    async with maker() as holder, maker() as caller:
        await holder.scalar(select(User).where(User.id == actor_id).with_for_update())
        caller.add(
            User(
                id=pending_id,
                email=f"busy-pending-{pending_id}@example.test",
                name="Caller work",
                role="viewer",
                password_hash="fixture",
                is_active=True,
            )
        )
        with pytest.raises(HTTPException) as rejected:
            await add_member(
                caller,
                project_id=project_id,
                actor_id=actor_id,
                target_user_id=employee_id,
                project_role="annotator",
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "账号正在更新，请刷新后重试"
        assert not caller.in_transaction()
        async with maker() as observer:
            assert await observer.get(User, pending_id) is None
        await holder.rollback()


async def test_native_audit_rejection_requires_explicit_caller_rollback(
    test_engine, monkeypatch
):
    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    _verify_postgres(test_engine.url)
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    trigger = f"workspace_audit_rejection_{uuid.uuid4().hex}"
    pending_id = uuid.uuid4()
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE FUNCTION public.{trigger}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN IF NEW.actor_id='{actor_id}'::uuid THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='workspace_test_audit_rejected'; END IF; RETURN NEW; END; $body$"
            )
        )
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.{trigger}()"
            )
        )
    try:
        async with maker() as caller:
            caller.add(
                User(
                    id=pending_id,
                    email=f"audit-pending-{pending_id}@example.test",
                    name="Caller work",
                    role="viewer",
                    password_hash="fixture",
                    is_active=True,
                )
            )
            with pytest.raises(DBAPIError) as rejected:
                await add_member(
                    caller,
                    project_id=project_id,
                    actor_id=actor_id,
                    target_user_id=employee_id,
                    project_role="annotator",
                )
            assert rejected.value.orig.sqlstate == "23514"
            with pytest.raises(DBAPIError) as aborted:
                await caller.scalar(text("SELECT 1"))
            assert aborted.value.orig.sqlstate == "25P02"
            await caller.rollback()
            assert await caller.scalar(text("SELECT 1")) == 1
        async with maker() as observer:
            assert await observer.get(User, pending_id) is None
            assert (
                await observer.scalar(
                    select(func.count(ProjectMember.id)).where(
                        ProjectMember.project_id == project_id
                    )
                )
                == 0
            )
            assert (
                await observer.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
                )
                == 0
            )
    finally:
        async with test_engine.begin() as db:
            await db.execute(text(f"DROP TRIGGER {trigger} ON public.audit_logs"))
            await db.execute(text(f"DROP FUNCTION public.{trigger}()"))


@pytest.mark.parametrize("binding", ["caller", "dedicated"])
@pytest.mark.parametrize("field", ["preview_token", "reason"])
@pytest.mark.parametrize("expected_version", [1, 2])
async def test_opaque_nul_text_keeps_legacy_conflict_precedence(
    test_engine, monkeypatch, binding, field, expected_version
):
    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "workspace_membership_database_url",
        _workspace_dsn(test_engine) if binding == "dedicated" else "",
    )
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    async with maker.begin() as db:
        member = await create_membership(
            db,
            project_id=project_id,
            user_id=employee_id,
            role="annotator",
            assigned_by=actor_id,
        )
        member_id = member.id
    async with maker() as caller:
        assert (
            await caller.scalar(
                text("SELECT json_typeof(CAST(:literal AS json))"),
                {"literal": json.dumps("\u0000", ensure_ascii=True)},
            )
            == "string"
        )
        with pytest.raises(HTTPException) as rejected:
            await change_role(
                caller,
                project_id=project_id,
                actor_id=actor_id,
                member_id=member_id,
                target_role="viewer",
                expected_version=expected_version,
                preview_token="\u0000" if field == "preview_token" else "0" * 64,
                reason="\u0000" if field == "reason" else "Conflict precedence",
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail["reason"] == (
            "stale_member_version"
            if expected_version == 2
            else "stale_resource_snapshot"
        )
    async with maker() as observer:
        member = await observer.get(ProjectMember, member_id)
        assert member.role == "annotator" and member.version == 1
        assert (
            await observer.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
            )
            == 0
        )


@pytest.mark.parametrize("binding", ["caller", "dedicated"])
async def test_current_nul_reason_still_aborts_at_audit_without_effects(
    test_engine, monkeypatch, binding
):
    import asyncpg

    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "workspace_membership_database_url",
        _workspace_dsn(test_engine) if binding == "dedicated" else "",
    )
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    async with maker.begin() as db:
        member = await create_membership(
            db,
            project_id=project_id,
            user_id=employee_id,
            role="annotator",
            assigned_by=actor_id,
        )
        member_id = member.id
    async with maker() as caller:
        preview = await preview_role_change(
            caller,
            project_id=project_id,
            actor_id=actor_id,
            member_id=member_id,
            target_role="viewer",
        )
        assert preview["blockers"] == []
        with pytest.raises((DBAPIError, asyncpg.PostgresError)) as rejected:
            await change_role(
                caller,
                project_id=project_id,
                actor_id=actor_id,
                member_id=member_id,
                target_role="viewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                reason="\u0000",
            )
        original = (
            rejected.value.orig
            if isinstance(rejected.value, DBAPIError)
            else rejected.value
        )
        assert original.sqlstate == "22P05"
        if binding == "caller":
            with pytest.raises(DBAPIError) as aborted:
                await caller.scalar(text("SELECT 1"))
            assert aborted.value.orig.sqlstate == "25P02"
        else:
            assert await caller.scalar(text("SELECT 1")) == 1
        await caller.rollback()
    async with maker() as observer:
        member = await observer.get(ProjectMember, member_id)
        assert member.role == "annotator" and member.version == 1
        assert (
            await observer.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
            )
            == 0
        )
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_membership_operation_receipts WHERE actor_id=:actor"
                ),
                {"actor": actor_id},
            )
            == 0
        )


@pytest.mark.parametrize("binding", ["caller", "dedicated"])
async def test_literal_reason_and_receipt_fingerprint_keep_exact_text(
    test_engine, monkeypatch, binding
):
    from aap_workspace.contract import decode_membership_request
    from aap_workspace.manager import MembershipManager
    from aap_workspace.postgres import MembershipPostgresData
    from app.services.workspace_membership_data import WorkspaceMembershipData

    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    dsn = _workspace_dsn(test_engine)
    monkeypatch.setattr(
        settings,
        "workspace_membership_database_url",
        dsn if binding == "dedicated" else "",
    )
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    async with maker.begin() as db:
        member = await create_membership(
            db,
            project_id=project_id,
            user_id=employee_id,
            role="annotator",
            assigned_by=actor_id,
        )
        member_id = member.id
    reasons = ["\\u0000", "null", "\u007f", '  "quote" \\ \n  ', "ß 中 😀"]
    async with maker() as caller:
        manager = MembershipManager(
            MembershipPostgresData(dsn)
            if binding == "dedicated"
            else WorkspaceMembershipData(caller)
        )
        for index, reason in enumerate(reasons):
            role = "viewer" if index % 2 == 0 else "annotator"
            preview = await preview_role_change(
                caller,
                project_id=project_id,
                actor_id=actor_id,
                member_id=member_id,
                target_role=role,
            )
            request = decode_membership_request(
                _command(
                    "change_role",
                    actor_id,
                    project_id,
                    {
                        "member_id": str(member_id),
                        "target_role": role,
                        "expected_version": index + 1,
                        "preview_token": preview["preview_token"],
                        "reason": reason,
                        "replacement_annotator_id": None,
                        "replacement_reviewer_id": None,
                    },
                )
            )
            original = await manager.execute(request)
            assert original.ok and original.data.member.version == index + 2
            assert await manager.execute(request) == original
            changed = request.model_copy(
                update={
                    "payload": request.payload.model_copy(
                        update={"reason": reason + "changed"}
                    )
                }
            )
            conflict = await manager.execute(changed)
            assert not conflict.ok and conflict.error.detail == {
                "reason": "operation_conflict"
            }
        await caller.rollback()
    async with maker() as observer:
        rows = list(
            (
                await observer.scalars(
                    select(AuditLog)
                    .where(AuditLog.actor_id == actor_id)
                    .order_by(AuditLog.id)
                )
            ).all()
        )
        assert [row.detail_json["reason"] for row in rows] == reasons
        assert await observer.scalar(
            text(
                "SELECT count(*) FROM public.workspace_membership_operation_receipts WHERE actor_id=:actor"
            ),
            {"actor": actor_id},
        ) == len(reasons)


@pytest.mark.parametrize("binding", ["caller", "dedicated", "remote"])
async def test_execute_rejects_an_unready_native_text_codec(
    test_engine, monkeypatch, tmp_path, binding
):
    monkeypatch.setattr(settings, "workspace_membership_url", "")
    monkeypatch.setattr(settings, "workspace_membership_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "workspace_membership_database_url",
        _workspace_dsn(test_engine) if binding == "dedicated" else "",
    )
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    owner = None
    control = secrets.token_urlsafe(32)
    async with test_engine.connect() as db:
        original = await db.scalar(
            text(
                "SELECT pg_get_functiondef('aap_workspace_membership_v1.membership_policy()'::regprocedure)"
            )
        )
    try:
        if binding == "remote":
            owner = await _start_workspace(test_engine, tmp_path, control)
        async with test_engine.begin() as db:
            await db.execute(
                text("""
                CREATE OR REPLACE FUNCTION aap_workspace_membership_v1.membership_policy()
                RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
                SET search_path=pg_catalog,pg_temp AS $body$
                SELECT pg_catalog.jsonb_build_object(
                    'schema','aap-workspace-membership-native/v1',
                    'operations',pg_catalog.jsonb_build_array(
                        'add_member','preview_role_change','change_role','remove_member'))
                $body$
            """)
            )
        if binding == "remote":
            async with httpx.AsyncClient(
                base_url=owner.origin, trust_env=False, timeout=5
            ) as client:
                assert (await client.get("/ready")).status_code == 503
                response = await client.post(
                    "/v1/workspace/membership/execute",
                    headers={"Authorization": f"Bearer {control}"},
                    json=_command(
                        "add_member",
                        actor_id,
                        project_id,
                        {
                            "target_user_id": str(employee_id),
                            "project_role": "annotator",
                        },
                    ),
                )
                assert response.status_code == 503
        else:
            async with maker() as caller:
                with pytest.raises(HTTPException) as rejected:
                    await add_member(
                        caller,
                        project_id=project_id,
                        actor_id=actor_id,
                        target_user_id=employee_id,
                        project_role="annotator",
                    )
                assert rejected.value.status_code == 503
        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(func.count(ProjectMember.id)).where(
                        ProjectMember.project_id == project_id
                    )
                )
                == 0
            )
            assert (
                await observer.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
                )
                == 0
            )
    finally:
        async with test_engine.begin() as db:
            await db.execute(text(original))
        if owner is not None:
            owner.stop()


async def test_readiness_rejects_nonmodule_definer_and_explicit_function_grants(
    test_engine, tmp_path
):
    maker, _, _, _ = await _seed_owned_member_scope(test_engine)
    control = secrets.token_urlsafe(32)
    function = f"workspace_readiness_probe_{uuid.uuid4().hex}"
    owner = await _start_workspace(test_engine, tmp_path, control)
    created = False
    try:
        async with httpx.AsyncClient(
            base_url=owner.origin, trust_env=False, timeout=5
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"CREATE FUNCTION public.{function}() RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS 'SELECT 1'"
                    )
                )
                created = True
            assert (await client.get("/ready")).status_code == 503
            async with test_engine.begin() as db:
                await db.execute(
                    text(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")
                )
            assert (await client.get("/ready")).status_code == 200
            async with test_engine.begin() as db:
                await db.execute(
                    text(f"ALTER FUNCTION public.{function}() SECURITY INVOKER")
                )
                await db.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION public.{function}() TO workspace_membership_client"
                    )
                )
            assert (await client.get("/ready")).status_code == 503
    finally:
        owner.stop()
        if created:
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP FUNCTION public.{function}()"))


async def test_installed_owner_full_lifecycle_replays_receipts_without_new_effects(
    test_engine, tmp_path
):
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    control = secrets.token_urlsafe(32)
    owner = await _start_workspace(test_engine, tmp_path / "owner", control)
    requests = []
    try:
        async with httpx.AsyncClient(
            base_url=owner.origin, trust_env=False, timeout=10
        ) as client:

            async def execute(command):
                response = await client.post(
                    "/v1/workspace/membership/execute",
                    json=command,
                    headers={"Authorization": f"Bearer {control}"},
                )
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["ok"] is True, result
                return result

            assert (await client.get("/health")).status_code == 200
            assert (
                await client.post("/v1/workspace/membership/execute", json={})
            ).status_code == 401
            added = _command(
                "add_member",
                actor_id,
                project_id,
                {"target_user_id": str(employee_id), "project_role": "annotator"},
            )
            requests.append(added)
            first = await execute(added)
            member_id = first["data"]["member"]["id"]
            preview = _command(
                "preview_role_change",
                actor_id,
                project_id,
                {
                    "member_id": member_id,
                    "target_role": "reviewer",
                    "replacement_annotator_id": None,
                    "replacement_reviewer_id": None,
                },
            )
            ready = await execute(preview)
            assert ready["data"]["blockers"] == []
            digest = hashlib.sha256(
                json.dumps(
                    ready["data"]["resource_snapshot"], sort_keys=True, default=str
                ).encode()
            ).hexdigest()
            assert ready["data"]["preview_token"] == digest
            changed = _command(
                "change_role",
                actor_id,
                project_id,
                {
                    **preview["payload"],
                    "expected_version": 1,
                    "preview_token": digest,
                    "reason": "Runtime qualification",
                },
            )
            requests.append(changed)
            changed_reply = await execute(changed)
            assert changed_reply["data"]["member"]["version"] == 2
            removed = _command(
                "remove_member", actor_id, project_id, {"member_id": member_id}
            )
            requests.append(removed)
            removed_reply = await execute(removed)
            assert removed_reply["data"] == {"member_id": member_id, "removed": True}
            assert await execute(added) == first
            assert await execute(changed) == changed_reply
            assert await execute(removed) == removed_reply
            collision = {
                **changed,
                "payload": {**changed["payload"], "reason": "Different request"},
            }
            response = await client.post(
                "/v1/workspace/membership/execute",
                json=collision,
                headers={"Authorization": f"Bearer {control}"},
            )
            assert response.status_code == 200 and response.json()["ok"] is False
            assert response.json()["error"]["status_code"] == 409
        async with maker() as observer:
            assert await observer.get(ProjectMember, uuid.UUID(member_id)) is None
            assert (
                await observer.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
                )
                == 3
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_membership_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor_id},
                )
                == 3
            )
    finally:
        owner.stop()


async def test_owner_exit_during_write_rolls_back_and_retry_after_restart_succeeds(
    test_engine, tmp_path
):
    maker, actor_id, employee_id, project_id = await _seed_owned_member_scope(
        test_engine
    )
    suffix = uuid.uuid4().hex
    trigger = f"workspace_crash_probe_{suffix}"
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE FUNCTION public.{trigger}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN IF NEW.project_id='{project_id}'::uuid THEN PERFORM pg_catalog.pg_sleep(2); END IF; RETURN NEW; END; $body$"
            )
        )
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.project_members FOR EACH ROW EXECUTE FUNCTION public.{trigger}()"
            )
        )
    owner = None
    in_flight = None
    command = _command(
        "add_member",
        actor_id,
        project_id,
        {"target_user_id": str(employee_id), "project_role": "annotator"},
    )
    control = secrets.token_urlsafe(32)
    try:
        owner = await _start_workspace(test_engine, tmp_path / "first", control)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            in_flight = asyncio.create_task(
                client.post(
                    owner.origin + "/v1/workspace/membership/execute",
                    json=command,
                    headers={"Authorization": f"Bearer {control}"},
                )
            )
            deadline = time.monotonic() + 10
            while True:
                async with maker() as observer:
                    sleeping = await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity WHERE datname=current_database() AND application_name='aap-workspace-membership' AND wait_event='PgSleep')"
                        )
                    )
                if sleeping:
                    break
                assert not in_flight.done(), (
                    "Write completed before the owned crash probe"
                )
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            owner.process.kill()
            await asyncio.to_thread(owner.process.wait, timeout=5)
            owner.stop()
            owner = None
            with pytest.raises(httpx.HTTPError):
                await in_flight
            in_flight = None
        deadline = time.monotonic() + 10
        while True:
            async with maker() as observer:
                connected = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname=current_database() AND application_name='aap-workspace-membership'"
                    )
                )
            if connected == 0:
                break
            assert time.monotonic() < deadline, "Dead owner transaction did not close"
            await asyncio.sleep(0.05)
        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(func.count(ProjectMember.id)).where(
                        ProjectMember.project_id == project_id
                    )
                )
                == 0
            )
            assert (
                await observer.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
                )
                == 0
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_membership_operation_receipts WHERE operation_id=:id"
                    ),
                    {"id": uuid.UUID(command["operation_id"])},
                )
                == 0
            )
        async with test_engine.begin() as db:
            await db.execute(text(f"DROP TRIGGER {trigger} ON public.project_members"))
            await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
        trigger = None
        owner = await _start_workspace(test_engine, tmp_path / "replacement", control)
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.post(
                owner.origin + "/v1/workspace/membership/execute",
                json=command,
                headers={"Authorization": f"Bearer {control}"},
            )
            assert response.status_code == 200 and response.json()["ok"] is True, (
                response.text
            )
        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(func.count(ProjectMember.id)).where(
                        ProjectMember.project_id == project_id
                    )
                )
                == 1
            )
            assert (
                await observer.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.actor_id == actor_id)
                )
                == 1
            )
    finally:
        if owner is not None:
            owner.stop()
        if in_flight is not None:
            in_flight.cancel()
            await asyncio.gather(in_flight, return_exceptions=True)
        if trigger is not None:
            async with test_engine.begin() as db:
                await db.execute(
                    text(f"DROP TRIGGER {trigger} ON public.project_members")
                )
                await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
