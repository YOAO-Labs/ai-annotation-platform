"""Replay pre-migration seed behavior under a fenced Project credential."""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import UTC, datetime
from unittest.mock import AsyncMock
import uuid
import subprocess
import struct

import asyncpg
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_user
from tests.test_workspace_consumer_profile_native import _grants, _world
from contextlib import asynccontextmanager
from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.services.project_access import get_workspace_project
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_import_nuscenes_lite import (
    _patch_storage_clients,
    _write_fake_nuscenes,
)
from scripts import seed_pointcloud as seed


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL and fenced credentials",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def _seed_world(test_engine):
    async with _world(test_engine) as (owner, api, role, _url):
        async with api() as db:
            for signature in (
                "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
                "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,:signature,'EXECUTE')"
                    ),
                    {"signature": signature},
                )
        result = await _grants(
            test_engine, ROOT / "infra/docker/workspace-seed-operator-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        # Match the existing development operator's Assets capabilities. This
        # slice fences Project; it does not qualify the operator's other owners.
        async with test_engine.begin() as db:
            signatures = (
                (
                    await db.execute(
                        text(
                            "SELECT p.oid::regprocedure::text FROM pg_proc p "
                            "JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='aap_assets_v1' "
                            "AND has_function_privilege('legacy_runtime',p.oid,'EXECUTE')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for signature in signatures:
                await db.execute(
                    text(f'GRANT EXECUTE ON FUNCTION {signature} TO "{role}"')
                )
        async with api() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.projects',"
                    "'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege("
                    "current_user,'public.projects','SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
            for signature in (
                "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
                "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege('identity_api_consumer',:signature,'EXECUTE')"
                    ),
                    {"signature": signature},
                )
        yield owner, api


@pytest.fixture
async def seed_world(test_engine):
    async with _seed_world(test_engine) as world:
        yield world


async def test_seed_profile_rejects_a_child_that_could_inherit_operator_writes(
    test_engine,
):
    async with _world(test_engine) as (_owner, api, role, _url):
        child = "seed_child_" + uuid.uuid4().hex[:12]
        try:
            async with test_engine.begin() as db:
                await db.execute(text(f'CREATE ROLE "{child}" LOGIN'))
                await db.execute(text(f'GRANT "{role}" TO "{child}"'))
            result = await _grants(
                test_engine,
                ROOT / "infra/docker/workspace-seed-operator-grants.sql",
                role,
            )
            assert result.returncode != 0
            assert "non-admin, non-inheriting LOGIN" in result.stderr
            async with api() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,"
                        "'aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)','EXECUTE')"
                    )
                )
        finally:
            async with test_engine.begin() as db:
                await db.execute(text(f'REVOKE "{role}" FROM "{child}"'))
                await db.execute(text(f'DROP ROLE "{child}"'))


async def _project_state(db, display_id):
    from app.services.project_workspace import find_import_project

    project_id = await find_import_project(db, display_id)
    project = await get_workspace_project(
        db, project_id=project_id, populate_existing=True
    )
    return {
        column.key: getattr(project, column.key)
        for column in Project.__table__.columns
        if column.key not in {"id", "owner_id", "created_at", "updated_at"}
    }


GOLDEN = json.loads(
    (ROOT / "apps/api/tests/fixtures/workspace_seed_operator_legacy.json").read_text()
)
# PostgreSQL JSONB renders REAL using its shortest decimal; ORM/native rows
# expose the exact binary32 value. Decode that one physical column losslessly.
for _field in ("directory_project", "nuscenes_project_before_rollback"):
    _state = GOLDEN["workflow"][_field]
    _state["box_threshold"] = struct.unpack(
        "!f", struct.pack("!f", _state["box_threshold"])
    )[0]


async def test_directory_and_nuscenes_match_original_workflow(
    test_engine, tmp_path, monkeypatch, seed_world, record_property
):
    _verify_postgres(test_engine.url)
    owner_maker, maker = seed_world
    async with owner_maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"seed-capture-{uuid.uuid4()}@example.test", "Seed owner"
        )
        actor_id = actor.id
    fake = _patch_storage_clients(monkeypatch)
    fixture = tmp_path / "pointcloud"
    (fixture / "lidar").mkdir(parents=True)
    (fixture / "lidar/000000.pcd").write_bytes(
        b"# .PCD v0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        b"WIDTH 1\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS 1\nDATA ascii\n0 0 0\n"
    )
    commits = []
    statements = []

    def committed(session):
        commits.append(True)

    def executed(_connection, _cursor, statement, _parameters, _context, _many):
        if "projects" in statement.lower():
            statements.append(statement)

    async with maker() as db:
        event.listen(db.sync_session, "after_commit", committed)
        event.listen(maker.kw["bind"].sync_engine, "before_cursor_execute", executed)
        try:
            result = await seed.seed_pointcloud(
                db,
                owner_id=actor_id,
                fixture=fixture,
                axis_convention="apollo",
                project_display_id="P-SEED-CAPTURE",
                dataset_display_id="D-SEED-CAPTURE",
                dataset_name="seed-capture-directory",
            )
        finally:
            event.remove(
                maker.kw["bind"].sync_engine, "before_cursor_execute", executed
            )
            event.remove(db.sync_session, "after_commit", committed)
        directory = await _project_state(db, "P-SEED-CAPTURE")
        assert directory["scene_mode"] is False
        assert directory["prefer_same_scene_continuation"] is False
        assert result["tasks"] == {"created": 1, "total": 1}
        assert commits, "directory workflow must retain its existing commits"
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM projects WHERE display_id='P-SEED-CAPTURE'"
                    )
                )
                == 1
            )
        skipped = await seed.seed_pointcloud(
            db,
            owner_id=actor_id,
            fixture=tmp_path / "missing-but-existing-project-skips",
            axis_convention="apollo",
            project_display_id="P-SEED-CAPTURE",
        )
        assert skipped is None

    nuscenes = tmp_path / "nuscenes"
    _write_fake_nuscenes(nuscenes, scenes=1, samples_per=1)
    monkeypatch.setattr(seed, "ensure_nuscenes_mini", lambda **kwargs: nuscenes)
    monkeypatch.setattr(seed, "NUSCENES_SCENES", ["scene-0000"])
    async with maker() as db:
        info, keys = await seed.seed_nuscenes_scene(
            db, owner_id=actor_id, offline=True, create_batch=False
        )
        scene = await _project_state(db, seed.PROJECT_DISPLAY_ID)
        assert scene["scene_mode"] is True
        assert scene["prefer_same_scene_continuation"] is True
        assert info["total_items"] == 3 and not keys
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM projects WHERE display_id='P-PC-DEV'")
                )
                == 0
            )
        await db.rollback()

    report = {
        "directory_project": directory,
        "directory_result": result,
        "directory_workflow_commits": len(commits),
        "existing_project_skips_missing_fixture": True,
        "nuscenes_project_before_rollback": scene,
        "nuscenes_not_visible_before_caller_commit": True,
        "fake_storage_objects": len(fake.objects),
    }
    for field in ("directory_project", "nuscenes_project_before_rollback"):
        expected = GOLDEN["workflow"][field]
        differences = {
            key: {"expected": expected.get(key), "actual": report[field].get(key)}
            for key in set(expected) | set(report[field])
            if expected.get(key) != report[field].get(key)
        }
        assert not differences, differences
    assert report == GOLDEN["workflow"]
    assert statements == [], (
        "the actual seed must not query or write the private Project table"
    )
    record_property("legacy_source_revision", GOLDEN["source_revision"])


