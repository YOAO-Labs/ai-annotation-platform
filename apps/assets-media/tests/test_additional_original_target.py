"""Exact additional source targets do not change platform or derivative storage."""

from __future__ import annotations

import pytest

from yoao_assets_media_runtime.additional_original import (
    load_additional_original_config,
)


def test_additional_target_requires_complete_non_secret_configuration():
    assert load_additional_original_config({}) is None
    with pytest.raises(ValueError):
        load_additional_original_config(
            {"AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": "http://fixture.internal"}
        )
    config = load_additional_original_config(
        {
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": "http://fixture.internal",
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": "fixture-originals",
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY": "read-only-key",
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY": "read-only-secret",
        }
    )
    assert "read-only-key" not in repr(config) and "read-only-secret" not in repr(
        config
    )


def test_additional_target_accepts_secret_files_without_exposing_values(tmp_path):
    access = tmp_path / "access"
    secret = tmp_path / "secret"
    access.write_text("read-only-key\n")
    secret.write_text("read-only-secret\n")
    values = {
        "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": "http://fixture.internal",
        "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": "fixture-originals",
        "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY_FILE": str(access),
        "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY_FILE": str(secret),
    }
    config = load_additional_original_config(values)
    assert (
        config.access_key == "read-only-key" and config.secret_key == "read-only-secret"
    )
    assert "read-only-key" not in repr(config) and "read-only-secret" not in repr(
        config
    )
    values["AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY"] = "conflicting"
    with pytest.raises(ValueError):
        load_additional_original_config(values)
