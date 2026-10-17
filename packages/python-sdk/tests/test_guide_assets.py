"""Guide-asset REST calls and credential-isolated signed uploads."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import APIStatusError
from ai_annotation.guide_asset_models import (
    GuideAssetSignedUrlResponse,
    GuideAssetUploadCompleteRequest,
    GuideAssetUploadInitRequest,
)
from ai_annotation.guide_assets import GuideAssets

from .conftest import API

PROJECT_ID = str(uuid4())
GUIDE_KEY = f"projects/{PROJECT_ID}/guide/{uuid4()}-hint.svg"


def test_guide_asset_rest_routes(client, respx_mock):
    guide = GuideAssets(client._http)
    init = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/guide-assets/upload-init"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "key": GUIDE_KEY,
                "upload_url": "http://storage.local/put?signature=temporary",
                "expires_in": 900,
            },
        )
    )
    prepared = guide.upload_init(
        PROJECT_ID,
        GuideAssetUploadInitRequest(
            filename="hint.svg", content_type="image/svg+xml", size=256
        ),
    )
    assert json.loads(init.calls.last.request.content) == {
        "filename": "hint.svg",
        "content_type": "image/svg+xml",
        "size": 256,
    }
    assert prepared.key == GUIDE_KEY
    assert prepared.expires_in == 900
    assert "signature=temporary" not in repr(prepared)

    complete = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/guide-assets/upload-complete"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "key": GUIDE_KEY,
                "original_name": "hint.svg",
                "content_type": "image/svg+xml",
                "size": 256,
                "uploaded_at": "2026-10-17T00:00:00Z",
            },
        )
    )
    entry = guide.upload_complete(
        PROJECT_ID,
        GuideAssetUploadCompleteRequest(
            key=GUIDE_KEY,
            original_name="hint.svg",
            content_type="image/svg+xml",
        ),
    )
    assert json.loads(complete.calls.last.request.content) == {
        "key": GUIDE_KEY,
        "original_name": "hint.svg",
        "content_type": "image/svg+xml",
    }
    assert entry.size == 256

    delete = respx_mock.delete(f"{API}/projects/{PROJECT_ID}/guide-assets").mock(
        return_value=httpx.Response(200, json={"deleted": GUIDE_KEY})
    )
    assert guide.delete(PROJECT_ID, GUIDE_KEY).deleted == GUIDE_KEY
    assert delete.calls.last.request.url.params["key"] == GUIDE_KEY

    sign = respx_mock.get(f"{API}/projects/{PROJECT_ID}/guide-assets/sign-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "http://storage.local/get?signature=temporary",
                "expires_in": 3600,
            },
        )
    )
    signed = guide.sign_url(PROJECT_ID, GUIDE_KEY)
    assert sign.calls.last.request.url.params["key"] == GUIDE_KEY
    assert signed.expires_in == 3600
    assert "signature=temporary" not in repr(signed)


def test_guide_asset_request_exposes_actual_supported_mime_types():
    with pytest.raises(ValueError):
        GuideAssetUploadInitRequest(
            filename="guide.pdf", content_type="application/pdf", size=12
        )
    assert (
        "image/svg+xml"
        in GuideAssetUploadInitRequest.model_json_schema()["properties"][
            "content_type"
        ]["enum"]
    )


def test_malformed_signed_response_does_not_leak_capability_in_validation_error():
    with pytest.raises(ValueError) as error:
        GuideAssetSignedUrlResponse.model_validate(
            {"url": "https://storage.example/read?signature=private-capability"}
        )
    assert "private-capability" not in str(error.value)


def test_signed_put_uses_bare_client_and_does_not_complete(
    client, respx_mock, tmp_path
):
    guide = GuideAssets(client._http)
    source = tmp_path / "hint.svg"
    source.write_bytes(b"<svg/>\n")
    upload_url = "http://storage.local/put/hint.svg?signature=temporary"
    put = respx_mock.put(upload_url).mock(return_value=httpx.Response(201))

    assert guide.upload_bytes(upload_url, source, "image/svg+xml") is None

    request = put.calls.last.request
    assert request.content == b"<svg/>\n"
    assert request.headers["content-type"] == "image/svg+xml"
    assert "authorization" not in request.headers
    assert len(respx_mock.calls) == 1


def test_signed_put_requires_a_success_status(client, respx_mock, tmp_path):
    guide = GuideAssets(client._http)
    source = tmp_path / "hint.svg"
    source.write_bytes(b"<svg/>\n")
    upload_url = "http://storage.local/put/hint.svg?signature=temporary"
    put = respx_mock.put(upload_url).mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://storage.local/elsewhere"}
        )
    )

    with pytest.raises(APIStatusError) as exc:
        guide.upload_bytes(upload_url, source, "image/svg+xml")
    assert exc.value.status_code == 302
    assert len(put.calls) == 1


def test_guide_asset_post_is_not_retried(client, respx_mock):
    guide = GuideAssets(client._http)
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/guide-assets/upload-init"
    ).mock(return_value=httpx.Response(503, json={"detail": "temporarily unavailable"}))

    with pytest.raises(APIStatusError):
        guide.upload_init(
            PROJECT_ID,
            GuideAssetUploadInitRequest(
                filename="hint.svg", content_type="image/svg+xml", size=256
            ),
        )
    assert route.call_count == 1
