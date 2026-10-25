from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aap_labeling.mask_edit_contract import (
    ClaimRasterMaskUploads,
    ClaimRasterMaskUploadsData,
)

from app.config import settings
from app.services import raster_mask_storage as storage


RLE = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
REFERENCE = storage.build_rle_reference(RLE)
GEOMETRY = {"type": "raster_mask", "mask": REFERENCE}


@pytest.fixture(autouse=True)
def content_epochs(monkeypatch):
    async def read_epochs(db, keys):
        assert "task_lock" not in db.events
        db.events.append("content_epoch")
        return {key: 7 for key in keys}

    monkeypatch.setattr(storage, "read_mask_write_epochs", read_epochs)

    async def get_project(_db, **_kwargs):
        return _db.project

    monkeypatch.setattr(storage, "get_workspace_project", get_project)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Database:
    def __init__(self, task, project=None):
        self.task = task
        self.autoflush = False
        self.identity_map = {}
        self.project = project or SimpleNamespace(
            raster_mask_native_editing_enabled=True,
            tool_bindings={"region": {"enabled": True}},
        )
        self.events: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        if "aap_labeling_v1.lock_task_for_core_read" in str(statement):
            self.events.append("task_lock")
        return _Result(self.task)

    async def scalar(self, _statement):
        return self.task

    async def get(self, _model, _identity, **_kwargs):
        return self.project


@pytest.mark.asyncio
async def test_rle_read_precedes_task_lock_and_final_registration_is_sql_only(
    monkeypatch,
):
    task = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    db = _Database(task)

    async def load(reference):
        assert reference == REFERENCE
        assert "task_lock" not in db.events
        db.events.append("storage_get")
        return RLE

    monkeypatch.setattr(storage, "load_coco_rle", load)
    monkeypatch.setattr(storage, "assert_raster_mask_write_enabled", lambda _p: None)

    async def validate(_db, _task, _geometry):
        return None

    async def lock_references(_db, _value, *, verify=True, content_epochs, **_kwargs):
        assert verify is False
        assert content_epochs == {REFERENCE["object_key"]: 7}
        db.events.append("sql_reference_lock")
        return [REFERENCE["object_key"]]

    async def claim_uploads(_db, request):
        assert isinstance(request, ClaimRasterMaskUploads)
        assert request.task_id == task.id
        assert request.object_keys == [REFERENCE["object_key"]]
        db.events.append("upload_claim")
        return ClaimRasterMaskUploadsData(uploads=[])

    monkeypatch.setattr(storage, "validate_mask_geometry_for_task", validate)
    monkeypatch.setattr(storage, "lock_raster_mask_references", lock_references)
    monkeypatch.setattr(storage, "mask_edit_command", claim_uploads)

    verified = await storage.verify_mask_payload_content(GEOMETRY, db=db)
    await storage.prepare_mask_payload_for_write(
        db, task, GEOMETRY, verified_content=verified
    )

    assert [event for event in db.events] == [
        "content_epoch",
        "storage_get",
        "task_lock",
        "sql_reference_lock",
        "upload_claim",
    ]
    assert dict(verified.rle_cache)[REFERENCE["object_key"]] == RLE


