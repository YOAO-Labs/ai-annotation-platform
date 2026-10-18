from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog import (
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
from app.services import assets_catalog
from app.api.v1 import datasets as dataset_routes
from app.schemas.dataset import DatasetCreate, DatasetUpdate


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
    assert "aap_assets_v1.list_catalog_datasets" in statement
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


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.asyncio
async def test_catalog_video_thumbnails_always_use_owner_reference(monkeypatch, remote):
    actor_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    video_ids = [uuid.uuid4(), uuid.uuid4()]
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
        ],
        total=3,
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

    async def owner_posters(_db, source_requests):
        requests.extend(source_requests)
        return ["browser:verified-poster", None]

    monkeypatch.setattr(assets_catalog, "video_poster_urls", owner_posters)

    result = await gateway.items(
        ListItemsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert [request.dataset_item_id for request in requests] == video_ids
    assert all(
        request.actor_id == actor_id and request.task_id is None for request in requests
    )
    assert result.items[0].thumbnail_url == "browser:verified-poster"
    assert result.items[0].blurhash is None
    assert result.items[1].thumbnail_url == (
        "upstream:image-poster"
        if remote
        else f"signed:media-assets/old/{image_id}.webp"
    )
    assert result.items[1].blurhash == "image-blurhash"
    assert result.items[2].thumbnail_url is None
    assert result.items[2].blurhash is None
    if not remote:
        assert f"media-assets/old/{video_ids[0]}.webp" not in signed
        assert f"media-assets/old/{video_ids[1]}.webp" not in signed
