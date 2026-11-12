"""Persistent RLE follows the selected byte owner or saved raster ownership."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.config import settings
from app.services import mask_content, raster_mask_objects, raster_mask_storage
from app.services.raster_mask_storage import (
    build_rle_reference,
    load_coco_rle,
    store_coco_rle,
)

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
TOKEN = "mask-owner-selection-unit-token-32-chars"


class ForbiddenStorage:
    @property
    def client(self):
        raise AssertionError("unselected mode must not call the configured owner")


def _unselected(monkeypatch):
    monkeypatch.setattr(settings, "assets_mask_content_url", "")
    monkeypatch.setattr(settings, "assets_mask_content_token", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_token", "")


def _selected(monkeypatch):
    monkeypatch.setattr(settings, "assets_mask_content_url", "http://mask-owner.test")
    monkeypatch.setattr(settings, "assets_mask_content_token", TOKEN)
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_token", "")
    original = httpx.Client

    def transport(**kwargs):
        return original(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(mask_content.httpx, "Client", transport)


def _handler(request):
    reference = build_rle_reference(RLE)
    if request.url.path.endswith("/put"):
        assert json.loads(request.content)["content"] == RLE
        return httpx.Response(
            200,
            json={
                "schema": "aap-assets-mask-content/v1",
                "ok": True,
                "data": {"reference": reference},
            },
        )
    return httpx.Response(
        200,
        json={
            "schema": "aap-assets-mask-content/v1",
            "ok": True,
            "data": {"reference": reference, "content": RLE},
        },
    )


async def test_unselected_mode_keeps_saved_raster_ownership(monkeypatch):
    _unselected(monkeypatch)
    reference = build_rle_reference(RLE)
    store = AsyncMock(return_value=reference)
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(raster_mask_objects, "store", store)
    monkeypatch.setattr(raster_mask_objects, "load", load)

    assert await store_coco_rle(RLE, storage=ForbiddenStorage()) == reference
    assert await load_coco_rle(reference, storage=ForbiddenStorage()) == RLE
    store.assert_awaited_once()
    load.assert_awaited_once()


async def test_selected_owner_publishes_no_saved_ownership_or_api_bytes(monkeypatch):
    _selected(monkeypatch)
    forbidden = AsyncMock(side_effect=AssertionError("saved ownership was written"))
    monkeypatch.setattr(raster_mask_objects, "store", forbidden)
    monkeypatch.setattr(raster_mask_objects, "load", forbidden)
    reference = build_rle_reference(RLE)

    assert await store_coco_rle(RLE, storage=ForbiddenStorage()) == reference
    assert await load_coco_rle(reference, storage=ForbiddenStorage()) == RLE
    forbidden.assert_not_awaited()


async def test_video_acceptance_reads_selected_owner_without_saved_ownership(
    monkeypatch,
):
    from app.services import video_prediction_acceptance as accept

    _selected(monkeypatch)
    reference = build_rle_reference(RLE)
    value = [{"geometry": {"type": "raster_mask", "mask": reference}}]
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(raster_mask_storage, "load_coco_rle", load)
    monkeypatch.setattr(accept, "_saved_results", lambda *_args: None)
    monkeypatch.setattr(
        raster_mask_objects,
        "load",
        AsyncMock(side_effect=AssertionError("saved ownership was read")),
    )

    facts = await accept.qualify_mask_references(
        SimpleNamespace(in_transaction=lambda: False),
        value,
        {"video": {"height": 2, "width": 3}},
    )

    load.assert_awaited_once_with(reference)
    assert facts == [accept._remote_mask_fact(reference)]
