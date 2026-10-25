from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlsplit

from botocore.exceptions import BotoCoreError, ClientError
import pytest

from aap_assets_source.guide_object_contract import GuideObjectFailure
from aap_assets_source_runtime import guide_storage as storage_module
from aap_assets_source_runtime.guide_config import load_guide_storage_config
from aap_assets_source_runtime.guide_storage import GuideStorage

_PREFIX = "ASSETS_SOURCE_GUIDE_S3_"
_KEY = "projects/11111111-1111-4111-8111-111111111111/guide/22222222-2222-4222-8222-222222222222-map.png"


def _config(**settings):
    environment = {
        _PREFIX + "ENDPOINT": "http://minio.internal:9000",
        _PREFIX + "BUCKET": "aap-guide-test",
        _PREFIX + "ACCESS_KEY": "explicit-access",
        _PREFIX + "SECRET_KEY": "explicit-secret",
    }
    environment.update(settings)
    return load_guide_storage_config(environment)


class FakeClient:
    def __init__(self, *, url=None):
        self.url = (
            url or f"http://minio.internal:9000/{_config().bucket}/{_KEY}?signed=1"
        )
        self.presign_calls = []
        self.head_calls = []
        self.delete_calls = []
        self.head_result = {"ContentLength": 37}
        self.head_error = None
        self.delete_error = None
        self.presign_error = None
        self.close_calls = 0

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        if self.presign_error is not None:
            raise self.presign_error
        return self.url

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        if self.head_error is not None:
            raise self.head_error
        return self.head_result

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def close(self):
        self.close_calls += 1


def _client_error(code, *, status=403):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "provider detail should not escape"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


async def test_upload_signs_exact_fixed_bucket_params_and_rewrites_the_public_prefix():
    internal, signer = FakeClient(), FakeClient()
    storage = GuideStorage(_config(), internal_client=internal, signer=signer)

    url = await storage.generate_upload_url(_KEY, content_type="image/png")

    assert url == f"/minio/{_config().bucket}/{_KEY}?signed=1"
    assert signer.presign_calls == [
        (
            "put_object",
            {"Bucket": "aap-guide-test", "Key": _KEY, "ContentType": "image/png"},
            900,
        )
    ]
    assert internal.presign_calls == []


async def test_download_uses_nominal_hour_ttl_with_legacy_ten_minute_alignment(
    monkeypatch,
):
    signer = FakeClient()
    storage = GuideStorage(_config(), internal_client=FakeClient(), signer=signer)
    monkeypatch.setattr(storage_module.time, "time", lambda: 1234.5)

    url = await storage.generate_download_url(_KEY)

    assert url == f"/minio/{_config().bucket}/{_KEY}?signed=1"
    operation, params, expires_in = signer.presign_calls[0]
    assert operation == "get_object"
    assert params == {"Bucket": "aap-guide-test", "Key": _KEY}
    assert 3600 <= expires_in <= 4200
    assert expires_in == 4166


async def test_absolute_public_prefix_is_added_after_signing_internal_endpoint():
    signer = FakeClient()
    storage = GuideStorage(
        _config(**{_PREFIX + "PUBLIC_URL": "https://cdn.example.test/s3"}),
        internal_client=FakeClient(),
        signer=signer,
    )

    url = await storage.generate_upload_url(_KEY, content_type="image/jpeg")

    assert url == f"https://cdn.example.test/s3/{_config().bucket}/{_KEY}?signed=1"
    assert signer.presign_calls[0][0] == "put_object"


async def test_real_signer_emits_sigv2_query_without_making_network_requests():
    storage = GuideStorage(_config())
    try:
        url = await storage.generate_download_url(_KEY)
    finally:
        await storage.close()

    parsed = urlsplit(url)
    query_names = {name for name, _ in parse_qsl(parsed.query)}
    assert parsed.scheme == ""
    assert parsed.path.startswith(f"/minio/aap-guide-test/{_KEY}")
    assert {"AWSAccessKeyId", "Expires", "Signature"} <= query_names
    assert not any(name.startswith("X-Amz-") for name in query_names)


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_head_maps_only_the_legacy_missing_error_codes_to_none(code):
    internal = FakeClient()
    internal.head_error = _client_error(code, status=403)
    storage = GuideStorage(_config(), internal_client=internal, signer=FakeClient())

    assert await storage.verify_upload(_KEY) is None
    assert internal.head_calls == [{"Bucket": "aap-guide-test", "Key": _KEY}]


