from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from aap_assets_source.guide_object_contract import (
    decode_guide_object_request,
    guide_object_failure_reply,
    guide_object_success_reply,
)
from aap_workspace.configuration_contract import ConfigurationUnavailable
from aap_workspace.guide_asset_storage import (
    GuideAssetStorageError,
    RemoteGuideAssetObjects,
)


def _scope():
    project_id = uuid4()
    return project_id, f"projects/{project_id}/guide/{uuid4()}-guide.png"


@pytest.mark.asyncio
async def test_four_effects_bind_exact_project_key_and_private_result():
    project_id, key = _scope()
    seen = []

    def serve(incoming):
        request = decode_guide_object_request(incoming.content)
        assert request.project_id == project_id and request.key == key
        seen.append(request.operation)
        result = {
            "presign_put": {"url": "/minio/bucket/put", "expires_in": 900},
            "head": {"content_length": 0},
            "delete": {"deleted": True},
            "presign_get": {"url": "/minio/bucket/get", "expires_in": 3600},
        }[request.operation]
        return httpx.Response(
            200,
            json=guide_object_success_reply(request, result).model_dump(
                mode="json", by_alias=True
            ),
        )

    port = RemoteGuideAssetObjects(
        "http://objects.test", "s" * 40, transport=httpx.MockTransport(serve)
    )
    assert (
        await port.generate_upload_url(project_id, key, content_type="image/png")
        == "/minio/bucket/put"
    )
    assert await port.verify_upload(project_id, key) == 0
    await port.delete_object(project_id, key)
    assert await port.generate_download_url(project_id, key) == "/minio/bucket/get"
    assert seen == ["presign_put", "head", "delete", "presign_get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["storage_error", "busy", "unavailable", "invalid_request"]
)
async def test_expected_storage_fault_stays_distinct_from_unavailable(code):
    def serve(incoming):
        request = decode_guide_object_request(incoming.content)
        return httpx.Response(
            200,
            json=guide_object_failure_reply(request, code).model_dump(
                mode="json", by_alias=True
            ),
        )

    port = RemoteGuideAssetObjects(
        "http://objects.test", "s" * 40, transport=httpx.MockTransport(serve)
    )
    error_type = (
        GuideAssetStorageError if code == "storage_error" else ConfigurationUnavailable
    )
    with pytest.raises(error_type):
        await port.verify_upload(*_scope())


@pytest.mark.asyncio
async def test_foreign_sign_reply_is_rejected_before_returning_url():
    calls = []

    def serve(incoming):
        calls.append(incoming)
        raw = json.loads(incoming.content)
        raw["request_id"] = str(uuid4())
        request = decode_guide_object_request(raw)
        return httpx.Response(
            200,
            json=guide_object_success_reply(
                request, {"url": "/minio/foreign", "expires_in": 3600}
            ).model_dump(mode="json", by_alias=True),
        )

    port = RemoteGuideAssetObjects(
        "http://objects.test", "s" * 40, transport=httpx.MockTransport(serve)
    )
    with pytest.raises(ConfigurationUnavailable):
        await port.generate_download_url(*_scope())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_secret_files_and_missing_configuration_are_resolved_at_effect_phase(
    tmp_path,
):
    filename = tmp_path / "empty-token"
    filename.write_text("")
    port = RemoteGuideAssetObjects("http://objects.test", token_file=str(filename))
    with pytest.raises(ConfigurationUnavailable):
        await port.verify_upload(*_scope())
    with pytest.raises(ConfigurationUnavailable):
        await RemoteGuideAssetObjects("").verify_upload(*_scope())
