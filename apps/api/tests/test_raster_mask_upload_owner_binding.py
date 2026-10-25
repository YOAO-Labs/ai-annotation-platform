from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm.util import identity_key

from aap_labeling.mask_edit_contract import (
    LockRasterMaskUploadTaskData,
    RasterMaskUploadRecord,
    ReserveRasterMaskUploadData,
)
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.services import raster_mask_storage as storage


class _Session:
    def __init__(self, *, autoflush: bool):
        self.autoflush = autoflush
        self.events: list[str] = []
        self.identity_map = {}

    @property
    def no_autoflush(self):
        @contextmanager
        def disabled():
            original = self.autoflush
            self.autoflush = False
            try:
                yield
            finally:
                self.autoflush = original

        return disabled()

    async def flush(self):
        self.events.append("flush")


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("existing", [False, True])
async def test_reservation_keeps_advisory_lock_autoflush_and_existing_cache_order(
    monkeypatch, autoflush, existing
):
    task_id = uuid.uuid4()
    object_key = "raster-masks/sha256/reserved.json"
    session = _Session(autoflush=autoflush)
    existing_id = uuid.uuid4()
    old_created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cached = RasterMaskUpload(
        id=existing_id,
        task_id=task_id,
        object_key=object_key,
        created_at=old_created_at,
    )
    if existing:
        session.identity_map[identity_key(RasterMaskUpload, ident=existing_id)] = cached

    async def permit(_db, _keys):
        session.events.append("content_permit")
        return {}

    async def command(_db, request):
        if request.operation == "lock_raster_mask_upload_task":
            assert not session.autoflush
            session.events.append("task_advisory_lock")
            return LockRasterMaskUploadTaskData(locked=True)

        assert request.operation == "reserve_raster_mask_upload"
        assert session.autoflush is autoflush
        session.events.append("reserve_count_and_write")
        record = RasterMaskUploadRecord(
            id=existing_id if existing else request.upload_id,
            task_id=task_id,
            object_key=object_key,
            linked_at=None,
            created_at=new_created_at,
        )
        return ReserveRasterMaskUploadData(count=1, reserved=True, upload=record)

    monkeypatch.setattr(storage, "begin_mask_content_write", permit)
    monkeypatch.setattr(storage, "mask_edit_command", command)

    assert (
        await storage.reserve_raster_mask_upload(
            session, task_id=task_id, object_key=object_key, limit=2
        )
        == 1
    )
    expected = ["content_permit", "task_advisory_lock"]
    if autoflush:
        expected.append("flush")
    expected.extend(["reserve_count_and_write", "flush"])
    assert session.events == expected
    if existing:
        assert cached.created_at == new_created_at
    else:
        assert not session.identity_map
