from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_assets_catalog import (
    CatalogItem,
    CreateDatasetRequest,
    DatasetPatch,
    DatasetMutationResult,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
)
from yoao_assets_images.contract import ImageOwner, ImageSource, ObjectIdentity
from yoao_assets_images.transport import ImageSourceReference
from yoao_assets_media.contract import MediaSource
from yoao_assets_media.transport import SourceReference
from app.services import assets_catalog
from app.api.v1 import datasets as dataset_routes
from app.schemas.dataset import DatasetCreate, DatasetUpdate
from tests.identity_e2e_support import (
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
)


def _dataset_payload(
    dataset_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str = "sample",
) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": str(dataset_id),
        "display_id": "D-123",
        "name": name,
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "has_scenes": False,
        "file_count": 0,
        "total_size": 0,
        "created_by": str(actor_id),
        "project_count": 0,
        "axis_convention": None,
        "created_at": now,
        "updated_at": now,
    }


def _reply(data: dict) -> dict:
    return {"schema": "aap-assets-catalog/v1", "ok": True, "data": data}


def _set_sql_reply(db: AsyncMock, payload: dict) -> None:
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: payload)


@pytest.mark.asyncio
async def test_local_list_uses_native_capability_on_the_caller_session():
    actor_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    _set_sql_reply(db, _reply({"items": [], "total": 0, "limit": 7, "offset": 11}))

    result = await assets_catalog.AssetsCatalogGateway(db).list(
        ListDatasetsRequest(
            actor_id=actor_id,
            search="camera_100%",
            data_type="lidar",
            has_scenes=False,
            limit=7,
            offset=11,
        )
    )

    assert result.total == 0
    statement = str(db.execute.await_args.args[0])
    assert "yoao_assets_v1.list_catalog_datasets" in statement
    assert db.execute.await_args.args[1] == {
        "actor_id": actor_id,
        "search": "camera_100%",
        "data_type": "lidar",
        "has_scenes": False,
        "limit": 7,
        "offset": 11,
    }
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_create_commits_receipt_and_native_default_payload():
    operation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    _set_sql_reply(
        db,
        _reply(
            {
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset": _dataset_payload(dataset_id, actor_id),
            }
        ),
    )

    result = await assets_catalog.AssetsCatalogGateway(db).create(
        CreateDatasetRequest(
            operation_id=operation_id,
            actor_id=actor_id,
            name="sample",
        )
    )

    assert result.operation_id == operation_id
    db.commit.assert_awaited_once()
    parameters = db.execute.await_args.args[1]
    assert parameters["operation_id"] == operation_id
    assert json.loads(parameters["payload"]) == {
        "name": "sample",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }


@pytest.mark.asyncio
async def test_local_update_keeps_explicit_null_and_omitted_fields_distinct():
    operation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    _set_sql_reply(
        db,
        _reply(
            {
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset": _dataset_payload(dataset_id, actor_id),
            }
        ),
    )

    await assets_catalog.AssetsCatalogGateway(db).update(
        UpdateDatasetRequest(
            operation_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            patch=DatasetPatch.model_validate({"axis_convention": None}),
        )
    )

    assert json.loads(db.execute.await_args.args[1]["payload"]) == {
        "axis_convention": None
    }
    db.commit.assert_awaited_once()


def _select_remote(monkeypatch, handler):
    monkeypatch.setattr(
        assets_catalog.settings, "assets_catalog_url", "http://catalog.local"
    )
    monkeypatch.setattr(assets_catalog.settings, "assets_catalog_token", "s" * 40)
    original_client = httpx.AsyncClient

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(assets_catalog.httpx, "AsyncClient", make_client)


