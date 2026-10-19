from __future__ import annotations

import pytest

from aap_assets_mask_content_runtime.config import load_config


def _environment(**overrides):
    values = {
        "MASK_CONTENT_ROLE": "content",
        "MASK_CONTENT_TOKEN": "t" * 40,
        "MASK_CONTENT_S3_ENDPOINT_URL": "http://minio.test",
        "MASK_CONTENT_S3_ACCESS_KEY": "access",
        "MASK_CONTENT_S3_SECRET_KEY": "secret",
        "MASK_CONTENT_BUCKET": "mask-bucket",
    }
    values.update(overrides)
    return values


def test_inline_configuration_and_defaults():
    config = load_config(_environment())
    assert config.role == "content"
    assert config.region == "us-east-1"
    assert config.host == "127.0.0.1"
    assert config.port == 8770
    assert "access" not in repr(config)
    assert "secret" not in repr(config)
    assert "t" * 40 not in repr(config)


def test_secret_file_trims_only_one_final_newline(tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_bytes(b"t" * 40 + b"\r\n")
    config = load_config(
        _environment(
            MASK_CONTENT_TOKEN="",
            MASK_CONTENT_TOKEN_FILE=str(secret_file),
        )
    )
    assert config.token == "t" * 40


@pytest.mark.parametrize(
    "override, message",
    [
        ({"MASK_CONTENT_TOKEN_FILE": "/missing"}, "unable to read"),
        ({"MASK_CONTENT_ROLE": "worker"}, "must be content or cleanup"),
        ({"MASK_CONTENT_PORT": "70000"}, "between 1 and 65535"),
    ],
)
def test_invalid_configuration_fails_without_echoing_secrets(override, message):
    values = _environment(**override)
    if "MASK_CONTENT_TOKEN_FILE" in override:
        values["MASK_CONTENT_TOKEN"] = ""
    with pytest.raises(RuntimeError, match=message):
        load_config(values)


def test_secret_file_alternatives_are_mutually_exclusive(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("t" * 40, encoding="utf-8")
    with pytest.raises(RuntimeError, match="only one"):
        load_config(_environment(MASK_CONTENT_TOKEN_FILE=str(token_file)))


def test_secret_file_size_and_encoding_are_bounded(tmp_path):
    large = tmp_path / "large"
    large.write_bytes(b"x" * 4097)
    invalid = tmp_path / "invalid"
    invalid.write_bytes(b"\xff" * 40)
    for filename, message in ((large, "exceeds"), (invalid, "UTF-8")):
        values = _environment(
            MASK_CONTENT_TOKEN="",
            MASK_CONTENT_TOKEN_FILE=str(filename),
        )
        with pytest.raises(RuntimeError, match=message):
            load_config(values)


def test_s3_endpoint_rejects_embedded_credentials_and_query_parameters():
    for endpoint in (
        "https://user:pass@minio.test",
        "https://minio.test/?token=secret",
    ):
        with pytest.raises(RuntimeError, match="without credentials"):
            load_config(_environment(MASK_CONTENT_S3_ENDPOINT_URL=endpoint))


def test_s3_endpoint_rejects_missing_host_or_scheme():
    for endpoint in ("minio.test:9000", "ftp://minio.test"):
        with pytest.raises(RuntimeError, match=r"HTTP\(S\)"):
            load_config(_environment(MASK_CONTENT_S3_ENDPOINT_URL=endpoint))
