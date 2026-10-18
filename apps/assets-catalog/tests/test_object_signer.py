from dataclasses import replace

import pytest
from botocore.exceptions import ClientError

from aap_assets_catalog.config import CatalogConfig
from aap_assets_catalog.object_signer import (
    MAX_CALIBRATION_SOURCE_BYTES,
    CatalogObjectSigner,
    SpatialCalibrationSourceReader,
    SourceObjectChanged,
    SourceObjectTooLarge,
)


def _config() -> CatalogConfig:
    return CatalogConfig(
        database_url="postgresql://catalog:secret@db/aap",
        driver_database_url="postgresql://catalog:secret@db/aap",
        gateway_token="catalog-gateway-token-that-is-long-enough",
        s3_public_endpoint="http://storage.example.test:9000",
        s3_endpoint="http://storage.example.test:9000",
        s3_region="us-east-1",
        s3_access_key="read-only-key",
        s3_secret_key="read-only-secret",
        datasets_bucket="datasets",
        media_cache_bucket="media-cache",
        host="0.0.0.0",
        port=8766,
        max_request_bytes=262144,
        max_response_bytes=16777216,
        pool_min_size=1,
        pool_max_size=8,
        pool_timeout_seconds=2,
        sql_timeout_seconds=5,
    )


class _FakePresignClient:
    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, client_method, *, Params, ExpiresIn):
        self.calls.append((client_method, Params, ExpiresIn))
        return f"https://signed.test/{Params['Bucket']}/{Params['Key']}"


class _FakeBody:
    def __init__(self, content: bytes):
        self.content = content
        self.offset = 0
        self.closed = False

    def read(self, limit: int) -> bytes:
        value = self.content[self.offset : self.offset + limit]
        self.offset += len(value)
        return value

    def close(self) -> None:
        self.closed = True


class _FakeSourceClient(_FakePresignClient):
    def __init__(self, content: bytes = b'{"extrinsic": []}', *, etag="etag-1"):
        super().__init__()
        self.content = content
        self.etag = etag
        self.head_calls = []
        self.get_calls = []
        self.body = _FakeBody(content)
        self.get_etag = etag
        self.get_size = len(content)
        self.head_error: ClientError | None = None

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        if self.head_error:
            raise self.head_error
        return {"ETag": f'"{self.etag}"', "ContentLength": len(self.content)}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        return {
            "ETag": f'"{self.get_etag}"',
            "ContentLength": self.get_size,
            "Body": self.body,
        }


def test_signer_only_requests_get_urls_and_routes_fixed_thumbnail_prefixes() -> None:
    client = _FakePresignClient()
    signer = CatalogObjectSigner(_config(), client=client)

    assert signer.sign_source("legacy-dataset/frame.jpg") == (
        "https://signed.test/datasets/legacy-dataset/frame.jpg"
    )
    for prefix in ("thumbnails/", "videos/", "playback/", "image-pyramids/"):
        signer.sign_thumbnail(f"{prefix}item/preview.webp")
    signer.sign_thumbnail("legacy-preview/preview.webp")

    assert [call[0] for call in client.calls] == ["get_object"] * 6
    assert [call[1]["Bucket"] for call in client.calls] == [
        "datasets",
        "media-cache",
        "media-cache",
        "media-cache",
        "media-cache",
        "datasets",
    ]
    assert all(call[2] >= 3600 for call in client.calls)


def test_public_endpoint_is_signing_host_and_no_write_methods_are_used() -> None:
    client = _FakePresignClient()
    signer = CatalogObjectSigner(
        replace(_config(), s3_public_endpoint="https://public-storage.test"),
        client=client,
    )
    signer.sign_source("dataset/file.jpg")
    operation, params, _expiry = client.calls[0]
    assert operation == "get_object"
    assert params == {"Bucket": "datasets", "Key": "dataset/file.jpg"}


