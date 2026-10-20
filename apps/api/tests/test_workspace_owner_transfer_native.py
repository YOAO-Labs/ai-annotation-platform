"""Native Workspace owner transfer preserves transaction and receipt semantics."""

from __future__ import annotations

import json
import os
import asyncio
import uuid
from datetime import datetime

import asyncpg
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.owner_transfer import OwnerTransferManager
from aap_workspace.owner_transfer_contract import (
    OWNER_TRANSFER_PROTOCOL,
    OwnerTransferFailure,
    OwnerTransferRequest,
    OwnerTransferSuccess,
)
from aap_workspace.owner_transfer_postgres import OwnerTransferPostgresData
from app.config import settings
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


def _transfer_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Workspace owner-transfer LOGIN acceptance requires the owned verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_owner_transfer_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _request(actor_id, project_id, new_owner_id, operation_id=None):
    return OwnerTransferRequest(
        schema=OWNER_TRANSFER_PROTOCOL,
        operation_id=operation_id or uuid.uuid4(),
        actor_id=actor_id,
        project_id=project_id,
        new_owner_id=new_owner_id,
    )


async def _seed(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker.begin() as session:
        actor = await create_user(
            session,
            "super_admin",
            f"owner-transfer-actor-{suffix}@example.test",
            "Owner transfer actor",
        )
        await session.flush()
        first_owner = await create_user(
            session,
            "project_admin",
            f"owner-transfer-first-{suffix}@example.test",
            "First owner",
        )
        second_owner = await create_user(
            session,
            "project_admin",
            f"owner-transfer-second-{suffix}@example.test",
            "Second owner",
        )
        employee = await create_user(
            session,
            "employee",
            f"owner-transfer-employee-{suffix}@example.test",
            "Not an eligible owner",
        )
        await session.flush()
        project = await create_project(
            session, owner_id=actor.id, name=f"Owner transfer {suffix}"
        )
        await session.flush()
    return maker, actor.id, first_owner.id, second_owner.id, employee.id, project.id


async def _cleanup(maker, user_ids, project_id, operation_ids=()):
    async with maker.begin() as session:
        if operation_ids:
            await session.execute(
                text(
                    "DELETE FROM public.workspace_owner_transfer_operation_receipts "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(operation_ids)},
            )
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(User).where(User.id.in_(user_ids)))


async def _state(maker, actor_id, project_id):
    async with maker() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT project.owner_id, project.updated_at, "
                        "(SELECT count(*) FROM public.audit_logs AS audit "
                        " WHERE audit.actor_id=:actor_id AND audit.target_type='project' "
                        " AND audit.target_id=CAST(project.id AS text)) AS audit_count, "
                        "(SELECT count(*) FROM public.project_members AS member "
                        " WHERE member.project_id=project.id) AS member_count "
                        "FROM public.projects AS project WHERE project.id=:project_id"
                    ),
                    {"actor_id": actor_id, "project_id": project_id},
                )
            )
            .mappings()
            .one()
        )


