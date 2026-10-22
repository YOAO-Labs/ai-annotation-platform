"""Exercise the portable deletion owner on a function-only caller transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_workspace.project_deletion import ProjectDeletionManager
from aap_workspace.project_deletion_contract import (
    ProjectDeletionCommand,
    ProjectDeletionReceipt,
    decode_project_deletion_reply,
    project_deletion_schema,
)
from aap_workspace.project_deletion_postgres import ProjectDeletionPostgresData
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_project_deletion_data import (
    _assert_graph_unchanged,
    _assert_no_receipt_or_audit,
    _cleanup_fixture,
    _seed_graph,
)
from tests.factory import create_membership, create_project

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module verifier and deletion-only LOGIN",
    ),
]


def _url(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_WORKSPACE_PROJECT_DELETION_URL"])
    assert url.username == "workspace_project_deletion_client"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database.endswith("_test")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def _world(test_engine):
    fixture = await _seed_graph(test_engine)
    connection = None
    try:
        connection = await asyncpg.connect(
            _url(test_engine), timeout=5, command_timeout=10
        )
        yield fixture, connection
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup_fixture(test_engine, fixture)


def _command(fixture):
    operation = uuid.uuid4()
    fixture["operation_ids"].append(operation)
    return ProjectDeletionCommand(
        operation_id=operation,
        actor_id=fixture["user_id"],
        project_id=fixture["project_id"],
        audit_context=None,
    )


async def test_frozen_receipt_and_deletion_only_capability(test_engine):
    descriptor = json.loads(
        (
            ROOT
            / "apps/workspace/src/aap_workspace/project_deletion_protocol.schema.json"
        ).read_text()
    )
    assert descriptor == project_deletion_schema()
    frozen = json.loads(
        (ROOT / "apps/api/alembic/sql/0199_project_deletion.schema.json").read_text()
    )
    receipt = ProjectDeletionReceipt.model_json_schema()
    assert receipt["properties"] == frozen["$defs"]["receipt"]["properties"]
    assert receipt["required"] == frozen["$defs"]["receipt"]["required"]
    async with _world(test_engine) as (fixture, connection):
        functions = await connection.fetch(
            "SELECT n.nspname||'.'||p.proname AS name FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname LIKE 'aap_%' "
            "AND has_function_privilege(current_user,p.oid,'EXECUTE') ORDER BY name"
        )
        assert [r["name"] for r in functions] == ["aap_workspace_v1.delete_project"]
        for query in (
            "SELECT id FROM public.projects",
            "SELECT aap_labeling_v1.delete_project_owned_records($1)",
            "SELECT aap_identity_v1.lock_audit_actor($1)",
            "SELECT aap_workspace_v1.read_import_project('denied')",
            "CREATE TEMP TABLE deletion_forbidden(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    query, *([fixture["project_id"]] if "$1" in query else [])
                )
        command = _command(fixture)
        with pytest.raises(ValueError, match="active caller transaction"):
            await ProjectDeletionManager(
                ProjectDeletionPostgresData(connection)
            ).execute(command)
        async with test_engine.connect() as db:
            await _assert_graph_unchanged(db, fixture)
            await _assert_no_receipt_or_audit(db, command.operation_id)


@pytest.mark.parametrize("finish", ["rollback", "commit"])
async def test_portable_owner_keeps_outer_transaction_and_frozen_replay(
    test_engine, finish
):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        transaction = connection.transaction()
        await transaction.start()
        receipt = await ProjectDeletionManager(
            ProjectDeletionPostgresData(connection)
        ).execute(command)
        assert receipt.operation_id == command.operation_id
        async with test_engine.connect() as db:
            await _assert_graph_unchanged(db, fixture)
            await _assert_no_receipt_or_audit(db, command.operation_id)
        if finish == "rollback":
            await transaction.rollback()
            async with test_engine.connect() as db:
                await _assert_graph_unchanged(db, fixture)
                await _assert_no_receipt_or_audit(db, command.operation_id)
        else:
            await transaction.commit()
            await connection.close()
            replacement = await asyncpg.connect(
                _url(test_engine), timeout=5, command_timeout=10
            )
            try:
                async with replacement.transaction():
                    replay = await ProjectDeletionManager(
                        ProjectDeletionPostgresData(replacement)
                    ).execute(command)
                assert replay == receipt
                async with test_engine.connect() as db:
                    assert (
                        await db.scalar(
                            text("SELECT count(*) FROM projects WHERE id=:id"),
                            {"id": command.project_id},
                        )
                        == 0
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM audit_logs WHERE action='project.delete' AND detail_json->>'operation_id'=:id"
                            ),
                            {"id": str(command.operation_id)},
                        )
                        == 1
                    )
            finally:
                await replacement.close()


@asynccontextmanager
async def _failed_commit(test_engine, operation_id):
    # This operation-scoped deferred trigger leaves the caller LOGIN function-only.
    name = "deletion_commit_" + uuid.uuid4().hex[:12]
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='owned deletion commit probe'; END; $body$"
            )
        )
        await db.execute(
            text(
                f"CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON public.project_deletion_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.operation_id='{operation_id}'::uuid) EXECUTE FUNCTION public.{name}()"
            )
        )
    try:
        yield
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text(f"DROP TRIGGER {name} ON public.project_deletion_operations")
            )
            await db.execute(text(f"DROP FUNCTION public.{name}()"))


async def test_failed_caller_commit_restores_graph_audit_and_receipt(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)
        async with _failed_commit(test_engine, command.operation_id):
            transaction = connection.transaction()
            await transaction.start()
            await ProjectDeletionManager(
                ProjectDeletionPostgresData(connection)
            ).execute(command)
            with pytest.raises(
                asyncpg.CheckViolationError, match="owned deletion commit probe"
            ):
                await transaction.commit()
            if connection.is_in_transaction():
                await connection.execute("ROLLBACK")
        async with test_engine.connect() as db:
            await _assert_graph_unchanged(db, fixture)
            await _assert_no_receipt_or_audit(db, command.operation_id)


async def test_bad_receipt_binding_aborts_actual_native_mutation(test_engine):
    async with _world(test_engine) as (fixture, connection):
        command = _command(fixture)

        class WrongBinding:
            def __getattr__(self, name):
                return getattr(connection, name)

            async def fetchval(self, query, *args, **kwargs):
                raw = await connection.fetchval(query, *args, **kwargs)
                if "aap_workspace_v1.delete_project(" in query:
                    payload = json.loads(raw)
                    payload["data"]["actor_id"] = str(uuid.uuid4())
                    return json.dumps(payload)
                return raw

        with pytest.raises(ValueError, match="command scope"):
            async with connection.transaction():
                await ProjectDeletionManager(
                    ProjectDeletionPostgresData(WrongBinding())
                ).execute(command)
        async with test_engine.connect() as db:
            await _assert_graph_unchanged(db, fixture)
            await _assert_no_receipt_or_audit(db, command.operation_id)


@pytest.mark.parametrize("finish", ["rollback", "failed_commit", "commit"])
async def test_node_caller_preserves_native_transaction_and_wire_integers(
    test_engine, finish
):
    async with _world(test_engine) as (fixture, _):
        command = _command(fixture)
        if finish == "commit":
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        "SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('public.audit_logs','id')::regclass, :value, true)"
                    ),
                    {"value": 2**53 + 17},
                )

        async def invoke():
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "node",
                    str(
                        ROOT
                        / "apps/api/tests/fixtures/workspace_project_deletion_peer.mjs"
                    ),
                ],
                input=json.dumps(
                    {
                        "database_url": _url(test_engine),
                        "command": command.model_dump(mode="json"),
                        "finish": finish,
                    }
                ),
                text=True,
                capture_output=True,
                timeout=20,
                cwd=ROOT,
                env={"PATH": os.environ.get("PATH", "")},
            )
            assert result.returncode == 0, "Workspace deletion Node peer failed"
            return json.loads(result.stdout)

        if finish == "failed_commit":
            async with _failed_commit(test_engine, command.operation_id):
                response = await invoke()
            assert response == {"commit_failed": "23514"}
        else:
            response = await invoke()
            receipt = decode_project_deletion_reply(response["reply"])
            assert (receipt.operation_id, receipt.actor_id, receipt.project_id) == (
                command.operation_id,
                command.actor_id,
                command.project_id,
            )
        async with test_engine.connect() as db:
            if finish in {"rollback", "failed_commit"}:
                await _assert_graph_unchanged(db, fixture)
                await _assert_no_receipt_or_audit(db, command.operation_id)
            else:
                audit_id = await db.scalar(
                    text(
                        "SELECT id FROM audit_logs WHERE action='project.delete' AND detail_json->>'operation_id'=:id"
                    ),
                    {"id": str(command.operation_id)},
                )
                assert audit_id == receipt.audit_id
                assert audit_id > 2**53


async def test_public_delete_keeps_manager_gate_audit_and_empty_success(
    db_session, httpx_client, project_admin, reviewer
):
    owner, owner_token = project_admin
    member, member_token = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    project_id = project.id
    await create_membership(
        db_session,
        project_id=project_id,
        user_id=member.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    await db_session.commit()
    path = f"/api/v1/projects/{project_id}"
    denied = await httpx_client.delete(
        path, headers={"Authorization": f"Bearer {member_token}"}
    )
    assert denied.status_code == 403
    deleted = await httpx_client.delete(
        path, headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert deleted.status_code == 204 and deleted.content == b""
    reply = await db_session.scalar(
        text("SELECT result FROM project_deletion_operations WHERE project_id=:id"),
        {"id": project_id},
    )
    receipt = decode_project_deletion_reply(reply)
    assert receipt.actor_id == owner.id and receipt.project_id == project_id
    audit = (
        await db_session.execute(
            text("SELECT action,method,path FROM audit_logs WHERE id=:id"),
            {"id": receipt.audit_id},
        )
    ).one()
    assert audit == ("project.delete", "DELETE", path)
    repeated = await httpx_client.delete(
        path, headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert repeated.status_code == 404
