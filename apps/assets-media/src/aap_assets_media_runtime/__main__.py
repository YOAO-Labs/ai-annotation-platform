"""Start the independent media control process; worker has its own entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, replace

from aap_assets_images.policy import image_policy_from_environment
from aap_assets_images_runtime.api import install_image_routes
from aap_assets_images_runtime.config import (
    load_storage_config as load_image_storage_config,
)
from aap_assets_images_runtime.control import ImageControl
from aap_assets_images_runtime.database import AsyncpgImageDatabase
from aap_assets_images_runtime.health import check_capabilities
from aap_assets_images_runtime.storage import S3ImageStorage
from aap_assets_inputs_runtime.api import install_input_routes
from aap_assets_inputs_runtime.control import InputControl
from aap_assets_inputs_runtime.database import AsyncpgInputDatabase
from aap_assets_inputs_runtime.storage import S3InputStorage
from aap_assets_input_manifests_runtime.api import install_manifest_routes
from aap_assets_input_manifests_runtime.control import ManifestControl
from aap_assets_input_manifests_runtime.database import AsyncpgManifestDatabase

from .api import create_app
from .control import MediaControl
from .control_config import load_control_config
from .database import AsyncpgMediaDatabase
from .storage import S3MediaStorage


async def _create_pool(**options):
    import asyncpg

    return await asyncpg.create_pool(**options)


def configured_app(config):
    state = {}

    class Control:
        def __getattr__(self, name):
            return getattr(state["control"], name)

    class ImageControlProxy:
        def __getattr__(self, name):
            return getattr(state["image_control"], name)

    class InputControlProxy:
        def __getattr__(self, name):
            return getattr(state["input_control"], name)

    class ManifestControlProxy:
        def __getattr__(self, name):
            return getattr(state["manifest_control"], name)

    async def ready():
        return "pool" in state and await check_capabilities(state["pool"], "control")

    app = create_app(Control(), config.token, readiness=ready)
    install_image_routes(app, ImageControlProxy(), config.token)
    install_input_routes(app, InputControlProxy(), config.token)
    install_manifest_routes(app, ManifestControlProxy(), config.token)

    image_storage = load_image_storage_config()
    image_storage = replace(
        image_storage,
        endpoint=config.storage.endpoint,
        public_endpoint=config.storage.public_endpoint,
        region=config.storage.region,
        datasets_bucket=config.storage.datasets_bucket,
        cache_bucket=config.storage.cache_bucket,
        inputs_bucket=config.storage.inputs_bucket,
        access_key=config.storage.access_key,
        secret_key=config.storage.secret_key,
        connect_timeout=config.storage.connect_timeout,
        read_timeout=config.storage.read_timeout,
        max_attempts=config.storage.max_attempts,
    )
    image_policy = image_policy_from_environment()

    @asynccontextmanager
    async def lifespan(app):
        pool = await _create_pool(
            dsn=config.database_url,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            timeout=config.pool_timeout_seconds,
            command_timeout=config.sql_timeout_seconds,
        )
        state["pool"] = pool
        try:
            if not await check_capabilities(pool, "control"):
                raise RuntimeError("Media control SQL capability qualification failed")
            state["control"] = MediaControl(
                AsyncpgMediaDatabase(pool, allowed_roles={"control", "system"}),
                S3MediaStorage(
                    **{
                        key: value
                        for key, value in asdict(config.storage).items()
                        if key != "inputs_bucket"
                    }
                ),
            )
            state["image_control"] = ImageControl(
                AsyncpgImageDatabase(pool, allowed_roles={"control", "system"}),
                S3ImageStorage(image_storage),
                image_policy,
                wakeup=None,
            )
            state["input_storage"] = S3InputStorage(
                image_storage, source_storage=state["image_control"].storage
            )
            state["input_control"] = InputControl(
                AsyncpgInputDatabase(pool, allowed_roles={"control"}),
                state["image_control"],
                state["input_storage"],
                url_expiry_seconds=min(image_policy.url_expiry_seconds, 3600),
            )
            state["manifest_control"] = ManifestControl(
                AsyncpgManifestDatabase(pool, allowed_roles={"control"}),
                state["image_control"],
            )
            yield
        finally:
            try:
                try:
                    input_storage = state.get("input_storage")
                    if input_storage is not None:
                        await input_storage.close()
                finally:
                    image_control = state.get("image_control")
                    if image_control is not None:
                        await image_control.close()
            finally:
                state.clear()
                await pool.close()

    app.router.lifespan_context = lifespan
    return app


def main():
    import uvicorn

    config = load_control_config()
    uvicorn.run(
        configured_app(config),
        host=config.host,
        port=config.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
