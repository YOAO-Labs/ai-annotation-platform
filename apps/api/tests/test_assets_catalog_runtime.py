"""Real catalog container, scoped storage and public gateway acceptance."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _expect_s3_denied,
    _inspect_docker,
    _local_s3_client,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
    _wait_healthy,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


def _write_environment(path: Path, values: dict[str, str]) -> None:
    assert all("\n" not in value and "\r" not in value for value in values.values())
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def _start_catalog(image: str, identity: str, environment: Path, suffix="") -> str:
    return _docker(
        "run",
        "--detach",
        "--name",
        f"aap-assets-catalog-{identity[:12]}{suffix}",
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        "127.0.0.1::8766",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--health-interval",
        "1s",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--env-file",
        str(environment),
        image,
    )


def _catalog_origin(container: str, image: str, identity: str) -> str:
    observed = _inspect_docker(container)
    config = observed.get("Config", {})
    labels = config.get("Labels") or {}
    bindings = observed["NetworkSettings"]["Ports"]["8766/tcp"]
    assert observed["Id"] == container
    assert config["Image"] == image and config["User"] == "10002:10002"
    assert labels["org.aap.acceptance"] == "module-data-boundary"
    assert labels["org.aap.acceptance-id"] == identity
    assert not observed.get("Mounts")
    assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
    assert not any(
        value.startswith(("DATABASE_URL=", "MINIO_", "AWS_", "CELERY_", "REDIS_"))
        for value in config.get("Env", [])
    )
    return f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"


async def _wait_public_ready(origin: str) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                if (await client.get(f"{origin}/ready")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    pytest.fail("catalog public origin did not become ready")


async def _assert_startup_rejected(container: str) -> None:
    for _ in range(60):
        state = await asyncio.to_thread(_inspect_docker, container)
        if not state["State"]["Running"]:
            assert state["State"]["ExitCode"] != 0
            return
        await asyncio.sleep(0.25)
    pytest.fail("catalog accepted an unsafe database credential")


@asynccontextmanager
async def _lost_response_proxy(origin: str):
    """Drop the response only after the real service has committed its command."""
    address = httpx.URL(origin)
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        upstream_writer = None
        forwarder = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                address.host, address.port
            )

            async def forward():
                while block := await reader.read(65536):
                    upstream_writer.write(block)
                    await upstream_writer.drain()

            forwarder = asyncio.create_task(forward())
            assert await asyncio.wait_for(upstream_reader.read(1), timeout=15)
        finally:
            if forwarder is not None:
                forwarder.cancel()
                with suppress(asyncio.CancelledError):
                    await forwarder
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), timeout=20)


async def _seed_actor(engine):
    from app.core.security import create_access_token
    from app.db.models.user import User

    actor_id = uuid4()
    async with async_sessionmaker(engine)() as owner:
        owner.add(
            User(
                id=actor_id,
                email=f"catalog-{actor_id}@example.test",
                name="Catalog actor",
                role="super_admin",
                password_hash="unused-acceptance-hash",
            )
        )
        await owner.commit()
    return actor_id, create_access_token(subject=str(actor_id), role="super_admin")


async def _cleanup_rows(engine, actor_id: UUID):
    async with engine.begin() as owner:
        for statement in (
            "DELETE FROM public.dataset_catalog_operations WHERE actor_id = :actor",
            "DELETE FROM public.datasets WHERE created_by = :actor",
            "DELETE FROM public.users WHERE id = :actor",
        ):
            await owner.execute(text(statement), {"actor": actor_id})


async def test_catalog_container_public_consumer_replay_and_local_switch(
    test_engine,
    app_module,
    monkeypatch,
):
    from app.config import settings
    from app.deps import get_db
    from app.db.models.dataset import DatasetItem

    catalog_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_CATALOG_URL"))
    assert catalog_url.username == "asset_catalog_client"
    assert catalog_url.host == "127.0.0.1"
    assert catalog_url.database == test_engine.url.database
    _verify_postgres(catalog_url)
    resources = _verified_resource_manifest(test_engine, catalog_url)
    datasets = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    cache = resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    scoped_s3 = _local_s3_client(
        settings,
        _required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"),
        _required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"),
    )
    for bucket in (datasets, cache):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    identity = secrets.token_hex(12)
    image = f"aap-assets-catalog-acceptance:{identity}"
    service_token = secrets.token_urlsafe(32)
    actor_id = None
    container = None
    forbidden_container = None
    membership_granted = False
    objects = []
    previous_db = app_module.dependency_overrides.get(get_db)

    async def gateway_database():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
            yield session

    try:
        build = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "--file",
                "apps/assets-catalog/Dockerfile",
                "--tag",
                image,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                ".",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert build.returncode == 0, "standalone catalog image build failed"
        actor_id, token = await _seed_actor(test_engine)
        app_module.dependency_overrides[get_db] = gateway_database
        monkeypatch.setattr(settings, "assets_catalog_token", service_token)
        scheme = "https" if settings.minio_use_ssl else "http"
        values = {
            "ASSETS_CATALOG_DATABASE_URL": _container_database_url(catalog_url),
            "ASSETS_CATALOG_TOKEN": service_token,
            "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_CATALOG_S3_REGION": "us-east-1",
            "ASSETS_CATALOG_DATASETS_BUCKET": datasets,
            "ASSETS_CATALOG_MEDIA_CACHE_BUCKET": cache,
            "ASSETS_CATALOG_S3_ACCESS_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"
            ),
            "ASSETS_CATALOG_S3_SECRET_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"
            ),
        }
        with tempfile.TemporaryDirectory(
            prefix="catalog-runtime-", dir=os.environ["TMPDIR"]
        ) as temporary:
            environment = Path(temporary) / "catalog.env"
            _write_environment(environment, values)
            container = await asyncio.to_thread(
                _start_catalog, image, identity, environment
            )
            origin = _catalog_origin(container, image, identity)
            await _wait_healthy(container)
            await _wait_public_ready(origin)
            _docker(
                "exec",
                container,
                "python",
                "-c",
                "import importlib.util; assert all(importlib.util.find_spec(n) is None "
                "for n in ('app','sqlalchemy','celery','redis','torch'))",
            )
            monkeypatch.setattr(settings, "assets_catalog_url", origin)
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as direct:
                assert (await direct.get("/health")).status_code == 200
                assert (await direct.get("/ready")).status_code == 200
                denied = await direct.post(
                    "/v1/catalog/list", json={"actor_id": str(actor_id)}
                )
                assert denied.status_code == 401
                malformed = await direct.post(
                    "/v1/catalog/list",
                    json={"actor_id": str(actor_id), "role": "super_admin"},
                    headers={"Authorization": f"Bearer {service_token}"},
                )
                assert malformed.status_code == 422

            headers = {"Authorization": f"Bearer {token}"}
            create_key, update_key = str(uuid4()), str(uuid4())
            name = f"catalog-runtime-{identity}"
            body = {"name": name, "data_type": "lidar", "axis_convention": "apollo"}
            transport = httpx.ASGITransport(app=app_module)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://gateway"
            ) as api:
                async with _lost_response_proxy(origin) as proxy:
                    monkeypatch.setattr(settings, "assets_catalog_url", proxy)
                    lost = await api.post(
                        "/api/v1/datasets",
                        json=body,
                        headers={**headers, "Idempotency-Key": create_key},
                    )
                    assert lost.status_code in {502, 503}
                async with test_engine.connect() as owner:
                    assert (
                        await owner.scalar(
                            text(
                                "SELECT count(*) FROM public.dataset_catalog_operations "
                                "WHERE operation_id=:operation"
                            ),
                            {"operation": UUID(create_key)},
                        )
                        == 1
                    )
                monkeypatch.setattr(settings, "assets_catalog_url", origin)
                created = await api.post(
                    "/api/v1/datasets",
                    json=body,
                    headers={**headers, "Idempotency-Key": create_key},
                )
                assert created.status_code == 201, created.text
                assert created.headers["Idempotency-Key"] == create_key
                original = created.json()
                dataset_id = UUID(original["id"])
                async with test_engine.connect() as owner:
                    assert (
                        await owner.scalar(
                            text(
                                "SELECT count(*) FROM public.datasets WHERE name=:name"
                            ),
                            {"name": name},
                        )
                        == 1
                    )
                conflict = await api.post(
                    "/api/v1/datasets",
                    json={**body, "name": name + "-other"},
                    headers={**headers, "Idempotency-Key": create_key},
                )
                assert conflict.status_code == 409
                key = f"legacy-{identity}/source.jpg"
                thumbnail = f"thumbnails/{identity}.jpg"
                for bucket, object_key in ((datasets, key), (cache, thumbnail)):
                    owner_s3.put_object(
                        Bucket=bucket, Key=object_key, Body=object_key.encode()
                    )
                    objects.append((bucket, object_key))
                async with async_sessionmaker(test_engine)() as owner:
                    owner.add(
                        DatasetItem(
                            dataset_id=dataset_id,
                            file_name="source.jpg",
                            file_path=key,
                            file_type="image",
                            file_size=31,
                            thumbnail_path=thumbnail,
                            metadata_={"future_field": {"retained": True}},
                        )
                    )
                    await owner.commit()
                listed = await api.get(
                    "/api/v1/datasets",
                    params={"search": name, "data_type": "point_cloud"},
                    headers=headers,
                )
                assert listed.status_code == 200 and listed.json()["total"] == 1
                assert listed.json()["items"][0]["total_size"] == 31
                items = await api.get(
                    f"/api/v1/datasets/{dataset_id}/items", headers=headers
                )
                assert items.status_code == 200, items.text
                item = items.json()["items"][0]
                assert "thumbnail_path" not in item
                assert item["metadata"]["future_field"] == {"retained": True}
                async with httpx.AsyncClient(trust_env=False) as source:
                    assert (await source.get(item["file_url"])).content == key.encode()
                    assert (
                        await source.get(item["thumbnail_url"])
                    ).content == thumbnail.encode()
                _expect_s3_denied(
                    lambda: scoped_s3.put_object(Bucket=datasets, Key=key, Body=b"bad")
                )
                _expect_s3_denied(
                    lambda: scoped_s3.delete_object(Bucket=datasets, Key=key)
                )
                _expect_s3_denied(
                    lambda: scoped_s3.list_objects_v2(Bucket=datasets, MaxKeys=1)
                )
                _expect_s3_denied(
                    lambda: scoped_s3.get_object(
                        Bucket=cache, Key=f"unowned-{identity}"
                    )
                )
                _expect_s3_denied(
                    lambda: scoped_s3.get_object(
                        Bucket=resources["buckets"]["MINIO_IMPORT_BUCKET"], Key=key
                    )
                )
                changed = await api.put(
                    f"/api/v1/datasets/{dataset_id}",
                    json={"description": "committed", "axis_convention": None},
                    headers={**headers, "Idempotency-Key": update_key},
                )
                assert (
                    changed.status_code == 200
                    and changed.json()["axis_convention"] is None
                )
                await asyncio.to_thread(_docker, "stop", "--time", "15", container)
                unavailable = await api.get(
                    f"/api/v1/datasets/{dataset_id}", headers=headers
                )
                assert unavailable.status_code in {502, 503}
                await asyncio.to_thread(_docker, "start", container)
                await _wait_healthy(container)
                # Docker may allocate a new dynamic host port after stop/start.
                origin = _catalog_origin(container, image, identity)
                await _wait_public_ready(origin)
                monkeypatch.setattr(settings, "assets_catalog_url", origin)
                replay = await api.put(
                    f"/api/v1/datasets/{dataset_id}",
                    json={"description": "committed", "axis_convention": None},
                    headers={**headers, "Idempotency-Key": update_key},
                )
                assert replay.status_code == 200 and replay.json() == changed.json()
                monkeypatch.setattr(settings, "assets_catalog_url", "")
                local = await api.get(f"/api/v1/datasets/{dataset_id}", headers=headers)
                assert local.status_code == 200 and local.json() == changed.json()
                local_replay = await api.post(
                    "/api/v1/datasets",
                    json=body,
                    headers={**headers, "Idempotency-Key": create_key},
                )
                assert (
                    local_replay.status_code == 201 and local_replay.json() == original
                )

            # A general migration credential must never start this runtime.
            values["ASSETS_CATALOG_DATABASE_URL"] = _container_database_url(
                test_engine.url
            )
            _write_environment(environment, values)
            forbidden_container = await asyncio.to_thread(
                _start_catalog,
                image,
                identity,
                environment,
                "-broad",
            )
            await _assert_startup_rejected(forbidden_container)
            _stop_remove_container(forbidden_container, identity)
            forbidden_container = None

            async with test_engine.begin() as owner:
                await owner.execute(
                    text("GRANT asset_catalog_client TO legacy_runtime")
                )
            membership_granted = True
            values["ASSETS_CATALOG_DATABASE_URL"] = _container_database_url(catalog_url)
            _write_environment(environment, values)
            forbidden_container = await asyncio.to_thread(
                _start_catalog,
                image,
                identity,
                environment,
                "-member",
            )
            await _assert_startup_rejected(forbidden_container)
    finally:
        if previous_db is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db
        for candidate in (forbidden_container, container):
            if candidate is not None:
                _stop_remove_container(candidate, identity)
        if membership_granted:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text("REVOKE asset_catalog_client FROM legacy_runtime")
                )
        if actor_id is not None:
            await _cleanup_rows(test_engine, actor_id)
        for bucket, key in objects:
            owner_s3.delete_object(Bucket=bucket, Key=key)
        _remove_worker_image(image, identity)
        owner_s3.close()
        scoped_s3.close()
