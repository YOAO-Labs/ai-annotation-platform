"""Portable Workspace unlink on real caller transactions and restricted roles."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_owner_data.postgres import FunctionRoleUnavailable
from aap_workspace.dataset_unlink import DatasetUnlinkManager
from aap_workspace.dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkFailure,
    DatasetUnlinkPreviewCommand,
    dataset_unlink_schema,
    decode_unlink_reply,
)
from aap_workspace.dataset_unlink_postgres import DatasetUnlinkPostgresData
from app.core.security import create_access_token
from app.db.models.dataset import ProjectDataset
from app.middleware import audit as audit_middleware
from tests.factory import create_task
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_dataset_unlink_coordinator import (
    _add_feedback,
    _assert_unlinked_graph,
    _assert_unmodified,
    _seed_complete_case,
)
from tests.test_labeling_counter_deltas_workflows_native import (
    _DIRECT_PROJECT_SQL,
    _configure_actual_api,
    _restricted_api_client,
)
from tests.test_project_owner_persistence_native import _restricted

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module resources and the two-function unlink LOGIN",
    ),
]


def _url(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_WORKSPACE_DATASET_UNLINK_URL"])
    assert url.username == "workspace_dataset_unlink_client"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def _world(test_engine):
    fixture = await _seed_complete_case(test_engine)
    connection = await asyncpg.connect(_url(test_engine), timeout=5, command_timeout=10)
    try:
        yield fixture, connection
    finally:
        await connection.close()
        # Immutable audit and graph fixtures leave with the owned disposable DB.


def _command(fixture):
    return DatasetUnlinkCommand(
        operation_id=uuid.uuid4(),
        actor_id=fixture["user_id"],
        dataset_id=fixture["dataset_id"],
        project_id=fixture["project_id"],
        cleanup_bucket="unlink-native-test",
    )


def _preview(command):
    return DatasetUnlinkPreviewCommand(
        actor_id=command.actor_id,
        dataset_id=command.dataset_id,
        project_id=command.project_id,
    )


def _manager(connection):
    return DatasetUnlinkManager(DatasetUnlinkPostgresData(connection))


async def test_two_function_login_has_exact_contract_and_no_private_capabilities(
    test_engine,
):
    descriptor = (
        ROOT / "apps/workspace/src/aap_workspace/dataset_unlink_protocol.schema.json"
    )
    assert json.loads(descriptor.read_text()) == dataset_unlink_schema()
    async with _world(test_engine) as (fixture, connection):
        assert (
            await connection.fetchval("SELECT current_user")
            == "workspace_dataset_unlink_client"
        )
        names = await connection.fetch(
            "SELECT n.nspname||'.'||p.proname AS name FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname LIKE 'aap_%' "
            "AND has_function_privilege(current_user,p.oid,'EXECUTE') ORDER BY name"
        )
        assert [row["name"] for row in names] == [
            "aap_workspace_v1.preview_dataset_unlink",
            "aap_workspace_v1.unlink_dataset",
        ]
        for sql in (
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.datasets LIMIT 1",
            "SELECT operation_id FROM public.dataset_unlink_operations LIMIT 1",
            "UPDATE public.projects SET total_tasks=0 WHERE false",
            "SELECT aap_workspace_v1.delete_project(NULL,NULL,NULL,NULL)",
            "SELECT aap_labeling_v1.delete_dataset_tasks(NULL,NULL)",
            "CREATE TEMP TABLE unlink_denied(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
        command = _command(fixture)
        for action in (
            lambda: _manager(connection).execute(command),
            lambda: _manager(connection).preview(_preview(command)),
        ):
            with pytest.raises(ValueError, match="active caller transaction"):
                await action()
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)


async def test_preview_keeps_counts_authority_locks_and_caller_transaction(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        transaction = connection.transaction()
        await transaction.start()
        before = await connection.fetchrow("SELECT pg_backend_pid(),txid_current()")
        preview = await _manager(connection).preview(_preview(command))
        assert preview.model_dump() == {
            "will_delete_tasks": 1,
            "will_delete_annotations": 1,
            "will_delete_batches": 1,
        }
        assert (
            await connection.fetchrow("SELECT pg_backend_pid(),txid_current()")
            == before
        )
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)
            with pytest.raises(DBAPIError) as busy:
                async with db.begin_nested():
                    await db.execute(
                        text("SELECT id FROM projects WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": command.project_id},
                    )
            assert busy.value.orig.sqlstate == "55P03"
        await transaction.rollback()
        async with test_engine.begin() as db:
            assert (
                await db.scalar(
                    text("SELECT id FROM projects WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": command.project_id},
                )
                == command.project_id
            )


@pytest.mark.parametrize("action", ["preview", "unlink"])
async def test_native_capabilities_reject_read_only_caller_transactions(
    test_engine, action
):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            async with connection.transaction(readonly=True):
                if action == "preview":
                    await _manager(connection).preview(_preview(command))
                else:
                    await _manager(connection).execute(command)
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)


@pytest.mark.parametrize("finish", ["rollback", "commit"])
async def test_portable_unlink_and_replay_keep_outer_transaction_and_generation(
    test_engine, finish
):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        transaction = connection.transaction()
        await transaction.start()
        before = await connection.fetchrow("SELECT pg_backend_pid(),txid_current()")
        receipt = await _manager(connection).execute(command)
        assert receipt.association_id == fixture["association_id"]
        assert receipt.public_result() == {
            "deleted_tasks": 1,
            "deleted_annotations": 1,
            "deleted_batches": 1,
            "deleted_batch_ids": [str(fixture["batch_id"])],
            "soft": False,
        }
        assert (
            await connection.fetchrow("SELECT pg_backend_pid(),txid_current()")
            == before
        )
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)
        if finish == "rollback":
            await transaction.rollback()
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                await _assert_unmodified(db, fixture, command.operation_id)
            return
        await transaction.commit()
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unlinked_graph(db, fixture)
        owner = async_sessionmaker(test_engine, expire_on_commit=False)
        async with owner.begin() as db:
            association = ProjectDataset(
                dataset_id=command.dataset_id, project_id=command.project_id
            )
            db.add(association)
            task = await create_task(db, project_id=command.project_id)
            task.dataset_item_id = fixture["item_a"]
            new_link, new_task = association.id, task.id
            await db.flush()
            new_link = association.id
        await connection.close()
        replacement = await asyncpg.connect(
            _url(test_engine), timeout=5, command_timeout=10
        )
        try:
            async with replacement.transaction():
                assert await _manager(replacement).execute(command) == receipt
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                assert (
                    await db.scalar(
                        text("SELECT id FROM tasks WHERE id=:id"), {"id": new_task}
                    )
                    == new_task
                )
                assert (
                    await db.scalar(
                        text("SELECT id FROM project_datasets WHERE id=:id"),
                        {"id": new_link},
                    )
                    == new_link
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM dataset_unlink_operations WHERE operation_id=:id"
                        ),
                        {"id": command.operation_id},
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM audit_logs WHERE detail_json->>'operation_id'=:id AND action='dataset.unlink'"
                        ),
                        {"id": str(command.operation_id)},
                    )
                    == receipt.audit_id
                )
        finally:
            await replacement.close()


@asynccontextmanager
async def _failed_commit(test_engine, operation_id):
    name = "unlink_commit_" + uuid.uuid4().hex[:12]
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned unlink commit probe'; END; $body$"
            )
        )
        await db.execute(
            text(
                f"CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON public.dataset_unlink_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.operation_id='{operation_id}'::uuid) EXECUTE FUNCTION public.{name}()"
            )
        )
    try:
        yield
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text(f"DROP TRIGGER {name} ON public.dataset_unlink_operations")
            )
            await db.execute(text(f"DROP FUNCTION public.{name}()"))


async def test_failed_outer_commit_restores_graph_audit_outbox_and_receipt(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        async with _failed_commit(test_engine, command.operation_id):
            with pytest.raises(
                asyncpg.CheckViolationError, match="owned unlink commit probe"
            ):
                async with connection.transaction():
                    await _manager(connection).execute(command)
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)
        async with connection.transaction():
            await _manager(connection).execute(command)
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unlinked_graph(db, fixture)


async def test_mismatched_reply_rolls_back_actual_native_unlink(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)

        class WrongScope(DatasetUnlinkPostgresData):
            async def unlink_dataset(self, value):
                raw = await super().unlink_dataset(value)
                reply = json.loads(raw)
                reply["data"]["dataset_id"] = str(uuid.uuid4())
                return reply

        with pytest.raises(ValueError, match="does not match its command scope"):
            async with connection.transaction():
                await DatasetUnlinkManager(WrongScope(connection)).execute(command)
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)


async def test_restricted_unlink_preserves_retention_failure_and_graph(test_engine):
    async with _world(test_engine) as (fixture, connection):
        await _add_feedback(test_engine, fixture)
        command = _command(fixture)
        async with connection.transaction():
            with pytest.raises(DatasetUnlinkFailure) as retained:
                await _manager(connection).execute(command)
            assert retained.value.code == "task_feedback_retained"
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)


async def test_restricted_unlink_audit_contention_rolls_back_then_recovers(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        async with test_engine.begin() as locker:
            await locker.execute(text("LOCK TABLE public.audit_logs IN SHARE MODE"))
            async with connection.transaction():
                await connection.execute("SET LOCAL lock_timeout='250ms'")
                with pytest.raises(DatasetUnlinkFailure) as busy:
                    await _manager(connection).execute(command)
                assert busy.value.code == "project_busy"
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                await _assert_unmodified(db, fixture, command.operation_id)
        async with connection.transaction():
            await _manager(connection).execute(command)


async def test_extra_function_privilege_rejected_before_native_mutation(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        sql = " ON FUNCTION aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb) "
        async with test_engine.begin() as db:
            await db.execute(
                text("GRANT EXECUTE" + sql + "TO workspace_dataset_unlink_client")
            )
        try:
            with pytest.raises(FunctionRoleUnavailable):
                async with connection.transaction():
                    await _manager(connection).execute(command)
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        "REVOKE EXECUTE" + sql + "FROM workspace_dataset_unlink_client"
                    )
                )
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            await _assert_unmodified(db, fixture, command.operation_id)


@pytest.mark.parametrize("finish", ["rollback", "commit", "failed_commit"])
async def test_node_peer_preserves_two_capabilities_transaction_and_large_receipt_integer(
    test_engine, finish
):
    async with _world(test_engine) as (fixture, _connection):
        command = _command(fixture)
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('public.audit_logs','id'),GREATEST((SELECT COALESCE(max(id),0) FROM audit_logs),9007199254741907),true)"
                )
            )

        @asynccontextmanager
        async def probe():
            if finish == "failed_commit":
                async with _failed_commit(test_engine, command.operation_id):
                    yield
            else:
                yield

        async with probe():
            process = await asyncio.create_subprocess_exec(
                "node",
                str(
                    Path(__file__).parent / "fixtures/workspace_dataset_unlink_peer.mjs"
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, errors = await asyncio.wait_for(
                    process.communicate(
                        json.dumps(
                            {
                                "database_url": _url(test_engine),
                                "command": command.model_dump(mode="json"),
                                "finish": finish,
                            }
                        ).encode()
                    ),
                    20,
                )
                assert process.returncode == 0, errors.decode()
                result = json.loads(out)
                if finish == "failed_commit":
                    assert result == {"commit_failed": "23514"}
                else:
                    receipt = decode_unlink_reply(result["reply"])
                    assert receipt.audit_id > 2**53
                    assert receipt.operation_id == command.operation_id
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            if finish == "commit":
                await _assert_unlinked_graph(db, fixture)
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM audit_logs WHERE action='dataset.unlink' AND detail_json->>'operation_id'=:id"
                        ),
                        {"id": str(command.operation_id)},
                    )
                    == receipt.audit_id
                )
            else:
                await _assert_unmodified(db, fixture, command.operation_id)


async def test_actual_http_preview_unlink_and_replay_without_private_project_access(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    path = f"/api/v1/datasets/{fixture['dataset_id']}/link/{fixture['project_id']}"
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(fixture["user_id"]), role="super_admin"),
        "Idempotency-Key": str(operation_id),
    }
    async with _restricted(test_engine) as (_owner, api, _world, _url, _batches):
        monkeypatch.setattr(audit_middleware, "async_session", api)
        statements = []
        async with _restricted_api_client(app_module, api, statements) as client:
            preview = await client.get(path + "/preview-unlink", headers=headers)
            assert preview.status_code == 200, preview.text
            assert preview.json() == {
                "will_delete_tasks": 1,
                "will_delete_annotations": 1,
                "will_delete_batches": 1,
            }
            response = await client.delete(path, headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["Idempotency-Key"] == str(operation_id)
            replay = await client.delete(path, headers=headers)
            assert replay.status_code == 200 and replay.json() == response.json()
        assert not [sql for sql in statements if _DIRECT_PROJECT_SQL.search(sql)]
        assert (
            sum("aap_workspace_v1.preview_dataset_unlink(" in sql for sql in statements)
            == 1
        )
        assert sum("aap_workspace_v1.unlink_dataset(" in sql for sql in statements) == 2
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        await _assert_unlinked_graph(db, fixture)
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM dataset_unlink_operations WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            == 1
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM audit_logs WHERE action='dataset.unlink' AND detail_json->>'operation_id'=:id"
                ),
                {"id": str(operation_id)},
            )
            == 1
        )
