"""API adapters for connector encryption configuration and environment parsing."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings, settings
from app.core import crypto


# ── Fernet 加解密 ─────────────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    # 用足够长的独特明文标记：Fernet 密文是带随机 IV 的 base64，短串（如 "AK"）会以
    # ~2% 概率偶然出现在密文里造成 flaky；长标记的碰撞概率可忽略，仍能验证「密文不含明文」。
    secret = {
        "access_key": "AKIA_DISTINCTIVE_PLAINTEXT_MARKER_0",
        "secret_key": "SK_DISTINCTIVE_PLAINTEXT_MARKER_1",
    }
    token = crypto.encrypt_secret(secret)
    assert isinstance(token, bytes)
    assert secret["access_key"].encode() not in token  # 密文里看不到明文
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
