from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io

import pytest

from aap_assets_source.connectors import (
    ConnectorHostDenied,
    SourcePathError,
    assert_host_allowed,
    normalize_allowlist,
)
from aap_assets_source.connectors.s3 import S3CompatibleSource, validate_s3_source_path
from aap_assets_source.connectors.sftp import validate_sftp_source_path


class _FakeS3Client:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.get_calls: list[dict] = []

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix") or ""
        contents = []
        for key, data in sorted(self.objects.items()):
            if key.startswith(prefix):
                contents.append(
                    {
                        "Key": key,
                        "Size": len(data),
                        "LastModified": datetime(2026, 5, 26, tzinfo=timezone.utc),
                        "ETag": f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"',
                    }
                )
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def test_s3_adapter_lists_filters_and_can_open_with_etag() -> None:
    client = _FakeS3Client(
        {
            "root/batch/a.jpg": b"a",
            "root/batch/b.txt": b"b",
            "root/batch/nested/c.png": b"c",
            "root-other/skip.jpg": b"x",
        }
    )
    source = S3CompatibleSource(
        {"endpoint": "http://example.test", "bucket": "bucket", "base_prefix": "root"},
        {"access_key": "access", "secret_key": "secret"},
        client=client,
        require_conditional_reads=True,
    )

    recursive = list(source.list("batch", recursive=True, include_globs=["*.png"]))
    assert [obj.relpath for obj in recursive] == ["batch/nested/c.png"]
    assert recursive[0].etag == hashlib.md5(b"c", usedforsecurity=False).hexdigest()
    assert source.open(recursive[0].relpath, etag=recursive[0].etag).read() == b"c"
    assert client.get_calls[0]["IfMatch"] == f'"{recursive[0].etag}"'

    flat = list(source.list("batch", recursive=False))
    assert [obj.relpath for obj in flat] == ["batch/a.jpg", "batch/b.txt"]


def test_s3_rejects_ambient_credentials_and_missing_conditional_etag() -> None:
    config = {"endpoint": "http://example.test", "bucket": "bucket"}
    with pytest.raises(ValueError, match="access_key and secret_key"):
        S3CompatibleSource(config, {"access_key": "", "secret_key": ""})

    source = S3CompatibleSource(
        config,
        {"access_key": "access", "secret_key": "secret"},
        client=_FakeS3Client({}),
        require_conditional_reads=True,
    )
    with pytest.raises(Exception, match="ETag"):
        source.open("file.jpg")


def test_connector_path_guards_reject_traversal_and_escape() -> None:
    with pytest.raises(SourcePathError):
        validate_s3_source_path({"base_prefix": "root"}, "../secret")
    with pytest.raises(SourcePathError):
        validate_sftp_source_path({"base_path": "/data/imports"}, "../secret")
    with pytest.raises(SourcePathError):
        validate_sftp_source_path({"base_path": "/data/imports"}, "/data/other")
    with pytest.raises(SourcePathError):
        validate_sftp_source_path({}, "/etc/passwd")
    validate_sftp_source_path({"base_path": "/data"}, "/data/batch")


def test_host_guard_normalizes_and_rejects_hard_blocked_addresses(monkeypatch) -> None:
    assert normalize_allowlist(["10.0.0.1", "10.0.0.1", ".Example.Test"]) == [
        "10.0.0.1",
        ".example.test",
    ]
    with pytest.raises(ConnectorHostDenied):
        assert_host_allowed("127.0.0.1", ["127.0.0.1/32"])

    monkeypatch.setattr(
        "aap_assets_source.connectors.host_guard.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 0))],
    )
    assert_host_allowed("media.example.test", [".example.test"])