async def test_owner_transfer_receipt_replays_after_supersession_and_noop_is_stable(
    test_engine,
):
    maker, actor_id, first_id, second_id, employee_id, project_id = await _seed(
        test_engine
    )
    operation_ids = []
    manager = OwnerTransferManager(
        OwnerTransferPostgresData(_transfer_dsn(test_engine))
    )
    try:
        first_request = _request(actor_id, project_id, first_id)
        operation_ids.append(first_request.operation_id)
        caller = await asyncpg.connect(_transfer_dsn(test_engine))
        try:
            caller_tx = caller.transaction()
            await caller_tx.start()
            transaction_timestamp = await caller.fetchval(
                "SELECT transaction_timestamp()"
            )
            await asyncio.sleep(0.02)
            first_raw = json.loads(
                await caller.fetchval(
                    "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                    json.dumps(first_request.model_dump(mode="json", by_alias=True)),
                )
            )
            assert first_raw["ok"] is True
            assert datetime.fromisoformat(first_raw["data"]["updated_at"]) == (
                transaction_timestamp
            )
            await caller_tx.commit()
        finally:
            await caller.close()

        first = await manager.execute(first_request)
        assert isinstance(first, OwnerTransferSuccess)
        assert first.data.previous_owner_id == actor_id
        assert first.data.changed is True
        assert first.data.updated_at == transaction_timestamp

        second_request = _request(actor_id, project_id, second_id)
        operation_ids.append(second_request.operation_id)
        second = await manager.execute(second_request)
        assert isinstance(second, OwnerTransferSuccess)
        assert second.data.previous_owner_id == first_id
        assert second.data.changed is True
        state_after_second = await _state(maker, actor_id, project_id)
        assert state_after_second["owner_id"] == second_id

        async with maker.begin() as session:
            await session.execute(delete(User).where(User.id == first_id))
        replay = await manager.execute(first_request)
        assert replay == first
        state_after_replay = await _state(maker, actor_id, project_id)
        assert state_after_replay["owner_id"] == second_id
        assert state_after_replay["updated_at"] == state_after_second["updated_at"]

        conflict = await manager.execute(
            _request(
                actor_id,
                project_id,
                second_id,
                operation_id=first_request.operation_id,
            )
        )
        assert isinstance(conflict, OwnerTransferFailure)
        assert conflict.error.status_code == 409
        assert conflict.error.detail == {"reason": "operation_conflict"}
        assert conflict.error.rollback is False

        noop_request = _request(actor_id, project_id, second_id)
        operation_ids.append(noop_request.operation_id)
        noop = await manager.execute(noop_request)
        assert isinstance(noop, OwnerTransferSuccess)
        assert noop.data.previous_owner_id == second_id
        assert noop.data.updated_at == state_after_second["updated_at"]
        assert noop.data.changed is False

        role_error = await manager.execute(_request(actor_id, project_id, employee_id))
        assert isinstance(role_error, OwnerTransferFailure)
        assert role_error.error.status_code == 400
        assert role_error.error.detail == "仅可转移给 project_admin"
        assert role_error.error.rollback is False
        final_state = await _state(maker, actor_id, project_id)
        assert final_state["owner_id"] == second_id
        assert final_state["updated_at"] == state_after_second["updated_at"]
        assert final_state["audit_count"] == 0
        assert final_state["member_count"] == 0

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET role='employee', is_active=false "
                    "WHERE id=:id"
                ),
                {"id": actor_id},
            )
        actor_revoked_replay = await manager.execute(first_request)
        assert isinstance(actor_revoked_replay, OwnerTransferFailure)
        assert actor_revoked_replay.error.status_code == 403
        assert actor_revoked_replay.error.detail == "当前账号已无管理权限"
        assert actor_revoked_replay.error.rollback is False
        still_current = await _state(maker, actor_id, project_id)
        assert still_current["owner_id"] == second_id
        assert still_current["updated_at"] == state_after_second["updated_at"]
    finally:
        await _cleanup(
            maker,
            [actor_id, first_id, second_id, employee_id],
            project_id,
            operation_ids,
        )


async def test_owner_transfer_lock_errors_carry_exact_phase_hints(test_engine):
    maker, actor_id, target_id, second_id, employee_id, project_id = await _seed(
        test_engine
    )
    dsn = _transfer_dsn(test_engine)
    request = _request(actor_id, project_id, target_id)
    payload = json.dumps(request.model_dump(mode="json", by_alias=True))
    client = await asyncpg.connect(dsn)
    manager = OwnerTransferManager(OwnerTransferPostgresData(dsn))
    try:
        async with maker() as account_holder:
            await account_holder.execute(
                select(User.id).where(User.id == actor_id).with_for_update()
            )
            mapped_account_busy = await manager.execute(request)
            assert isinstance(mapped_account_busy, OwnerTransferFailure)
            assert mapped_account_busy.error.status_code == 409
            assert mapped_account_busy.error.detail == "账号正在更新，请刷新后重试"
            assert mapped_account_busy.error.rollback is True

            account_tx = client.transaction()
            await account_tx.start()
            try:
                with pytest.raises(asyncpg.PostgresError) as account_busy:
                    await client.fetchval(
                        "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                        payload,
                    )
                assert account_busy.value.sqlstate == "55P03"
                assert account_busy.value.hint.splitlines()[0] == (
                    "aap.workspace.owner_transfer.account_lock"
                )
                with pytest.raises(asyncpg.PostgresError) as account_aborted:
                    await client.fetchval("SELECT 1")
                assert account_aborted.value.sqlstate == "25P02"
            finally:
                await account_tx.rollback()

        async with maker() as project_holder:
            await project_holder.execute(
                select(Project.id).where(Project.id == project_id).with_for_update()
            )
            mapped_project_busy = await manager.execute(request)
            assert isinstance(mapped_project_busy, OwnerTransferFailure)
            assert mapped_project_busy.error.status_code == 409
            assert mapped_project_busy.error.detail == {"reason": "project_busy"}
            assert mapped_project_busy.error.rollback is False

            project_tx = client.transaction()
            await project_tx.start()
            try:
                with pytest.raises(asyncpg.PostgresError) as project_busy:
                    await client.fetchval(
                        "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                        payload,
                    )
                assert project_busy.value.sqlstate == "55P03"
                assert project_busy.value.hint.splitlines()[0] == (
                    "aap.workspace.owner_transfer.project_lock"
                )
                with pytest.raises(asyncpg.PostgresError) as project_aborted:
                    await client.fetchval("SELECT 1")
                assert project_aborted.value.sqlstate == "25P02"
            finally:
                await project_tx.rollback()
    finally:
        await client.close()
        await _cleanup(maker, [actor_id, target_id, second_id, employee_id], project_id)


