from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import stat
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from aap_assets_source.connectors import (
    ConnectorAllowlistInvalid,
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    ConnectorHostDenied,
    SourceChangedError,
    SourcePathError,
    assert_host_allowed,
    decrypt_secret,
    encrypt_secret,
    extract_host,
    normalize_allowlist,
)
from aap_assets_source.connectors.s3 import (
    S3CompatibleSource,
    validate_s3_endpoint,
    validate_s3_source_path,
)
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
    with pytest.raises(SourceChangedError, match="ETag"):
        source.open("file.jpg")


def test_s3_adapter_keeps_legacy_unconditional_open_for_api_consumers() -> None:
    client = _FakeS3Client({"file.jpg": b"content"})
    source = S3CompatibleSource(
        {"endpoint": "http://example.test", "bucket": "bucket"},
        {"access_key": "access", "secret_key": "secret"},
        client=client,
    )

    assert source.open("file.jpg").read() == b"content"
    assert "IfMatch" not in client.get_calls[0]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@example.test",
        "ftp://example.test",
        "http:///missing-host",
    ],
)
def test_s3_endpoint_rejects_userinfo_and_non_http_schemes(endpoint: str) -> None:
    with pytest.raises(ValueError, match="HTTP"):
        validate_s3_endpoint({"endpoint": endpoint})


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


def test_allowlist_normalization_matches_the_public_connector_contract() -> None:
    assert normalize_allowlist(
        [
            " 10.0.3.5/24 ",
            "2001:0db8::1",
            "Example.COM.",
            ".AliYunCS.com.",
            "example.com",
            "",
        ]
    ) == ["10.0.3.0/24", "2001:db8::1", "example.com", ".aliyuncs.com"]
    with pytest.raises(ConnectorAllowlistInvalid):
        normalize_allowlist(["*.example.test"])
    with pytest.raises(ConnectorAllowlistInvalid):
        normalize_allowlist([f"host-{index}.example.test" for index in range(257)])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://storage.example.test:443/bucket", "storage.example.test"),
        ("http://1.2.3.4:9000", "1.2.3.4"),
        ("1.2.3.4:9000", "1.2.3.4"),
        ("sftp-host", "sftp-host"),
        ("[::1]:22", "::1"),
    ],
)
def test_extract_host_supports_url_bare_and_ipv6_forms(raw, expected) -> None:
    assert extract_host(raw) == expected


def test_host_guard_allows_explicit_public_and_private_cidrs() -> None:
    assert_host_allowed("8.8.8.8", ["8.8.8.0/24"])
    assert_host_allowed("10.0.3.5", ["10.0.3.0/24"])
    with pytest.raises(ConnectorHostDenied):
        assert_host_allowed("8.8.8.8", ["9.9.9.0/24"])
    with pytest.raises(ConnectorHostDenied):
        assert_host_allowed("10.0.3.5", ["10.9.9.0/24"])
    with pytest.raises(ConnectorHostDenied):
        assert_host_allowed("169.254.169.254", ["169.254.0.0/16"])


def test_host_guard_rejects_domain_dns_rebinding(monkeypatch) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.1.2.3", 0),
            )
        ],
    )
    with pytest.raises(ConnectorHostDenied):
        assert_host_allowed("evil.example.test", ["evil.example.test"])


def test_connector_secret_crypto_uses_explicit_key_and_fails_closed() -> None:
    key = Fernet.generate_key()
    secret = {
        "access_key": "AKIA_DISTINCTIVE_PLAINTEXT_MARKER_0",
        "secret_key": "SK_DISTINCTIVE_PLAINTEXT_MARKER_1",
    }
    token = encrypt_secret(secret, key)

    assert secret["access_key"].encode() not in token
    assert secret["secret_key"].encode() not in token
    assert decrypt_secret(token, key) == secret
    with pytest.raises(ConnectorCryptoError):
        decrypt_secret(token, Fernet.generate_key())
    with pytest.raises(ConnectorCryptoNotConfigured):
        encrypt_secret(secret, "")


