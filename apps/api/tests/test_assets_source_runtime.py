"""Public source consumers use a real isolated SQL/S3 service."""

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
import httpx
from PIL import Image
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _inspect_docker,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import (
    _assert_startup_rejected,
    _lost_response_proxy,
    _wait_public_ready,
    _write_environment,
)
from tests.test_dataset_upload_native import _cleanup_world, _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires disposable PostgreSQL and scoped S3 identities",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


def _start_source(image, identity, environment, suffix=""):
    return _docker(
        "run",
        "-d",
        "--name",
        f"aap-assets-source-{identity[:12]}{suffix}",
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        "127.0.0.1::8767",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "0.5",
        "--health-interval",
        "1s",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--env-file",
        str(environment),
        image,
    )


def _source_origin(container, image, identity):
    observed = _inspect_docker(container)
    config = observed["Config"]
    assert observed["Id"] == container and config["Image"] == image
    assert config["User"] == "10003:10003"
    assert config["Labels"]["org.aap.acceptance-id"] == identity
    assert config["Labels"]["org.aap.acceptance"] == "module-data-boundary"
    assert not observed["Mounts"]
    assert not any(
        value.startswith(("DATABASE_URL=", "MINIO_", "AWS_", "CELERY_", "REDIS_"))
        for value in config["Env"]
    )
    bindings = observed["NetworkSettings"]["Ports"]["8767/tcp"]
    assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
    return f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"