async def test_bucket_missing_code_is_not_mistaken_for_an_uploaded_object_miss():
    internal = FakeClient()
    internal.head_error = _client_error("NoSuchBucket", status=404)
    storage = GuideStorage(_config(), internal_client=internal, signer=FakeClient())

    with pytest.raises(GuideObjectFailure) as caught:
        await storage.verify_upload(_KEY)
    assert caught.value.code == "storage_error"


async def test_head_returns_only_content_length_and_preserves_zero():
    internal = FakeClient()
    internal.head_result = {"ContentLength": "0", "ETag": "opaque"}
    storage = GuideStorage(_config(), internal_client=internal, signer=FakeClient())

    assert await storage.verify_upload(_KEY) == 0


async def test_delete_uses_the_fixed_bucket_and_never_selects_a_version():
    internal = FakeClient()
    storage = GuideStorage(_config(), internal_client=internal, signer=FakeClient())

    result = await storage.delete_object(_KEY)

    assert result is None
    assert internal.delete_calls == [{"Bucket": "aap-guide-test", "Key": _KEY}]
    assert "VersionId" not in internal.delete_calls[0]


@pytest.mark.parametrize("operation", ["presign_put", "head", "delete", "presign_get"])
async def test_expected_client_errors_are_reduced_to_the_storage_error_code(
    operation, caplog
):
    internal, signer = FakeClient(), FakeClient()
    storage = GuideStorage(_config(), internal_client=internal, signer=signer)
    provider_error = _client_error("AccessDenied")
    if operation == "presign_put" or operation == "presign_get":
        signer.presign_error = provider_error
        call = (
            storage.generate_upload_url(_KEY, content_type="image/png")
            if operation == "presign_put"
            else storage.generate_download_url(_KEY)
        )
    elif operation == "head":
        internal.head_error = provider_error
        call = storage.verify_upload(_KEY)
    else:
        internal.delete_error = provider_error
        call = storage.delete_object(_KEY)

    with pytest.raises(GuideObjectFailure) as caught:
        await call
    assert caught.value.code == "storage_error"
    assert str(caught.value) == "storage_error"
    assert "provider detail should not escape" not in caplog.text
    assert "minio.internal" not in caplog.text
    assert "explicit-secret" not in caplog.text


async def test_botocore_errors_are_sanitized_but_unexpected_python_errors_escape():
    internal = FakeClient()
    internal.head_error = BotoCoreError(error_msg="private endpoint details")
    storage = GuideStorage(_config(), internal_client=internal, signer=FakeClient())
    with pytest.raises(GuideObjectFailure) as caught:
        await storage.verify_upload(_KEY)
    assert caught.value.code == "storage_error"
    assert "private endpoint details" not in str(caught.value)

    internal.head_error = ValueError("unexpected adapter bug")
    with pytest.raises(ValueError, match="unexpected adapter bug"):
        await storage.verify_upload(_KEY)


async def test_only_owned_clients_are_closed_and_close_is_idempotent():
    internal, signer = FakeClient(), FakeClient()
    storage = GuideStorage(_config(), internal_client=internal, signer=signer)
    await storage.close()
    await storage.close()
    assert internal.close_calls == 0
    assert signer.close_calls == 0

    created = []

    def client_factory(service_name, **kwargs):
        assert service_name == "s3"
        client = FakeClient()
        created.append((kwargs, client))
        return client

    original = storage_module.boto3.client
    storage_module.boto3.client = client_factory
    try:
        owned = GuideStorage(_config())
    finally:
        storage_module.boto3.client = original

    assert len(created) == 2
    await owned.close()
    assert [client.close_calls for _, client in created] == [1, 1]


def test_owned_clients_use_explicit_credentials_fixed_path_signing_and_no_retry(
    monkeypatch,
):
    created = []

    def client_factory(service_name, **kwargs):
        assert service_name == "s3"
        created.append(kwargs)
        return FakeClient()

    monkeypatch.setattr(storage_module.boto3, "client", client_factory)
    config = _config()
    storage = GuideStorage(config)

    assert len(created) == 2
    internal, signer = created
    for client in (internal, signer):
        assert client["endpoint_url"] == config.endpoint
        assert client["aws_access_key_id"] == "explicit-access"
        assert client["aws_secret_access_key"] == "explicit-secret"
        assert client["region_name"] == config.region
        assert client["config"].s3 == {"addressing_style": "path"}
        assert client["config"].connect_timeout == 2
        assert client["config"].read_timeout == 5
        assert client["config"].retries["total_max_attempts"] == 1
    assert internal["config"].signature_version == "s3v4"
    assert signer["config"].signature_version == "s3"

    asyncio.run(storage.close())
