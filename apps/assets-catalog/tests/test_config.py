from pathlib import Path

import pytest

from aap_assets_catalog.config import ConfigurationError, load_config


def _environment() -> dict[str, str]:
    return {
        "ASSETS_CATALOG_DATABASE_URL": "postgresql+asyncpg://catalog:secret@db/aap",
        "ASSETS_CATALOG_TOKEN": "catalog-gateway-token-that-is-long-enough",
        "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT": "http://storage.example.test:9000",
        "ASSETS_CATALOG_S3_ACCESS_KEY": "read-only-key",
        "ASSETS_CATALOG_S3_SECRET_KEY": "read-only-secret",
        "ASSETS_CATALOG_DATASETS_BUCKET": "datasets",
        "ASSETS_CATALOG_MEDIA_CACHE_BUCKET": "media-cache",
    }


def test_explicit_environment_builds_asyncpg_dsn_and_bounded_defaults() -> None:
    config = load_config(_environment())
    assert config.driver_database_url == "postgresql://catalog:secret@db/aap"
    assert config.s3_endpoint == config.s3_public_endpoint
    assert config.host == "0.0.0.0"
    assert config.port == 8766
    assert config.pool_min_size == 1
    assert config.pool_max_size == 8
    assert config.max_request_bytes == 1048576
    assert config.max_response_bytes == 16777216


def test_secret_files_are_explicit_and_cannot_be_combined_with_values(
    tmp_path: Path,
) -> None:
    environment = _environment()
    token_path = tmp_path / "gateway-token"
    token_path.write_text("catalog-gateway-token-that-is-long-enough\n")
    environment.pop("ASSETS_CATALOG_TOKEN")
    environment["ASSETS_CATALOG_TOKEN_FILE"] = str(token_path)
    assert (
        load_config(environment).gateway_token
        == "catalog-gateway-token-that-is-long-enough"
    )

    environment["ASSETS_CATALOG_TOKEN"] = "another-token-that-is-long-enough"
    with pytest.raises(ConfigurationError, match="Set only one"):
        load_config(environment)


def test_configuration_does_not_fall_back_to_generic_credentials() -> None:
    with pytest.raises(ConfigurationError, match="ASSETS_CATALOG_DATABASE_URL"):
        load_config(
            {
                "DATABASE_URL": "postgresql://broad-api-role:secret@db/aap",
                "AWS_ACCESS_KEY_ID": "broad-key",
                "AWS_SECRET_ACCESS_KEY": "broad-secret",
            }
        )


def test_private_source_endpoint_can_differ_from_browser_signing_origin() -> None:
    environment = _environment()
    environment["ASSETS_CATALOG_S3_PUBLIC_ENDPOINT"] = "http://127.0.0.1:9000"
    environment["ASSETS_CATALOG_S3_ENDPOINT"] = "http://minio:9000"

    config = load_config(environment)
    assert config.s3_public_endpoint == "http://127.0.0.1:9000"
    assert config.s3_endpoint == "http://minio:9000"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("ASSETS_CATALOG_S3_PUBLIC_ENDPOINT", "http://minio:9000/path"),
        ("ASSETS_CATALOG_S3_ENDPOINT", "http://minio:9000/path"),
        ("ASSETS_CATALOG_DATASETS_BUCKET", "127.0.0.1"),
        ("ASSETS_CATALOG_MEDIA_CACHE_BUCKET", "BadBucket"),
        ("ASSETS_CATALOG_PORT", "65536"),
        ("ASSETS_CATALOG_POOL_MIN_SIZE", "9"),
        ("ASSETS_CATALOG_MAX_REQUEST_BYTES", "5000000"),
        ("ASSETS_CATALOG_MAX_RESPONSE_BYTES", "70000000"),
    ],
)
def test_unsafe_or_unbounded_configuration_is_rejected(
    setting: str, value: str
) -> None:
    environment = _environment()
    environment[setting] = value
    with pytest.raises(ConfigurationError):
        load_config(environment)


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "non-ascii-token-长度超过32-bytes",
        "contains whitespace and is long enough",
        " catalog-gateway-token-that-is-long-enough ",
    ],
)
def test_gateway_token_must_be_long_ascii_without_whitespace(token: str) -> None:
    environment = _environment()
    environment["ASSETS_CATALOG_TOKEN"] = token
    with pytest.raises(ConfigurationError, match="ASSETS_CATALOG_TOKEN"):
        load_config(environment)
