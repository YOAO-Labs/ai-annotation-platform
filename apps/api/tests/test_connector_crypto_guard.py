"""连接器凭据加密 + 主机白名单/SSRF 校验单测（无 DB）。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings, settings
from app.core import crypto
from app.services import connector_guard as cg
from yoao_assets_source.connectors.sftp import load_sftp_private_key


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    secret = {
        "access_key": "AKIA_DISTINCTIVE_PLAINTEXT_MARKER_0",
        "secret_key": "SK_DISTINCTIVE_PLAINTEXT_MARKER_1",
    }
    token = crypto.encrypt_secret(secret)
    assert isinstance(token, bytes)
    assert secret["access_key"].encode() not in token
    assert secret["secret_key"].encode() not in token
    assert crypto.decrypt_secret(token) == secret


def test_decrypt_wrong_key_raises(monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    token = crypto.encrypt_secret({"password": "p"})
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    with pytest.raises(crypto.ConnectorCryptoError):
        crypto.decrypt_secret(token)


def test_not_configured_raises(monkeypatch):
    monkeypatch.setattr(settings, "connector_encryption_key", "")
    with pytest.raises(crypto.ConnectorCryptoNotConfigured):
        crypto.encrypt_secret({"x": "y"})


def test_connector_host_allowlist_settings_accepts_csv():
    cfg = Settings(
        connector_host_allowlist="172.17.0.1/32, 172.26.1.17/32, .example.com"
    )
    assert cfg.connector_host_allowlist == [
        "172.17.0.1/32",
        "172.26.1.17/32",
        ".example.com",
    ]


def test_connector_host_allowlist_settings_accepts_json_list():
    cfg = Settings(connector_host_allowlist='["172.17.0.1/32", ".example.com"]')
    assert cfg.connector_host_allowlist == ["172.17.0.1/32", ".example.com"]


def test_normalize_allowlist_canonicalizes_and_deduplicates():
    assert cg.normalize_allowlist(
        [
            " 10.0.3.5/24 ",
            "2001:0db8::1",
            "Example.COM.",
            ".AliYunCS.com.",
            "example.com",
            "",
        ]
    ) == ["10.0.3.0/24", "2001:db8::1", "example.com", ".aliyuncs.com"]


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com",
        "example.com:443",
        "*.example.com",
        "example.com/path",
        ".",
        "bad_label.example.com",
        "10.0.0.1/999",
    ],
)
def test_normalize_allowlist_rejects_invalid_entries(entry):
    with pytest.raises(cg.ConnectorAllowlistInvalid):
        cg.normalize_allowlist([entry])


def test_normalize_allowlist_rejects_more_than_256_entries():
    with pytest.raises(cg.ConnectorAllowlistInvalid, match="too many entries"):
        cg.normalize_allowlist([f"host-{index}.example.com" for index in range(257)])


@pytest.mark.parametrize("accepted_name", ["RSAKey", "Ed25519Key", "ECDSAKey"])
def test_sftp_private_key_parser_supports_all_declared_types(
    monkeypatch, accepted_name
):
    import paramiko

    for name in ("RSAKey", "Ed25519Key", "ECDSAKey"):
        key_cls = getattr(paramiko, name)

        def loader(_stream, password=None, *, current=name):
            if current == accepted_name:
                return (current, password)
            raise ValueError("not this key type")

        monkeypatch.setattr(key_cls, "from_private_key", staticmethod(loader))

    assert load_sftp_private_key(
        {"private_key": "key-data", "passphrase": "secret-passphrase"}
    ) == (accepted_name, "secret-passphrase")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://oss-cn.aliyuncs.com:443/bucket", "oss-cn.aliyuncs.com"),
        ("http://1.2.3.4:9000", "1.2.3.4"),
        ("1.2.3.4:9000", "1.2.3.4"),
        ("sftp-host", "sftp-host"),
        ("[::1]:22", "::1"),
    ],
)
def test_extract_host(raw, expected):
    assert cg.extract_host(raw) == expected


def test_empty_allowlist_denies():
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("8.8.8.8", [])


def test_public_ip_in_cidr_allowed():
    cg.assert_host_allowed("8.8.8.8", ["8.8.8.0/24"])


def test_public_ip_not_in_allowlist_denied():
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("8.8.8.8", ["9.9.9.0/24"])


def test_loopback_hard_blocked_even_if_allowlisted():
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("127.0.0.1", ["127.0.0.0/8"])


def test_link_local_metadata_hard_blocked():
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("169.254.169.254", ["169.254.0.0/16"])


def test_private_ip_allowed_only_via_cidr_entry():
    cg.assert_host_allowed("10.0.3.5", ["10.0.3.0/24"])
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("10.0.3.5", ["10.9.9.0/24"])


def test_dns_rebinding_to_internal_denied(monkeypatch):
    import socket as s

    def fake_getaddrinfo(host, *a, **k):
        return [(s.AF_INET, s.SOCK_STREAM, s.IPPROTO_TCP, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(s, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(cg.ConnectorHostDenied):
        cg.assert_host_allowed("evil.example.com", ["evil.example.com"])


def test_domain_suffix_entry_matches_subdomain(monkeypatch):
    import socket as s

    def fake_getaddrinfo(host, *a, **k):
        return [(s.AF_INET, s.SOCK_STREAM, s.IPPROTO_TCP, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(s, "getaddrinfo", fake_getaddrinfo)
    cg.assert_host_allowed("oss-cn-hangzhou.aliyuncs.com", [".aliyuncs.com"])
