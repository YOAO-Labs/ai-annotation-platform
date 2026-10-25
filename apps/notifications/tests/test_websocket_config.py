from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from aap_notifications.websocket_config import NotificationSocketSettings


_ENV_KEYS = [
    "DATABASE_URL",
    "DATABASE_URL_FILE",
    "REDIS_URL",
    "REDIS_URL_FILE",
    "IDENTITY_URL",
    "IDENTITY_TOKEN",
    "IDENTITY_TOKEN_FILE",
    "HOST",
    "PORT",
]


def _clear_socket_environment(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(f"NOTIFICATION_SOCKET_{key}", raising=False)


def _settings_kwargs():
    return {
        "database_url": SecretStr("postgresql://socket:secret@127.0.0.1/socket"),
        "redis_url": SecretStr("redis://socket:secret@127.0.0.1/0"),
        "identity_url": "http://127.0.0.1:8170",
        "identity_token": SecretStr("identity-service-secret"),
    }


def test_socket_settings_keep_loopback_default_and_port(monkeypatch):
    _clear_socket_environment(monkeypatch)

    settings = NotificationSocketSettings(**_settings_kwargs())

    assert settings.host == "127.0.0.1"
    assert settings.port == 8180


@pytest.mark.parametrize("name", ["database_url", "redis_url", "identity_token"])
def test_socket_settings_require_exactly_one_secret_source(monkeypatch, name):
    _clear_socket_environment(monkeypatch)
    kwargs = _settings_kwargs()
    kwargs[name + "_file"] = "/missing/socket-secret"

    with pytest.raises(ValidationError) as raised:
        NotificationSocketSettings(**kwargs)

    assert "Set exactly one notification socket" in str(raised.value)
    assert "identity-service-secret" not in str(raised.value)
    assert "postgresql://socket:secret" not in str(raised.value)
    assert "redis://socket:secret" not in str(raised.value)


def test_socket_settings_read_one_bounded_secret_file(monkeypatch, tmp_path: Path):
    _clear_socket_environment(monkeypatch)
    token_path = tmp_path / "identity-token"
    token_path.write_text("socket-file-token\n", encoding="utf-8")
    kwargs = _settings_kwargs()
    kwargs.pop("identity_token")
    kwargs["identity_token_file"] = str(token_path)

    settings = NotificationSocketSettings(**kwargs)

    assert settings.identity_token.get_secret_value() == "socket-file-token"
    assert settings.identity_token_file == str(token_path)


def test_socket_settings_redact_invalid_secret_file_content(
    monkeypatch, tmp_path: Path
):
    _clear_socket_environment(monkeypatch)
    token_path = tmp_path / "oversized-token"
    token_path.write_text("hidden-value-" + "x" * 4096, encoding="utf-8")
    kwargs = _settings_kwargs()
    kwargs.pop("identity_token")
    kwargs["identity_token_file"] = str(token_path)

    with pytest.raises(ValidationError) as raised:
        NotificationSocketSettings(**kwargs)

    assert "Invalid notification socket secret file" in str(raised.value)
    assert "hidden-value" not in str(raised.value)


@pytest.mark.parametrize(
    "identity_url",
    [
        "",
        "ftp://identity.internal",
        "http://user:pass@identity.internal",
        "http://identity.internal:invalid",
        "http://identity.internal/prefix",
        "http://identity.internal?token=secret",
        "http://identity.internal#fragment",
    ],
)
def test_socket_settings_reject_invalid_identity_origins(monkeypatch, identity_url):
    _clear_socket_environment(monkeypatch)
    kwargs = _settings_kwargs()
    kwargs["identity_url"] = identity_url

    with pytest.raises(ValidationError):
        NotificationSocketSettings(**kwargs)
