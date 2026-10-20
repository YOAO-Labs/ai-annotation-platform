from __future__ import annotations

import base64
import hashlib
import sys
import types

import pytest
from aap_identity_runtime.mail_config import MailWorkerSettings
from pydantic import ValidationError

FERNET_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture(autouse=True)
def source_injected_mail_key_validator(monkeypatch):
    crypto = types.ModuleType("aap_identity.recovery_crypto")

    def mail_encryption_id(key: str) -> str:
        decoded = base64.urlsafe_b64decode(key.encode("ascii"))
        if (
            len(decoded) != 32
            or base64.urlsafe_b64encode(decoded).decode("ascii") != key
        ):
            raise ValueError("Invalid mail encryption key")
        return hashlib.sha256(decoded).hexdigest()

    crypto.mail_encryption_id = mail_encryption_id
    monkeypatch.setitem(sys.modules, "aap_identity.recovery_crypto", crypto)


def test_worker_requires_only_its_two_dsns_and_dedicated_mail_key():
    settings = MailWorkerSettings(
        _env_file=None,
        database_url="postgresql://mail-client:secret@db/identity",
        settings_database_url="postgresql://settings-client:secret@db/settings",
        encryption_key=FERNET_KEY,
        signing_secret="ignored-auth-secret",
        token="ignored-api-bearer",
    )

    assert settings.port == 8171
    assert settings.smtp_password.get_secret_value() == ""
    assert "secret" not in repr(settings)


def test_worker_reads_required_secrets_from_mounted_files(tmp_path):
    files = {}
    for name, value in (
        ("mail-database", "postgresql://mail-role/db\n"),
        ("settings-database", "postgresql://settings-role/db\n"),
        ("encryption-key", FERNET_KEY + "\n"),
        ("smtp-password", "p" * 5000 + "\n"),
    ):
        path = tmp_path / name
        path.write_text(value)
        files[name] = str(path)

    settings = MailWorkerSettings(
        _env_file=None,
        database_url_file=files["mail-database"],
        settings_database_url_file=files["settings-database"],
        encryption_key_file=files["encryption-key"],
        smtp_password_file=files["smtp-password"],
    )

    assert len(settings.smtp_password.get_secret_value()) == 5000
    assert settings.database_url.get_secret_value() == "postgresql://mail-role/db"


def test_worker_rejects_missing_or_conflicting_secret_sources():
    with pytest.raises(ValidationError):
        MailWorkerSettings(_env_file=None)

    with pytest.raises(ValidationError):
        MailWorkerSettings(
            _env_file=None,
            database_url="postgresql://mail/db",
            settings_database_url="postgresql://settings/db",
            encryption_key=FERNET_KEY,
            smtp_password="password",
            smtp_password_file="/ignored/file",
        )


def test_worker_rejects_malformed_fernet_key_before_queue_access():
    with pytest.raises(ValidationError):
        MailWorkerSettings(
            _env_file=None,
            database_url="postgresql://mail/db",
            settings_database_url="postgresql://settings/db",
            encryption_key="bad-key",
        )


def test_worker_keeps_long_smtp_policy_fallback_strings():
    settings = MailWorkerSettings(
        _env_file=None,
        database_url="postgresql://mail/db",
        settings_database_url="postgresql://settings/db",
        encryption_key=FERNET_KEY,
        smtp_host="h" * 300,
        smtp_user="u" * 300,
        smtp_password="p" * 5000,
        smtp_from="noreply@" + "x" * 400,
    )
    assert len(settings.smtp_host) == 300
    assert len(settings.smtp_user) == 300
    assert len(settings.smtp_password.get_secret_value()) == 5000
    assert len(settings.smtp_from) == 408
