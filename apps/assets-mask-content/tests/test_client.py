from __future__ import annotations

import httpx
import pytest

from aap_assets_mask_content import codec
from aap_assets_mask_content.client import MaskContentClient
from aap_assets_mask_content.contract import (
    CONTENT_SCHEMA,
    DeleteRequest,
    GetRequest,
    GetResult,
    MaskContentFailure,
    PutRequest,
    PutResult,
    RleContent,
    success_envelope,
)


def _rle():
    return RleContent(encoding="coco_rle", size=(2, 2), counts=(1, 3))


def test_client_rejects_untrusted_origin_or_weak_token():
    for origin, token in (
        ("https://user:pass@example.test", "x" * 40),
        ("https://example.test/?token=secret", "x" * 40),
        ("https://example.test", "short"),
    ):
        with pytest.raises(MaskContentFailure):
            MaskContentClient(origin, token)


def test_put_validates_exact_content_and_allows_only_bounded_gzip_fallback():
    content = RleContent(
        encoding="coco_rle", size=(100, 100), counts=(*([1] * 1000), 9000)
    )
    expected = codec.build_rle_reference(content)

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + "t" * 40
        assert request.url.path == "/v1/mask-content/put"
        return httpx.Response(
            200,
            json=success_envelope(PutResult(reference=expected)),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with MaskContentClient(
            "http://mask-content.test", "t" * 40, client=http_client
        ) as client:
            result = client.put(PutRequest(content=content, storage_encoding="gzip"))
    assert result.reference == expected


def test_get_rejects_a_substituted_reference_or_bad_content():
    expected = codec.build_rle_reference(_rle())
    wrong = codec.build_rle_reference(
        RleContent(encoding="coco_rle", size=(2, 2), counts=(2, 2))
    )
    response_value = GetResult(reference=wrong, content=_rle())

    def handler(_request):
        return httpx.Response(200, json=success_envelope(response_value))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with MaskContentClient(
            "http://mask-content.test", "t" * 40, client=http_client
        ) as client:
            with pytest.raises(MaskContentFailure) as caught:
                client.get(GetRequest(reference=expected))
    assert caught.value.code == "invalid_response"


def test_client_preserves_closed_error_codes_and_bounds_response_bytes():
    def missing(_request):
        return httpx.Response(
            404,
            json={
                "schema": CONTENT_SCHEMA,
                "ok": False,
                "error": {"code": "object_missing"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(missing)) as http_client:
        with MaskContentClient(
            "http://mask-content.test", "t" * 40, client=http_client
        ) as client:
            reference = codec.build_rle_reference(_rle())
            with pytest.raises(MaskContentFailure) as caught:
                client.get(GetRequest(reference=reference))
    assert caught.value.code == "object_missing"


def test_delete_requires_same_key_in_the_result():
    reference = codec.build_rle_reference(_rle())
    from aap_assets_mask_content.contract import DeleteResult

    def handler(_request):
        return httpx.Response(
            200,
            json=success_envelope(
                DeleteResult(object_key=reference.object_key, deleted=True)
            ),
        )

    request = DeleteRequest(
        object_key=reference.object_key,
        etag="etag",
        last_modified="2026-01-01T00:00:00Z",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with MaskContentClient(
            "http://mask-content.test", "t" * 40, client=http_client
        ) as client:
            result = client.delete(request)
    assert result.deleted is True
