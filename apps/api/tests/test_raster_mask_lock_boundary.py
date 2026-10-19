from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import raster_mask_storage as storage


RLE = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
REFERENCE = {
    "encoding": "coco_rle_ref",
    "size": [1, 1],
    "object_key": "raster-masks/sha256/ab/cd/" + "a" * 64 + ".json",
    "sha256": "a" * 64,
    "runs": 2,
    "bytes": 42,
}
GEOMETRY = {"type": "raster_mask", "mask": REFERENCE}


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Database:
    def __init__(self, task):
        self.task = task
        self.events: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        if "FOR UPDATE" in str(statement).upper():
            self.events.append("task_lock")
        return _Result(self.task)

    async def get(self, _model, _identity):
        return SimpleNamespace(id=self.task.project_id)


@pytest.mark.asyncio
async def test_rle_read_precedes_task_lock_and_final_registration_is_sql_only(
    monkeypatch,
):
    task = SimpleNamespace(id="task", project_id="project")
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

    async def lock_references(_db, _value, *, verify=True, **_kwargs):
        assert verify is False
        db.events.append("sql_reference_lock")
        return [REFERENCE["object_key"]]

    monkeypatch.setattr(storage, "validate_mask_geometry_for_task", validate)
    monkeypatch.setattr(storage, "lock_raster_mask_references", lock_references)

    verified = await storage.verify_mask_payload_content(GEOMETRY)
    await storage.prepare_mask_payload_for_write(
        db, task, GEOMETRY, verified_content=verified
    )

    assert [event for event in db.events] == [
        "storage_get",
        "task_lock",
        "sql_reference_lock",
    ]
    assert dict(verified.rle_cache)[REFERENCE["object_key"]] == RLE


@pytest.mark.asyncio
async def test_slow_or_failed_rle_read_never_holds_a_task_lock(monkeypatch):
    task = SimpleNamespace(id="task", project_id="project")
    db = _Database(task)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_load(_reference):
        db.events.append("storage_get")
        started.set()
        await release.wait()
        return RLE

    monkeypatch.setattr(storage, "load_coco_rle", slow_load)
    pending = asyncio.create_task(storage.verify_mask_payload_content(GEOMETRY))
    await started.wait()
    assert db.events == ["storage_get"]

    release.set()
    await pending
    assert db.events == ["storage_get"]

    async def failed_load(_reference):
        db.events.append("storage_failure")
        raise OSError("object storage unavailable")

    monkeypatch.setattr(storage, "load_coco_rle", failed_load)
    with pytest.raises(storage.RasterMaskContractError) as captured:
        await storage.verify_mask_payload_content(GEOMETRY)
    assert captured.value.status_code == 503
    assert captured.value.detail["reason"] == "mask_storage_unavailable"
    assert db.events == ["storage_get", "storage_failure"]
