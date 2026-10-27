"""Frozen canonical LiDAR inputs against bounded, independently owned DB rows."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, Scene, SensorCalibrationRevision
from app.db.models.scene_track import SceneTrack
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import original_sources
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.exporting import lidar_inputs as inputs
from tests.factory import build_tool_bindings
from tests.test_original_sources import add_link, dataset_rows
from tests import test_managed_original_creation as creation_tests

from tests.test_auxiliary_originals import map_source, raw_source

auxiliary_client = creation_tests.client


@pytest.fixture
async def input_db(test_engine):
    schema = "lidar_input_test_" + uuid.uuid4().hex
    tables = (
        "projects",
        "task_batches",
        "tasks",
        "task_dataset_item_links",
        "datasets",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "task_originals",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "managed_original_creations",
        "dataset_auxiliary_originals",
        "annotations",
        "sensor_calibration_revisions",
        "scenes",
        "scene_frame_poses",
        "scene_tracks",
        "scene_track_intervals",
        "predictions",
    )
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" '
                    f'(LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
        # LIKE deliberately omits foreign keys. These are the actual parent-key
        # insert fences under test, independent of the application's writers.
        for child, column, parent in (
            ("task_dataset_item_links", "task_id", "tasks"),
            ("annotations", "task_id", "tasks"),
            ("sensor_calibration_revisions", "dataset_item_id", "dataset_items"),
            ("scene_track_intervals", "scene_track_id", "scene_tracks"),
        ):
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}"."{child}" ADD FOREIGN KEY ("{column}") '
                    f'REFERENCES "{schema}"."{parent}" (id)'
                )
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def setup(db, *, direct=True, primary=True):
    project = Project(
        id=uuid.uuid4(),
        display_id="freeze-" + uuid.uuid4().hex[:12],
        name="LiDAR",
        type_key="lidar-detection",
        type_label="LiDAR",
        data_type="lidar",
        owner_id=uuid.uuid4(),
        tool_bindings=build_tool_bindings(["car"], unit="point_mask_3d"),
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="DS-" + uuid.uuid4().hex[:14],
        name="PCD",
        data_type="point_cloud",
        created_by=uuid.uuid4(),
        metadata_={"axis_convention": "iso8855"},
    )
    db.add_all([project, dataset])
    await db.flush()
    item, asset, replica, task = await dataset_rows(db, file_type="point_cloud")
    item.dataset_id = dataset.id
    task.project_id = project.id
    if not direct:
        task.dataset_item_id = None
    await db.flush()
    if primary:
        await add_link(db, task.id, item.id)
    cameras = []
    for role in ("camera_front", "camera_back"):
        camera, camera_asset, camera_replica, unused_task = await dataset_rows(db)
        camera.dataset_id = dataset.id
        # dataset_rows creates a Task as well; keep the fixture's export scope
        # explicit and its original helper unchanged.
        await db.delete(unused_task)
        link = await add_link(db, task.id, camera.id, role=role)
        link.sensor_name = role.removeprefix("camera_")
        cameras.append((camera, camera_asset, camera_replica, link))
    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        class_name="car",
        annotation_type="point_mask_3d",
        tool_unit_id="point_mask_3d",
        geometry={
            "type": "point_mask_3d",
            "point_indices": [0, 2],
            "source_point_count": 3,
        },
        attributes={},
        is_active=True,
        was_cancelled=False,
    )
    db.add(annotation)
    await db.commit()
    return project, dataset, item, asset, replica, task, cameras, annotation


async def attach_nuscenes(
    db, dataset, item, cameras, *, scene=None, map_owner=None, raw_data=b"r" * 80
):
    """Create actual independent auxiliary receipts before binding business rows."""
    from app.services import auxiliary_originals as auxiliary
    from app.services import managed_original_creation as creation

    source = raw_source()
    source["sample_data"]["filename"] = f"samples/LIDAR_TOP/{item.id}.pcd.bin"
    if scene is None:
        scene = Scene(
            id=uuid.uuid4(),
            display_id="SC-" + uuid.uuid4().hex[:12],
            dataset_id=dataset.id,
            name="owned-nuscenes",
            source_format="nuscenes",
            source_metadata={
                "frame": "ego",
                "nuscenes_export": {
                    "scene": {"token": "scene", "log_token": "log"},
                    "log": {"token": "log"},
                    "map": map_source(),
                },
            },
        )
        db.add(scene)
    item.scene_id, item.frame_index = scene.id, 0
    # Independent allocation/PUT/publish never commits pending business edits.
    await db.commit()

    async def create(kind, contract, data):
        allocation = await creation.allocate_auxiliary(
            db,
            dataset_id=dataset.id,
            kind=kind,
            source_contract=contract,
            logical_path=f"aux/{kind}",
            max_bytes=100,
            authority={"policy": "owned-export-test"},
        )
        written = await creation.create_stream(
            db, allocation, io.BytesIO(data), content_type=allocation["content_type"]
        )
        return await auxiliary.publish(
            db, allocation, written, kind=kind, source_contract=contract
        )

    raw_owner = await create("raw_lidar", source, raw_data)
    map_owner = map_owner or await create("map", map_source(), b"shared-map")
    item.metadata_ = {
        **(item.metadata_ or {}),
        "nuscenes_export": {
            **source,
            "source_storage_key": raw_owner.object_key,
            "source_file_size": raw_owner.size_bytes,
            "source_sha256": raw_owner.sha256,
        },
    }
    scene.source_metadata = {
        **scene.source_metadata,
        "nuscenes_export": {
            **scene.source_metadata["nuscenes_export"],
            "map_storage_key": map_owner.object_key,
            "map_file_size": map_owner.size_bytes,
            "map_sha256": map_owner.sha256,
        },
    }
    for camera, _, _, link in cameras:
        camera.scene_id, camera.frame_index = scene.id, item.frame_index
        camera.metadata_ = {
            **(camera.metadata_ or {}),
            "nuscenes_export": {
                "sample_data": {"filename": f"samples/{link.role}/{camera.id}.jpg"}
            },
        }
    await auxiliary.bind_raw(db, item, raw_owner)
    await auxiliary.bind_map(db, scene, map_owner)
    await db.commit()
    return scene, raw_owner, map_owner


async def capture(db, project, targets=("pointmask",), *, lock=False):
    return await inputs.collect(
        db,
        project.id,
        None,
        list(targets),
        {
            "lidar": {"kitti_camera_role": "camera_front"},
        },
        lock=lock,
    )


@pytest.mark.parametrize(
    "targets,roles",
    [
        (("kitti",), {"primary_lidar", "camera_front"}),
        (("pointmask",), {"primary_lidar", "camera_front", "camera_back"}),
        (("coco-multicamera",), {"camera_front", "camera_back"}),
        (
            ("kitti", "coco-multicamera"),
            {"primary_lidar", "camera_front", "camera_back"},
        ),
    ],
)
async def test_actual_union_uses_canonical_saved_assets_without_object_io(
    input_db, monkeypatch, targets, roles
):
    db = input_db
    project, _, item, asset, replica, task, _, _ = await setup(db)
    from app.services.storage import storage_service

    def forbidden(*args, **kwargs):
        pytest.fail("input capture is database-only, including preflight")

    monkeypatch.setattr(storage_service.client, "get_object", forbidden)
    monkeypatch.setattr(storage_service.client, "head_object", forbidden)
    monkeypatch.setattr(storage_service, "datasets_bucket", "same-key-trap")
    frozen = await capture(db, project, targets, lock=True)
    assert {entry["role"] for entry in frozen.snapshot["media"]} == roles
    assert set(frozen.sources) == {str(task.id) + ":" + role for role in roles}
    source = frozen.sources.get(str(task.id) + ":primary_lidar")
    if source:
        assert source.sha256 == asset.checksum_value != item.content_hash
        assert source.size_bytes == 5 != item.file_size
        assert source.bucket == replica.bucket == "saved-original-bucket"
    assert frozen.digest == inputs.digest(frozen.snapshot)
    assert len(frozen.snapshot["tasks"]) == 1


async def test_primary_link_only_and_camera_only_keep_different_requirements(input_db):
    db = input_db
    project, _, item, _, _, task, _, _ = await setup(db, direct=False)
    frozen = await capture(db, project)
    assert frozen.sources[str(task.id) + ":primary_lidar"].item_id == item.id
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink

    await db.execute(
        text("DELETE FROM task_dataset_item_links WHERE role='primary_lidar'")
    )
    await db.commit()
    assert len((await capture(db, project, ("coco-multicamera",))).sources) == 2
    with pytest.raises(ContentError, match="no_readable_original"):
        await capture(db, project)
    assert await db.scalar(select(TaskDatasetItemLink.id).limit(1))


async def test_conflicting_primary_is_never_resolved_by_path_or_priority(input_db):
    db = input_db
    project, _, _, _, _, task, cameras, _ = await setup(db, primary=False)
    await add_link(db, task.id, cameras[0][0].id)
    await db.commit()
    for targets in (("pointmask",), ("coco-multicamera",)):
        with pytest.raises(ContentError, match="original_relation_conflict"):
            await capture(db, project, targets)


async def test_task_heartbeat_does_not_change_input_but_annotation_edit_does(input_db):
    db = input_db
    project, _, _, _, _, task, _, annotation = await setup(db)
    frozen = await capture(db, project)
    await db.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(updated_at=datetime.now(timezone.utc), version=Task.version + 1)
    )
    await db.commit()
    assert (await inputs.check(db, frozen.snapshot)).digest == frozen.digest
    annotation.geometry = {**annotation.geometry, "point_indices": [1]}
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await inputs.check(db, frozen.snapshot, lock=True)


@pytest.mark.parametrize(
    "changed", ["source", "axis", "role", "new_task", "calibration"]
)
async def test_frozen_actual_input_changes_are_rejected(input_db, changed):
    db = input_db
    project, dataset, item, asset, _, task, cameras, _ = await setup(db)
    frozen = await capture(db, project)
    if changed == "source":
        asset.checksum_value = hashlib.sha256(b"newbytes").hexdigest()
    elif changed == "axis":
        dataset.metadata_ = {"axis_convention": "threejs"}
    elif changed == "role":
        cameras[0][3].sensor_name = "renamed"
    elif changed == "new_task":
        new_item, _, _, new_task = await dataset_rows(db, file_type="point_cloud")
        new_item.dataset_id = dataset.id
        new_task.project_id = project.id
    else:
        db.add(
            SensorCalibrationRevision(
                id=uuid.uuid4(),
                dataset_item_id=cameras[0][0].id,
                revision=1,
                digest="c" * 64,
                calibration={},
            )
        )
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await inputs.check(db, frozen.snapshot)


async def test_camera_member_on_historical_calibration_is_a_frozen_input(input_db):
    db = input_db
    project, _, _, _, _, task, cameras, _ = await setup(db)
    camera = cameras[0][0]
    scene = Scene(
        id=uuid.uuid4(),
        display_id="SC-" + uuid.uuid4().hex[:12],
        dataset_id=camera.dataset_id,
        name="frame-scene",
    )
    db.add(scene)
    await db.flush()
    track = SceneTrack(
        id=uuid.uuid4(),
        project_id=project.id,
        scene_id=scene.id,
        track_id="track-1",
        class_name="car",
    )
    db.add(track)
    await db.flush()
    for revision in (1, 2):
        db.add(
            SensorCalibrationRevision(
                id=uuid.uuid4(),
                dataset_item_id=camera.id,
                revision=revision,
                digest=str(revision) * 64,
                calibration={},
            )
        )
    db.add(
        Annotation(
            id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            class_name="car",
            annotation_type="bbox",
            tool_unit_id="bbox",
            scene_track_id=track.id,
            track_id=track.track_id,
            sensor_dataset_item_id=camera.id,
            sensor_role="camera_front",
            sensor_visibility="visible",
            calibration_revision=1,
            calibration_digest="1" * 64,
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            attributes={},
            is_active=True,
            was_cancelled=False,
        )
    )
    await db.commit()
    frozen = await capture(db, project, ("coco-multicamera",))
    assert len(frozen.snapshot["calibrations"]) == 2
    assert any(
        ann["calibration_revision"] == 1 for ann in frozen.snapshot["annotations"]
    )


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_PCD_COUNT",
        "MAX_ANNOTATIONS",
        "MAX_INDICES",
        "MAX_JSON_BYTES",
        "MAX_IMAGES_TOTAL",
    ],
)
async def test_count_and_json_budgets_reject_without_original_io(
    input_db, monkeypatch, limit
):
    db = input_db
    project, *_ = await setup(db)
    monkeypatch.setattr(inputs, limit, 1)
    if limit in {"MAX_PCD_COUNT", "MAX_ANNOTATIONS"}:
        monkeypatch.setattr(inputs, limit, 0)
    with pytest.raises(ContentError, match="lidar_export_input_budget_exceeded"):
        await capture(db, project)


async def test_inactive_unknown_class_does_not_hide_active_unknown_class(input_db):
    db = input_db
    project, _, _, _, _, _, _, ann = await setup(db)
    ann.class_name = "unknown"
    ann.is_active = False
    await db.commit()
    await capture(db, project)
    ann.is_active = True
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_pointmask_invalid"):
        await capture(db, project)


@pytest.mark.parametrize("operation", ["link", "annotation", "calibration"])
async def test_final_parent_locks_block_new_input_phantoms(input_db, operation):
    db = input_db
    project, _, _, _, _, task, cameras, _ = await setup(db)
    await capture(db, project, lock=True)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
        await other.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(DBAPIError) as error:
            if operation == "link":
                await add_link(other, task.id, cameras[0][0].id, role="camera_other")
            elif operation == "annotation":
                other.add(
                    Annotation(
                        id=uuid.uuid4(),
                        project_id=project.id,
                        task_id=task.id,
                        class_name="car",
                        geometry={},
                        attributes={},
                    )
                )
                await other.flush()
            else:
                other.add(
                    SensorCalibrationRevision(
                        id=uuid.uuid4(),
                        dataset_item_id=cameras[0][0].id,
                        revision=1,
                        digest="a" * 64,
                        calibration={},
                    )
                )
                await other.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        await other.rollback()
    await db.rollback()


async def test_parent_lock_conflict_is_explicit_and_caller_can_rollback(input_db):
    db = input_db
    project, _, _, _, _, task, _, _ = await setup(db)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
        await other.execute(select(Task.id).where(Task.id == task.id).with_for_update())
        with pytest.raises(ContentError, match="lidar_export_inputs_busy"):
            await capture(db, project, lock=True)
        await db.rollback()
        await other.rollback()
    assert await db.scalar(select(1)) == 1


def test_digest_preserves_budget_code_and_freeze_is_canonical():
    assert inputs.digest({"中": 1, "a": [2]}) == inputs.digest({"a": [2], "中": 1})
    with pytest.raises(
        ContentError, match="lidar_export_input_budget_exceeded"
    ) as error:
        inputs.digest({"a": "long"}, limit=2)
    assert error.value.status == 422
    for invalid in ({"a": float("nan")}, {"a": object()}):
        with pytest.raises(ContentError, match="lidar_export_input_invalid"):
            inputs.digest(invalid)


async def test_check_preserves_source_error_and_does_not_mutate_saved_input(input_db):
    db = input_db
    project, _, _, asset, _, _, _, _ = await setup(db)
    frozen = await capture(db, project)
    saved = deepcopy(frozen.snapshot)
    asset.verification_state = "unverified"
    asset.checksum_algorithm = asset.checksum_value = None
    await db.commit()
    with pytest.raises(ContentError, match="content_unverified"):
        await inputs.check(db, frozen.snapshot)
    assert frozen.snapshot == saved
    assert original_sources is inputs.original_sources


async def test_object_guard_checks_exact_binding_while_final_fence_covers_other_links(
    input_db,
):
    db = input_db
    project, _, _, _, _, task, cameras, _ = await setup(db)
    frozen = await capture(db, project)
    main = next(
        binding
        for binding in frozen.snapshot["media"]
        if binding["role"] == "primary_lidar"
    )
    selected = next(
        binding
        for binding in frozen.snapshot["media"]
        if binding["role"] == "camera_front"
    )
    assert len(main["source"]["relation"]["links"]) == 1
    assert len(selected["source"]["relation"]["links"]) == 2
    await add_link(db, task.id, cameras[1][0].id, role="camera_other")
    await db.commit()
    assert (await inputs.check_source(db, main)).sha256 == main["source"]["sha256"]
    assert (await inputs.check_source(db, selected)).sha256 == selected["source"][
        "sha256"
    ]
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await inputs.check(db, frozen.snapshot)


async def test_mixed_scene_freezes_project_members_outside_batch(
    input_db, auxiliary_client
):
    db = input_db
    project, dataset, item, _, _, task, cameras, _ = await setup(db)
    from app.db.models.task_batch import TaskBatch
    from app.services.exporting.lidar_frozen import FrozenLidar

    scene, _, _ = await attach_nuscenes(db, dataset, item, cameras)
    batch = TaskBatch(
        id=uuid.uuid4(), project_id=project.id, display_id="batch", name="partial scene"
    )
    db.add(batch)
    await db.flush()
    item.scene_id, item.frame_index = scene.id, 0
    task.batch_id = batch.id
    outside = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="outside-" + uuid.uuid4().hex[:15],
        file_name=task.file_name,
        file_path=task.file_path,
        file_type="point_cloud",
        dataset_item_id=item.id,
    )
    db.add(outside)
    await db.commit()
    frozen = await inputs.collect(
        db, project.id, batch.id, ["pointmask", "nuscenes"], {"lidar": {}}
    )
    assert [row["id"] for row in frozen.snapshot["tasks"]] == [str(task.id)]
    view = FrozenLidar(frozen.snapshot)
    assert {row.id for row, _ in view.all_scene_tasks[scene.id]} == {
        task.id,
        outside.id,
    }
    await db.delete(outside)
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await inputs.check(db, frozen.snapshot, lock=True)


async def test_generated_calibration_paths_cannot_silently_overwrite_other_camera(
    input_db,
):
    db = input_db
    project, _, _, _, _, _, cameras, _ = await setup(db)
    cameras[0][3].sensor_name = cameras[1][3].sensor_name = "shared-camera"
    await db.commit()
    with pytest.raises(
        ContentError, match="lidar_export_archive_path_conflict"
    ) as error:
        await capture(db, project)
    assert error.value.status == 422
    # COCO names by role/Task instead, so this naming conflict is not imposed on it.
    await capture(db, project, ("coco-multicamera",))


async def test_duplicate_task_frame_labels_are_rejected_instead_of_overwritten(
    input_db,
):
    db = input_db
    project, _, item, _, _, task, _, _ = await setup(db)
    db.add(
        Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id="other-" + uuid.uuid4().hex[:18],
            file_name=task.file_name,
            file_path=task.file_path,
            file_type=task.file_type,
            dataset_item_id=item.id,
        )
    )
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_archive_path_conflict"):
        await capture(db, project)


@pytest.mark.parametrize("targets", [("nuscenes",), ("kitti", "nuscenes")])
@pytest.mark.parametrize("raw_data", [b"r" * 80, b""])
async def test_nuscenes_capture_owns_all_media_and_checks_exact_auxiliary_pointer(
    input_db, auxiliary_client, monkeypatch, targets, raw_data
):
    db = input_db
    project, dataset, item, _, _, task, cameras, _ = await setup(db)
    _, raw_owner, map_owner = await attach_nuscenes(
        db, dataset, item, cameras, raw_data=raw_data
    )
    from app.services.storage import storage_service

    def forbidden(*args, **kwargs):
        pytest.fail("synchronous NuScenes freeze must not read, HEAD or sign originals")

    monkeypatch.setattr(storage_service.client, "get_object", forbidden)
    monkeypatch.setattr(storage_service.client, "head_object", forbidden)
    monkeypatch.setattr(storage_service, "generate_download_url", forbidden)
    monkeypatch.setattr(storage_service, "datasets_bucket", "current-bucket-trap")
    frozen = await capture(db, project, targets)
    assert frozen.snapshot["schema"] == 2
    assert {row["role"] for row in frozen.snapshot["media"]} == {
        "primary_lidar",
        "camera_front",
        "camera_back",
        "raw_lidar",
        "map",
    }
    assert frozen.sources[f"{task.id}:raw_lidar"].content_id == raw_owner.id
    assert frozen.sources[f"{task.id}:map"].content_id == map_owner.id
    binding = next(
        row for row in frozen.snapshot["media"] if row["role"] == "raw_lidar"
    )
    assert (await inputs.check_source(db, binding)).sha256 == raw_owner.sha256
    item.nuscenes_raw_original_id = None
    await db.commit()
    with pytest.raises(ContentError, match="auxiliary_original_unpinned"):
        await inputs.check_source(db, binding)
    with pytest.raises(ContentError):
        await inputs.check(db, frozen.snapshot, lock=True)
