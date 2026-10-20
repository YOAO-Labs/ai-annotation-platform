from __future__ import annotations

import pytest
from aap_identity_runtime.mail_config import MailWorkerSettings
from pydantic import ValidationError


def test_worker_requires_only_its_two_dsns_and_dedicated_mail_key():
    settings = MailWorkerSettings(
        _env_file=None,
        database_url="postgresql://mail-client:secret@db/identity",
        settings_database_url="postgresql://settings-client:secret@db/settings",
        encryption_key="dedicated-fernet-key",
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
        ("encryption-key", "fernet-key\n"),
        ("smtp-password", "smtp-password\n"),
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

    assert settings.smtp_password.get_secret_value() == "smtp-password"
    assert settings.database_url.get_secret_value() == "postgresql://mail-role/db"


def test_worker_rejects_missing_or_conflicting_secret_sources():
    with pytest.raises(ValidationError):
        MailWorkerSettings(_env_file=None)

    with pytest.raises(ValidationError):
        MailWorkerSettings(
            _env_file=None,
            database_url="postgresql://mail/db",
            settings_database_url="postgresql://settings/db",
            encryption_key="key",
            smtp_password="password",
            smtp_password_file="/ignored/file",
        )