@pytest.mark.parametrize(
    "autoflush,dirty", [(True, False), (False, False), (False, True)]
)
async def test_import_select_hydrates_expired_fields_without_overwriting_loaded_values(
    seed_world, autoflush, dirty
):
    from app.services.project_workspace import read_import_project

    owner_maker, operator = seed_world
    display_id = "P-EXPIRED-" + uuid.uuid4().hex[:8]
    async with owner_maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"expired-{uuid.uuid4()}@example.test", "Owner"
        )
        project = Project(
            display_id=display_id,
            name="recognized seed",
            owner_id=actor.id,
            type_key="lidar",
            type_label="点云检测",
        )
        db.add(project)
        await db.flush()
        project_id, owner_id = project.id, actor.id
    async with operator(autoflush=autoflush) as db:
        cached = await get_workspace_project(db, project_id=project_id)
        if dirty:
            cached.status = "paused"
        db.expire(cached, ["name", "owner_id"])
        observed = await read_import_project(db, display_id)
        assert observed is cached
        assert observed.name == "recognized seed" and observed.owner_id == owner_id
        assert observed.status == ("paused" if dirty else "in_progress")
        await db.rollback()


async def test_seed_native_descriptor_restrictions_errors_and_rollback(test_engine):
    from aap_workspace.seed_project_contract import (
        decode_nuscenes_seed_defaults_write,
        decode_point_cloud_seed_project_created,
        seed_project_schema,
    )
    from tests.test_workspace_project_maintenance_native import _dsn

    expected = seed_project_schema()
    for path in (
        "apps/workspace/src/aap_workspace/seed_project_protocol.schema.json",
        "apps/api/alembic/sql/0301_workspace_seed_project.schema.json",
    ):
        assert json.loads((ROOT / path).read_text()) == expected
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"native-seed-{uuid.uuid4()}@example.test", "Owner"
        )
        owner_id = actor.id
    client = await asyncpg.connect(_dsn(test_engine), command_timeout=10)
    try:
        project_id = uuid.uuid4()
        display_id = "P-NATIVE-" + uuid.uuid4().hex[:8]
        create = "SELECT aap_workspace_v1.create_point_cloud_seed_project($1,$2,$3,$4,$5::jsonb)"
        write = "SELECT aap_workspace_v1.write_nuscenes_seed_defaults($1,$2,$3::jsonb)"
        for role in ("identity_api_consumer", "append_client", "asset_operator_client"):
            async with maker() as db:
                for function in expected["x-native-capabilities"]["functions"]:
                    assert not await db.scalar(
                        text("SELECT has_function_privilege(:role,:sig,'EXECUTE')"),
                        {"role": role, "sig": function["signature"]},
                    )
        for params in (
            (None, display_id, "seed", owner_id, "{}"),
            (project_id, display_id, None, owner_id, "{}"),
            (project_id, display_id, "seed", owner_id, "[]"),
        ):
            with pytest.raises(asyncpg.InvalidParameterValueError):
                await client.fetchval(create, *params)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await client.fetchval(
                create, project_id, display_id, "seed", uuid.uuid4(), "{}"
            )
        assert (
            decode_nuscenes_seed_defaults_write(
                await client.fetchval(write, project_id, "seed", "{}"),
                project_id=project_id,
            )
            is None
        )
        await client.execute("BEGIN")
        assert (
            decode_point_cloud_seed_project_created(
                await client.fetchval(
                    create, project_id, display_id, "seed", owner_id, "{}"
                ),
                display_id=display_id,
                expected_project_id=project_id,
            )
            == project_id
        )
        first_time = decode_nuscenes_seed_defaults_write(
            await client.fetchval(write, project_id, "scene", "{}"),
            project_id=project_id,
        )
        assert first_time is not None
        assert (
            decode_nuscenes_seed_defaults_write(
                await client.fetchval(write, project_id, "scene", "{}"),
                project_id=project_id,
            )
            == first_time
        )
        async with maker() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM projects WHERE id=:id"),
                    {"id": project_id},
                )
                == 0
            )
        await client.execute("ROLLBACK")
        await client.fetchval(create, project_id, display_id, "seed", owner_id, "{}")
        with pytest.raises(asyncpg.UniqueViolationError):
            await client.fetchval(
                create, uuid.uuid4(), display_id, "duplicate", owner_id, "{}"
            )
        await client.execute("BEGIN READ ONLY")
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await client.fetchval(write, project_id, "read only", "{}")
        await client.execute("ROLLBACK")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await client.execute("SELECT id FROM public.projects")
    finally:
        await client.close()


