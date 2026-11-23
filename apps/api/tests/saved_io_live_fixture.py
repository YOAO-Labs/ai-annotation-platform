"""Case-owned real Source listener with the installed scoped PostgreSQL logins."""

import asyncio
from contextlib import asynccontextmanager, closing
import json
import os
import secrets
import socket
from tempfile import TemporaryDirectory

import httpx
import pytest
import uvicorn

from app.config import settings
from yoao_assets_source_runtime.api import create_app
from yoao_assets_source_runtime.config import load_config


def required(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Missing Saved I/O acceptance setting: {name}")
    return value


@asynccontextmanager
async def real_saved_io_source(*, allowlist, encryption_key, sftp_roots=None):
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = settings.minio_endpoint
    if "://" not in endpoint:
        endpoint = f"{scheme}://{endpoint}"
    env = {
        "ASSETS_SOURCE_DATABASE_URL": required("AAP_MODULE_DATA_ASSET_SOURCE_URL"),
        "ASSETS_SOURCE_SAVED_IO_DATABASE_URL": required(
            "AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"
        ),
        "ASSETS_SOURCE_S3_ENDPOINT": endpoint,
        "ASSETS_SOURCE_S3_ACCESS_KEY": required(
            "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"
        ),
        "ASSETS_SOURCE_S3_SECRET_KEY": required(
            "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"
        ),
        "ASSETS_SOURCE_DATASETS_BUCKET": settings.minio_datasets_bucket,
        "ASSETS_SOURCE_IMPORT_BUCKET": settings.minio_import_bucket,
        "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": settings.minio_media_cache_bucket,
    }
    env.update(
        ASSETS_SOURCE_TOKEN=secrets.token_urlsafe(32),
        ASSETS_SOURCE_IMPORT_VERIFY_TOKEN=secrets.token_urlsafe(32),
        ASSETS_SOURCE_S3_PUBLIC_ENDPOINT=env["ASSETS_SOURCE_S3_ENDPOINT"],
        ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY=encryption_key,
        ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST=json.dumps(allowlist),
        ASSETS_SOURCE_SAVED_IO_READ_TOKEN=secrets.token_urlsafe(32),
        ASSETS_SOURCE_SAVED_IO_WRITE_TOKEN=secrets.token_urlsafe(32),
        ASSETS_SOURCE_SAVED_IO_CLEANUP_TOKEN=secrets.token_urlsafe(32),
        STORAGE_SFTP_ROOTS=json.dumps(sftp_roots or {}),
    )
    with (
        TemporaryDirectory(prefix="aap-saved-io-") as spool,
        closing(socket.socket()) as listener,
    ):
        env["ASSETS_SOURCE_SAVED_IO_SPOOL_DIR"] = spool
        config = load_config(env)
        service = create_app(config=config)
        server = uvicorn.Server(
            uvicorn.Config(service, access_log=False, log_level="critical")
        )
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(12):
                while not server.started:
                    if task.done():
                        task.result()
                        pytest.fail("Assets Source exited before acceptance readiness")
                    await asyncio.sleep(0.025)
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as http:
                assert (await http.get("/ready")).json() == {"status": "ready"}
            yield origin, config.saved_io.tokens
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=12)
