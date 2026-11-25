"""Replay pre-migration seed behavior under a fenced Project credential."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tests.contract_schemas import load_contract_schema
from datetime import UTC, datetime
from unittest.mock import AsyncMock
import uuid
import subprocess

import asyncpg
import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_user
from tests.frozen_namespaces import historical
from tests.test_workspace_consumer_profile_native import _grants, _world
from contextlib import asynccontextmanager
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.services.project_access import get_workspace_project
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_datasets_upload_zip import (
    _FakeDatasetSourceStorage,
    _patch_storage_clients,
)
from tests.test_import_nuscenes_lite import _write_fake_nuscenes
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
        from app.services import managed_original_creation

        async with owner() as db:
            await managed_original_creation.bootstrap(db)
        async with api() as db:
            for signature in (
                "yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
                "yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,:signature,'EXECUTE')"
                    ),
                    {"signature": signature},
                )
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/workspace-grants.sql",
            role,
            capability="seed_operator",
        )
        assert result.returncode == 0, result.stderr
        async with test_engine.begin() as db:
            signatures = (
                (
                    await db.execute(
                        text(
                            "SELECT p.oid::regprocedure::text FROM pg_proc p "
                            "JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='yoao_assets_v1' "
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
                "yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
                "yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
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
                ROOT / "infra/docker/workspace-grants.sql",
                role,
                capability="seed_operator",
            )
            assert result.returncode != 0
            async with api() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,"
                        "'yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)','EXECUTE')"
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
        key: getattr(project, key)
        for key in (
            "display_id",
            "name",
            "type_key",
            "type_label",
            "data_type",
            "tool_bindings",
            "scene_mode",
            "prefer_same_scene_continuation",
            "total_tasks",
        )
    }


GOLDEN = json.loads(
    (ROOT / "apps/api/tests/fixtures/workspace_seed_operator_legacy.json").read_text()
)


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
    fake = _FakeDatasetSourceStorage()
    _patch_storage_clients(monkeypatch, fake)
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
        assert info["total_items"] == 3 and info["batches"] == 0 and not keys
        assert not await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM yoao_labeling_v1.batch_rows "
                "WHERE project_id=:project_id AND display_id <> 'B-DEFAULT')"
            ),
            {"project_id": info["project_id"]},
        )
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM projects WHERE display_id='P-PC-DEV'")
                )
                == 0
            )
        await db.rollback()

    for field, actual in (
        ("directory_project", directory),
        ("nuscenes_project_before_rollback", scene),
    ):
        expected = GOLDEN["workflow"][field]
        assert actual == {key: expected[key] for key in actual}
    assert result == GOLDEN["workflow"]["directory_result"]
    assert len(fake.objects) == GOLDEN["workflow"]["fake_storage_objects"]
    assert statements == [], (
        "the actual seed must not query or write the private Project table"
    )
    record_property("legacy_source_revision", GOLDEN["source_revision"])


@pytest.mark.parametrize(
    "failure, fail_on",
    [
        (None, None),
        ("unavailable", 1),
        ("unavailable", 2),
        ("busy", 2),
        ("other_400", 2),
        ("unknown", 2),
    ],
)
async def test_nuscenes_seed_split_owns_only_the_empty_outcome(
    tmp_path, monkeypatch, seed_world, failure, fail_on
):
    from app.db import base

    owner_maker, maker = seed_world
    suffix = uuid.uuid4().hex[:8]
    project_display_id = f"P-SEED-{suffix}"
    dataset_display_id = f"DS-SEED-{suffix}"
    legacy_project_id = uuid.uuid4()
    legacy_dataset_id = uuid.uuid4()
    old_key = f"__aap_trusted_nuscenes__-{uuid.uuid4().hex}/legacy/old.pcd"
    async with owner_maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"seed-batch-{suffix}@example.test", "Seed owner"
        )
        actor_id = actor.id
        db.add_all(
            [
                Project(
                    id=legacy_project_id,
                    owner_id=actor_id,
                    display_id=project_display_id,
                    name="点云联合标注 (dev)",
                    type_key="lidar",
                    type_label="点云检测",
                    data_type="lidar",
                ),
                Dataset(
                    id=legacy_dataset_id,
                    created_by=actor_id,
                    display_id=dataset_display_id,
                    name="pc-scene-dev",
                    data_type="point_cloud",
                    file_count=1,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                ProjectDataset(
                    project_id=legacy_project_id, dataset_id=legacy_dataset_id
                ),
                DatasetItem(
                    dataset_id=legacy_dataset_id,
                    file_name="old.pcd",
                    file_path=old_key,
                    file_type="point_cloud",
                ),
            ]
        )

    fake = _FakeDatasetSourceStorage()
    fake.objects[old_key] = b"legacy point cloud"
    _patch_storage_clients(monkeypatch, fake)
    nuscenes = tmp_path / "nuscenes"
    _write_fake_nuscenes(nuscenes, scenes=2, samples_per=2)
    monkeypatch.setattr(seed, "ensure_nuscenes_mini", lambda **kwargs: nuscenes)
    monkeypatch.setattr(seed, "NUSCENES_SCENES", ["scene-0000", "scene-0001"])
    monkeypatch.setattr(seed, "PROJECT_DISPLAY_ID", project_display_id)
    monkeypatch.setattr(seed, "DATASET_DISPLAY_ID", dataset_display_id)
    monkeypatch.setattr(seed, "_ensure_admin", AsyncMock(return_value=actor_id))

    errors = {
        "other_400": HTTPException(400, "No unassigned tasks to split"),
        "unknown": RuntimeError("unexpected batch response"),
    }
    creates = []
    commits = []
    cleanup = seed.cleanup_legacy_dev_seed_objects

    def cleanup_after_commit(keys):
        assert commits == [True]
        assert keys == [old_key] and old_key in fake.objects
        cleanup(keys)

    @asynccontextmanager
    async def seed_session():
        async with maker() as db:
            event.listen(
                db.sync_session, "after_commit", lambda session: commits.append(True)
            )
            scalar = db.scalar

            async def batch_scalar(statement, params=None, **kwargs):
                if "yoao_labeling_v1.batch_control_command(" not in str(statement):
                    return await scalar(statement, params, **kwargs)
                command = json.loads(params["request"])
                if command["operation"] != "create_batch":
                    return await scalar(statement, params, **kwargs)
                creates.append(command)
                if len(creates) != fail_on:
                    return await scalar(statement, params, **kwargs)
                if fail_on == 2:
                    first_batch = uuid.UUID(creates[0]["batch_id"])
                    assert (
                        await scalar(
                            text(
                                "SELECT count(*) FROM yoao_labeling_v1.task_rows "
                                "WHERE batch_id=:batch"
                            ),
                            {"batch": first_batch},
                        )
                        == 2
                    )
                if failure == "unavailable":
                    await scalar(statement, params, **kwargs)
                    return None
                if failure == "busy":
                    return await scalar(
                        text("DO $$ BEGIN RAISE SQLSTATE '55P03'; END $$")
                    )
                raise errors[failure]

            monkeypatch.setattr(db, "scalar", batch_scalar)
            yield db

    monkeypatch.setattr(base, "async_session", seed_session)
    monkeypatch.setattr(seed, "cleanup_legacy_dev_seed_objects", cleanup_after_commit)
    if failure is None:
        await seed.main()
        assert commits == [True]
        assert old_key in fake.deleted and old_key not in fake.objects
    else:
        error_type = RuntimeError if failure == "unknown" else HTTPException
        with pytest.raises(error_type) as exc:
            await seed.main()
        assert len(creates) == fail_on
        assert commits == [] and old_key not in fake.deleted
        assert fake.objects[old_key] == b"legacy point cloud"
        if failure in errors:
            assert exc.value is errors[failure]
        elif failure == "unavailable":
            assert exc.value.status_code == 503
            assert exc.value.detail == {"reason": "batch_control_unavailable"}
        else:
            assert exc.value.status_code == 409
            assert exc.value.detail == {"reason": "batch_control_busy"}

    async with owner_maker() as db:
        project = (
            await db.execute(
                text("SELECT id, name FROM projects WHERE display_id=:display_id"),
                {"display_id": project_display_id},
            )
        ).one()
        dataset = (
            await db.execute(
                text("SELECT id, name FROM datasets WHERE display_id=:display_id"),
                {"display_id": dataset_display_id},
            )
        ).one()
        batches = (
            await db.execute(
                text(
                    "SELECT name, total_tasks FROM task_batches "
                    "WHERE project_id=:project_id AND display_id <> 'B-DEFAULT' ORDER BY name"
                ),
                {"project_id": project.id},
            )
        ).all()
        if failure is None:
            assert project.id != legacy_project_id and dataset.id != legacy_dataset_id
            assert batches == [("Scene · scene-0000", 2), ("Scene · scene-0001", 2)]
        else:
            assert project == (legacy_project_id, "点云联合标注 (dev)")
            assert dataset == (legacy_dataset_id, "pc-scene-dev")
            assert batches == []
            assert (
                await db.scalar(
                    text(
                        "SELECT file_path FROM dataset_items WHERE dataset_id=:dataset_id"
                    ),
                    {"dataset_id": legacy_dataset_id},
                )
                == old_key
            )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM task_batches WHERE id=ANY(:batch_ids))"
                ),
                {"batch_ids": [uuid.UUID(command["batch_id"]) for command in creates]},
            )

    if failure is None:
        async with maker.begin() as db:
            info, old_keys = await seed.seed_nuscenes_scene(
                db, owner_id=actor_id, offline=True
            )
            assert info["project_id"] == project.id and info["dataset_id"] == dataset.id
            assert info["batches"] == 0 and old_keys == []
            assert all(scene["skipped"] for scene in info["scenes"])


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
    from yoao_workspace.seed_project_contract import (
        decode_nuscenes_seed_defaults_write,
        decode_point_cloud_seed_project_created,
        seed_project_schema,
    )
    from tests.test_workspace_project_maintenance_native import _dsn

    expected = seed_project_schema()
    assert (
        json.loads(
            (
                ROOT
                / "apps/workspace/src/yoao_workspace/seed_project_protocol.schema.json"
            ).read_text()
        )
        == expected
    )
    assert load_contract_schema(
        "0301_workspace_seed_project.schema.json"
    ) == historical(expected)
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
        create = "SELECT yoao_workspace_v1.create_point_cloud_seed_project($1,$2,$3,$4,$5::jsonb)"
        write = "SELECT yoao_workspace_v1.write_nuscenes_seed_defaults($1,$2,$3::jsonb)"
        for role in (
            "identity_api_consumer",
            "yoao_append_client",
            "yoao_asset_operator_client",
        ):
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
