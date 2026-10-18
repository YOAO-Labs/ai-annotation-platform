from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from aap_assets_source_runtime.config import ConfigurationError, load_config


def _environment() -> dict[str, str]:
    return {
        "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
        "ASSETS_SOURCE_TOKEN": "s" * 40,
        "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
        "ASSETS_SOURCE_S3_REGION": "us-east-1",
        "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
        "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
        "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
        "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
        "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
    }


def test_config_uses_bounded_defaults_and_media_cache_cleanup_target() -> None:
    config = load_config(_environment())

    assert config.driver_database_url == config.database_url
    assert config.host == "0.0.0.0"
    assert config.port == 8767
    assert config.max_request_bytes == 1_048_576
    assert config.max_response_bytes == 16_777_216
    assert (config.pool_min_size, config.pool_max_size) == (1, 8)
    assert config.cleanup_bucket == "media-cache"
    assert config.connector_encryption_key == ""
    assert config.connector_host_allowlist == ()
    assert config.sftp_known_hosts_file is None
    assert config.upload_url_expires_seconds == 900
    assert config.image_header_bytes == 256 * 1024


def test_generic_api_and_aws_environment_values_are_never_fallbacks() -> None:
    with pytest.raises(ConfigurationError, match="ASSETS_SOURCE_DATABASE_URL"):
        load_config(
            {
                "DATABASE_URL": "postgresql://api:secret@db.test/aap",
                "AWS_ACCESS_KEY_ID": "broad-access",
                "AWS_SECRET_ACCESS_KEY": "broad-secret",
            }
        )

    environment = _environment()
    del environment["ASSETS_SOURCE_S3_ACCESS_KEY"]
    with pytest.raises(ConfigurationError, match="ASSETS_SOURCE_S3_ACCESS_KEY"):
        load_config({**environment, "AWS_ACCESS_KEY_ID": "broad-access"})


def test_secrets_accept_file_input_and_reject_direct_and_file_together(
    tmp_path,
) -> None:
    token_file = tmp_path / "source-token"
    token_file.write_text("f" * 40, encoding="utf-8")
    environment = _environment()
    del environment["ASSETS_SOURCE_TOKEN"]
    config = load_config({**environment, "ASSETS_SOURCE_TOKEN_FILE": str(token_file)})
    assert config.service_token == "f" * 40

    with pytest.raises(ConfigurationError, match="Set only one"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_TOKEN_FILE": str(token_file),
            }
        )


def test_connector_key_is_optional_and_accepts_explicit_direct_or_file_input(
    tmp_path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    direct_config = load_config(
        {**_environment(), "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": key}
    )
    assert direct_config.connector_encryption_key == key
    assert key not in repr(direct_config)

    key_file = tmp_path / "connector-key"
    key_file.write_text(f"{key}\n", encoding="utf-8")
    assert (
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY_FILE": str(key_file),
            }
        ).connector_encryption_key
        == key
    )

    with pytest.raises(ConfigurationError, match="Set only one"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": key,
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY_FILE": str(key_file),
            }
        )

    with pytest.raises(ConfigurationError, match="Cannot read configured secret file"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY_FILE": str(
                    tmp_path / "missing-connector-key"
                ),
            }
        )


def test_connector_settings_never_fall_back_to_api_or_importer_environment() -> None:
    config = load_config(
        {
            **_environment(),
            "CONNECTOR_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "CONNECTOR_HOST_ALLOWLIST": '["api-only.example.test"]',
        }
    )
    assert config.connector_encryption_key == ""
    assert config.connector_host_allowlist == ()


def test_connector_config_is_strict_and_does_not_echo_secrets(tmp_path) -> None:
    invalid_key = "not-a-fernet-key-sensitive"
    with pytest.raises(ConfigurationError, match="is invalid") as key_error:
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": invalid_key,
            }
        )
    assert invalid_key not in str(key_error.value)

    with pytest.raises(ConfigurationError, match="JSON string array"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": '{"host":"x"}',
            }
        )
    with pytest.raises(ConfigurationError, match="invalid entry"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": '["https://host.test"]',
            }
        )

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 key\n", encoding="utf-8")
    config = load_config(
        {
            **_environment(),
            "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": '[".example.test", "192.0.2.0/24"]',
            "ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE": str(known_hosts),
        }
    )
    assert config.connector_host_allowlist == (".example.test", "192.0.2.0/24")
    assert config.sftp_known_hosts_file == str(known_hosts)

    with pytest.raises(ConfigurationError, match="readable file"):
        load_config(
            {
                **_environment(),
                "ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE": str(tmp_path / "missing"),
            }
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT",
            "https://storage.example.test/minio",
            "HTTP",
        ),
        (
            "ASSETS_SOURCE_IMPORT_BUCKET",
            "192.0.2.1",
            "is invalid",
        ),
        (
            "ASSETS_SOURCE_TOKEN",
            "é" * 40,
            "must be ASCII",
        ),
        (
            "ASSETS_SOURCE_POOL_MIN_SIZE",
            "9",
            "must not exceed POOL_MAX_SIZE",
        ),
        (
            "ASSETS_SOURCE_MAX_REQUEST_BYTES",
            "1048577",
            "must be between 1 and 1048576",
        ),
        (
            "ASSETS_SOURCE_MAX_RESPONSE_BYTES",
            "16777217",
            "must be between 1 and 16777216",
        ),
    ],
)
def test_config_rejects_unsafe_or_inconsistent_values(
    key: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config({**_environment(), key: value})