async def test_node_producer_uses_seed_native_contract_without_private_tables(
    test_engine,
):
    from tests.test_workspace_project_maintenance_native import _dsn

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"node-seed-{uuid.uuid4()}@example.test", "Owner"
        )
        owner_id = actor.id
    process = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/workspace_seed_project_peer.mjs")],
        input=json.dumps(
            {"database_url": _dsn(test_engine), "owner_id": str(owner_id)}
        ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "ok": True,
        "seed_functions": 2,
        "rolled_back": True,
    }


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("state", ["changed", "equal", "missing"])
async def test_nuscenes_reused_project_matches_original(
    test_engine, tmp_path, monkeypatch, autoflush, state, seed_world, record_property
):
    """Isolate the old post-import consumer, with its existing identity cache."""
    _verify_postgres(test_engine.url)
    owner_maker, maker = seed_world
    old_time = datetime(2000, 1, 1, tzinfo=UTC)
    project_id = uuid.uuid4()
    async with owner_maker.begin() as db:
        owner = await create_user(
            db, "super_admin", f"reused-seed-{uuid.uuid4()}@example.test", "Owner"
        )
        owner_id = owner.id
        if state != "missing":
            db.add(
                Project(
                    id=project_id,
                    display_id="P-REUSED-" + uuid.uuid4().hex[:10],
                    name=seed.NUSCENES_PROJECT_NAME if state == "equal" else "old",
                    owner_id=owner_id,
                    type_key="lidar",
                    type_label="点云检测",
                    scene_mode=state == "equal",
                    prefer_same_scene_continuation=state == "equal",
                    tool_bindings=seed.POINTCLOUD_TOOL_BINDINGS
                    if state == "equal"
                    else {},
                    updated_at=old_time,
                )
            )
        dataset = Dataset(
            display_id="DS-REUSED-" + uuid.uuid4().hex[:10],
            name="old dataset",
            created_by=owner_id,
            data_type="point_cloud",
        )
        db.add(dataset)
        await db.flush()
        dataset_id = dataset.id

    monkeypatch.setattr(seed, "ensure_nuscenes_mini", lambda **kwargs: tmp_path)
    monkeypatch.setattr(seed, "_remove_legacy_dev_seed", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        seed,
        "import_nuscenes",
        AsyncMock(return_value={"project_id": project_id, "dataset_id": dataset_id}),
    )
    batch_snapshot = []

    async def split(service, requested_id, request, *, created_by):
        project = await get_workspace_project(service.db, project_id=requested_id)
        batch_snapshot.append(
            None
            if project is None
            else [
                project.name,
                project.scene_mode,
                project.prefer_same_scene_continuation,
                project.tool_bindings,
            ]
        )
        return []

    monkeypatch.setattr("app.services.batch.BatchService.split", split)
    commits = []
    async with maker(autoflush=autoflush) as db:
        cached = await get_workspace_project(db, project_id=project_id)
        pending = Dataset(
            display_id="DS-PENDING-" + uuid.uuid4().hex[:8],
            name="pending caller work",
            created_by=owner_id,
            data_type="image",
        )
        db.add(pending)
        event.listen(
            db.sync_session, "after_commit", lambda session: commits.append(True)
        )
        info, keys = await seed.seed_nuscenes_scene(db, owner_id=owner_id)
        current = await get_workspace_project(db, project_id=project_id)
        assert current is cached
        assert pending.id is not None
        observed = await get_workspace_project(
            db, project_id=project_id, populate_existing=True
        )
        row = (
            None
            if observed is None
            else {
                key: getattr(observed, key)
                for key in (
                    "name",
                    "scene_mode",
                    "prefer_same_scene_continuation",
                    "tool_bindings",
                    "updated_at",
                )
            }
        )
        if state == "missing":
            assert row is None and batch_snapshot == [None]
        else:
            assert row["name"] == seed.NUSCENES_PROJECT_NAME
            assert row["scene_mode"] and row["prefer_same_scene_continuation"]
            assert row["tool_bindings"] == seed.POINTCLOUD_TOOL_BINDINGS
            assert (row["updated_at"] == old_time) == (state == "equal")
            assert batch_snapshot == [
                [seed.NUSCENES_PROJECT_NAME, True, True, seed.POINTCLOUD_TOOL_BINDINGS]
            ]
        assert not commits and info["batches"] == 0 and keys == []
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT name FROM datasets WHERE id=:id"), {"id": dataset_id}
                )
                == "old dataset"
            )
        await db.rollback()
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT name FROM datasets WHERE id=:id"), {"id": dataset_id}
                )
                == "old dataset"
            )

    report = {
        "state": state,
        "autoflush": autoflush,
        "cached_identity_retained": True,
        "batch_snapshot": batch_snapshot,
        "equal_timestamp_preserved": state == "equal",
        "pending_work_flushed": True,
        "commits": len(commits),
        "caller_rollback_retained": True,
        "scope": "original post-import consumer; importer and batch execution isolated",
    }
    expected = next(
        item
        for item in GOLDEN["reused"]
        if item["state"] == state and item["autoflush"] == autoflush
    )
    assert report == expected
    record_property("legacy_source_revision", GOLDEN["source_revision"])