async def test_owner_transfer_operation_id_contention_is_nonblocking(test_engine):
    maker, actor_id, target_id, second_id, employee_id, project_id = await _seed(
        test_engine
    )
    dsn = _transfer_dsn(test_engine)
    request = _request(actor_id, project_id, target_id)
    payload = json.dumps(request.model_dump(mode="json", by_alias=True))
    first = await asyncpg.connect(dsn)
    second = await asyncpg.connect(dsn)
    first_tx = first.transaction()
    second_tx = second.transaction()
    first_started = False
    second_started = False
    try:
        await first_tx.start()
        first_started = True
        result = json.loads(
            await first.fetchval(
                "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                payload,
            )
        )
        assert result["ok"] is True

        await second_tx.start()
        second_started = True
        busy = json.loads(
            await second.fetchval(
                "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                payload,
            )
        )
        assert busy == {
            "schema": OWNER_TRANSFER_PROTOCOL,
            "ok": False,
            "error": {
                "status_code": 409,
                "detail": {"reason": "operation_busy"},
                "rollback": False,
            },
        }
    finally:
        if second_started:
            await second_tx.rollback()
        if first_started:
            await first_tx.rollback()
        await second.close()
        await first.close()
        await _cleanup(
            maker,
            [actor_id, target_id, second_id, employee_id],
            project_id,
        )


@pytest.mark.parametrize(
    "corruption", ["duplicate", "unrequested", "coercible_boolean"]
)
async def test_owner_transfer_fails_closed_on_malformed_identity_lock_rows(
    test_engine, corruption
):
    maker, actor_id, target_id, second_id, employee_id, project_id = await _seed(
        test_engine
    )
    owner_url = make_url(settings.effective_migration_database_url)
    client_url = make_url(_transfer_dsn(test_engine))
    assert (owner_url.host, owner_url.port, owner_url.database) == (
        client_url.host,
        client_url.port,
        client_url.database,
    )
    owner_dsn = owner_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    other_id = uuid.uuid4()
    rows = [
        {
            "id": str(actor_id),
            "name": "Owner transfer actor",
            "email": f"malformed-actor-{actor_id.hex}@example.test",
            "role": "super_admin",
            "is_active": True,
        },
        {
            "id": str(target_id),
            "name": "Owner transfer target",
            "email": f"malformed-target-{target_id.hex}@example.test",
            "role": "project_admin",
            "is_active": True,
        },
    ]
    if corruption == "duplicate":
        rows.append(dict(rows[1]))
    elif corruption == "unrequested":
        rows.append(
            {
                "id": str(other_id),
                "name": "Unrequested account",
                "email": f"malformed-extra-{other_id.hex}@example.test",
                "role": "employee",
                "is_active": True,
            }
        )
    else:
        rows[0]["is_active"] = "true"
    account_payload = json.dumps(
        {"schema": "aap-identity-account-locks/v1", "accounts": rows}
    )
    request = _request(actor_id, project_id, target_id)
    request_json = json.dumps(request.model_dump(mode="json", by_alias=True))
    owner = await asyncpg.connect(owner_dsn)
    transaction = owner.transaction()
    started = False
    try:
        await transaction.start()
        started = True
        await owner.execute(
            """
            CREATE OR REPLACE FUNCTION aap_identity_v1.lock_accounts_update(
                p_account_ids uuid[], p_nowait boolean
            ) RETURNS jsonb
            LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
                SELECT $owner_transfer_test$"""
            + account_payload
            + """$owner_transfer_test$::jsonb
            $function$
            """
        )
        with pytest.raises(asyncpg.PostgresError) as malformed:
            await owner.fetchval(
                "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                request_json,
            )
        assert malformed.value.sqlstate == "XX000"
        assert "account lock" in str(malformed.value)
        await transaction.rollback()
        started = False

        async with maker() as session:
            state = (
                (
                    await session.execute(
                        text(
                            "SELECT project.owner_id, "
                            "(SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts "
                            " WHERE operation_id=:operation_id) AS receipt_count "
                            "FROM public.projects AS project WHERE project.id=:project_id"
                        ),
                        {
                            "operation_id": request.operation_id,
                            "project_id": project_id,
                        },
                    )
                )
                .mappings()
                .one()
            )
            assert state["owner_id"] == actor_id
            assert state["receipt_count"] == 0
            functiondef = await session.scalar(
                text(
                    "SELECT pg_catalog.pg_get_functiondef("
                    "'aap_identity_v1.lock_accounts_update(uuid[],boolean)'::regprocedure)"
                )
            )
            assert "FOR UPDATE OF account NOWAIT" in functiondef
    finally:
        if started:
            await transaction.rollback()
        await owner.close()
        await _cleanup(
            maker,
            [actor_id, target_id, second_id, employee_id],
            project_id,
            [request.operation_id],
        )
