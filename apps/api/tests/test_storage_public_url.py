from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
import pytest

from app.config import settings
from app.services import storage_presigning as signing
from app.services.storage import StorageService


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(settings, "minio_use_ssl", False)
    monkeypatch.setattr(settings, "minio_endpoint", "internal.test:9000")
    monkeypatch.setattr(settings, "minio_access_key", "test-access")
    monkeypatch.setattr(settings, "minio_secret_key", "test-secret")
    monkeypatch.setattr(settings, "minio_public_url", "/minio")
    monkeypatch.setattr(settings, "ml_backend_storage_host", "ml.test:9000")
    monkeypatch.setattr(
        "botocore.auth.get_current_datetime",
        lambda **_: datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
    )
    value = StorageService(
        client_config=Config(signature_version="s3v4", s3={"addressing_style": "path"})
    )
    try:
        yield value
    finally:
        value.client.close()


def download(service, audience="browser"):
    return service.generate_download_url(
        "目录/a +%.png",
        bucket="saved-bucket",
        version_id="version/1+%",
        expires_in=61,
        align=False,
        audience=audience,
    )


def test_relative_browser_proxy_and_ml_have_independent_valid_signing_inputs(service):
    browser = urlsplit(download(service))
    internal = urlsplit(download(service, "internal"))
    ml = urlsplit(download(service, "ml"))
    assert browser.netloc == browser.scheme == ""
    assert browser.path == "/minio" + internal.path
    assert ml.netloc == "ml.test:9000"
    assert internal.netloc == "internal.test:9000"
    assert ml.path == internal.path
    assert browser.query == internal.query
    internal_query, ml_query = parse_qs(internal.query), parse_qs(ml.query)
    assert internal_query["X-Amz-SignedHeaders"] == ["host"]
    assert internal_query["X-Amz-Expires"] == ["61"]
    assert internal_query["versionId"] == ["version/1+%"]
    assert internal_query["X-Amz-Signature"] != ml_query["X-Amz-Signature"]
    assert service.client.meta.endpoint_url == "http://internal.test:9000"


@pytest.mark.parametrize(
    "public", ["https://public.test:9443", "https://public.test:9443/files"]
)
def test_absolute_public_origin_is_signed_before_optional_route_prefix(
    service, monkeypatch, public
):
    monkeypatch.setattr(settings, "minio_public_url", public)
    public_parts = urlsplit(download(service))
    internal_parts = urlsplit(download(service, "internal"))
    assert public_parts.netloc == "public.test:9443"
    assert public_parts.scheme == "https"
    assert public_parts.path == urlsplit(public).path + internal_parts.path
    assert (
        parse_qs(public_parts.query)["X-Amz-Signature"]
        != parse_qs(internal_parts.query)["X-Amz-Signature"]
    )
    monkeypatch.setattr(settings, "minio_public_url", "https://public.test:9443")
    assert urlsplit(download(service)).query == public_parts.query


def test_ml_without_override_uses_internal_not_absolute_public_origin(
    service, monkeypatch
):
    monkeypatch.setattr(settings, "ml_backend_storage_host", "")
    monkeypatch.setattr(settings, "minio_public_url", "https://browser.test")
    assert download(service, "ml") == download(service, "internal")


def test_upload_preserves_signed_content_type_and_browser_origin(service, monkeypatch):
    monkeypatch.setattr(settings, "minio_public_url", "https://uploads.test")
    url = urlsplit(service.generate_upload_url("pending/file.png", "image/png", 73))
    assert url.netloc == "uploads.test"
    assert parse_qs(url.query)["X-Amz-Expires"] == ["73"]
    assert parse_qs(url.query)["X-Amz-SignedHeaders"] == ["content-type;host"]


def test_parallel_and_failed_calls_do_not_change_another_audience(service):
    expected = {
        audience: download(service, audience)
        for audience in ("browser", "ml", "internal")
    }
    audiences = list(expected) * 20
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda audience: download(service, audience), audiences)
        )
    assert results == [expected[audience] for audience in audiences]
    with pytest.raises(Exception):
        signing.generate_url(
            service.client, "not_an_operation", params={}, expires_in=30, audience="ml"
        )
    assert download(service, "internal") == expected["internal"]


@pytest.mark.parametrize("audience", ["ml", "browser"])
def test_override_does_not_silently_drop_a_virtual_host_bucket(service, audience):
    client = boto3.client(
        "s3",
        endpoint_url="http://internal.test:9000",
        aws_access_key_id="test-access",
        aws_secret_access_key="test-secret",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    try:
        signing.install(client)
        with pytest.raises(ValueError, match="path-style"):
            signing.generate_url(
                client,
                "get_object",
                params={"Bucket": "saved-bucket", "Key": "image.png"},
                expires_in=30,
                audience=audience,
            )
    finally:
        client.close()
