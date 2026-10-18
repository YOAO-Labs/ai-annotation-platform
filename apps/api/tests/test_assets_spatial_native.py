"""Native Assets spatial capabilities; root runs these on its disposable DB."""

from __future__ import annotations

import json
import os
import uuid
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_assets_catalog.spatial_contract import (
    LayoutPage,
    PoseMutationResult,
    PosePage,
    PlannedScene,
    PreparationMutationResult,
    PreparationPage,
    SceneAssignment,
    SceneMutationResult,
    SpatialActorResult,
    SpatialLayoutPlan,
    SPATIAL_NATIVE_SIGNATURES,
    SPATIAL_SCHEMA,
    decode_spatial_reply,
)
from aap_assets_catalog.spatial_values import FramePose
from tests.factory import create_project
from tests.test_assets_catalog_native import (
    _CATALOG_LOGIN_FUNCTIONS,
    _CATALOG_URL,
    _ROLE_ACCEPTANCE,
    _call,
    _seed_actor,
    _seed_dataset,
)

pytestmark = pytest.mark.asyncio

_GUARD_ACTOR = text(
    "SELECT aap_assets_v1.guard_spatial_actor("
    "CAST(:actor_id AS uuid), CAST(:access AS text))"
)
_CREATE_SCENE = text(
    "SELECT aap_assets_v1.create_spatial_scene("
    "CAST(:operation_id AS uuid), CAST(:scene_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:name AS text), "
    "CAST(:source_format AS text), CAST(:source_metadata AS jsonb), "
    "CAST(:created_by AS uuid))"
)
_ASSIGN_ITEMS = text(
    "SELECT aap_assets_v1.assign_spatial_items("
    "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:assignments AS jsonb))"
)
_READ_LAYOUT = text(
    "SELECT aap_assets_v1.read_spatial_layout("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:creation_operation_id AS uuid), CAST(:after_item_id AS uuid), "
    "CAST(:page_size AS integer))"
)
_APPLY_LAYOUT = text(
    "SELECT aap_assets_v1.apply_spatial_layout("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:mode AS text), "
    "CAST(:dry_run AS boolean), CAST(:plan AS jsonb), CAST(:snapshot AS jsonb))"
)
_UPSERT_POSES = text(
    "SELECT aap_assets_v1.upsert_spatial_poses("
    "CAST(:operation_id AS uuid), CAST(:scene_id AS uuid), CAST(:poses AS jsonb))"
)
_READ_POSES = text(
    "SELECT aap_assets_v1.read_spatial_poses("
    "CAST(:actor_id AS uuid), CAST(:scene_id AS uuid), "
    "CAST(:after_frame_index AS integer), CAST(:page_size AS integer))"
)
_ASSOCIATE = text(
    "SELECT aap_assets_v1.associate_project("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
)
_CREATE_JOB = text(
    "SELECT aap_jobs_v1.create_task_creation_job("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:association_id AS uuid))"
)
_ACCEPT_TASK_CREATION = text(
    "SELECT aap_labeling_v1.accept_dataset_task_creation("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:association_id AS uuid), CAST(:job_id AS uuid), "
    "CAST(:mode AS text), CAST(:chunk_size AS integer))"
)
_CANCEL_JOB = text(
    "SELECT aap_jobs_v1.cancel_task_creation_job("
    "CAST(:actor_id AS uuid), CAST(:job_id AS uuid))"
)
_UNLINK_DATASET = text(
    "SELECT aap_workspace_v1.unlink_dataset("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:cleanup_bucket AS text), CAST(:audit_context AS jsonb))"
)
_READ_PREPARATION = text(
    "SELECT aap_assets_v1.read_pointcloud_preparation("
    "CAST(:creation_operation_id AS uuid), CAST(:after_item_id AS uuid), "
    "CAST(:page_size AS integer))"
)
_APPLY_PREPARATION = text(
    "SELECT aap_assets_v1.apply_pointcloud_preparation("
    "CAST(:creation_operation_id AS uuid), CAST(:snapshot AS jsonb), "
    "CAST(:calibration_updates AS jsonb), CAST(:frame_plan AS jsonb))"
)


