"""Actor-free Project facts preserve caller transactions without private reads."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid
import warnings

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, inspect, text

from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.project_access import get_workspace_project
from tests.test_workspace_project_authority_locking import (
    _assert_private_denied,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


async def test_actorless_snapshot_is_read_only_unlocked_and_same_transaction(
    test_engine,
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            project = await get_workspace_project(db, project_id=world["project_id"])
            assert project.id == world["project_id"] and not inspect(project).unloaded
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for table, identity in (
                ("projects", world["project_id"]),
                ("users", world["member_id"]),
                ("project_members", world["membership_id"]),
            ):
                async with owner() as contender:
                    assert (
                        await contender.scalar(
                            text(
                                f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": identity},
                        )
                        == identity
                    )


async def test_actorless_snapshot_sees_uncommitted_caller_write_and_rollback(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            initial = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
            await db.execute(
                text("UPDATE public.projects SET name='caller pending' WHERE id=:id"),
                {"id": world["project_id"]},
            )
            project = await get_workspace_project(db, project_id=world["project_id"])
            assert project.name == "caller pending"
            await db.rollback()
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == initial
            )


@pytest.mark.parametrize("actor_present", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_null_project_get_matches_installed_orm_with_pending_work(
    test_engine, actor_present, autoflush
):
    async with _world(test_engine) as (owner, _api, world, _url):
        results = []
        for legacy in (True, False):
            async with owner() as db:
                pending = Task(
                    id=uuid.uuid4(),
                    project_id=world["project_id"],
                    display_id=f"T-NULL-{uuid.uuid4().hex[:12]}",
                    file_name="null.png",
                    file_path="null/pending.png",
                    file_type="image",
                    status="pending",
                )
                db.add(pending)
                db.autoflush = autoflush
                with warnings.catch_warnings(record=True):
                    result = (
                        await db.get(Project, None)
                        if legacy
                        else await get_workspace_project(
                            db,
                            actor_id=world["member_id"] if actor_present else None,
                            project_id=None,
                        )
                    )
                results.append(
                    (result is None, pending in db.new, inspect(pending).persistent)
                )
                await db.rollback()
        assert results[0] == results[1]


async def test_actorless_scalar_contract_and_exact_acl(test_engine):
    async with _world(test_engine) as (owner, api, _world_data, _url):
        artifact = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "alembic/sql/0293_workspace_project_read.schema.json"
            ).read_text()
        )
        signatures = artifact["x-postgresql-functions"]
        assert signatures == ["aap_workspace_v1.read_project_row(uuid)"]
        assert {
            item["signature"] for item in artifact["x-native-capabilities"]["functions"]
        } == set(signatures)
        signature = signatures[0]
        async with owner() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT pg_get_userbyid(proowner),prosecdef,proisstrict,"
                        "format_type(prorettype,NULL) FROM pg_proc WHERE oid=CAST(:fn AS regprocedure)"
                    ),
                    {"fn": signature},
                )
            ).one()
            assert row == ("workspace_code", True, True, "text")
            for role, expected in (
                ("identity_api_consumer", True),
                ("legacy_runtime", True),
                ("append_client", False),
                ("assets_code", False),
                ("inference_code", False),
                ("workspace_project_deletion_client", False),
            ):
                assert (
                    await db.scalar(
                        text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"),
                        {"role": role, "fn": signature},
                    )
                    is expected
                )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL "
                    "aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
                    "WHERE p.oid=CAST(:fn AS regprocedure) AND a.grantee=0 "
                    "AND a.privilege_type='EXECUTE')"
                ),
                {"fn": signature},
            )
        async with api() as db:
            for project_id in (None, uuid.uuid4()):
                assert (
                    await db.scalar(
                        text(
                            "SELECT aap_workspace_v1.read_project_row(CAST(:id AS uuid))"
                        ),
                        {"id": project_id},
                    )
                    is None
                )


@pytest.mark.parametrize("corruption", ["type", "scope", "duplicate_key"])
async def test_malformed_actorless_snapshot_rolls_back_prior_caller_write(
    test_engine, monkeypatch, corruption
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            initial = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
        with pytest.raises(HTTPException) as caught:
            async with owner.begin() as db:
                scalar = db.scalar

                async def corrupt(statement, *args, **kwargs):
                    value = await scalar(statement, *args, **kwargs)
                    if "aap_workspace_v1.read_project_row(" not in str(statement):
                        return value
                    row = json.loads(value)
                    if corruption == "type":
                        return row
                    if corruption == "scope":
                        row["id"] = str(uuid.uuid4())
                        return json.dumps(row)
                    return '{"id":"' + str(world["project_id"]) + '",' + value[1:]

                monkeypatch.setattr(db, "scalar", corrupt)
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='pending bad read' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await get_workspace_project(db, project_id=world["project_id"])
        assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == initial
            )


async def test_actorless_read_node_peer_has_no_python_proxy(test_engine):
    async with _world(test_engine) as (_owner, _api, world, url):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(
                Path(__file__).parent
                / "fixtures/workspace_actorless_project_read_peer.mjs"
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {
                            "database_url": url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "project_id": str(world["project_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {"ok": True, "transaction_unchanged": True}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_actual_actorless_retry_relay_reads_project_metadata(test_engine):
    from app.workers.predictions_retry import _retry_job_metadata

    async with _world(test_engine) as (owner, api, world, _url):
        failed_id = uuid.uuid4()
        async with owner.begin() as seed:
            seed.add(
                FailedPrediction(
                    id=failed_id,
                    project_id=world["project_id"],
                    error_type="test",
                    message="relay",
                    extra={},
                )
            )
        try:
            async with api() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await _assert_private_denied(db)
                project_id, payload = await _retry_job_metadata(db, failed_id)
                assert project_id == world["project_id"]
                assert payload["project_display_id"] and payload["project_name"]
                assert "actor_id" not in payload
        finally:
            async with owner.begin() as cleanup:
                await cleanup.execute(
                    delete(FailedPrediction).where(FailedPrediction.id == failed_id)
                )
