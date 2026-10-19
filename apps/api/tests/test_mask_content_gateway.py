"""API Mask wrappers must use the selected byte owner without a storage fallback."""

import json

import httpx
import pytest

from aap_assets_mask_content.contract import MaskContentFailure
from app.config import settings
from app.services import mask_content
from app.services.raster_mask_storage import (
    build_rle_reference,
    load_coco_rle,
    store_coco_rle,
)

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
TOKEN = "mask-gateway-unit-token-with-32-characters"


class ForbiddenStorage:
    @property
    def client(self):
        raise AssertionError("configured remote Mask operation touched API S3")


def _remote(monkeypatch, handler):
    monkeypatch.setattr(settings, "assets_mask_content_url", "http://mask-owner.test")
    monkeypatch.setattr(settings, "assets_mask_content_token", TOKEN)
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_token", "")
    original = httpx.Client

    def transport(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mask_content.httpx, "Client", transport)


def _success(data):
    return httpx.Response(
        200, json={"schema": "aap-assets-mask-content/v1", "ok": True, "data": data}
    )


async def test_mask_wrappers_use_remote_put_and_get_with_existing_references(
    monkeypatch,
):
    reference = build_rle_reference(RLE)
    operations = []

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        body = json.loads(request.content)
        operations.append(request.url.path)
        if request.url.path.endswith("/put"):
            assert body["content"] == RLE
            assert body["storage_encoding"] == "identity"
            return _success({"reference": reference})
        assert body["reference"]["sha256"] == reference["sha256"]
        return _success({"reference": reference, "content": RLE})

    _remote(monkeypatch, handler)
    stored = await store_coco_rle(RLE, storage=ForbiddenStorage())
    assert stored == reference
    assert await load_coco_rle(stored, storage=ForbiddenStorage()) == RLE
    assert operations == ["/v1/mask-content/put", "/v1/mask-content/get"]


async def test_remote_failure_keeps_storage_unavailable_and_never_falls_back(
    monkeypatch,
):
    def handler(request):
        raise httpx.ConnectError("owner unavailable", request=request)

    _remote(monkeypatch, handler)
    with pytest.raises(MaskContentFailure) as captured:
        await store_coco_rle(RLE, storage=ForbiddenStorage())
    assert captured.value.code == "storage_unavailable"


async def test_remote_get_cannot_retarget_another_valid_content_reference(monkeypatch):
    other = {"encoding": "coco_rle", "size": [2, 3], "counts": [6]}
    _remote(
        monkeypatch,
        lambda _request: _success(
            {"reference": build_rle_reference(other), "content": other}
        ),
    )
    with pytest.raises(MaskContentFailure):
        await load_coco_rle(build_rle_reference(RLE), storage=ForbiddenStorage())


async def test_invalid_reference_is_rejected_before_any_owner_request(monkeypatch):
    def handler(_request):
        raise AssertionError("invalid reference escaped the API boundary")

    _remote(monkeypatch, handler)
    reference = {
        **build_rle_reference(RLE),
        "object_key": "other-private-prefix/object",
    }
    with pytest.raises(ValueError, match="invalid mask reference"):
        await load_coco_rle(reference, storage=ForbiddenStorage())


def test_content_configuration_does_not_supply_a_cleanup_fallback(monkeypatch):
    _remote(monkeypatch, lambda _request: pytest.fail("unexpected request"))
    with pytest.raises(MaskContentFailure) as captured:
        with mask_content.mask_content_owner(ForbiddenStorage(), cleanup=True):
            pytest.fail("missing cleanup owner was accepted")
    assert captured.value.code == "storage_unavailable"


@pytest.mark.parametrize("origin", ["http://[", "http://owner:invalid"])
async def test_malformed_remote_origin_is_service_unavailability(monkeypatch, origin):
    _remote(monkeypatch, lambda _request: pytest.fail("unexpected request"))
    monkeypatch.setattr(settings, "assets_mask_content_url", origin)
    with pytest.raises(MaskContentFailure) as captured:
        await store_coco_rle(RLE, storage=ForbiddenStorage())
    assert captured.value.code == "storage_unavailable"