def _json_value(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _spatial_result(value: object, result_type):
    return decode_spatial_reply(_json_value(value), result_type)


def _spatial_error(value: object, code: str) -> None:
    assert _json_value(value) == {
        "schema": SPATIAL_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


async def _create_scene(
    db_session,
    *,
    operation_id: UUID,
    dataset_id: UUID,
    name: str,
    scene_id: UUID | None = None,
    created_by: UUID | None = None,
) -> SceneMutationResult:
    reply = await _call(
        db_session,
        _CREATE_SCENE,
        {
            "operation_id": operation_id,
            "scene_id": scene_id,
            "dataset_id": dataset_id,
            "name": name,
            "source_format": "operator",
            "source_metadata": json.dumps({"origin": "native-test"}),
            "created_by": created_by,
        },
    )
    return _spatial_result(reply, SceneMutationResult)


def _single_frame_plan(dataset_name: str, dataset_item_id: UUID) -> SpatialLayoutPlan:
    scene_id = uuid.uuid4()
    return SpatialLayoutPlan(
        scenes=[
            PlannedScene(
                id=scene_id,
                name=dataset_name,
                source_format="inferred",
                source_metadata={"mode": "single", "group_key": "_single"},
            )
        ],
        assignments=[
            SceneAssignment(item_id=dataset_item_id, scene_id=scene_id, frame_index=0)
        ],
        created_scenes=1,
        assigned_items=1,
        skipped_items=0,
        dry_run=False,
        notes=[],
    )


async def _seed_pointcloud_preparation(db_session, *, actor_role: str) -> dict:
    from app.db.models.dataset import DatasetItem

    actor_id = await _seed_actor(db_session, role=actor_role)
    project = await create_project(
        db_session,
        owner_id=actor_id,
        type_key="lidar",
        type_label="Point cloud",
    )
    project.data_type = "lidar"
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"spatial-prep-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=1,
    )
    item = DatasetItem(
        dataset_id=dataset_id,
        file_name="000001.pcd",
        file_path=f"spatial-prep/{dataset_id}/lidar/000001.pcd",
        file_type="point_cloud",
    )
    db_session.add(item)
    await db_session.flush()
    association = _json_value(
        await _call(
            db_session,
            _ASSOCIATE,
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project.id,
            },
        )
    )
    assert association["schema"] == "aap-dataset-association/v1"
    assert association["ok"] is True
    return {
        "actor_id": actor_id,
        "project_id": project.id,
        "dataset_id": dataset_id,
        "association_id": UUID(association["data"]["link_id"]),
        "item_id": item.id,
    }


async def _accept_pointcloud_creation(
    db_session, scope: dict, *, job_id: UUID | None
) -> UUID:
    operation_id = uuid.uuid4()
    if job_id is not None:
        created = _json_value(
            await _call(
                db_session,
                _CREATE_JOB,
                {
                    "operation_id": job_id,
                    "actor_id": scope["actor_id"],
                    "dataset_id": scope["dataset_id"],
                    "project_id": scope["project_id"],
                    "association_id": scope["association_id"],
                },
            )
        )
        assert created["ok"] is True
        operation_id = job_id
    accepted = _json_value(
        await _call(
            db_session,
            _ACCEPT_TASK_CREATION,
            {
                "operation_id": operation_id,
                "actor_id": scope["actor_id"],
                "dataset_id": scope["dataset_id"],
                "project_id": scope["project_id"],
                "association_id": scope["association_id"],
                "job_id": job_id,
                "mode": "pointcloud",
                "chunk_size": 100,
            },
        )
    )
    assert accepted["ok"] is True
    return operation_id


async def _read_preparation(db_session, operation_id: UUID) -> PreparationPage:
    reply = await _call(
        db_session,
        _READ_PREPARATION,
        {
            "creation_operation_id": operation_id,
            "after_item_id": None,
            "page_size": 200,
        },
    )
    return _spatial_result(reply, PreparationPage)


async def _apply_preparation(
    db_session,
    operation_id: UUID,
    page: PreparationPage,
    frame_plan: SpatialLayoutPlan,
):
    return await _call(
        db_session,
        _APPLY_PREPARATION,
        {
            "creation_operation_id": operation_id,
            "snapshot": json.dumps({"snapshot_digest": page.snapshot_digest}),
            "calibration_updates": json.dumps([]),
            "frame_plan": frame_plan.model_dump_json(),
        },
    )


async def test_spatial_capability_returns_a_real_typed_envelope(db_session) -> None:
    actor_id = await _seed_actor(db_session)

    reply = await _call(
        db_session,
        _GUARD_ACTOR,
        {"actor_id": actor_id, "access": "manager_write"},
    )
    actor = _spatial_result(reply, SpatialActorResult)

    assert actor.actor_id == actor_id
    assert actor.access == "manager_write"


