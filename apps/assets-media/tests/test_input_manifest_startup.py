from aap_assets_images.policy import ImagePolicy
from aap_assets_images_runtime.config import ImageStorageConfig
from aap_assets_media_runtime.config import MediaStorageConfig
from aap_assets_media_runtime.control_config import MediaControlConfig
from aap_assets_media_runtime import __main__ as media_main

TOKEN = "manifest-startup-test-" + "x" * 32


def test_manifest_routes_are_mounted_by_the_existing_media_control_app(monkeypatch):
    image_storage = ImageStorageConfig(
        endpoint="http://minio.internal",
        public_endpoint="http://minio.public",
        region="us-east-1",
        datasets_bucket="datasets",
        legacy_bucket="annotations",
        cache_bucket="cache",
        inputs_bucket="accepted-inputs",
        access_key="test-access-key",
        secret_key="test-secret-key",
    )
    storage = MediaStorageConfig(
        endpoint="http://minio.internal",
        public_endpoint="http://minio.public",
        region="us-east-1",
        datasets_bucket="datasets",
        cache_bucket="cache",
        inputs_bucket="accepted-inputs",
        access_key="test-access-key",
        secret_key="test-secret-key",
    )
    monkeypatch.setattr(media_main, "load_image_storage_config", lambda: image_storage)
    monkeypatch.setattr(media_main, "image_policy_from_environment", ImagePolicy)
    app = media_main.configured_app(
        MediaControlConfig(
            database_url="postgresql://media@localhost/db",
            token=TOKEN,
            storage=storage,
        )
    )

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {
        "/v1/input-manifests/record",
        "/v1/input-manifests/page",
        "/v1/input-manifests/prepare",
        "/v1/input-manifests/seal",
    } <= paths
    assert "/v1/input-manifests/capture" not in paths