@pytest.mark.asyncio
async def test_remote_get_authenticates_and_never_queries_local_session(monkeypatch):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/catalog/get"
        assert request.url.query == b""
        assert request.headers["Authorization"] == "Bearer " + "s" * 40
        assert json.loads(request.content) == {
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
        }
        return httpx.Response(
            200,
            json=_reply({"dataset": _dataset_payload(dataset_id, actor_id)}),
        )

    _select_remote(monkeypatch, handler)
    result = await assets_catalog.AssetsCatalogGateway(db).get(
        GetDatasetRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.dataset.id == dataset_id
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_failure_does_not_fall_back_to_local_sql(monkeypatch):
    db = AsyncMock(spec=AsyncSession)
    _select_remote(
        monkeypatch,
        lambda request: httpx.Response(503, json={"error": "private upstream detail"}),
    )

    with pytest.raises(assets_catalog.CatalogGatewayError) as failure:
        await assets_catalog.AssetsCatalogGateway(db).list(
            ListDatasetsRequest(actor_id=uuid.uuid4())
        )

    assert failure.value.status_code == 503
    assert failure.value.detail == "Assets catalog service is unavailable"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_rejects_mismatched_operation_receipt(monkeypatch):
    actor_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    _select_remote(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=_reply(
                {
                    "operation_id": str(uuid.uuid4()),
                    "actor_id": str(actor_id),
                    "dataset": _dataset_payload(uuid.uuid4(), actor_id),
                }
            ),
        ),
    )

    with pytest.raises(assets_catalog.CatalogGatewayError) as failure:
        await assets_catalog.AssetsCatalogGateway(db).create(
            CreateDatasetRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                name="sample",
            )
        )

    assert failure.value.status_code == 502
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_update_preserves_omitted_patch_fields(monkeypatch):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["patch"] == {"description": "updated"}
        return httpx.Response(
            200,
            json=_reply(
                {
                    "operation_id": str(operation_id),
                    "actor_id": str(actor_id),
                    "dataset": _dataset_payload(dataset_id, actor_id),
                }
            ),
        )

    _select_remote(monkeypatch, handler)
    result = await assets_catalog.AssetsCatalogGateway(db).update(
        UpdateDatasetRequest(
            operation_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            patch=DatasetPatch.model_validate({"description": "updated"}),
        )
    )

    assert result.dataset.id == dataset_id
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_stream_wait_keeps_event_loop_responsive(monkeypatch):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    started = asyncio.Event()
    release = asyncio.Event()
    payload = json.dumps(
        _reply({"dataset": _dataset_payload(dataset_id, actor_id)})
    ).encode()

    class DelayedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await release.wait()
            yield payload

        async def aclose(self):
            return None

    _select_remote(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=DelayedBody(),
        ),
    )
    pending = asyncio.create_task(
        assets_catalog.AssetsCatalogGateway(db).get(
            GetDatasetRequest(actor_id=actor_id, dataset_id=dataset_id)
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    assert (await pending).dataset.id == dataset_id


@pytest.mark.asyncio
async def test_encoded_request_budget_applies_before_any_native_or_remote_call():
    db = AsyncMock(spec=AsyncSession)
    request = CreateDatasetRequest(
        operation_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        name="sample",
        description="x" * (assets_catalog._MAX_REQUEST_BYTES + 1),
    )

    with pytest.raises(assets_catalog.CatalogGatewayError) as failure:
        await assets_catalog.AssetsCatalogGateway(db).create(request)

    assert failure.value.status_code == 422
    db.execute.assert_not_awaited()


def test_public_dataset_name_and_type_match_native_column_bounds():
    with pytest.raises(ValidationError):
        DatasetCreate(name="x" * 256)
    with pytest.raises(ValidationError):
        DatasetCreate(name="valid", data_type="x" * 31)
    with pytest.raises(ValidationError):
        DatasetUpdate(name="x" * 256)


@pytest.mark.asyncio
async def test_create_route_uses_authenticated_actor_and_echoes_selected_key(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    untrusted_actor_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    result = DatasetMutationResult(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset=_dataset_payload(dataset_id, actor_id),
    )
    gateway = MockGateway(result)
    monkeypatch.setattr(dataset_routes, "AssetsCatalogGateway", lambda db: gateway)
    response = Response()

    await dataset_routes.create_dataset(
        data=DatasetCreate.model_validate(
            {
                "name": "sample",
                "actor_id": str(untrusted_actor_id),
                "role": "super_admin",
            }
        ),
        response=response,
        db=AsyncMock(spec=AsyncSession),
        current_user=SimpleNamespace(id=actor_id),
        idempotency_key=operation_id,
    )

    request = gateway.create.await_args.args[0]
    assert request.actor_id == actor_id
    assert request.operation_id == operation_id
    assert response.headers["Idempotency-Key"] == str(operation_id)


class MockGateway:
    def __init__(self, result: DatasetMutationResult):
        self.create = AsyncMock(return_value=result)


def _catalog_item(
    *, item_id: uuid.UUID, dataset_id: uuid.UUID, file_type: str, thumbnail_url: str
) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name=f"{item_id}.{'mp4' if file_type == 'video' else 'jpg'}",
        file_path=f"source/{item_id}",
        file_type=file_type,
        file_size=1,
        content_hash=None,
        width=16,
        height=9,
        metadata={},
        file_url="upstream:file",
        thumbnail_url=thumbnail_url,
        blurhash="stale-blurhash" if file_type == "video" else "image-blurhash",
        created_at=datetime.now(UTC),
        thumbnail_path=f"media-assets/old/{item_id}.webp",
    )


def _video_source_reference(
    item_id: uuid.UUID,
    *,
    poster_url: str | None,
    materialized_generation: int = 2,
):
    return SourceReference(
        source=MediaSource(
            dataset_item_id=item_id,
            dataset_id=uuid.uuid4(),
            source_generation=2,
            source_snapshot="a" * 64,
            timetable_revision=0,
            materialized_generation=materialized_generation,
            object_identity=None,
            source_drifted=False,
            file_name=f"{item_id}.mp4",
            file_path=f"source/{item_id}",
            file_type="video",
            file_size=1,
            content_hash=None,
            width=16,
            height=9,
            thumbnail_path=None,
            blurhash=None,
            video_metadata={},
        ),
        original_url=f"https://browser.test/source/{item_id}",
        video_url=f"https://browser.test/source/{item_id}",
        poster_url=poster_url,
        expires_in=3600,
    )


def _image_source_reference(
    item_id: uuid.UUID,
    dataset_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    file_path: str | None = None,
    thumbnail_url: str | None = "https://browser.test/verified-image-thumbnail",
) -> ImageSourceReference:
    return ImageSourceReference(
        actor_id=actor_id,
        source=ImageSource(
            owner=ImageOwner(kind="dataset_item", id=item_id),
            task_id=None,
            project_id=None,
            dataset_id=dataset_id,
            item_id=item_id,
            source_generation=2,
            source_snapshot="b" * 64,
            object_identity=ObjectIdentity(etag="source-etag", byte_size=1),
            source_drifted=False,
            upload_ready=True,
            file_path=file_path or f"source/{item_id}",
            file_name=f"{item_id}.jpg",
            file_type="image",
            bucket_kind="datasets",
            file_size=1,
            content_hash=None,
            width=16,
            height=9,
            thumbnail_path="media-assets/derived/image.webp",
            blurhash="verified-image-blurhash",
            thumbnail_source_generation=2,
            thumbnail_error=None,
        ),
        original_url=f"https://browser.test/image/{item_id}",
        thumbnail_url=thumbnail_url,
        expires_in=3600,
    )


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.asyncio
async def test_catalog_video_links_always_use_owner_reference(monkeypatch, remote):
    actor_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    video_ids = [uuid.uuid4() for _ in range(3)]
    image_id = uuid.uuid4()
    page = ItemPage(
        dataset_id=dataset_id,
        items=[
            _catalog_item(
                item_id=video_ids[0],
                dataset_id=dataset_id,
                file_type="video",
                thumbnail_url="upstream:stale-video-poster",
            ),
            _catalog_item(
                item_id=image_id,
                dataset_id=dataset_id,
                file_type="image",
                thumbnail_url="upstream:image-poster",
            ),
            _catalog_item(
                item_id=video_ids[1],
                dataset_id=dataset_id,
                file_type="video",
                thumbnail_url="upstream:other-stale-poster",
            ),
            _catalog_item(
                item_id=video_ids[2],
                dataset_id=dataset_id,
                file_type="video",
                thumbnail_url="upstream:third-stale-poster",
            ),
        ],
        total=4,
        limit=50,
        offset=0,
    )
    db = AsyncMock(spec=AsyncSession)
    gateway = assets_catalog.AssetsCatalogGateway(db)

    async def fake_call(*_args):
        return page, remote

    monkeypatch.setattr(gateway, "_call", fake_call)
    signed = []

    def sign(key, *, bucket):
        signed.append(key)
        return f"signed:{key}"

    monkeypatch.setattr(assets_catalog.storage_service, "generate_download_url", sign)
    requests = []

    async def owner_sources(_db, source_requests):
        requests.extend(source_requests)
        return [
            _video_source_reference(
                video_ids[0], poster_url="https://browser.test/verified-poster"
            ),
            _video_source_reference(
                video_ids[1],
                poster_url=None,
                materialized_generation=1,
            ),
            None,
        ]

    monkeypatch.setattr(assets_catalog, "video_source_references", owner_sources)
    image_requests = []

    async def owner_image_sources(_db, source_requests):
        image_requests.extend(source_requests)
        return [_image_source_reference(image_id, dataset_id, actor_id=actor_id)]

    monkeypatch.setattr(assets_catalog, "image_source_references", owner_image_sources)

    result = await gateway.items(
        ListItemsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert [request.dataset_item_id for request in requests] == video_ids
    assert all(
        request.actor_id == actor_id and request.task_id is None for request in requests
    )
    assert len(image_requests) == 1
    assert image_requests[0].actor_id == actor_id
    assert image_requests[0].dataset_item_id == image_id
    assert result.items[0].file_url == (f"https://browser.test/source/{video_ids[0]}")
    assert result.items[0].thumbnail_url == "https://browser.test/verified-poster"
    assert result.items[0].blurhash is None
    assert result.items[2].file_url == (f"https://browser.test/source/{video_ids[1]}")
    assert result.items[2].thumbnail_url is None
    assert result.items[2].blurhash is None
    assert result.items[1].file_url == f"https://browser.test/image/{image_id}"
    assert result.items[1].thumbnail_url == (
        "https://browser.test/verified-image-thumbnail"
    )
    assert result.items[1].blurhash == "verified-image-blurhash"
    assert result.items[3].file_url is None
    assert result.items[3].thumbnail_url is None
    assert result.items[3].blurhash is None
    if not remote:
        assert f"media-assets/old/{video_ids[0]}.webp" not in signed
        assert f"media-assets/old/{video_ids[1]}.webp" not in signed
        assert f"media-assets/old/{video_ids[2]}.webp" not in signed
        assert f"source/{video_ids[0]}" not in signed
        assert f"source/{video_ids[1]}" not in signed
        assert f"source/{video_ids[2]}" not in signed


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.asyncio
async def test_catalog_image_links_withhold_unverified_urls_without_storage_fallback(
    monkeypatch, remote, mismatch
):
    actor_id, dataset_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = _catalog_item(
        item_id=item_id,
        dataset_id=dataset_id,
        file_type="image",
        thumbnail_url="upstream:stale-image-thumbnail",
    )
    page = ItemPage(
        dataset_id=dataset_id,
        items=[item],
        total=1,
        limit=50,
        offset=0,
    )
    gateway = assets_catalog.AssetsCatalogGateway(AsyncMock(spec=AsyncSession))

    async def fake_call(*_args):
        return page, remote

    monkeypatch.setattr(gateway, "_call", fake_call)
    signed = []
    monkeypatch.setattr(
        assets_catalog.storage_service,
        "generate_download_url",
        lambda key, *, bucket: signed.append((key, bucket)) or "unsafe:direct-sign",
    )

    async def missing_image_reference(_db, requests):
        assert len(requests) == 1
        assert requests[0].actor_id == actor_id
        if mismatch:
            return [
                _image_source_reference(
                    item_id,
                    dataset_id,
                    actor_id=actor_id,
                    file_path="source/changed",
                )
            ]
        return [None]

    monkeypatch.setattr(
        assets_catalog, "image_source_references", missing_image_reference
    )

    result = await gateway.items(
        ListItemsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.items[0].file_url is None
    assert result.items[0].thumbnail_url is None
    assert result.items[0].thumbnail_path is None
    assert result.items[0].blurhash is None
    assert signed == []


_RETAINED_BACKFILL_RUNTIME = []


def _backfill_worker_scope(resources, store):
    """Use only the fixture test's existing cache-output storage domain."""
    import secrets
    from types import SimpleNamespace

    from app.config import settings
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=store)
    )
    scope["policy"] = "fixture-backfill-writer-" + secrets.token_hex(8)
    cache = f"arn:aws:s3:::{resources['buckets']['MINIO_MEDIA_CACHE_BUCKET']}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
                "Resource": [f"{cache}/thumbnails/objects/*"],
            }
        ],
    }
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or not os.environ.get("AAP_WORKSPACE_MODULE_API_IMAGE"),
    reason="requires the owned verifier and qualified installed API worker",
)
@pytest.mark.parametrize(
    ("route", "purpose"),
    [("backfill-dimensions", "dimensions"), ("backfill-media", "thumbnail")],
)
async def test_dataset_backfill_accepts_cutoff_and_retires_real_delivery(
    fenced_http, test_engine, monkeypatch, tmp_path, route, purpose
):
    import hashlib
    import secrets
    import subprocess
    import sys
    import tempfile
    from contextlib import closing
    from pathlib import Path

    import boto3
    from botocore.config import Config
    from celery import Celery
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings
    from app.services import image_pyramid_originals as originals

    from tests.identity_e2e_support import fixture_routing_owner
    from tests.project_guide_asset_owner_helpers import (
        assert_workspace_identity,
        endpoint_url,
    )
    from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
    from tests.seed_owner_support import (
        OwnedSuite,
        _fixture_versions,
        _read_versions,
        fixture_original_reader,
    )
    from tests.test_assets_media_live import _guard_role_url
    from tests.test_fixture_inference_retirement_live import (
        _eventually,
        _login,
        _PreservedSeedOwner,
        _start_media_runtime,
        _stop_media_runtime,
    )
    from tests.test_retry_born_dataset_scope import _admit, _world
    from tests.test_workspace_cross_frame_worker_live import _seed_member_job
    from tests.test_workspace_worker_assembly_live import (
        _delete_owned_queue,
        _wait_broker_ack,
        _wait_worker_ready,
    )
    from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

    root = Path(__file__).resolve().parents[3]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import fixture_media_control
    from fixture_suite_coordinator import _resource_root, _verified_database, state_path
    from verify_module_data_boundary import _destroy_cleanup_storage_scope
    from verify_playwright_fixture_teardown import (
        observe_native_absence,
        observe_native_birth,
    )
    from yoao_assets_media_runtime.fixture_native_outputs import load_target_file

    publication_url = (
        _guard_role_url(
            "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
            "yoao_jobs_publication_client",
            test_engine,
        )
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    monkeypatch.setattr(settings, "jobs_publication_database_url", publication_url)
    monkeypatch.setattr(settings, "jobs_publication_database_url_file", "")
    monkeypatch.setenv("JOBS_PUBLICATION_DATABASE_URL", publication_url)
    monkeypatch.setenv("JOBS_PUBLICATION_DATABASE_URL_FILE", "")
    monkeypatch.setattr(settings, "jobs_queue_url", "")
    routing = fixture_routing_owner(test_engine, monkeypatch)
    owner = suite = media = worker = sender = scope = store = output_store = (
        owner_log
    ) = None
    run = neighbor = job_id = None
    completed = False
    try:
        await routing.__aenter__()
        media = await _start_media_runtime(test_engine, tmp_path)
        media_url = str(media.control_client.base_url).rstrip("/")
        monkeypatch.setattr(settings, "assets_media_url", media_url)
        monkeypatch.setattr(settings, "assets_media_token", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_URL", media_url)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN_FILE", "")
        suite_id = uuid.uuid4()
        owner_environment = {
            **os.environ,
            "AAP_PRIVATE_FIXTURE_AUTHORITY_NATIVE": "1",
            "AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE": str(suite_id),
            "PLAYWRIGHT_FIXTURE_RUN_ID": str(suite_id),
        }
        owner_log = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [sys.executable, str(root / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=root / "apps/api",
            env=owner_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_log,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(process, owner_log)
        suite = OwnedSuite(owner, suite_id)
        await owner.verify_suite(suite_id, "owned")
        run = suite.new_run("bf" + uuid.uuid4().hex[:8])
        neighbor = suite.new_run("nb" + uuid.uuid4().hex[:8])
        world = await _build_owned(owner, run)
        await _build_owned(owner, neighbor)
        async with test_engine.connect() as db:
            dataset_id = await db.scalar(
                text(
                    "SELECT item.dataset_id FROM yoao_labeling_v1.task_rows AS task "
                    "JOIN public.dataset_items AS item ON item.id=task.dataset_item_id "
                    "WHERE task.id=:task"
                ),
                {"task": uuid.UUID(world["task_ids"][0])},
            )
        assert dataset_id is not None
        # Two ordinary Jobs share the database with both fixture runs. Neither
        # may be treated as settled or removed by retirement of the backfill.
        neighbors = async_sessionmaker(test_engine, expire_on_commit=False)
        cross_frame = await _seed_member_job(neighbors, label="backfill-neighbor")
        async with neighbors.begin() as db:
            retry = await _world(db)
            retry_id = await _admit(db, retry)
            scope_query = text(
                "SELECT j.id,b.dataset_ids FROM public.async_jobs j "
                "JOIN yoao_jobs_v1.fixture_worker_job_datasets b ON b.job_id=j.id "
                "WHERE j.id=ANY(:ids) AND j.status='pending' ORDER BY j.id"
            )
            neighbor_ids = {"ids": [cross_frame["job_id"], retry_id]}
            original_scopes = dict((await db.execute(scope_query, neighbor_ids)).all())
            assert original_scopes[retry_id] == [retry.dataset.id]
            cross_dataset = await db.scalar(
                text("SELECT dataset_id FROM public.scenes WHERE id=:scene"),
                {"scene": cross_frame["scene_id"]},
            )
            assert original_scopes[cross_frame["job_id"]] == [cross_dataset]
            assert dataset_id not in {retry.dataset.id, cross_dataset}
        seed_payload = {**run.payload(), "dataset_id": str(dataset_id)}
        before = await owner.post(
            "/api/v1/__test/seed/backfill-image", json=seed_payload
        )
        assert before.status_code == 200, before.text
        existing_item = uuid.UUID(before.json()["item_id"])

        secret_path = await fixture_media_control.receipt_path(run.run_id)
        control_url = await fixture_media_control.load_control(run.run_id, secret_path)
        native_target = await fixture_media_control.native_target(run.run_id)
        _, fixture_resources, fixture_identity = await _verified_database()
        owner_path = state_path(
            _resource_root(fixture_resources),
            fixture_resources["mode"],
            "fixture-store-owner.json",
        )
        birth = await asyncio.to_thread(
            observe_native_birth, owner_path, fixture_identity, run.run_id
        )
        target_reader = load_target_file(
            native_target["reader_file"],
            control_url.render_as_string(hide_password=False),
            "aap_fixture_media_",
        )
        output_store = boto3.client(
            "s3",
            endpoint_url=target_reader["endpoint"],
            aws_access_key_id=target_reader["access_key"],
            aws_secret_access_key=target_reader["secret_key"],
            region_name="us-east-1",
            config=Config(signature_version="s3v4"),
        )
        resources, _, store = await assert_workspace_identity(test_engine)
        scope = await asyncio.to_thread(_backfill_worker_scope, resources, store)
        sender = Celery(
            "fixture-backfill-verifier",
            broker=settings.effective_celery_broker,
            backend=settings.effective_celery_broker,
        )
        # Keep the owner's qualified worker alive but prevent it racing the
        # installed worker before the post-cutoff item has been created.
        native_node = f"aap-fixture-{run.run_id.hex}-media@127.0.0.1"
        native_queue = f"aap_workspace_worker_{run.run_id.hex}_media"
        paused = await asyncio.to_thread(
            sender.control.cancel_consumer,
            native_queue,
            destination=[native_node],
            reply=True,
        )
        assert paused and any(
            native_node in reply and "ok" in reply[native_node] for reply in paused
        )
        inspect = sender.control.inspect(timeout=2, destination=[native_node])
        queues = await asyncio.to_thread(inspect.active_queues)
        assert queues and native_node in queues
        assert native_queue not in {row["name"] for row in queues[native_node]}
        headers = await _login(owner, world)
        operation_id = uuid.uuid4()
        headers["Idempotency-Key"] = str(operation_id)
        path = f"/api/v1/datasets/{dataset_id}/{route}"
        missing = await fenced_http.post(
            f"/api/v1/datasets/{uuid.uuid4()}/{route}", headers=headers
        )
        assert missing.status_code == 404, missing.text
        accepted = await fenced_http.post(path, headers=headers)
        assert accepted.status_code == 202, accepted.text
        repeated = await fenced_http.post(path, headers=headers)
        assert repeated.status_code == 202, repeated.text
        assert repeated.json() == accepted.json()
        assert accepted.headers["Idempotency-Key"] == str(operation_id)
        receipt = accepted.json()
        assert receipt["dataset_id"] == str(dataset_id)
        assert receipt["operation_id"] == str(operation_id)
        assert receipt["purpose"] == purpose and receipt["status"] == "pending"
        cutoff = datetime.fromisoformat(receipt["accepted_before"])
        job_id = uuid.UUID(receipt["job_id"])
        other_route = (
            "backfill-media" if purpose == "dimensions" else "backfill-dimensions"
        )
        conflict = await fenced_http.post(
            f"/api/v1/datasets/{dataset_id}/{other_route}", headers=headers
        )
        assert conflict.status_code == 409, conflict.text

        after = await owner.post(
            "/api/v1/__test/seed/backfill-image", json=seed_payload
        )
        assert after.status_code == 200, after.text
        late_item = uuid.UUID(after.json()["item_id"])
        async with test_engine.connect() as db:
            rows = (
                (
                    await db.execute(
                        text(
                            "SELECT id,created_at,width,height,thumbnail_path "
                            "FROM public.dataset_items WHERE id=ANY(:ids)"
                        ),
                        {"ids": [existing_item, late_item]},
                    )
                )
                .mappings()
                .all()
            )
            items = {row["id"]: dict(row) for row in rows}
            assert (
                items[existing_item]["created_at"]
                <= cutoff
                < items[late_item]["created_at"]
            )
            job = (
                (
                    await db.execute(
                        text(
                            "SELECT kind,project_id,payload FROM public.async_jobs WHERE id=:job"
                        ),
                        {"job": job_id},
                    )
                )
                .mappings()
                .one()
            )
            assert job["kind"] == "media_backfill" and job["project_id"] is None
            assert job["payload"]["dataset_id"] == str(dataset_id)
            assert job["payload"]["purpose"] == purpose
            assert job["payload"]["operation_id"] == str(operation_id)
            assert datetime.fromisoformat(job["payload"]["accepted_before"]) == cutoff
            dispatches = (
                (
                    await db.execute(
                        text(
                            "SELECT execution_id,envelope FROM yoao_jobs_v1.dispatch_intents "
                            "WHERE job_id=:job"
                        ),
                        {"job": job_id},
                    )
                )
                .mappings()
                .all()
            )
            assert len(dispatches) == 1
            assert dispatches[0]["execution_id"] == str(job_id)
            envelope = dispatches[0]["envelope"]
            assert envelope["task_name"] == "app.workers.media.run_media_backfill"
            assert envelope["args"] == [str(job_id)]
            assert envelope["options"] == {
                "queue": f"aap_workspace_worker_{run.run_id.hex}_media"
            }

        worker = WorkspaceCeleryWorker(
            test_engine,
            tmp_path / "media-worker",
            backend_token=secrets.token_urlsafe(32),
            object_store_endpoint=endpoint_url(),
            object_store_bucket=resources["buckets"]["MINIO_BUCKET"],
            object_store_use_ssl=endpoint_url().startswith("https://"),
            object_store_access_key=scope["access_key"],
            object_store_secret_key=scope["secret_key"],
            object_store_buckets=resources["buckets"],
            queue_name="fixture-media",
            fixture_run_id=run.run_id,
        )
        await worker.configure_fixture_original_reader(
            media_url=media_url, media_token=media.control_token
        )
        worker.configure_fixture_media_control(
            control_url, output_file=native_target["writer_file"]
        )
        worker.start()
        await _wait_worker_ready(sender, worker)

        async def settled():
            async with test_engine.connect() as db:
                return await db.scalar(
                    text(
                        "SELECT state='settled' FROM yoao_jobs_v1.fixture_media_backfills "
                        "WHERE job_id=:job AND run_id=:run AND dataset_id=:dataset"
                    ),
                    {"job": job_id, "run": run.run_id, "dataset": dataset_id},
                )

        await _eventually("exact backfill execution settled", settled, timeout=180)
        await _wait_broker_ack(sender, worker, job_id)
        async with test_engine.connect() as db:
            status = await db.scalar(
                text("SELECT status FROM public.async_jobs WHERE id=:job"),
                {"job": job_id},
            )
            assert status == "completed"
            observed = (
                (
                    await db.execute(
                        text(
                            "SELECT id,width,height,thumbnail_path FROM public.dataset_items WHERE id=ANY(:ids)"
                        ),
                        {"ids": [existing_item, late_item]},
                    )
                )
                .mappings()
                .all()
            )
        async with test_engine.connect() as db:
            versions = (
                (
                    await db.execute(
                        text(
                            "SELECT version.id,version.object_key,version.version_id,"
                            "version.sha256,version.size_bytes,output.storage_ref->>'bucket' AS bucket "
                            "FROM yoao_assets_v1.fixture_media_versions AS version "
                            "JOIN public.thumbnail_outputs AS output ON output.id=version.owner_id "
                            "WHERE version.run_id=:run AND version.dataset_id=:dataset "
                            "AND version.owner_kind='thumbnail' AND version.state='verified'"
                        ),
                        {"run": run.run_id, "dataset": dataset_id},
                    )
                )
                .mappings()
                .all()
            )
        assert versions
        for version in versions:
            assert version["bucket"] == native_target["bucket"]
            params = {"Bucket": version["bucket"], "Key": version["object_key"]}
            if version["version_id"] is not None:
                params["VersionId"] = version["version_id"]
            response = await asyncio.to_thread(output_store.get_object, **params)
            with closing(response["Body"]) as body:
                payload = await asyncio.to_thread(body.read)
            assert len(payload) == version["size_bytes"]
            assert hashlib.sha256(payload).hexdigest() == version["sha256"]
        observed = {row["id"]: dict(row) for row in observed}
        assert (
            observed[existing_item]["width"],
            observed[existing_item]["height"],
        ) == (64, 48)
        assert observed[existing_item]["thumbnail_path"] is not None
        assert (
            observed[late_item]["width"] is None
            and observed[late_item]["height"] is None
        )
        assert observed[late_item]["thumbnail_path"] is None
        async with test_engine.connect() as db:
            completed_generation = await db.scalar(
                text(
                    "SELECT generation FROM yoao_jobs_v1.fixture_media_backfills WHERE job_id=:job"
                ),
                {"job": job_id},
            )
        mismatch_id = uuid.uuid4()
        sender.send_task(
            "app.workers.media.run_media_backfill",
            args=[str(job_id)],
            queue=worker.queue,
            task_id=str(mismatch_id),
        )
        await _wait_broker_ack(sender, worker, mismatch_id)
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT generation FROM yoao_jobs_v1.fixture_media_backfills WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                == completed_generation
            )

        neighbor_versions = await _fixture_versions(test_engine, neighbor)
        with closing(fixture_original_reader()) as reader:
            neighbor_bytes = await asyncio.to_thread(
                _read_versions, reader, neighbor_versions
            )
            factory = async_sessionmaker(test_engine, expire_on_commit=False)
            async with factory() as db:
                source = await originals.pin(
                    db, "task", uuid.UUID(world["task_ids"][0])
                )
                authority = await originals.delegated(
                    db,
                    originals.system_authorization(
                        "task", uuid.UUID(world["task_ids"][0]), source
                    ),
                )
                await db.rollback()
                async with originals.media(db, authority, source) as stage:
                    async with test_engine.connect() as observer:
                        held = await observer.scalar(
                            text(
                                "SELECT count(*) FROM public.storage_operations "
                                "WHERE purpose=:purpose AND node_id='aap-assets-media'"
                            ),
                            {
                                "purpose": f"media_original_{source.replica_facts.creation_id.hex}"
                            },
                        )
                        generation = await observer.scalar(
                            text(
                                "SELECT generation FROM yoao_jobs_v1.fixture_media_backfills "
                                "WHERE job_id=:job"
                            ),
                            {"job": job_id},
                        )
                    assert held == 1
                    assert os.pread(stage.file.fileno(), 4, 0)
                    blocked = await _cleanup_owned(owner, run)
                    assert blocked.status_code == 409, blocked.text
                    detail = blocked.json()["detail"]
                    assert (detail["code"], detail["reason"]) == (
                        "fixture_prediction_retirement_blocked",
                        "original_reader_pending",
                    )
                    async with test_engine.connect() as observer:
                        assert await observer.scalar(
                            text(
                                "SELECT state='sealing' FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                            ),
                            {"run": run.run_id},
                        )
                    await asyncio.to_thread(sender.AsyncResult(str(job_id)).forget)
                    sender.send_task(
                        "app.workers.media.run_media_backfill",
                        args=[str(job_id)],
                        queue=worker.queue,
                        task_id=str(job_id),
                    )
                    await _wait_broker_ack(sender, worker, job_id)
                    async with test_engine.connect() as observer:
                        assert (
                            await observer.scalar(
                                text(
                                    "SELECT generation FROM yoao_jobs_v1.fixture_media_backfills "
                                    "WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                            == generation
                        )
                        assert (
                            await observer.scalar(
                                text(
                                    "SELECT count(*) FROM public.storage_operations "
                                    "WHERE purpose=:purpose AND node_id='aap-assets-media'"
                                ),
                                {
                                    "purpose": f"media_original_{source.replica_facts.creation_id.hex}"
                                },
                            )
                            == 1
                        )
                    assert (
                        await asyncio.to_thread(
                            _read_versions, reader, neighbor_versions
                        )
                        == neighbor_bytes
                    )
            installed_birth = worker.assert_runtime()
            await asyncio.to_thread(
                worker._docker, "kill", "--signal", "TERM", worker.container_id
            )
            installed_exit = await asyncio.to_thread(
                worker._docker, "wait", worker.container_id, timeout=20
            )
            assert installed_exit.strip() == "0"
            installed_stopped = worker._inspect(worker.container_id)
            worker._verify_container_record(installed_stopped)
            assert (
                installed_stopped["State"]["StartedAt"] == installed_birth["started_at"]
            )
            assert installed_stopped["State"]["Running"] is False
            assert installed_stopped["State"]["ExitCode"] == 0
            worker.stop()
            cleaned = await _cleanup_owned(owner, run)
            assert cleaned.status_code == 200, cleaned.text
            await asyncio.to_thread(
                observe_native_absence, owner_path, fixture_identity, run.run_id, birth
            )
            async with test_engine.connect() as observer:
                assert await observer.scalar(
                    text(
                        "SELECT state='retired' FROM yoao_jobs_v1.fixture_media_backfills "
                        "WHERE job_id=:job AND run_id=:run AND dataset_id=:dataset"
                    ),
                    {"job": job_id, "run": run.run_id, "dataset": dataset_id},
                )
                retirements = dict(
                    (
                        await observer.execute(
                            text(
                                "SELECT version.id,retirement.state "
                                "FROM yoao_assets_v1.fixture_media_versions AS version "
                                "LEFT JOIN yoao_assets_v1.fixture_media_version_retirements AS retirement "
                                "ON retirement.version_id=version.id "
                                "WHERE version.run_id=:run AND version.dataset_id=:dataset"
                            ),
                            {"run": run.run_id, "dataset": dataset_id},
                        )
                    ).all()
                )
                assert {version["id"] for version in versions} <= retirements.keys()
                assert all(state == "absent" for state in retirements.values())
                neighbor_proofs = (
                    await observer.execute(
                        text(
                            "SELECT r.state,yoao_assets_v1.fixture_dataset_media_retired(r.id,d.id) "
                            "FROM yoao_fixture_v1.fixture_runs r "
                            "JOIN yoao_fixture_v1.fixture_run_resources f ON f.run_id=r.id "
                            "JOIN public.datasets d ON d.id=f.resource_id "
                            "WHERE r.id=:run AND f.kind='dataset' AND f.state='published'"
                        ),
                        {"run": neighbor.run_id},
                    )
                ).all()
                assert neighbor_proofs and all(
                    row == ("open", None) for row in neighbor_proofs
                )
            assert (
                await asyncio.to_thread(_read_versions, reader, neighbor_versions)
                == neighbor_bytes
            )
        cleaned_neighbor = await _cleanup_owned(owner, neighbor)
        assert cleaned_neighbor.status_code == 200, cleaned_neighbor.text
        async with test_engine.connect() as db:
            surviving = dict((await db.execute(scope_query, neighbor_ids)).all())
            assert surviving == original_scopes
        await suite.finish()
        await owner.retire_native_suite()
        await asyncio.to_thread(_delete_owned_queue, sender, worker.queue)
        await asyncio.to_thread(sender.AsyncResult(str(job_id)).forget)
        await asyncio.to_thread(sender.AsyncResult(str(mismatch_id)).forget)
        sender.close()
        await asyncio.to_thread(_destroy_cleanup_storage_scope, scope)
        await owner.shutdown_authority(suite.suite_id)
        owner.process.stdin.close()
        await asyncio.to_thread(owner.process.wait, timeout=30)
        assert owner.process.returncode == 0
        owner_log.close()
        await _stop_media_runtime(media)
        await routing.__aexit__(None, None, None)
        store.close()
        output_store.close()
        completed = True
    finally:
        if not completed:
            _RETAINED_BACKFILL_RUNTIME.append(
                (
                    owner,
                    suite,
                    media,
                    worker,
                    sender,
                    scope,
                    store,
                    output_store,
                    owner_log,
                    routing,
                )
            )
            print(
                f"Retained unsettled media backfill run={run} neighbor={neighbor} "
                f"job={job_id} worker={worker.container_id if worker else None}",
                flush=True,
            )