async def test_create_scene_replay_and_dataset_name_conflict(db_session) -> None:
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    operation_id = uuid.uuid4()
    scene_id = uuid.uuid4()

    first = await _create_scene(
        db_session,
        operation_id=operation_id,
        scene_id=scene_id,
        dataset_id=dataset_id,
        name="scene-1",
        created_by=actor_id,
    )
    replay = await _create_scene(
        db_session,
        operation_id=operation_id,
        scene_id=scene_id,
        dataset_id=dataset_id,
        name="scene-1",
        created_by=actor_id,
    )
    assert replay == first
    assert first.scene.id == scene_id
    assert first.scene.dataset_id == dataset_id

    conflict = await _call(
        db_session,
        _CREATE_SCENE,
        {
            "operation_id": uuid.uuid4(),
            "scene_id": uuid.uuid4(),
            "dataset_id": dataset_id,
            "name": "scene-1",
            "source_format": "operator",
            "source_metadata": json.dumps({}),
            "created_by": actor_id,
        },
    )
    _spatial_error(conflict, "scene_conflict")
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.scenes WHERE dataset_id=:id"),
            {"id": dataset_id},
        )
        == 1
    )


async def test_assign_spatial_items_rejects_cross_dataset_without_writes(
    db_session,
) -> None:
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    other_dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"other-{uuid.uuid4().hex[:8]}",
    )
    scene = await _create_scene(
        db_session,
        operation_id=uuid.uuid4(),
        dataset_id=dataset_id,
        name="target-scene",
    )
    from app.db.models.dataset import DatasetItem

    item = DatasetItem(
        dataset_id=other_dataset_id,
        file_name="outside.jpg",
        file_path="outside/outside.jpg",
        file_type="image",
    )
    db_session.add(item)
    await db_session.flush()

    rejected = await _call(
        db_session,
        _ASSIGN_ITEMS,
        {
            "operation_id": uuid.uuid4(),
            "dataset_id": dataset_id,
            "assignments": json.dumps(
                [
                    {
                        "item_id": str(item.id),
                        "scene_id": str(scene.scene_id),
                        "frame_index": 0,
                    }
                ]
            ),
        },
    )

    _spatial_error(rejected, "source_changed")
    assignment = (
        await db_session.execute(
            text("SELECT scene_id, frame_index FROM public.dataset_items WHERE id=:id"),
            {"id": item.id},
        )
    ).one()
    assert assignment == (None, None)


async def test_layout_snapshot_change_fails_before_any_scene_or_item_write(
    db_session,
) -> None:
    from app.db.models.dataset import DatasetItem

    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="image")
    original = DatasetItem(
        dataset_id=dataset_id,
        file_name="000001.jpg",
        file_path="layout/000001.jpg",
        file_type="image",
    )
    db_session.add(original)
    await db_session.flush()
    layout = _spatial_result(
        await _call(
            db_session,
            _READ_LAYOUT,
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "creation_operation_id": None,
                "after_item_id": None,
                "page_size": 200,
            },
        ),
        LayoutPage,
    )
    plan = _single_frame_plan(layout.dataset_name, original.id)
    added = DatasetItem(
        dataset_id=dataset_id,
        file_name="000002.jpg",
        file_path="layout/000002.jpg",
        file_type="image",
    )
    db_session.add(added)
    await db_session.flush()

    rejected = await _call(
        db_session,
        _APPLY_LAYOUT,
        {
            "operation_id": uuid.uuid4(),
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "mode": "single",
            "dry_run": False,
            "plan": plan.model_dump_json(),
            "snapshot": json.dumps({"snapshot_digest": layout.snapshot_digest}),
        },
    )

    _spatial_error(rejected, "source_changed")
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.scenes WHERE dataset_id=:id"),
            {"id": dataset_id},
        )
        == 0
    )
    assignments = (
        await db_session.execute(
            text(
                "SELECT scene_id, frame_index FROM public.dataset_items "
                "WHERE id IN (:first, :second) ORDER BY id"
            ),
            {"first": original.id, "second": added.id},
        )
    ).all()
    assert assignments == [(None, None), (None, None)]


async def test_pose_pages_preserve_signed_timestamps_and_complete_trajectory(
    db_session,
) -> None:
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    scene = await _create_scene(
        db_session,
        operation_id=uuid.uuid4(),
        dataset_id=dataset_id,
        name="pose-scene",
    )
    poses = [
        FramePose(
            frame_index=index,
            timestamp_us=timestamp,
            ego_translation=[index, 0, 0],
            ego_rotation=[1, 0, 0, 0],
        )
        for index, timestamp in enumerate((-12, 0, -1))
    ]
    upsert = _spatial_result(
        await _call(
            db_session,
            _UPSERT_POSES,
            {
                "operation_id": uuid.uuid4(),
                "scene_id": scene.scene_id,
                "poses": json.dumps([pose.model_dump(mode="json") for pose in poses]),
            },
        ),
        PoseMutationResult,
    )
    assert upsert.upserted == 3

    first = _spatial_result(
        await _call(
            db_session,
            _READ_POSES,
            {
                "actor_id": actor_id,
                "scene_id": scene.scene_id,
                "after_frame_index": None,
                "page_size": 2,
            },
        ),
        PosePage,
    )
    second = _spatial_result(
        await _call(
            db_session,
            _READ_POSES,
            {
                "actor_id": actor_id,
                "scene_id": scene.scene_id,
                "after_frame_index": first.next_frame_index,
                "page_size": 2,
            },
        ),
        PosePage,
    )

    assert first.complete is False and first.next_frame_index == 1
    assert second.complete is True and second.next_frame_index is None
    assert first.snapshot_digest == second.snapshot_digest
    assert [pose.frame_index for pose in (*first.poses, *second.poses)] == [0, 1, 2]
    assert [pose.timestamp_us for pose in (*first.poses, *second.poses)] == [
        -12,
        0,
        -1,
    ]


