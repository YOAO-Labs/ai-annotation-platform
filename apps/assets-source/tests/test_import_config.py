from __future__ import annotations

import pytest

from yoao_assets_source_import.config import ConfigurationError, load_config


def _environment() -> dict[str, str]:
    return {
        "ASSETS_IMPORT_DATABASE_URL": "postgresql://assets_import:secret@db.test/aap",
        "ASSETS_IMPORT_VERIFY_URL": "http://source.internal:8767",
        "ASSETS_IMPORT_VERIFY_TOKEN": "v" * 40,
        "ASSETS_IMPORT_SAVED_IO_URL": "http://source.internal:8767",
        "ASSETS_IMPORT_SAVED_IO_READ_TOKEN": "r" * 40,
        "ASSETS_IMPORT_SAVED_IO_WRITE_TOKEN": "w" * 40,
        "ASSETS_IMPORT_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_IMPORT_S3_ACCESS_KEY": "explicit-access",
        "ASSETS_IMPORT_S3_SECRET_KEY": "explicit-secret",
        "ASSETS_IMPORT_DATASETS_BUCKET": "datasets",
    }


def test_import_config_uses_isolated_settings_and_bounded_defaults() -> None:
    config = load_config(_environment())

    assert config.driver_database_url == config.database_url
    assert config.datasets_bucket == "datasets"
    assert config.saved_io_url == "http://source.internal:8767"
    assert config.saved_io_timeout_seconds == 120
    assert not hasattr(config, "connector_encryption_key")
    assert not hasattr(config, "connector_host_allowlist")
    assert not hasattr(config, "sftp_known_hosts_file")
    assert config.lease_seconds == 120
    assert config.heartbeat_seconds == 20
    assert config.poll_seconds == 2
    assert not config.image_policy.auto_generate_pyramids


def test_import_config_captures_explicit_image_policy_environment():
    config = load_config(
        {
            **_environment(),
            "IMAGE_PYRAMID_AUTO_GENERATE": "true",
            "IMAGE_PYRAMID_PROFILE_VERSION": "pyramid-import-test",
        }
    )

    assert config.image_policy.auto_generate_pyramids
    assert config.image_policy.profile_version == "pyramid-import-test"


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


def test_saved_io_loads_its_own_scoped_files(tmp_path) -> None:
    environment = _environment()
    for scope in ("READ", "WRITE"):
        name = f"ASSETS_IMPORT_SAVED_IO_{scope}_TOKEN"
        path = tmp_path / scope
        path.write_text(environment.pop(name) + "\n", encoding="utf-8")
        environment[f"{name}_FILE"] = str(path)

    config = load_config(environment)
    assert config.saved_io_read_token == "r" * 40
    assert config.saved_io_write_token == "w" * 40
    with pytest.raises(ConfigurationError, match="ASSETS_IMPORT_SAVED_IO_READ_TOKEN"):
        del environment["ASSETS_IMPORT_SAVED_IO_READ_TOKEN_FILE"]
        load_config(
            {
                **environment,
                "ASSETS_SOURCE_SAVED_IO_READ_TOKEN": "a" * 40,
                "ASSETS_SOURCE_TOKEN": "b" * 40,
                "CONNECTOR_ENCRYPTION_KEY": "unused",
            }
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("ASSETS_IMPORT_S3_ENDPOINT", "http://user:secret@host.test", "origin"),
        ("ASSETS_IMPORT_VERIFY_URL", "http://user:secret@host.test", "origin"),
        ("ASSETS_IMPORT_VERIFY_TOKEN", "short", "32–256 printable ASCII"),
        ("ASSETS_IMPORT_LEASE_SECONDS", "20", "between 30 and 3600"),
        ("ASSETS_IMPORT_HEARTBEAT_SECONDS", "70", "less than half"),
        ("ASSETS_IMPORT_DATASETS_BUCKET", "192.0.2.4", "is invalid"),
        ("ASSETS_IMPORT_SAVED_IO_URL", "https://source.test/?secret=1", "origin"),
        ("ASSETS_IMPORT_SAVED_IO_READ_TOKEN", "v" * 40, "distinct"),
        ("ASSETS_IMPORT_SAVED_IO_WRITE_TOKEN", "r" * 40, "distinct"),
        ("ASSETS_IMPORT_SAVED_IO_TIMEOUT_SECONDS", "inf", "between"),
        ("ASSETS_IMPORT_POOL_MIN_SIZE", "5", "must not exceed"),
        ("ASSETS_IMPORT_POOL_MAX_SIZE", "1", "between 2 and 32"),
    ],
)
def test_import_config_rejects_unsafe_values(
    key: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config({**_environment(), key: value})
