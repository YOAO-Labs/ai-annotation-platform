import pytest

from aap_assets_cleanup.config import ConfigurationError, load_config


def _environment():
    return {
        "ASSET_CLEANUP_DATABASE_URL": "postgresql://asset_cleanup:secret@postgres/annotation",
        "ASSET_CLEANUP_BUCKET": "media-cache",
        "ASSET_CLEANUP_S3_ENDPOINT": "http://minio:9000",
        "ASSET_CLEANUP_S3_ACCESS_KEY": "dedicated-user",
        "ASSET_CLEANUP_S3_SECRET_KEY": "dedicated-secret",
    }


def test_load_config_requires_explicit_cleanup_credentials_and_uses_bounded_defaults():
    config = load_config(_environment())

    assert config.database_url.startswith("postgresql://asset_cleanup:")
    assert config.bucket == "media-cache"
    assert config.poll_seconds == 5
    assert config.page_size == 100
    assert config.health_host == "127.0.0.1"


def test_general_database_url_is_never_a_fallback():
    with pytest.raises(ConfigurationError, match="ASSET_CLEANUP_DATABASE_URL"):
        load_config({"DATABASE_URL": "postgresql://broad:secret@postgres/annotation"})


def test_general_storage_settings_are_never_fallbacks():
    environment = {
        "ASSET_CLEANUP_DATABASE_URL": "postgresql://asset_cleanup:secret@postgres/annotation",
        "ASSET_CLEANUP_BUCKET": "media-cache",
        "MINIO_ENDPOINT": "http://minio:9000",
        "MINIO_ACCESS_KEY": "minioadmin",
        "MINIO_SECRET_KEY": "minioadmin",
        "AWS_ACCESS_KEY_ID": "ambient-key",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
    }

    with pytest.raises(ConfigurationError, match="ASSET_CLEANUP_S3_ENDPOINT"):
        load_config(environment)


def test_cleanup_secrets_accept_secret_files_and_reject_ambiguous_sources(tmp_path):
    access_path = tmp_path / "access"
    secret_path = tmp_path / "secret"
    access_path.write_text("dedicated-user\n", encoding="utf-8")
    secret_path.write_text("dedicated-secret\n", encoding="utf-8")
    environment = _environment()
    del environment["ASSET_CLEANUP_S3_ACCESS_KEY"]
    del environment["ASSET_CLEANUP_S3_SECRET_KEY"]
    environment["ASSET_CLEANUP_S3_ACCESS_KEY_FILE"] = str(access_path)
    environment["ASSET_CLEANUP_S3_SECRET_KEY_FILE"] = str(secret_path)

    assert load_config(environment).s3_access_key == "dedicated-user"

    environment["ASSET_CLEANUP_S3_ACCESS_KEY"] = "second-value"
    with pytest.raises(ConfigurationError, match="Set only one"):
        load_config(environment)


def test_health_server_cannot_bind_public_interfaces():
    environment = _environment()
    environment["ASSET_CLEANUP_HEALTH_HOST"] = "0.0.0.0"

    with pytest.raises(ConfigurationError, match="loopback"):
        load_config(environment)


def test_poll_interval_and_endpoint_are_bounded():
    environment = _environment()
    environment["ASSET_CLEANUP_POLL_SECONDS"] = "0.01"
    with pytest.raises(ConfigurationError, match="0.1 and 3600"):
        load_config(environment)

    environment = _environment()
    environment["ASSET_CLEANUP_S3_ENDPOINT"] = "https://minio.example/path"
    with pytest.raises(ConfigurationError, match="origin"):
        load_config(environment)