async def test_accepted_pointcloud_preparation_survives_actor_disable(
    db_session,
) -> None:
    from app.db.models.user import User

    scope = await _seed_pointcloud_preparation(
        db_session,
        actor_role="project_admin",
    )
    operation_id = await _accept_pointcloud_creation(db_session, scope, job_id=None)
    page = await _read_preparation(db_session, operation_id)
    assert page.creation_operation_id == operation_id
    assert page.actor_id is None
    plan = _single_frame_plan(page.dataset_name, scope["item_id"])
    actor = await db_session.get(User, scope["actor_id"])
    assert actor is not None
    actor.is_active = False
    await db_session.flush()

    result = _spatial_result(
        await _apply_preparation(db_session, operation_id, page, plan),
        PreparationMutationResult,
    )

    assert result.creation_operation_id == operation_id
    assert result.created_scenes == 1
    assert result.assigned_items == 1


@pytest.mark.parametrize("fence", ["cancel", "unlink"])
async def test_cancelled_or_unlinked_creation_cannot_apply_preparation(
    db_session, fence: str
) -> None:
    scope = await _seed_pointcloud_preparation(db_session, actor_role="project_admin")
    job_id = uuid.uuid4()
    operation_id = await _accept_pointcloud_creation(
        db_session,
        scope,
        job_id=job_id,
    )
    page = await _read_preparation(db_session, operation_id)
    plan = _single_frame_plan(page.dataset_name, scope["item_id"])

    if fence == "cancel":
        cancelled = _json_value(
            await _call(
                db_session,
                _CANCEL_JOB,
                {"actor_id": scope["actor_id"], "job_id": job_id},
            )
        )
        assert cancelled["ok"] is True
        expected_code = "operation_cancelled"
    else:
        unlinked = _json_value(
            await _call(
                db_session,
                _UNLINK_DATASET,
                {
                    "operation_id": uuid.uuid4(),
                    "actor_id": scope["actor_id"],
                    "dataset_id": scope["dataset_id"],
                    "project_id": scope["project_id"],
                    "cleanup_bucket": "assets-spatial-test-bucket",
                    "audit_context": json.dumps({}),
                },
            )
        )
        assert unlinked["schema"] == "aap-dataset-unlink/v1"
        assert unlinked["ok"] is True
        expected_code = "association_changed"
    _spatial_error(
        await _apply_preparation(db_session, operation_id, page, plan),
        expected_code,
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.scenes WHERE dataset_id=:id"),
            {"id": scope["dataset_id"]},
        )
        == 0
    )


@pytest.mark.skipif(
    not _ROLE_ACCEPTANCE,
    reason="requires the disposable module-data boundary verifier",
)
async def test_catalog_login_has_exact_spatial_and_catalog_capability_surface():
    raw_url = os.environ.get(_CATALOG_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_CATALOG_URL}")
    url = make_url(raw_url)
    if url.username != "asset_catalog_client" or not url.database.endswith("_test"):
        pytest.fail("catalog LOGIN URL does not target the owned test database")
    engine = create_async_engine(url, hide_parameters=True)
    expected = set(_CATALOG_LOGIN_FUNCTIONS) | {
        f"aap_assets_v1.{name}({signature})"
        for name, signature in SPATIAL_NATIVE_SIGNATURES.items()
    }
    try:
        async with engine.connect() as catalog:
            for signature in expected:
                assert await catalog.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                    ),
                    {"signature": signature},
                )
            allowed = await catalog.execute(
                text(
                    "SELECT proc.oid::regprocedure::text "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = proc.pronamespace "
                    "WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$' "
                    "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                )
            )
            assert {row[0].replace(", ", ",") for row in allowed} == {
                signature.replace(", ", ",") for signature in expected
            }
            for relation in (
                "public.tasks",
                "public.task_dataset_item_links",
                "public.annotations",
                "public.scene_tracks",
            ):
                assert not await catalog.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :relation, 'SELECT')"
                    ),
                    {"relation": relation},
                )
    finally:
        await engine.dispose()