def test_source_reads_use_the_private_endpoint_while_presigns_keep_public_origin(
    monkeypatch,
) -> None:
    public_client = _FakePresignClient()
    source_client = _FakeSourceClient()
    endpoints = []
    client_configs = []

    def make_client(service, **kwargs):
        endpoints.append(kwargs["endpoint_url"])
        client_configs.append(kwargs)
        return source_client

    monkeypatch.setattr("aap_assets_catalog.object_signer.boto3.client", make_client)
    config = replace(
        _config(),
        s3_public_endpoint="http://127.0.0.1:9000",
        s3_endpoint="http://minio:9000",
    )
    signer = CatalogObjectSigner(config, client=public_client)

    assert signer.sign_source("dataset/frame.jpg") == (
        "https://signed.test/datasets/dataset/frame.jpg"
    )
    assert (
        signer.get_calibration_source(
            "dataset/calib/camera.json",
            expected_etag="etag-1",
            expected_size=len(source_client.content),
        )
        == source_client.content
    )
    assert endpoints == ["http://minio:9000"]
    source_config = client_configs[0]["config"]
    assert source_config.connect_timeout == 2
    assert source_config.read_timeout == 10
    assert source_config.retries == {"max_attempts": 1, "mode": "standard"}
    assert source_config.signature_version == "s3v4"
    assert source_config.s3 == {"addressing_style": "path"}
    assert public_client.calls[0][1] == {
        "Bucket": "datasets",
        "Key": "dataset/frame.jpg",
    }


def test_source_head_and_conditional_get_are_fixed_to_datasets_bucket() -> None:
    client = _FakeSourceClient()
    signer = CatalogObjectSigner(_config(), client=client, source_client=client)

    facts = signer.head_source(
        "dataset/calib/camera.json",
        expected_etag="etag-1",
        expected_size=len(client.content),
    )
    assert facts.etag == "etag-1"
    assert facts.size == len(client.content)
    assert client.head_calls == [
        {
            "Bucket": "datasets",
            "Key": "dataset/calib/camera.json",
            "IfMatch": '"etag-1"',
        }
    ]

    body = signer.get_calibration_source(
        "dataset/calib/camera.json",
        expected_etag="etag-1",
        expected_size=len(client.content),
    )
    assert body == client.content
    assert client.get_calls == [
        {
            "Bucket": "datasets",
            "Key": "dataset/calib/camera.json",
            "IfMatch": '"etag-1"',
        }
    ]
    assert client.body.closed


def test_source_get_rejects_a_changed_or_oversized_object() -> None:
    client = _FakeSourceClient()
    signer = CatalogObjectSigner(_config(), client=client, source_client=client)

    with pytest.raises(SourceObjectChanged):
        signer.get_calibration_source(
            "dataset/calib/camera.json",
            expected_etag="old-etag",
            expected_size=len(client.content),
        )
    assert client.get_calls == []

    too_large = _FakeSourceClient(content=b"x" * (MAX_CALIBRATION_SOURCE_BYTES + 1))
    too_large_signer = CatalogObjectSigner(
        _config(), client=too_large, source_client=too_large
    )
    with pytest.raises(SourceObjectTooLarge):
        too_large_signer.get_calibration_source(
            "dataset/calib/camera.json",
            expected_etag="etag-1",
            expected_size=len(too_large.content),
        )
    assert too_large.get_calls == []


def test_source_get_enforces_the_read_cap_even_when_s3_size_lies() -> None:
    content = b"x" * (MAX_CALIBRATION_SOURCE_BYTES + 1)
    client = _FakeSourceClient(content=b"x", etag="etag-1")
    client.body = _FakeBody(content)
    client.get_size = 1
    signer = CatalogObjectSigner(_config(), client=client, source_client=client)

    with pytest.raises(SourceObjectTooLarge):
        signer.get_calibration_source(
            "dataset/calib/camera.json", expected_etag="etag-1", expected_size=1
        )
    assert client.body.closed


def test_source_if_match_precondition_failure_is_changed_not_missing() -> None:
    client = _FakeSourceClient()
    client.head_error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "HeadObject",
    )
    signer = CatalogObjectSigner(_config(), client=client, source_client=client)

    with pytest.raises(SourceObjectChanged):
        signer.get_calibration_source(
            "dataset/calib/camera.json",
            expected_etag="etag-1",
            expected_size=len(client.content),
        )


def test_source_get_closes_body_when_returned_etag_does_not_match() -> None:
    client = _FakeSourceClient()
    client.get_etag = "changed-etag"
    signer = CatalogObjectSigner(_config(), client=client, source_client=client)

    with pytest.raises(SourceObjectChanged):
        signer.get_calibration_source(
            "dataset/calib/camera.json",
            expected_etag="etag-1",
            expected_size=len(client.content),
        )
    assert client.body.closed


def test_spatial_reader_can_use_the_existing_assets_storage_client_and_bucket() -> None:
    client = _FakeSourceClient()
    reader = SpatialCalibrationSourceReader(client, "datasets")

    facts, body = reader.read_calibration_source("dataset/calib/camera.json")

    assert facts.etag == "etag-1"
    assert facts.size == len(client.content)
    assert body == client.content
    assert client.get_calls[0]["IfMatch"] == '"etag-1"'
