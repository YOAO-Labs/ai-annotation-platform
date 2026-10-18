from __future__ import annotations

from datetime import UTC, datetime
import json
from uuid import UUID, uuid4, uuid5

import pytest

from aap_assets_catalog import spatial_contract as spatial
from aap_assets_catalog.object_signer import SourceObjectFacts
from aap_assets_catalog.spatial_workflow import SpatialWorkflow


_SNAPSHOT = "a" * 64


def _item(
    dataset_id: UUID,
    *,
    path: str,
    file_name: str | None = None,
    file_type: str,
    metadata: dict | None = None,
    camera_roles: list[str] | None = None,
) -> spatial.SpatialItem:
    return spatial.SpatialItem(
        id=uuid4(),
        dataset_id=dataset_id,
        file_name=file_name or path.rsplit("/", 1)[-1],
        file_path=path,
        file_type=file_type,
        scene_id=None,
        frame_index=None,
        metadata=metadata or {},
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
        width=None,
        height=None,
        file_size=None,
        content_hash=None,
        thumbnail_path=None,
        axis_convention=None,
        calibration_revision=None,
        source_snapshot={"file_path": path, "revision": 1},
        camera_roles=camera_roles or [],
    )


def _layout_page(
    request: spatial.ReadLayoutRequest,
    items: list[spatial.SpatialItem],
    *,
    after: UUID | None,
    next_item: UUID | None,
    complete: bool,
    total: int,
) -> spatial.LayoutPage:
    return spatial.LayoutPage(
        actor_id=request.actor_id,
        dataset_id=request.dataset_id,
        creation_operation_id=request.creation_operation_id,
        dataset_name="dataset",
        is_temporal=False,
        axis_convention=None,
        scene_count=0,
        assigned_count=0,
        after_item_id=after,
        next_item_id=next_item,
        snapshot_digest=_SNAPSHOT,
        total=total,
        complete=complete,
        items=items,
        scenes=[],
    )


class _BackfillNative:
    def __init__(self, dataset_id: UUID, items: list[spatial.SpatialItem]):
        self.dataset_id = dataset_id
        self.items = sorted(items, key=lambda item: str(item.id))
        self.calls = []
        self.apply_request = None

    async def guard_spatial_actor(self, request):
        self.calls.append(("guard", request))
        return spatial.SpatialActorResult(**request.model_dump())

    async def read_spatial_layout(self, request):
        self.calls.append(("layout", request))
        rows = [
            row
            for row in self.items
            if request.after_item_id is None or row.id > request.after_item_id
        ]
        rows = rows[:1]
        last = rows[-1].id if rows else None
        complete = last == self.items[-1].id if rows else not self.items
        return _layout_page(
            request,
            rows,
            after=request.after_item_id,
            next_item=None if complete else last,
            complete=complete,
            total=len(self.items),
        )

    async def apply_spatial_layout(self, request):
        self.calls.append(("apply", request))
        self.apply_request = request
        return spatial.LayoutMutationResult(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            created_scenes=request.plan.created_scenes,
            assigned_items=request.plan.assigned_items,
            skipped_items=request.plan.skipped_items,
            dry_run=request.dry_run,
            notes=request.plan.notes,
        )


@pytest.mark.asyncio
async def test_backfill_collects_all_pages_and_uses_operation_stable_scene_ids():
    dataset_id = uuid4()
    actor_id = uuid4()
    operation_id = uuid4()
    items = [
        _item(dataset_id, path="pack/lidar/0001.bin", file_type="point_cloud"),
        _item(dataset_id, path="pack/camera/front/0001.jpg", file_type="image"),
    ]
    native = _BackfillNative(dataset_id, items)
    workflow = SpatialWorkflow(native, object())

    result = await workflow.backfill_scenes(
        spatial.BackfillScenesRequest(
            operation_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            mode="single",
        )
    )

    assert [name for name, _request in native.calls] == [
        "guard",
        "layout",
        "layout",
        "apply",
    ]
    request = native.apply_request
    assert request is not None
    planned_scene = request.plan.scenes[0]
    assert planned_scene.id == uuid5(operation_id, f"{dataset_id}:_single")
    assert request.snapshot == {"snapshot_digest": _SNAPSHOT}
    assert result.created_scenes == 1
    assert result.assigned_items == 2
    assert {row.scene_id for row in request.plan.assignments} == {planned_scene.id}


class _AxisNative:
    def __init__(self, item: spatial.SpatialItem):
        self.item = item
        self.calls = []

    async def guard_spatial_actor(self, request):
        self.calls.append(("guard", request))
        return spatial.SpatialActorResult(**request.model_dump())

    async def read_spatial_items(self, request):
        self.calls.append(("items", request))
        return spatial.ItemFactsPage(
            dataset_id=request.dataset_id,
            item_ids=request.item_ids,
            after_item_id=request.after_item_id,
            next_item_id=None,
            snapshot_digest=_SNAPSHOT,
            total=1,
            complete=True,
            items=[self.item],
            scenes=[],
        )