@pytest.mark.asyncio
async def test_slow_or_failed_rle_read_never_holds_a_task_lock(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    db = _Database(task)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_load(_reference):
        db.events.append("storage_get")
        started.set()
        await release.wait()
        return RLE

    monkeypatch.setattr(storage, "load_coco_rle", slow_load)
    pending = asyncio.create_task(storage.verify_mask_payload_content(GEOMETRY, db=db))
    await started.wait()
    assert db.events == ["content_epoch", "storage_get"]

    release.set()
    await pending
    assert db.events == ["content_epoch", "storage_get"]

    async def failed_load(_reference):
        db.events.append("storage_failure")
        raise OSError("object storage unavailable")

    monkeypatch.setattr(storage, "load_coco_rle", failed_load)
    with pytest.raises(storage.RasterMaskContractError) as captured:
        await storage.verify_mask_payload_content(GEOMETRY, db=db)
    assert captured.value.status_code == 503
    assert captured.value.detail["reason"] == "mask_storage_unavailable"
    assert db.events == [
        "content_epoch",
        "storage_get",
        "content_epoch",
        "storage_failure",
    ]


@pytest.mark.asyncio
async def test_rollout_gate_rejects_before_preflight_reads_rle(monkeypatch):
    task = SimpleNamespace(id="task", project_id="project")
    db = _Database(task)
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    monkeypatch.setattr(storage, "load_coco_rle", load)

    with pytest.raises(storage.RasterMaskContractError) as captured:
        await storage.preflight_mask_payload_for_write(db, task, GEOMETRY)

    assert captured.value.detail["reason"] == "raster_mask_create_disabled"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_membership_compares_full_canonical_reference_pairs(
    monkeypatch,
):
    reference = storage.build_rle_reference(RLE)
    alias = {**reference, "storage_encoding": "identity"}
    storage.validate_reference_for_rle(alias, RLE)
    geometry = {"type": "raster_mask", "mask": reference}
    task = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    db = _Database(task)
    accepted: list[object] = []

    async def load(_reference):
        return RLE

    async def validate(_db, _task, _geometry):
        return None

    async def lock_references(_db, value, *, verify=True, **_kwargs):
        assert verify is False
        accepted.append(value)
        return [reference["object_key"]]

    async def claim_uploads(_db, request):
        assert isinstance(request, ClaimRasterMaskUploads)
        return ClaimRasterMaskUploadsData(uploads=[])

    monkeypatch.setattr(storage, "load_coco_rle", load)
    monkeypatch.setattr(storage, "assert_raster_mask_write_enabled", lambda _p: None)
    monkeypatch.setattr(storage, "validate_mask_geometry_for_task", validate)
    monkeypatch.setattr(storage, "lock_raster_mask_references", lock_references)
    monkeypatch.setattr(storage, "mask_edit_command", claim_uploads)
    verified = await storage.verify_mask_payload_content(
        geometry, db=db, reference_value=[reference, alias]
    )

    assert len(verified.reference_signature) == 2
    await storage.prepare_mask_payload_for_write(
        db,
        task,
        geometry,
        reference_value=[alias],
        verified_content=verified,
        require_verified_content=True,
    )

    assert accepted == [[alias]]


@pytest.mark.asyncio
async def test_active_collection_fence_rejects_preflight_before_reading_bytes(
    monkeypatch,
):
    db = _Database(SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4()))
    load = AsyncMock(return_value=RLE)

    async def fenced(_db, _keys):
        raise storage.RasterMaskContractError(
            status_code=409,
            reason="mask_content_deleting",
            message="Collection is pending",
            retryable=True,
        )

    monkeypatch.setattr(storage, "read_mask_write_epochs", fenced)
    monkeypatch.setattr(storage, "load_coco_rle", load)
    with pytest.raises(storage.RasterMaskContractError) as rejected:
        await storage.verify_mask_payload_content(GEOMETRY, db=db)
    assert rejected.value.status_code == 409
    assert rejected.value.detail["retryable"] is True
    assert db.events == []
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_reference_gate_cannot_recapture_missing_epoch(monkeypatch):
    db = _Database(SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4()))
    begin = AsyncMock()
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(storage, "begin_mask_content_write", begin)
    monkeypatch.setattr(storage, "load_coco_rle", load)
    with pytest.raises(storage.RasterMaskContractError) as rejected:
        await storage.lock_raster_mask_references(db, GEOMETRY, content_epochs={})
    assert rejected.value.detail["reason"] == "mask_content_epoch_changed"
    begin.assert_not_awaited()
    load.assert_not_awaited()
