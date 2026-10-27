"""Guide-asset authenticated transfer routes."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import APIStatusError
from ai_annotation.guide_assets import GuideAssets

from .conftest import API

PROJECT_ID = str(uuid4())
GUIDE_KEY = f"projects/{PROJECT_ID}/guide/{uuid4()}-hint.svg"
ENTRY = {
    "key": GUIDE_KEY,
    "original_name": "hint.svg",
    "content_type": "image/svg+xml",
    "size": 7,
    "uploaded_at": "2026-10-17T00:00:00Z",
}


def test_guide_asset_upload_posts_authenticated_bytes(client, respx_mock, tmp_path):
    guide = GuideAssets(client._http)
    source = tmp_path / "hint.svg"
    source.write_bytes(b"<svg/>\n")
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/guide-assets/upload").mock(
        return_value=httpx.Response(200, json=ENTRY)
    )

    entry = guide.upload(PROJECT_ID, source)

    request = route.calls.last.request
    assert request.content == b"<svg/>\n"
    assert request.url.params["file_name"] == "hint.svg"
    assert request.headers["content-type"] == "image/svg+xml"
    assert request.headers["authorization"] == "Bearer ak_test"
    assert entry.key == GUIDE_KEY
    assert entry.size == 7


def test_guide_asset_upload_rejects_unsupported_type_before_request(
    client, respx_mock, tmp_path
):
    guide = GuideAssets(client._http)
    source = tmp_path / "guide.pdf"
    source.write_bytes(b"%PDF")
    assert not respx_mock.calls

    with pytest.raises(ValueError):
        guide.upload(PROJECT_ID, source)
    assert not respx_mock.calls


def test_guide_asset_content_reads_authenticated_bytes(client, respx_mock):
    guide = GuideAssets(client._http)
    route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/guide-assets/content").mock(
        return_value=httpx.Response(
            200, content=b"<svg/>\n", headers={"content-type": "image/svg+xml"}
        )
    )

    assert guide.content(PROJECT_ID, GUIDE_KEY) == b"<svg/>\n"
    request = route.calls.last.request
    assert request.url.params["key"] == GUIDE_KEY
    assert request.headers["authorization"] == "Bearer ak_test"


def test_guide_asset_delete_uses_storage_key(client, respx_mock):
    guide = GuideAssets(client._http)
    route = respx_mock.delete(f"{API}/projects/{PROJECT_ID}/guide-assets").mock(
        return_value=httpx.Response(200, json={"deleted": GUIDE_KEY})
    )

    assert guide.delete(PROJECT_ID, GUIDE_KEY).deleted == GUIDE_KEY
    assert route.calls.last.request.url.params["key"] == GUIDE_KEY


def test_guide_asset_upload_is_not_retried(client, respx_mock, tmp_path):
    guide = GuideAssets(client._http)
    source = tmp_path / "hint.svg"
    source.write_bytes(b"<svg/>\n")
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/guide-assets/upload").mock(
        return_value=httpx.Response(503, json={"detail": "temporarily unavailable"})
    )

    with pytest.raises(APIStatusError):
        guide.upload(PROJECT_ID, source)
    assert route.call_count == 1