@pytest.mark.asyncio
async def test_axis_sniff_uses_linked_camera_facts_from_complete_item_pages():
    dataset_id = uuid4()
    actor_id = uuid4()
    item = _item(
        dataset_id,
        path="pack/camera/front/0001.jpg",
        file_type="image",
        camera_roles=["camera_front"],
        metadata={
            "calibration": {
                "extrinsic": [1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1],
                "intrinsic": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            }
        },
    )
    native = _AxisNative(item)
    workflow = SpatialWorkflow(native, object())

    result = await workflow.sniff_axis(
        spatial.SniffAxisRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.actor_id == actor_id
    assert result.dataset_id == dataset_id
    assert result.result is not None
    assert result.result.source == "task_link"
    assert result.result.camera_item_id == item.id
    assert result.result.best == "iso_8855"
    assert [name for name, _request in native.calls] == ["guard", "items"]


class _PreparationNative:
    def __init__(self, dataset_id: UUID, operation_id: UUID, items):
        self.dataset_id = dataset_id
        self.operation_id = operation_id
        self.items = sorted(items, key=lambda item: str(item.id))
        self.calls = []
        self.apply_request = None

    async def read_pointcloud_preparation(self, request):
        self.calls.append(("read", request))
        rows = [
            row
            for row in self.items
            if request.after_item_id is None or row.id > request.after_item_id
        ]
        rows = rows[:1]
        last = rows[-1].id if rows else None
        complete = last == self.items[-1].id if rows else not self.items
        return spatial.PreparationPage(
            actor_id=None,
            dataset_id=self.dataset_id,
            creation_operation_id=self.operation_id,
            dataset_name="dataset",
            is_temporal=False,
            axis_convention=None,
            scene_count=0,
            assigned_count=0,
            after_item_id=request.after_item_id,
            next_item_id=None if complete else last,
            snapshot_digest=_SNAPSHOT,
            total=len(self.items),
            complete=complete,
            items=rows,
            scenes=[],
        )

    async def apply_pointcloud_preparation(self, request):
        self.calls.append(("apply", request))
        self.apply_request = request
        return spatial.PreparationMutationResult(
            creation_operation_id=request.creation_operation_id,
            dataset_id=self.dataset_id,
            calibrated_items=len(request.calibration_updates),
            created_scenes=request.frame_plan.created_scenes,
            assigned_items=request.frame_plan.assigned_items,
        )


class _SourceReader:
    def __init__(self):
        self.calls = []

    def read_calibration_source(self, key, *, expected_etag=None, expected_size=None):
        self.calls.append((key, expected_etag, expected_size))
        payload = json.dumps(
            {
                "extrinsic": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "intrinsic": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                "vendor_metadata": "discarded",
            }
        ).encode()
        return SourceObjectFacts(etag="calibration-etag", size=len(payload)), payload


@pytest.mark.asyncio
async def test_pointcloud_preparation_reads_sources_between_capture_and_apply():
    dataset_id = uuid4()
    operation_id = uuid4()
    calibration_file = _item(dataset_id, path="pack/calib/front.json", file_type="json")
    camera = _item(
        dataset_id,
        path="pack/camera/front/0001.jpg",
        file_type="image",
    )
    lidar = _item(dataset_id, path="pack/lidar/0001.bin", file_type="point_cloud")
    native = _PreparationNative(
        dataset_id, operation_id, [calibration_file, camera, lidar]
    )
    source_reader = _SourceReader()
    workflow = SpatialWorkflow(native, source_reader)

    result = await workflow.prepare_pointcloud(
        spatial.PreparePointcloudRequest(creation_operation_id=operation_id)
    )

    assert result.creation_operation_id == operation_id
    assert [name for name, _request in native.calls] == ["read"] * 3 + ["apply"]
    assert source_reader.calls == [(calibration_file.file_path, None, None)]
    request = native.apply_request
    assert request is not None
    assert request.snapshot == {"snapshot_digest": _SNAPSHOT}
    assert request.calibration_updates[0].dataset_item_id == camera.id
    assert request.calibration_updates[0].source_item_id == calibration_file.id
    assert (
        request.calibration_updates[0].source_snapshot
        == calibration_file.source_snapshot
    )
    assert request.calibration_updates[0].source_etag == "calibration-etag"
    assert (
        "vendor_metadata" not in request.calibration_updates[0].calibration.model_dump()
    )
    assert request.frame_plan.created_scenes == 1
    assert native.calls[-1][1].creation_operation_id == operation_id


class _CalibrationNative:
    def __init__(self, item):
        self.item = item
        self.current_calibration = spatial.WireCalibration(
            extrinsic=[1.0] * 16,
            intrinsic=[1.0] * 9,
        )
        self.current_revision = 2
        self.calls = []
        self.updated_requests = []
        self.receipt = None

    async def read_spatial_camera_target(self, request):
        self.calls.append(("target", request))
        return spatial.CameraTargetResult(
            actor_id=request.actor_id,
            task_id=request.task_id,
            camera_role=request.camera_role,
            dataset_item_id=self.item.id,
        )

    async def read_spatial_items(self, request):
        self.calls.append(("item", request))
        return spatial.ItemFactsPage(
            dataset_id=None,
            item_ids=request.item_ids,
            after_item_id=None,
            next_item_id=None,
            snapshot_digest=_SNAPSHOT,
            total=1,
            complete=True,
            items=[self.item],
            scenes=[],
        )

    async def lock_spatial_calibration_facts(self, request):
        self.calls.append(("facts", request))
        return spatial.CalibrationRecord(
            dataset_item_id=self.item.id,
            revision=self.current_revision,
            calibration=self.current_calibration.model_dump(mode="json"),
            digest="b" * 64 if self.current_revision == 2 else "d" * 64,
            created_at=None,
            source_snapshot=self.item.source_snapshot,
            error=None,
        )

    async def update_spatial_calibration(self, request):
        self.calls.append(("update", request))
        self.updated_requests.append(request)
        if self.receipt is not None:
            prior = self.updated_requests[0]
            assert request.operation_id == prior.operation_id
            assert request.actor_id == prior.actor_id
            assert request.task_id == prior.task_id
            assert request.camera_role == prior.camera_role
            assert request.expected_revision == prior.expected_revision
            assert request.expected_digest == prior.expected_digest
            assert request.canonical_document == prior.canonical_document
            assert request.audit_context == prior.audit_context
            return self.receipt

        self.receipt = spatial.CalibrationMutationResult(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            task_id=request.task_id,
            camera_role=request.camera_role,
            state=spatial.CalibrationState(
                dataset_item_id=self.item.id,
                revision=3,
                calibration=request.calibration,
                digest="c" * 64,
                created_at=None,
                source_snapshot=request.source_snapshot,
            ),
        )
        self.current_calibration = spatial.WireCalibration(
            extrinsic=[2.0] * 16,
            intrinsic=[3.0] * 9,
        )
        self.current_revision = 3
        self.item = self.item.model_copy(
            update={
                "source_snapshot": {
                    "file_path": self.item.file_path,
                    "revision": 2,
                }
            }
        )
        return self.receipt


@pytest.mark.asyncio
async def test_calibration_workflow_replays_same_key_after_current_facts_change():
    actor_id, task_id = uuid4(), uuid4()
    item = _item(uuid4(), path="dataset/camera/front.jpg", file_type="image")
    desired = spatial.WireCalibration(
        extrinsic=[0.0] * 16,
        intrinsic=[1.0] * 9,
    )
    request = spatial.UpdateCalibrationRequest(
        operation_id=uuid4(),
        actor_id=actor_id,
        task_id=task_id,
        camera_role="camera_front",
        expected_revision=1,
        expected_digest="a" * 64,
        calibration=desired,
        audit_context={"reason": "review"},
    )
    native = _CalibrationNative(item)
    workflow = SpatialWorkflow(native, object())

    result = await workflow.update_calibration(request)
    replay = await workflow.update_calibration(request)

    assert [name for name, _payload in native.calls] == [
        "target",
        "item",
        "facts",
        "update",
        "target",
        "item",
        "facts",
        "update",
    ]
    first_request, replay_request = native.updated_requests
    assert first_request.operation_id == request.operation_id
    assert first_request.expected_revision == request.expected_revision
    assert first_request.expected_digest == request.expected_digest
    assert first_request.canonical_document == spatial.canonical_calibration_document(
        desired
    )
    assert (
        first_request.current_canonical_document
        == spatial.canonical_calibration_document(
            spatial.WireCalibration(
                extrinsic=[1.0] * 16,
                intrinsic=[1.0] * 9,
            )
        )
    )
    assert first_request.source_snapshot == item.source_snapshot
    assert replay_request.operation_id == first_request.operation_id
    assert replay_request.expected_revision == first_request.expected_revision
    assert replay_request.expected_digest == first_request.expected_digest
    assert replay_request.canonical_document == first_request.canonical_document
    assert (
        replay_request.current_canonical_document
        != first_request.current_canonical_document
    )
    assert replay_request.source_snapshot != first_request.source_snapshot
    assert replay == result
    assert result.state.revision == 3