@pytest.mark.parametrize("accepted_name", ["RSAKey", "Ed25519Key", "ECDSAKey"])
def test_sftp_private_key_parser_supports_declared_key_types(
    monkeypatch, accepted_name
) -> None:
    import paramiko

    for name in ("RSAKey", "Ed25519Key", "ECDSAKey"):
        key_class = getattr(paramiko, name)

        def loader(_stream, password=None, *, current=name):
            if current == accepted_name:
                return current, password
            raise ValueError("not this key type")

        monkeypatch.setattr(key_class, "from_private_key", staticmethod(loader))

    from aap_assets_source.connectors.sftp import load_sftp_private_key

    assert load_sftp_private_key(
        {"private_key": "key-data", "passphrase": "private-passphrase"}
    ) == (accepted_name, "private-passphrase")


def test_sftp_connection_failure_closes_partial_client(monkeypatch) -> None:
    import paramiko

    class _FailingClient:
        closed = False

        def load_system_host_keys(self):
            return None

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            raise OSError("connection failed")

        def close(self):
            self.closed = True

    client = _FailingClient()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    from aap_assets_source.connectors.sftp import SftpSource

    with pytest.raises(OSError, match="connection failed"):
        SftpSource(
            {"host": "source.example.test", "username": "user"},
            {"password": "secret"},
        )
    assert client.closed


class _FakeSftpAttr:
    def __init__(self, filename: str, mode: int, size: int = 0) -> None:
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = 1_777_000_000


class _FakeSftpFile(io.BytesIO):
    def __init__(self, data: bytes, attrs: _FakeSftpAttr) -> None:
        super().__init__(data)
        self._attrs = attrs

    def stat(self):
        return self._attrs


class _FakeSftpClient:
    def __init__(self) -> None:
        self.files = {
            "/data/batch/a.jpg": (b"image", _FakeSftpAttr("a.jpg", stat.S_IFREG, 5)),
            "/data/batch/b.txt": (b"text", _FakeSftpAttr("b.txt", stat.S_IFREG, 4)),
        }
        self.directories = {
            "/data": _FakeSftpAttr("data", stat.S_IFDIR),
            "/data/batch": _FakeSftpAttr("batch", stat.S_IFDIR),
        }

    def normalize(self, path: str) -> str:
        return path

    def lstat(self, path: str):
        if path in self.directories:
            return self.directories[path]
        return self.files[path][1]

    def stat(self, path: str):
        return self.files[path][1]

    def listdir_iter(self, path: str):
        if path == "/data":
            return iter([_FakeSftpAttr("batch", stat.S_IFDIR)])
        if path == "/data/batch":
            return iter([attrs for _data, attrs in self.files.values()])
        return iter([])

    def open(self, path: str, _mode: str):
        data, attrs = self.files[path]
        return _FakeSftpFile(data, attrs)

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda _timeout: None)

    def close(self):
        return None


def test_sftp_adapter_lists_filters_and_opens_with_listing_identity(
    monkeypatch,
) -> None:
    fake_sftp = _FakeSftpClient()

    class _FakeSshClient:
        def load_system_host_keys(self):
            return None

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            return None

        def open_sftp(self):
            return fake_sftp

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(SSHClient=_FakeSshClient, RejectPolicy=lambda: object()),
    )
    from aap_assets_source.connectors.sftp import SftpSource

    source = SftpSource(
        {"host": "sftp.example.test", "username": "user", "base_path": "/data"},
        {"password": "secret"},
    )

    objects = list(source.list("batch", recursive=True, include_globs=["*.jpg"]))
    assert [item.relpath for item in objects] == ["batch/a.jpg"]
    assert objects[0].etag is not None
    assert source.open(objects[0].relpath, etag=objects[0].etag).read() == b"image"
