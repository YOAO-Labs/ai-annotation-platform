from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.raster_mask_storage import (
    RasterMaskContractError,
    canonical_rle_bytes,
    load_coco_rle,
    lock_raster_mask_references,
    store_coco_rle,
    validate_mask_geometry_for_task,
)


def _storage(*, exists: bool = False):
    storage = MagicMock()
    storage.bucket = "annotation-data"
    if exists:
        storage.client.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )
        storage.client.get_object.return_value = {
            "Body": BytesIO(
                canonical_rle_bytes(
                    {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
                )
            )
        }
    return storage


def _patch_video_summary(monkeypatch, *, width: int, height: int, frame_count: int):
    async def current_video_summary(_reader, _dataset_item_id):
        return width, height, {"frame_count": frame_count}

    monkeypatch.setattr(
        "app.services.assets_media_summary.AssetsTaskMediaSummaryReader.current_video_summary",
        current_video_summary,
    )


async def test_store_coco_rle_uses_content_addressed_key_and_put_if_absent():
    storage = _storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=storage,
    )
    assert reference["encoding"] == "coco_rle_ref"
    assert reference["object_key"].endswith(f"{reference['sha256']}.json")
    assert reference["runs"] == 4
    storage.client.put_object.assert_called_once()

    existing = _storage(exists=True)
    await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=existing,
    )
    assert existing.client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    existing.client.get_object.assert_called_once()


async def test_load_coco_rle_verifies_reference_metadata():
    storage = _storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=storage,
    )
    body = storage.client.put_object.call_args.kwargs["Body"]
    stream = BytesIO(body)
    stream.close = MagicMock()
    storage.client.get_object.return_value = {"Body": stream}
    assert (await load_coco_rle(reference, storage=storage))["counts"] == [
        1,
        2,
        2,
        1,
    ]
    stream.close.assert_called_once()


async def test_load_coco_rle_rejects_digest_mismatch():
    storage = _storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [6]},
        storage=storage,
    )
    stream = BytesIO(b'{"encoding":"coco_rle","size":[2,3],"counts":[0,6]}')
    stream.close = MagicMock()
    storage.client.get_object.return_value = {"Body": stream}
    with pytest.raises(ValueError, match="digest mismatch"):
        await load_coco_rle(reference, storage=storage)


@pytest.mark.asyncio
async def test_reference_locks_are_sorted_deduplicated_and_verified(monkeypatch):
    import app.services.raster_mask_storage as module

    db = SimpleNamespace(execute=AsyncMock())
    first = module.build_rle_reference(
        {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    )
    second = module.build_rle_reference(
        {"encoding": "coco_rle", "size": [1, 1], "counts": [1]}
    )
    ordered = sorted((first, second), key=lambda reference: reference["object_key"])
    refs = [ordered[1], ordered[0], ordered[1]]
    keys = [reference["object_key"] for reference in ordered]
    load = AsyncMock()

    async def permit(actual_db, actual_keys):
        assert actual_db is db
        assert actual_keys == keys
        assert db.execute.await_count == 2
        load.assert_not_awaited()
        return dict.fromkeys(keys, 0)

    gate = AsyncMock(side_effect=permit)
    monkeypatch.setattr(module, "load_coco_rle", load)
    monkeypatch.setattr(module, "begin_mask_content_write", gate)

    assert await lock_raster_mask_references(db, {"items": refs}) == keys
    assert [call.args[1]["key"] for call in db.execute.await_args_list] == [
        f"aap:raster-mask:{key}" for key in keys
    ]
    assert [call.args[0]["sha256"] for call in load.call_args_list] == [
        reference["sha256"] for reference in ordered
    ]
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_mask_geometry_context_matches_video_size_and_frame_count(monkeypatch):
    _patch_video_summary(monkeypatch, width=1920, height=1080, frame_count=10)
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=1920,
                height=1080,
                metadata_={"video": {"frame_count": 10}},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    geometry = {
        "type": "video_track_mask",
        "keyframes": [{"frame_index": 9, "mask": {"size": [1080, 1920]}}],
        "outside": [{"from": 2, "to": 4}],
    }
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0]["mask"]["size"] = (1080, 1920)
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0]["frame_index"] = 10
    with pytest.raises(ValueError, match="frame_index"):
        await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0] = {"frame_index": 9, "mask": {"size": [720, 1280]}}
    with pytest.raises(ValueError, match="mask size"):
        await validate_mask_geometry_for_task(db, task, geometry)


@pytest.mark.asyncio
async def test_single_frame_video_mask_matches_video_size_and_frame_count(monkeypatch):
    _patch_video_summary(monkeypatch, width=1920, height=1080, frame_count=10)
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=1920,
                height=1080,
                metadata_={"video": {"frame_count": 10}},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    geometry = {
        "type": "video_mask",
        "frame_index": 9,
        "mask": {"size": [1080, 1920]},
    }
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["frame_index"] = 10
    with pytest.raises(ValueError, match="frame_index"):
        await validate_mask_geometry_for_task(db, task, geometry)

    geometry["frame_index"] = 9
    geometry["mask"]["size"] = [720, 1280]
    with pytest.raises(ValueError, match="mask size"):
        await validate_mask_geometry_for_task(db, task, geometry)


@pytest.mark.asyncio
async def test_mask_geometry_context_separates_image_8k_from_video_4k_limit(
    monkeypatch,
):
    image_db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="image",
                width=8192,
                height=8192,
                metadata_={},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    await validate_mask_geometry_for_task(
        image_db,
        task,
        {"type": "raster_mask", "mask": {"size": [8192, 8192]}},
    )

    _patch_video_summary(monkeypatch, width=4097, height=1, frame_count=1)
    video_db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=4097,
                height=1,
                metadata_={"video": {"frame_count": 1}},
            )
        )
    )
    with pytest.raises(RasterMaskContractError) as captured:
        await validate_mask_geometry_for_task(
            video_db,
            task,
            {
                "type": "video_track_mask",
                "keyframes": [{"frame_index": 0, "mask": {"size": [1, 4097]}}],
            },
        )

    assert captured.value.detail["reason"] == "video_mask_dimensions_exceeded"
