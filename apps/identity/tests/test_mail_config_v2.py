from __future__ import annotations

import base64
import hashlib
import sys
import types

import pytest
from pydantic import ValidationError

from aap_identity_runtime.mail_config_v2 import MailV2WorkerSettings

FERNET_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture(autouse=True)
def source_injected_key_validator(monkeypatch):
    crypto = types.ModuleType("aap_identity.recovery_crypto")

    def mail_encryption_id(key: str) -> str:
        decoded = base64.urlsafe_b64decode(key.encode("ascii"))
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode() != key:
            raise ValueError("Invalid key")
        return hashlib.sha256(decoded).hexdigest()

    crypto.mail_encryption_id = mail_encryption_id
    monkeypatch.setitem(sys.modules, "aap_identity.recovery_crypto", crypto)


def test_v2_worker_requires_only_its_owner_settings_key_and_smtp_sources():
    settings = MailV2WorkerSettings(
        _env_file=None,
        database_url="postgresql://mail-v2-role/db",
        settings_database_url="postgresql://settings-role/db",
        encryption_key=FERNET_KEY,
        jwt_signing_secret="ignored-auth-secret",
        control_token="ignored-api-token",
    )
    assert settings.port == 8171
    assert settings.lease_seconds == 60
    assert settings.smtp_password.get_secret_value() == ""
    assert "ignored" not in repr(settings)
    assert "jwt_signing_secret" not in MailV2WorkerSettings.model_fields


def test_v2_worker_reads_dedicated_files_and_keeps_long_smtp_password(tmp_path):
    files = {}
    for name, value in (
        ("mail-v2-db", "postgresql://mail-v2/db\n"),
        ("settings-db", "postgresql://settings/db\n"),
        ("encryption-key", FERNET_KEY + "\n"),
        ("smtp-password", "p" * 5000 + "\n"),
    ):
        path = tmp_path / name
        path.write_text(value)
        files[name] = str(path)

    settings = MailV2WorkerSettings(
        _env_file=None,
        database_url_file=files["mail-v2-db"],
        settings_database_url_file=files["settings-db"],
        encryption_key_file=files["encryption-key"],
        smtp_password_file=files["smtp-password"],
    )
    assert settings.database_url.get_secret_value() == "postgresql://mail-v2/db"
    assert (
        settings.settings_database_url.get_secret_value() == "postgresql://settings/db"
    )
    assert len(settings.smtp_password.get_secret_value()) == 5000


def test_v2_worker_rejects_missing_conflicting_and_malformed_key_sources():
    with pytest.raises(ValidationError):
        MailV2WorkerSettings(_env_file=None)
    with pytest.raises(ValidationError):
        MailV2WorkerSettings(
            _env_file=None,
            database_url="postgresql://mail-v2/db",
            database_url_file="/ignored/path",
            settings_database_url="postgresql://settings/db",
            encryption_key=FERNET_KEY,
        )
    with pytest.raises(ValidationError):
        MailV2WorkerSettings(
            _env_file=None,
            database_url="postgresql://mail-v2/db",
            settings_database_url="postgresql://settings/db",
            encryption_key="bad-key",
        )
