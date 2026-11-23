import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("WORKSPACE_MEMBERSHIP_URL", "env"),
        ("WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE", "env"),
        ("WORKSPACE_CONFIGURATION_DATABASE_URL", "dotenv"),
        ("WORKSPACE_TEMPLATE_QUERY_URL", "dotenv"),
        ("WORKSPACE_PROJECT_QUERY_DATABASE_URL", "env"),
        ("WORKSPACE_DIRECTORY_QUERY_DATABASE_URL_FILE", "dotenv"),
        ("LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE", "env"),
        ("LABELING_CLASS_QUERY_URL", "dotenv"),
        ("IDENTITY_MAIL_V2_DATABASE_URL", "env"),
        ("ASSETS_CATALOG_TOKEN_FILE", "dotenv"),
        ("JOBS_QUEUE_BROKER_URL", "dotenv"),
        ("JOBS_DISPATCH_RELAY_ENABLED", "env"),
        ("NOTIFICATION_SOCKET_URL", "env"),
    ],
)
def test_retired_host_refuses_startup_without_disclosing_value(
    monkeypatch, tmp_path, key, source
):
    env_file = tmp_path / ".env"
    value = (
        "true" if key == "JOBS_DISPATCH_RELAY_ENABLED" else "private-deployment-value"
    )
    if source == "env":
        monkeypatch.setenv(key, value)
    else:
        monkeypatch.delenv(key, raising=False)
        env_file.write_text(f"{key}={value}\n")

    with pytest.raises(RuntimeError, match=key) as error:
        Settings(_env_file=env_file)
    assert value not in str(error.value)


def test_unselected_workspace_does_not_disable_independent_probe(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WORKSPACE_CONFIGURATION_URL=http://old-workspace.invalid\n")
    monkeypatch.setenv("WORKSPACE_CONFIGURATION_URL", "")
    monkeypatch.setenv("INFERENCE_BACKEND_PROBE_URL", "http://inference.invalid")

    assert Settings(_env_file=env_file).inference_backend_probe_url == (
        "http://inference.invalid"
    )


def test_issuer_key_is_independent_from_media_key(monkeypatch, tmp_path):
    key = tmp_path / "issuer-key"
    key.write_text("issuer-key-value\n")
    monkeypatch.setenv("IDENTITY_EMBEDDED_SIGNING_SECRET_FILE", str(key))
    settings = Settings(_env_file=None, secret_key="unrelated-media-key")
    assert settings.identity_embedded_signing_secret == "issuer-key-value"
    assert settings.secret_key == "unrelated-media-key"
    assert "issuer-key-value" not in repr(settings)
    assert "identity_embedded_signing_secret" not in settings.model_dump()

    with pytest.raises(RuntimeError, match="only one"):
        Settings(_env_file=None, identity_embedded_signing_secret="second-key")
    with pytest.raises(RuntimeError, match="remote Identity"):
        Settings(_env_file=None, identity_url="http://issuer", identity_token="bearer")
    key.unlink()
    with pytest.raises(RuntimeError, match="Cannot read"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "key",
    [
        "notification_publish_database_url",
        "notification_publish_database_url_file",
        "notification_publish_redis_url",
        "notification_publish_redis_url_file",
    ],
)
def test_remote_publisher_cannot_also_hold_embedded_publish_credentials(key):
    remote = {"notification_publication_url": "http://publisher"}
    with pytest.raises(RuntimeError, match="remote Notification") as error:
        Settings(_env_file=None, **remote, **{key: "private-deployment-value"})
    assert "private-deployment-value" not in str(error.value)
    settings = Settings(
        _env_file=None, **remote, notification_subscribe_redis_url_file="/subscribe"
    )
    assert settings.notification_publication_url == "http://publisher"


def test_disabled_retired_dispatch_flag_remains_unselected(monkeypatch):
    monkeypatch.setenv("JOBS_DISPATCH_RELAY_ENABLED", "false")
    assert not Settings(_env_file=None).notification_publication_url
