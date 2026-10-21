from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.exceptions import ClientError
import pytest

from aap_workspace.configuration_contract import ConfigurationUnavailable
from aap_workspace.guide_asset_storage import GuideAssetStorageError
from app.services import workspace_guide_objects as binding


class ExistingClient:
    meta = SimpleNamespace(endpoint_url="http://existing-storage.test:9000")

    def __init__(self):
        self.calls = []
        self.failure = False

    def head_object(self, **values):
        self.calls.append(values)
        if self.failure:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "private-provider-detail",
                    }
                },
                "HeadObject",
            )
        return {"ContentLength": 0}


def scoped_key():
    project_id = uuid4()
    return project_id, f"projects/{project_id}/guide/{uuid4()}-image.png"


def local_client(monkeypatch):
    client = ExistingClient()
    monkeypatch.setattr(binding.storage_service, "client", client)
    monkeypatch.setattr(binding.storage_service, "bucket", "fixed-guide-bucket")
    monkeypatch.setattr(binding.settings, "workspace_guide_objects_url", "")
    return client


@pytest.mark.asyncio
async def test_local_binding_uses_existing_client_and_keeps_zero_distinct_from_missing(
    monkeypatch,
):
    client = local_client(monkeypatch)
    project_id, key = scoped_key()
    assert await binding.guide_asset_objects().verify_upload(project_id, key) == 0
    assert client.calls == [{"Bucket": "fixed-guide-bucket", "Key": key}]


@pytest.mark.asyncio
async def test_local_binding_rejects_foreign_project_before_storage(monkeypatch):
    client = local_client(monkeypatch)
    _, key = scoped_key()
    with pytest.raises(ConfigurationUnavailable):
        await binding.guide_asset_objects().verify_upload(uuid4(), key)
    assert client.calls == []


@pytest.mark.asyncio
async def test_local_storage_denial_keeps_safe_plain_error(monkeypatch):
    client = local_client(monkeypatch)
    client.failure = True
    with pytest.raises(GuideAssetStorageError) as failure:
        await binding.guide_asset_objects().verify_upload(*scoped_key())
    assert "private-provider-detail" not in str(failure.value)


@pytest.mark.asyncio
async def test_explicit_remote_failure_never_uses_local_client(monkeypatch):
    client = local_client(monkeypatch)
    monkeypatch.setattr(
        binding.settings, "workspace_guide_objects_url", "http://selected-owner.test"
    )
    monkeypatch.setattr(binding.settings, "workspace_guide_objects_token", "")
    monkeypatch.setattr(binding.settings, "workspace_guide_objects_token_file", "")
    port = binding.guide_asset_objects()
    with pytest.raises(ConfigurationUnavailable):
        await port.verify_upload(*scoped_key())
    assert client.calls == []


@pytest.mark.asyncio
async def test_existing_default_client_keeps_legacy_presign_and_get_alignment(
    monkeypatch,
):
    sdk = boto3.client(
        "s3",
        endpoint_url="http://existing-storage.test:9000",
        aws_access_key_id="fixture-access",
        aws_secret_access_key="fixture-secret",
        region_name="us-east-1",
    )
    calls = []

    class ObservedClient:
        meta = sdk.meta

        def generate_presigned_url(self, operation, **values):
            calls.append((operation, values["ExpiresIn"]))
            return sdk.generate_presigned_url(operation, **values)

    monkeypatch.setattr(binding.storage_service, "client", ObservedClient())
    monkeypatch.setattr(binding.storage_service, "bucket", "fixed-guide-bucket")
    monkeypatch.setattr(binding.settings, "minio_public_url", "/minio")
    monkeypatch.setattr(binding.settings, "workspace_guide_objects_url", "")
    project_id, key = scoped_key()
    try:
        port = binding.guide_asset_objects()
        upload = await port.generate_upload_url(
            project_id, key, content_type="image/png"
        )
        assert sorted(parse_qs(urlsplit(upload).query)) == [
            "AWSAccessKeyId",
            "Expires",
            "Signature",
            "content-type",
        ]
        download = await port.generate_download_url(project_id, key)
        assert sorted(parse_qs(urlsplit(download).query)) == [
            "AWSAccessKeyId",
            "Expires",
            "Signature",
        ]
        assert calls[0] == ("put_object", 900)
        assert calls[1][0] == "get_object" and 3600 <= calls[1][1] <= 4200
    finally:
        sdk.close()
