from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aap_labeling_mask_gc_runtime.config import ConfigurationError, load_config

TOKEN = "g" * 48
CONTENT_TOKEN = "c" * 48


def _environment():
    return {
        "MASK_GC_DATABASE_URL": "postgresql://gc_user:secret@db/annotation",
        "MASK_GC_TOKEN": TOKEN,
        "MASK_GC_CONTENT_URL": "http://mask-cleanup:8770/",
        "MASK_GC_CONTENT_TOKEN": CONTENT_TOKEN,
        "MASK_GC_HOST": "127.0.0.1",
        "MASK_GC_PORT": "8771",
        "MASK_GC_POLL_SECONDS": "2.5",
    }


def test_configuration_is_immutable_and_redacts_credentials():
    config = load_config(_environment())
    assert config.content_url == "http://mask-cleanup:8770"
    assert config.poll_seconds == 2.5
    rendered = repr(config)
    assert "secret" not in rendered
    assert TOKEN not in rendered
    assert CONTENT_TOKEN not in rendered
    with pytest.raises(FrozenInstanceError):
        config.port = 1


def test_configuration_supports_bounded_secret_files(tmp_path):
    environment = _environment()
    environment.pop("MASK_GC_TOKEN")
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    environment["MASK_GC_TOKEN_FILE"] = str(token_file)
    assert load_config(environment).token == TOKEN

    environment["MASK_GC_TOKEN"] = TOKEN
    with pytest.raises(ConfigurationError, match="set only one"):
        load_config(environment)


def test_configuration_rejects_unsafe_origins_and_invalid_bounds():
    environment = _environment()
    environment["MASK_GC_CONTENT_URL"] = "http://user:pass@mask-cleanup:8770"
    with pytest.raises(ConfigurationError):
        load_config(environment)

    environment = _environment()
    environment["MASK_GC_PORT"] = "65536"
    with pytest.raises(ConfigurationError):
        load_config(environment)

    environment = _environment()
    environment["MASK_GC_POLL_SECONDS"] = "nan"
    with pytest.raises(ConfigurationError):
        load_config(environment)


def test_secret_file_size_is_bounded(tmp_path):
    environment = _environment()
    environment.pop("MASK_GC_TOKEN")
    token_file = tmp_path / "large"
    token_file.write_bytes(b"x" * 8193)
    environment["MASK_GC_TOKEN_FILE"] = str(token_file)
    with pytest.raises(ConfigurationError):
        load_config(environment)
