from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_notifications.delivery_config import NotificationPublisherSettings


def test_publisher_settings_need_only_owner_dsn_and_redis_url(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_PUBLISH_API_TOKEN", raising=False)
    monkeypatch.delenv("NOTIFICATION_PUBLISH_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_PUBLISH_HOST", raising=False)
    monkeypatch.delenv("NOTIFICATION_PUBLISH_PORT", raising=False)
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
    assert settings.api_token is None
    assert settings.host == "127.0.0.1"
    assert settings.port == 8181
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


def test_optional_publication_bearer_can_come_from_either_source(tmp_path):
    token_file = tmp_path / "api-token"
    token_file.write_text("private-service-token\n")
    file_settings = NotificationPublisherSettings(
        _env_file=None,
        database_url="postgresql://publisher/db",
        redis_url="redis://cache/0",
        api_token_file=str(token_file),
    )
    env_settings = NotificationPublisherSettings(
        _env_file=None,
        database_url="postgresql://publisher/db",
        redis_url="redis://cache/0",
        api_token="another-service-token",
        host="0.0.0.0",
        port=8182,
    )

    assert file_settings.api_token.get_secret_value() == "private-service-token"
    assert env_settings.api_token.get_secret_value() == "another-service-token"
    assert "another-service-token" not in repr(env_settings)
    assert env_settings.host == "0.0.0.0" and env_settings.port == 8182


def test_optional_publication_settings_use_the_existing_publisher_prefix(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_PUBLISH_API_TOKEN", "env-service-token")
    monkeypatch.delenv("NOTIFICATION_PUBLISH_API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("NOTIFICATION_PUBLISH_HOST", "publisher.internal")
    monkeypatch.setenv("NOTIFICATION_PUBLISH_PORT", "8182")

    settings = NotificationPublisherSettings(
        _env_file=None,
        database_url="postgresql://publisher/db",
        redis_url="redis://cache/0",
    )

    assert settings.api_token.get_secret_value() == "env-service-token"
    assert settings.host == "publisher.internal"
    assert settings.port == 8182


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
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "api_token": "service-token",
            "api_token_file": "/ignored/token-file",
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "api_token": "unsafe\nheader",
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "api_token": "t" * 4097,
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "host": "publisher.test/path",
        },
        {
            "database_url": "postgresql://publisher/db",
            "redis_url": "redis://cache/0",
            "port": 65536,
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