@asynccontextmanager
async def _source_stack(test_engine, app_module, monkeypatch):
    from app.config import settings
    from app.core.security import create_access_token
    from app.deps import get_db, get_media_job_dispatcher
    from app.services.storage import storage_service

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
    assert (
        role_url.username == "asset_source_client"
        and role_url.database == test_engine.url.database
    )
    _verify_postgres(role_url)
    resources = _verified_resource_manifest(test_engine, role_url)
    identity = secrets.token_hex(12)
    image = f"aap-assets-source-acceptance:{identity}"
    token = secrets.token_urlsafe(32)
    containers = []
    world = None
    extras = set()
    auth_engine = create_async_engine(test_engine.url, hide_parameters=True)

    @event.listens_for(auth_engine.sync_engine, "before_cursor_execute")
    def forbid_owner_reads(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if re.search(
            r'\b(?:FROM|JOIN)\s+(?:public\.)?"?(?:datasets|dataset_items|project_datasets|projects|tasks|dataset_upload_intents)\b',
            statement,
            re.I,
        ):
            raise AssertionError("source gateway queried a provider-private relation")

    async def authentication_db():
        async with async_sessionmaker(auth_engine, expire_on_commit=False)() as db:
            yield db

    previous = dict(app_module.dependency_overrides)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=Mock(), dispatch_video_metadata=Mock()
    )
    try:
        build = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "--file",
                "apps/assets-source/Dockerfile",
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
        assert build.returncode == 0, build.stderr[-2500:]
        world = await _seed_world(test_engine)
        app_module.dependency_overrides[get_db] = authentication_db
        app_module.dependency_overrides[get_media_job_dispatcher] = lambda: dispatcher
        monkeypatch.setattr(settings, "assets_source_token", token)
        scheme = "https" if settings.minio_use_ssl else "http"
        endpoint = urlsplit(f"{scheme}://{settings.minio_endpoint}")
        values = {
            "ASSETS_SOURCE_DATABASE_URL": _container_database_url(role_url),
            "ASSETS_SOURCE_TOKEN": token,
            "ASSETS_SOURCE_S3_ENDPOINT": f"{scheme}://host.docker.internal:{endpoint.port or 9000}",
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_SOURCE_S3_ACCESS_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"
            ),
            "ASSETS_SOURCE_S3_SECRET_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"
            ),
            "ASSETS_SOURCE_DATASETS_BUCKET": resources["buckets"][
                "MINIO_DATASETS_BUCKET"
            ],
            "ASSETS_SOURCE_IMPORT_BUCKET": resources["buckets"]["MINIO_IMPORT_BUCKET"],
            "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": resources["buckets"][
                "MINIO_MEDIA_CACHE_BUCKET"
            ],
        }
        with tempfile.TemporaryDirectory(
            prefix="assets-source-runtime-", dir=os.environ["TMPDIR"]
        ) as directory:
            environment = Path(directory) / "source.env"
            _write_environment(environment, values)
            container = await asyncio.to_thread(
                _start_source, image, identity, environment
            )
            containers.append(container)
            await _wait_healthy(container)
            origin = _source_origin(container, image, identity)
            await _wait_public_ready(origin)
            monkeypatch.setattr(settings, "assets_source_url", origin)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module),
                base_url="http://gateway",
                timeout=30,
            ) as api:
                yield SimpleNamespace(
                    world=world,
                    api=api,
                    origin=origin,
                    image=image,
                    identity=identity,
                    container=container,
                    containers=containers,
                    values=values,
                    directory=Path(directory),
                    extras=extras,
                    storage=storage_service,
                    dispatcher=dispatcher,
                    headers={
                        "manager": {
                            "Authorization": f"Bearer {create_access_token(str(world.actor_id), 'super_admin')}"
                        },
                        "employee": {
                            "Authorization": f"Bearer {create_access_token(str(world.other_actor_id), 'employee')}"
                        },
                    },
                )
    finally:
        app_module.dependency_overrides.clear()
        app_module.dependency_overrides.update(previous)
        for container in reversed(containers):
            _stop_remove_container(container, identity)
        await auth_engine.dispose()
        if world is not None:
            async with world.maker.begin() as owner:
                datasets = [world.dataset_id, *extras]
                for table in (
                    "dataset_source_ingest_deliveries",
                    "dataset_source_ingest_entries",
                ):
                    await owner.execute(
                        text(
                            f"DELETE FROM public.{table} WHERE operation_id IN "
                            "(SELECT operation_id FROM public.dataset_source_ingest_operations "
                            "WHERE dataset_id=ANY(CAST(:ids AS uuid[])))"
                        ),
                        {"ids": datasets},
                    )
                for table in (
                    "dataset_source_ingest_operations",
                    "dataset_legacy_upload_operations",
                    "dataset_source_deletion_operations",
                    "task_append_operations",
                ):
                    await owner.execute(
                        text(
                            f"DELETE FROM public.{table} WHERE dataset_id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": datasets},
                    )
                await owner.execute(text("SET LOCAL app.allow_audit_update='true'"))
                await owner.execute(
                    text(
                        "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [world.actor_id, world.other_actor_id]},
                )
                for dataset in extras:
                    await owner.execute(
                        text("DELETE FROM public.datasets WHERE id=:id"),
                        {"id": dataset},
                    )
            await _cleanup_world(world)
        _remove_worker_image(image, identity)


def _picture(color="white"):
    output = BytesIO()
    image = Image.new("RGB", (11, 19), color=color)
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


async def _initialize(
    stack, dataset_id, *, key=None, name="image.jpg", actor="employee"
):
    key = key or uuid4()
    reply = await stack.api.post(
        f"/api/v1/datasets/{dataset_id}/items/upload-init",
        headers={**stack.headers[actor], "Idempotency-Key": str(key)},
        json={"file_name": name, "content_type": "image/jpeg"},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["item_id"] == str(key)
    assert reply.headers["Idempotency-Key"] == str(key)
    return reply.json()


async def _put(url, body):
    async with httpx.AsyncClient(trust_env=False) as client:
        reply = await client.put(
            url, content=body, headers={"Content-Type": "image/jpeg"}
        )
        assert reply.status_code == 200, reply.text


async def _complete(stack, dataset_id, item_id, *, key=None, actor="employee"):
    return await stack.api.post(
        f"/api/v1/datasets/{dataset_id}/items/upload-complete/{item_id}",
        headers={**stack.headers[actor], "Idempotency-Key": str(key or uuid4())},
    )


async def test_independent_source_publication_legacy_replay_deletion_and_switching(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.db.models.dataset import Dataset, DatasetItem

    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        world, storage = stack.world, stack.storage
        async with httpx.AsyncClient(trust_env=False) as direct:
            denied = await direct.post(stack.origin + "/v1/source/upload-init", json={})
            assert denied.status_code == 401
        isolated = await asyncio.to_thread(
            _docker,
            "exec",
            stack.container,
            "python",
            "-c",
            "import importlib.util; assert all(importlib.util.find_spec(x) is None for x in ('app','sqlalchemy','celery','redis','torch')); from importlib.resources import files; assert files('aap_assets_source').joinpath('protocol.schema.json').is_file()",
        )
        assert isolated == ""
        initialized = await _initialize(stack, world.dataset_id)
        item = initialized["item_id"]
        await _put(initialized["upload_url"], _picture())
        wrong_actor = await _complete(stack, world.dataset_id, item, actor="manager")
        assert wrong_actor.status_code == 403
        operation = uuid4()
        async with _lost_response_proxy(stack.origin) as proxy:
            monkeypatch.setattr(settings, "assets_source_url", proxy)
            lost = await _complete(stack, world.dataset_id, item, key=operation)
            assert lost.status_code == 503
        monkeypatch.setattr(settings, "assets_source_url", stack.origin)
        accepted = await _complete(stack, world.dataset_id, item, key=operation)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"status": "ok", "item_id": item, "linked_tasks": 1}
        assert accepted.headers["Idempotency-Key"] == str(operation)
        stack.dispatcher.dispatch_thumbnail.assert_called_once()
        async with world.maker() as owner:
            metadata = (
                await owner.execute(
                    text(
                        "SELECT file_path,width,height,file_size FROM public.dataset_items WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": item},
                )
            ).one()
            assert (metadata.width, metadata.height, metadata.file_size) == (
                19,
                11,
                len(_picture()),
            )
            assert (
                await owner.scalar(
                    text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                    {"id": world.project_id},
                )
                == 1
            )
        assert (
            storage.client.get_object(
                Bucket=storage.datasets_bucket, Key=metadata.file_path
            )["Body"].read()
            == _picture()
        )
        retained = await stack.api.delete(
            f"/api/v1/datasets/{world.dataset_id}/items/{item}",
            headers=stack.headers["manager"],
        )
        assert (
            retained.status_code == 409
            and retained.json()["detail"]["code"] == "source_in_use"
        )
        duplicate = await _initialize(stack, world.dataset_id)
        await _put(duplicate["upload_url"], _picture())
        duplicate_reply = await _complete(stack, world.dataset_id, duplicate["item_id"])
        assert (
            duplicate_reply.status_code == 409
            and duplicate_reply.json()["detail"]["duplicate_of"] == item
        )
        missing = await _initialize(stack, world.dataset_id, name="missing.jpg")
        missing_reply = await _complete(stack, world.dataset_id, missing["item_id"])
        assert missing_reply.status_code == 400, missing_reply.text
        abandoned = await _initialize(stack, world.dataset_id, name="withdrawn.jpg")
        cancelled = await stack.api.delete(
            f"/api/v1/datasets/{world.dataset_id}/items/{abandoned['item_id']}",
            headers=stack.headers["manager"],
        )
        assert cancelled.status_code == 204, cancelled.text
        withdrawn = await _complete(stack, world.dataset_id, abandoned["item_id"])
        assert (
            withdrawn.status_code == 409
            and withdrawn.json()["detail"]["reason"] == "upload_not_pending"
        )
        legacy_id, legacy_operation = uuid4(), uuid4()
        legacy_key = f"legacy-source-runtime/{legacy_id}.jpg"
        storage.client.put_object(
            Bucket=storage.datasets_bucket,
            Key=legacy_key,
            Body=_picture("blue"),
            ContentType="image/jpeg",
        )
        async with world.maker.begin() as owner:
            owner.add(
                DatasetItem(
                    id=legacy_id,
                    dataset_id=world.dataset_id,
                    file_name="legacy.jpg",
                    file_path=legacy_key,
                    file_type="image",
                )
            )
            await owner.execute(
                text("UPDATE public.datasets SET file_count=file_count+1 WHERE id=:id"),
                {"id": world.dataset_id},
            )
        # Legacy completion is not attributed to an invented historical uploader.
        async with _lost_response_proxy(stack.origin) as proxy:
            monkeypatch.setattr(settings, "assets_source_url", proxy)
            lost = await _complete(
                stack,
                world.dataset_id,
                legacy_id,
                key=legacy_operation,
                actor="manager",
            )
            assert lost.status_code == 503
        monkeypatch.setattr(settings, "assets_source_url", stack.origin)
        legacy = await _complete(
            stack, world.dataset_id, legacy_id, key=legacy_operation, actor="manager"
        )
        assert legacy.status_code == 200 and legacy.json()["linked_tasks"] == 1
        async with world.maker() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.dataset_upload_intents WHERE item_id=:id"
                    ),
                    {"id": legacy_id},
                )
                == 0
            )
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.dataset_legacy_upload_operations WHERE item_id=:id"
                    ),
                    {"id": legacy_id},
                )
                == 1
            )
        monkeypatch.setattr(settings, "assets_source_url", "")
        local = await _complete(
            stack, world.dataset_id, legacy_id, key=legacy_operation, actor="manager"
        )
        assert local.status_code == 200 and local.json() == legacy.json()
        monkeypatch.setattr(settings, "assets_source_url", stack.origin)
        extra = uuid4()
        stack.extras.add(extra)
        async with world.maker.begin() as owner:
            owner.add(
                Dataset(
                    id=extra,
                    display_id=f"D-SRC-{uuid4().hex[:10]}",
                    name=f"source-extra-{extra}",
                    data_type="image",
                    created_by=world.actor_id,
                )
            )
        extra_init = await _initialize(stack, extra)
        await _put(extra_init["upload_url"], _picture())
        assert (await _complete(stack, extra, extra_init["item_id"])).status_code == 200
        async with world.maker() as owner:
            original_key = await owner.scalar(
                text(
                    "SELECT file_path FROM public.dataset_items WHERE id=CAST(:id AS uuid)"
                ),
                {"id": extra_init["item_id"]},
            )
        item_delete_key, dataset_delete_key = uuid4(), uuid4()
        for _ in range(2):
            deleted = await stack.api.delete(
                f"/api/v1/datasets/{extra}/items/{extra_init['item_id']}",
                headers={
                    **stack.headers["manager"],
                    "Idempotency-Key": str(item_delete_key),
                },
            )
            assert deleted.status_code == 204, deleted.text
        assert (
            storage.client.get_object(Bucket=storage.datasets_bucket, Key=original_key)[
                "Body"
            ].read()
            == _picture()
        )
        for _ in range(2):
            deleted = await stack.api.delete(
                f"/api/v1/datasets/{extra}",
                headers={
                    **stack.headers["manager"],
                    "Idempotency-Key": str(dataset_delete_key),
                },
            )
            assert deleted.status_code == 204, deleted.text
        scoped = boto3.client(
            "s3",
            endpoint_url=f"http://{settings.minio_endpoint}",
            aws_access_key_id=stack.values["ASSETS_SOURCE_S3_ACCESS_KEY"],
            aws_secret_access_key=stack.values["ASSETS_SOURCE_S3_SECRET_KEY"],
            region_name="us-east-1",
        )
        for command in (
            lambda: scoped.delete_object(
                Bucket=storage.datasets_bucket, Key=original_key
            ),
            lambda: scoped.put_object(
                Bucket=storage.datasets_bucket, Key=legacy_key, Body=b"overwrite"
            ),
            lambda: scoped.list_objects_v2(Bucket=storage.import_bucket),
            lambda: scoped.get_object(
                Bucket=storage.media_cache_bucket, Key="image-pyramids/forbidden"
            ),
        ):
            with pytest.raises(ClientError) as error:
                command()
            assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
        assert (
            scoped.list_objects_v2(Bucket=storage.datasets_bucket, MaxKeys=1)[
                "ResponseMetadata"
            ]["HTTPStatusCode"]
            == 200
        )
        scoped.close()
        bad_values = {
            **stack.values,
            "ASSETS_SOURCE_DATABASE_URL": _container_database_url(
                make_url(_required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"))
            ),
        }
        bad_env = stack.directory / "broad.env"
        _write_environment(bad_env, bad_values)
        broad = await asyncio.to_thread(
            _start_source, stack.image, stack.identity, bad_env, "-broad"
        )
        stack.containers.append(broad)
        await _assert_startup_rejected(broad)
        await asyncio.to_thread(_docker, "stop", "--time", "15", stack.container)
        assert (
            await _complete(stack, world.dataset_id, item, key=operation)
        ).status_code == 503
        await asyncio.to_thread(_docker, "start", stack.container)
        await _wait_healthy(stack.container)
        origin = _source_origin(stack.container, stack.image, stack.identity)
        await _wait_public_ready(origin)
        monkeypatch.setattr(settings, "assets_source_url", origin)
        assert (
            await _complete(stack, world.dataset_id, item, key=operation)
        ).json() == accepted.json()


async def test_source_process_loss_preserves_staging_and_single_publication_owner(
    test_engine, app_module, monkeypatch
):
    from app.config import settings

    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        world = stack.world
        initialized = await _initialize(stack, world.dataset_id)
        item, operation = initialized["item_id"], uuid4()
        await _put(initialized["upload_url"], _picture())
        function = "aap_assets_v1.complete_dataset_upload"
        original = "aap_assets_v1._source_acceptance_original_complete"
        signature = "uuid,uuid,uuid,uuid,bigint,text,integer,integer"
        lock_key = int(uuid4().hex[:15], 16)
        gate = await test_engine.connect()
        request_task = None
        wrapped = False
        try:
            async with test_engine.begin() as owner:
                await owner.exec_driver_sql(
                    f"ALTER FUNCTION {function}({signature}) RENAME TO _source_acceptance_original_complete"
                )
                await owner.exec_driver_sql(
                    f"REVOKE EXECUTE ON FUNCTION {original}({signature}) FROM asset_source_client"
                )
                await owner.exec_driver_sql(
                    f"CREATE FUNCTION {function}(p_actor uuid,p_dataset uuid,p_item uuid,p_publication uuid,p_size bigint,p_hash text,p_width integer,p_height integer) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN PERFORM pg_advisory_xact_lock({lock_key}); RETURN {original}(p_actor,p_dataset,p_item,p_publication,p_size,p_hash,p_width,p_height); END $$"
                )
                await owner.exec_driver_sql(
                    f"REVOKE ALL ON FUNCTION {function}({signature}) FROM PUBLIC"
                )
                await owner.exec_driver_sql(
                    f"ALTER FUNCTION {function}({signature}) OWNER TO assets_code"
                )
                await owner.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {function}({signature}) TO asset_source_client"
                )
            wrapped = True
            await gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
            request_task = asyncio.create_task(
                _complete(stack, world.dataset_id, item, key=operation)
            )
            deadline = asyncio.get_running_loop().time() + 15
            while True:
                if request_task.done():
                    pytest.fail(
                        f"source operation did not reach the controlled SQL gate: {(await request_task).text}"
                    )
                async with test_engine.connect() as observer:
                    waiting = await observer.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='asset_source_client' AND wait_event='advisory')"
                        )
                    )
                if waiting:
                    break
                assert asyncio.get_running_loop().time() < deadline, (
                    "publication did not reach controlled gate"
                )
                await asyncio.sleep(0.03)
            prefix = f"dataset-sources/{world.dataset_id}/{item}/"
            objects = stack.storage.client.list_objects_v2(
                Bucket=stack.storage.datasets_bucket, Prefix=prefix
            ).get("Contents", [])
            assert len(objects) == 1
            orphan_key = objects[0]["Key"]
            # Copy has finished, but no SQL completion reply or COMMIT has reached the service.
            await asyncio.to_thread(
                _docker, "kill", "--signal", "KILL", stack.container
            )
            await gate.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            interrupted = await asyncio.wait_for(request_task, timeout=15)
            assert interrupted.status_code == 503
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                async with test_engine.connect() as observer:
                    active = await observer.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='asset_source_client')"
                        )
                    )
                if not active:
                    break
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.03)
            async with world.maker() as owner:
                assert (
                    await owner.scalar(
                        text(
                            "SELECT count(*) FROM public.dataset_items WHERE id=CAST(:id AS uuid)"
                        ),
                        {"id": item},
                    )
                    == 0
                )
                assert (
                    await owner.scalar(
                        text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                        {"id": world.project_id},
                    )
                    == 0
                )
                assert (
                    await owner.scalar(
                        text(
                            "SELECT state FROM public.dataset_upload_intents WHERE item_id=CAST(:id AS uuid)"
                        ),
                        {"id": item},
                    )
                    == "pending"
                )
            await asyncio.to_thread(_docker, "start", stack.container)
            await _wait_healthy(stack.container)
            origin = _source_origin(stack.container, stack.image, stack.identity)
            await _wait_public_ready(origin)
            monkeypatch.setattr(settings, "assets_source_url", origin)
            completed = await _complete(stack, world.dataset_id, item, key=operation)
            assert completed.status_code == 200, completed.text
            assert completed.json()["linked_tasks"] == 1
            async with world.maker() as owner:
                final_key = await owner.scalar(
                    text(
                        "SELECT file_path FROM public.dataset_items WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": item},
                )
                assert (
                    await owner.scalar(
                        text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                        {"id": world.project_id},
                    )
                    == 1
                )
                assert (
                    await owner.scalar(
                        text("SELECT total_tasks FROM public.projects WHERE id=:id"),
                        {"id": world.project_id},
                    )
                    == 1
                )
            assert final_key != orphan_key
            for key in (orphan_key, final_key):
                assert (
                    stack.storage.client.get_object(
                        Bucket=stack.storage.datasets_bucket, Key=key
                    )["Body"].read()
                    == _picture()
                )
        finally:
            await gate.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            await gate.close()
            if request_task is not None and not request_task.done():
                await asyncio.wait_for(request_task, timeout=15)
            if wrapped:
                async with test_engine.begin() as owner:
                    await owner.exec_driver_sql(
                        f"DROP FUNCTION {function}({signature})"
                    )
                    await owner.exec_driver_sql(
                        f"ALTER FUNCTION {original}({signature}) RENAME TO complete_dataset_upload"
                    )
                    await owner.exec_driver_sql(
                        f"GRANT EXECUTE ON FUNCTION {function}({signature}) TO asset_source_client"
                    )
