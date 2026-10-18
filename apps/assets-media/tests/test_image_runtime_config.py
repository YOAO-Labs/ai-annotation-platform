from pathlib import Path

import pytest

from aap_assets_images_runtime.config import ConfigurationError, load_config


def _values() -> dict[str, str]:
    return {
        "ASSETS_IMAGES_DATABASE_URL": "postgresql://image-worker@db/images",
        "ASSETS_MEDIA_S3_ENDPOINT": "http://s3.internal",
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": "http://s3.public",
        "ASSETS_MEDIA_DATASETS_BUCKET": "datasets",
        "ASSETS_MEDIA_CACHE_BUCKET": "media-cache",
        "ASSETS_MEDIA_S3_ACCESS_KEY": "image-worker-access",
        "ASSETS_MEDIA_S3_SECRET_KEY": "image-worker-secret",
    }


def test_empty_legacy_bucket_setting_falls_back_only_to_existing_bucket_name():
    values = _values()
    values["ASSETS_IMAGES_LEGACY_BUCKET"] = ""
    values["MINIO_BUCKET"] = "annotations"

    assert load_config(values).storage.legacy_bucket == "annotations"

    values["MINIO_BUCKET"] = ""
    values["MINIO_BUCKET_ANNOTATIONS"] = "legacy-tasks"
    assert load_config(values).storage.legacy_bucket == "legacy-tasks"

    values.pop("MINIO_BUCKET_ANNOTATIONS")
    with pytest.raises(ConfigurationError, match="ASSETS_IMAGES_LEGACY_BUCKET"):
        load_config(values)


def test_empty_image_srgb_setting_uses_legacy_value_then_packaged_default():
    values = _values()
    values["ASSETS_IMAGES_LEGACY_BUCKET"] = "annotations"
    values["ASSETS_IMAGES_SRGB_PROFILE"] = ""
    values["IMAGE_PYRAMID_SRGB_PROFILE"] = "/srv/legacy/srgb.icc"

    assert load_config(values).srgb_profile == Path("/srv/legacy/srgb.icc")

    values["IMAGE_PYRAMID_SRGB_PROFILE"] = ""
    assert load_config(values).srgb_profile == Path("/usr/share/color/icc/sRGB.icc")
