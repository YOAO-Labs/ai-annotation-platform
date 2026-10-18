from __future__ import annotations

import pytest

from aap_assets_source_import.config import ConfigurationError, load_config


def _environment() -> dict[str, str]:
    return {
        "ASSETS_IMPORT_DATABASE_URL": "postgresql://assets_import:secret@db.test/aap",
        "ASSETS_IMPORT_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_IMPORT_S3_ACCESS_KEY": "explicit-access",
        "ASSETS_IMPORT_S3_SECRET_KEY": "explicit-secret",
        "ASSETS_IMPORT_DATASETS_BUCKET": "datasets",
        "CONNECTOR_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "CONNECTOR_HOST_ALLOWLIST": "10.0.0.0/8,.example.test",
    }


def test_import_config_uses_isolated_settings_and_bounded_defaults() -> None:
    config = load_config(_environment())

    assert config.driver_database_url == config.database_url
    assert config.datasets_bucket == "datasets"
    assert config.connector_host_allowlist == ["10.0.0.0/8", ".example.test"]
    assert config.lease_seconds == 120
    assert config.heartbeat_seconds == 20
    assert config.poll_seconds == 2
    assert config.part_bytes == 8 * 1024 * 1024
    assert config.image_header_bytes == 256 * 1024


def test_api_and_ambient_aws_settings_are_not_importer_fallbacks() -> None:
    with pytest.raises(ConfigurationError, match="ASSETS_IMPORT_DATABASE_URL"):
        load_config(
            {
                "DATABASE_URL": "postgresql://api:secret@db.test/aap",
                "AWS_ACCESS_KEY_ID": "ambient-access",
                "AWS_SECRET_ACCESS_KEY": "ambient-secret",
            }
        )

    environment = _environment()
    del environment["ASSETS_IMPORT_S3_ACCESS_KEY"]
    with pytest.raises(ConfigurationError, match="ASSETS_IMPORT_S3_ACCESS_KEY"):
        load_config({**environment, "AWS_ACCESS_KEY_ID": "ambient-access"})


def test_secret_file_input_and_conflict(tmp_path) -> None:
    access_file = tmp_path / "access"
    access_file.write_text("file-access\n", encoding="utf-8")
    environment = _environment()
    del environment["ASSETS_IMPORT_S3_ACCESS_KEY"]
    config = load_config(
        {**environment, "ASSETS_IMPORT_S3_ACCESS_KEY_FILE": str(access_file)}
    )
    assert config.s3_access_key == "file-access"

    with pytest.raises(ConfigurationError, match="Set only one"):
        load_config(
            {
                **_environment(),
                "ASSETS_IMPORT_S3_ACCESS_KEY_FILE": str(access_file),
            }
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("ASSETS_IMPORT_S3_ENDPOINT", "http://user:secret@host.test", "origin"),
        ("ASSETS_IMPORT_LEASE_SECONDS", "20", "between 30 and 3600"),
        ("ASSETS_IMPORT_HEARTBEAT_SECONDS", "70", "less than half"),
        ("ASSETS_IMPORT_DATASETS_BUCKET", "192.0.2.4", "is invalid"),
        ("CONNECTOR_HOST_ALLOWLIST", "*.example.test", "invalid"),
        ("CONNECTOR_ENCRYPTION_KEY", "not-a-fernet-key", "is invalid"),
        ("ASSETS_IMPORT_POOL_MIN_SIZE", "5", "must not exceed"),
        ("ASSETS_IMPORT_POOL_MAX_SIZE", "1", "between 2 and 32"),
    ],
)
def test_import_config_rejects_unsafe_values(
    key: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config({**_environment(), key: value})
