from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_identity_runtime.notification_config import NotificationPublisherSettings


def test_publisher_settings_need_only_owner_dsn_and_redis_url():
    settings = NotificationPublisherSettings(
        _env_file=None,
        database_url="postgresql://publisher:private@db/notifications",
        redis_url="redis://:private@cache/0",
        signing_secret="ignored-auth-setting",
        identity_database_url="ignored-identity-dsn",
    )

    assert settings.claim_limit == 16
    assert settings.lease_seconds == 60
    assert settings.concurrency == 4
    assert "private" not in repr(settings)
    assert "identity_database_url" not in NotificationPublisherSettings.model_fields


def test_publisher_settings_read_owner_credentials_from_files(tmp_path):
    database_url = tmp_path / "database-url"
    redis_url = tmp_path / "redis-url"
    database_url.write_text("postgresql://publisher/db\n")
    redis_url.write_text("redis://cache/0\n")

    settings = NotificationPublisherSettings(
        _env_file=None,
        database_url_file=str(database_url),
        redis_url_file=str(redis_url),
    )

    assert settings.database_url.get_secret_value() == "postgresql://publisher/db"
    assert settings.redis_url.get_secret_value() == "redis://cache/0"


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"database_url": "postgresql://publisher/db"},
        {
            "database_url": "postgresql://publisher/db",
            "database_url_file": "/ignored/path",
            "redis_url": "redis://cache/0",
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "claim_limit": 101,
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "concurrency": 5,
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "redis_timeout_seconds": 5.1,
        },
    ],
)
def test_publisher_settings_reject_missing_conflicting_or_unsafe_sources(values):
    with pytest.raises((ValidationError, ValueError)):
        NotificationPublisherSettings(_env_file=None, **values)


def test_publisher_settings_rejects_oversized_or_empty_secret_files(tmp_path):
    database_url = tmp_path / "database-url"
    redis_url = tmp_path / "redis-url"
    database_url.write_text("d" * 4097)
    redis_url.write_text("   \n")

    with pytest.raises(ValidationError):
        NotificationPublisherSettings(
            _env_file=None,
            database_url_file=str(database_url),
            redis_url="redis://cache/0",
        )
    with pytest.raises(ValidationError):
        NotificationPublisherSettings(
            _env_file=None,
            database_url="postgresql://publisher/db",
            redis_url_file=str(redis_url),
        )
