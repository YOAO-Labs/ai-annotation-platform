from __future__ import annotations

import httpx
import pytest

from aap_assets_images.contract import ImagePolicy
from aap_assets_images_runtime.config import ImageStorageConfig
from aap_assets_media import contract as video
from aap_assets_media_runtime.config import MediaStorageConfig
from aap_assets_media_runtime.control_config import MediaControlConfig
from aap_assets_media_runtime import __main__ as shared_runtime


class _Pool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _Asyncpg:
    def __init__(self):
        self.pool = _Pool()
        self.arguments = None

    async def create_pool(self, *args, **kwargs):
        self.arguments = (args, kwargs)
        return self.pool


class _VideoDatabase:
    def __init__(self, pool, *, allowed_roles):
        self.pool = pool
        self.allowed_roles = allowed_roles

    async def call(self, operation, request):
        assert operation == "read_media_preferences"
        return video.MediaPreferences(warmup_lookahead=None)


class _ImageDatabase:
    def __init__(self, pool, *, allowed_roles):
        self.pool = pool
        self.allowed_roles = allowed_roles

    async def call(self, operation, request):
        assert operation == "read_image_usage"
        from aap_assets_images.contract import ImageUsage

        return ImageUsage(
            source_bytes=0,
            thumbnail_bytes=0,
            pyramid_bytes=0,
            pending=0,
            running=0,
            failed=0,
            garbage_pending=0,
        )


class _VideoStorage:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _ImageStorage:
    def __init__(self, config):
        self.config = config
        self.closed = False

    async def close(self):
        self.closed = True


def _control_config() -> MediaControlConfig:
    return MediaControlConfig(
        database_url="postgresql://media_control@db/annotation",
        token="media-token-" + "t" * 40,
        storage=MediaStorageConfig(
            endpoint="http://s3.internal",
            public_endpoint="https://s3.public",
            region="us-east-1",
            datasets_bucket="datasets",
            cache_bucket="media-cache",
            access_key="control-access",
            secret_key="control-secret",
            connect_timeout=2,
            read_timeout=10,
        ),
    )


def _image_storage_config() -> ImageStorageConfig:
    return ImageStorageConfig(
        endpoint="http://placeholder.internal",
        public_endpoint="https://placeholder.public",
        region="placeholder-region",
        datasets_bucket="placeholder-datasets",
        legacy_bucket="annotations",
        cache_bucket="placeholder-cache",
        access_key="placeholder-access",
        secret_key="placeholder-secret",
    )


@pytest.mark.asyncio
async def test_shared_runtime_installs_image_routes_on_video_app(monkeypatch):
    asyncpg = _Asyncpg()
    health_calls = []
    health_results = iter((True, True))
    image_storage_configs = []
    image_storages = []

    async def check_capabilities(pool, role):
        health_calls.append((pool, role))
        return next(health_results)

    def image_storage(config):
        image_storage_configs.append(config)
        storage = _ImageStorage(config)
        image_storages.append(storage)
        return storage

    monkeypatch.setattr(shared_runtime, "_create_pool", asyncpg.create_pool)
    monkeypatch.setattr(shared_runtime, "check_capabilities", check_capabilities)
    monkeypatch.setattr(shared_runtime, "AsyncpgMediaDatabase", _VideoDatabase)
    monkeypatch.setattr(shared_runtime, "AsyncpgImageDatabase", _ImageDatabase)
    monkeypatch.setattr(shared_runtime, "S3MediaStorage", _VideoStorage)
    monkeypatch.setattr(shared_runtime, "S3ImageStorage", image_storage)
    monkeypatch.setattr(
        shared_runtime, "load_image_storage_config", _image_storage_config
    )
    monkeypatch.setattr(
        shared_runtime,
        "image_policy_from_environment",
        lambda: ImagePolicy(profile_version="shared-test-profile"),
    )

    app = shared_runtime.configured_app(_control_config())
    assert sum(route.path == "/health/live" for route in app.routes) == 1
    assert sum(route.path == "/health/ready" for route in app.routes) == 1
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://media.local"
        ) as client:
            headers = {
                "Authorization": "Bearer " + _control_config().token,
            }
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            video_response = await client.post(
                "/v1/commands/read_media_preferences", json={}, headers=headers
            )
            image_response = await client.post(
                "/v1/images/commands/read_image_usage", json={}, headers=headers
            )

    assert live.status_code == 200
    assert ready.status_code == 200
    assert video_response.status_code == 200
    assert video_response.json()["schema"] == video.MEDIA_SCHEMA
    assert image_response.status_code == 200
    assert image_response.json()["schema"] == "aap-assets-images/v1"
    assert asyncpg.pool.closed
    assert image_storages[0].closed
    assert {role for _pool, role in health_calls} == {"control"}
    assert all(pool is asyncpg.pool for pool, _role in health_calls)
    assert image_storage_configs[0].endpoint == "http://s3.internal"
    assert image_storage_configs[0].public_endpoint == "https://s3.public"
    assert image_storage_configs[0].legacy_bucket == "annotations"
    assert image_storage_configs[0].connect_timeout == 2
    assert image_storage_configs[0].read_timeout == 10


@pytest.mark.asyncio
async def test_shared_readiness_fails_when_combined_capability_check_fails(monkeypatch):
    asyncpg = _Asyncpg()
    health_results = iter((True, False))

    async def check_capabilities(_pool, role):
        assert role == "control"
        return next(health_results)

    monkeypatch.setattr(shared_runtime, "_create_pool", asyncpg.create_pool)
    monkeypatch.setattr(shared_runtime, "check_capabilities", check_capabilities)
    monkeypatch.setattr(shared_runtime, "AsyncpgMediaDatabase", _VideoDatabase)
    monkeypatch.setattr(shared_runtime, "AsyncpgImageDatabase", _ImageDatabase)
    monkeypatch.setattr(shared_runtime, "S3MediaStorage", _VideoStorage)
    monkeypatch.setattr(shared_runtime, "S3ImageStorage", _ImageStorage)
    monkeypatch.setattr(
        shared_runtime, "load_image_storage_config", _image_storage_config
    )
    monkeypatch.setattr(
        shared_runtime,
        "image_policy_from_environment",
        lambda: ImagePolicy(),
    )

    app = shared_runtime.configured_app(_control_config())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://media.local"
        ) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert asyncpg.pool.closed
