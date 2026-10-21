from __future__ import annotations

import pytest

from aap_assets_source_runtime.config import ConfigurationError
from aap_assets_source_runtime.guide_config import load_guide_storage_config

_PREFIX = "ASSETS_SOURCE_GUIDE_S3_"


def _environment() -> dict[str, str]:
    return {
        _PREFIX + "ENDPOINT": "http://minio.internal:9000",
        _PREFIX + "BUCKET": "aap-guide-test",
        _PREFIX + "ACCESS_KEY": "guide-access-secret",
        _PREFIX + "SECRET_KEY": "guide-secret-key-value",
    }


def test_explicit_guide_settings_have_isolated_defaults_and_hidden_secrets():
    config = load_guide_storage_config(_environment())

    assert config.endpoint == "http://minio.internal:9000"
    assert config.public_url == "/minio"
    assert config.region == "us-east-1"
    assert config.bucket == "aap-guide-test"
    assert config.connect_timeout_seconds == 2
    assert config.read_timeout_seconds == 5
    assert "guide-access-secret" not in repr(config)
    assert "guide-secret-key-value" not in repr(config)


def test_public_url_accepts_path_prefix_and_safe_absolute_prefixes():
    environment = _environment()
    environment[_PREFIX + "PUBLIC_URL"] = "/edge/minio/"
    assert load_guide_storage_config(environment).public_url == "/edge/minio"

    environment[_PREFIX + "PUBLIC_URL"] = "https://cdn.example.test/assets/minio/"
    assert (
        load_guide_storage_config(environment).public_url
        == "https://cdn.example.test/assets/minio"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "//evil.example.test/minio",
        "https://user:secret@cdn.example.test/minio",
        "ftp://cdn.example.test/minio",
        "https://cdn.example.test/minio?redirect=1",
        "https://cdn.example.test/minio#fragment",
        "/minio/../outside",
        "/minio\\outside",
        "https://cdn.example.test//outside",
        "https://cdn.example.test/%2e%2e/outside",
    ],
)
def test_public_url_rejects_unsafe_values(value):
    environment = _environment()
    environment[_PREFIX + "PUBLIC_URL"] = value
    with pytest.raises(ConfigurationError):
        load_guide_storage_config(environment)


def test_configuration_requires_explicit_endpoint_credentials_and_fixed_bucket_name():
    with pytest.raises(ConfigurationError):
        load_guide_storage_config({})

    environment = _environment()
    del environment[_PREFIX + "ACCESS_KEY"]
    with pytest.raises(ConfigurationError, match="ACCESS_KEY"):
        load_guide_storage_config(environment)

    environment = _environment()
    environment[_PREFIX + "BUCKET"] = "Bad_Bucket"
    with pytest.raises(ConfigurationError, match="BUCKET"):
        load_guide_storage_config(environment)


def test_secrets_can_come_from_files_and_cannot_be_supplied_twice(tmp_path):
    access_file = tmp_path / "access-key"
    secret_file = tmp_path / "secret-key"
    access_file.write_text("  file-access-key\n", encoding="utf-8")
    secret_file.write_text("file-secret-key\n", encoding="utf-8")
    environment = _environment()
    del environment[_PREFIX + "ACCESS_KEY"]
    del environment[_PREFIX + "SECRET_KEY"]
    environment[_PREFIX + "ACCESS_KEY_FILE"] = str(access_file)
    environment[_PREFIX + "SECRET_KEY_FILE"] = str(secret_file)

    config = load_guide_storage_config(environment)
    assert config.access_key == "file-access-key"
    assert config.secret_key == "file-secret-key"
    assert "file-secret-key" not in repr(config)

    environment[_PREFIX + "ACCESS_KEY"] = "duplicate"
    with pytest.raises(ConfigurationError, match="Set only one"):
        load_guide_storage_config(environment)


@pytest.mark.parametrize(
    "name,value",
    [
        (_PREFIX + "CONNECT_TIMEOUT_SECONDS", "0"),
        (_PREFIX + "CONNECT_TIMEOUT_SECONDS", "31"),
        (_PREFIX + "READ_TIMEOUT_SECONDS", "nan"),
        (_PREFIX + "READ_TIMEOUT_SECONDS", "121"),
    ],
)
def test_timeouts_are_bounded(name, value):
    environment = _environment()
    environment[name] = value
    with pytest.raises(ConfigurationError):
        load_guide_storage_config(environment)
