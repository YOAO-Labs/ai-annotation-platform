from dataclasses import replace

from aap_assets_catalog.config import CatalogConfig
from aap_assets_catalog.object_signer import CatalogObjectSigner


def _config() -> CatalogConfig:
    return CatalogConfig(
        database_url="postgresql://catalog:secret@db/aap",
        driver_database_url="postgresql://catalog:secret@db/aap",
        gateway_token="catalog-gateway-token-that-is-long-enough",
        s3_public_endpoint="http://storage.example.test:9000",
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
