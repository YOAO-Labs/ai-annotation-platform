from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.services import raster_mask_storage as storage
from tests.test_raster_mask_storage import owned_mask_storage as owned_mask_storage
from tests.test_raster_mask_write_gate import (
    FOREGROUND_RLE,
    _seed_image_task,
    _seed_upload,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_alias", [False, True])
async def test_final_write_requires_the_exact_verified_reference(
    db_session, test_engine, super_admin, owned_mask_storage, monkeypatch, verify_alias
):
    from app.workers import _db

    @asynccontextmanager
    async def test_task_session():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
            yield session

    monkeypatch.setattr(_db, "task_session", test_task_session)
    actor, _ = super_admin
    _, task = await _seed_image_task(db_session, actor.id)
    objects = owned_mask_storage()
    reference = await storage.store_coco_rle(
        FOREGROUND_RLE, storage=objects, db=db_session
    )
    load_coco_rle = storage.load_coco_rle

    async def load_from_owned_provider(value):
        return await load_coco_rle(value, storage=objects, db=db_session)

    monkeypatch.setattr(storage, "load_coco_rle", load_from_owned_provider)
    alias = {**reference, "storage_encoding": "identity"}
    geometry = {"type": "raster_mask", "mask": reference}
    upload = await _seed_upload(db_session, task.id, reference)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    verified = await storage.verify_mask_payload_content(
        geometry,
        db=db_session,
        reference_value=[reference, alias] if verify_alias else [reference],
    )
    if verify_alias:
        await storage.prepare_mask_payload_for_write(
            db_session,
            task,
            geometry,
            reference_value=[alias],
            verified_content=verified,
            require_verified_content=True,
        )
    else:
        with pytest.raises(storage.RasterMaskContractError) as rejected:
            await storage.prepare_mask_payload_for_write(
                db_session,
                task,
                geometry,
                reference_value=[alias],
                verified_content=verified,
                require_verified_content=True,
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail["reason"] == "mask_reference_invalid"

    await db_session.refresh(upload)
    assert (upload.linked_at is not None) is verify_alias


@pytest.mark.asyncio
async def test_final_reference_gate_rejects_missing_epoch_in_caller_transaction(
    db_session,
):
    reference = storage.build_rle_reference(FOREGROUND_RLE)
    with pytest.raises(storage.RasterMaskContractError) as rejected:
        await storage.lock_raster_mask_references(
            db_session, reference, content_epochs={}
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["reason"] == "mask_content_epoch_changed"
