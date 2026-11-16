"""Public Native pyramid reads go through the Images owner, never API credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import Response
from yoao_assets_images import contract as images_contract
from yoao_assets_images.contract import ImageFailure

from app.api.v1.tasks import image_pyramid as api
from app.services import image_pyramid as pyramids
from app.services import image_pyramid_objects as objects
from app.services.content_snapshot import ContentError


def _native_fixture():
    task_id, actor_id, asset_id, output_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    source_snapshot = "a" * 64
    overview_digest = "sha256:" + "b" * 64
    levels = pyramids.pyramid_levels(1024, 768)
    overview = next(
        level for level in levels if level["width"] <= 512 and level["height"] <= 512
    )
    raw = pyramids.build_manifest(
        generation=1,
        source_fingerprint="sha256:" + "c" * 64,
        width=1024,
        height=768,
        overview_width=overview["width"],
        overview_height=overview["height"],
        overview_digest=overview_digest,
    )
    manifest = images_contract.ImagePyramidManifest.model_validate(raw)
    manifest_digest = pyramids.sha256_digest(pyramids.canonical_manifest_bytes(raw))
    target = images_contract.NativePyramidSavedTarget(
        schema=2,
        purpose="media-cache",
        location_id=uuid.uuid4(),
        config_revision=1,
        binding_generation=1,
        endpoint_url="http://minio:9000",
        bucket="media-cache",
        key_prefix="",
        prefix=f"image-pyramids/objects/{output_id}/",
        storage_identity="d" * 64,
    )
    saved = images_contract.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=3600,
        fixture_owned=False,
    )
    projection = SimpleNamespace(
        native=True,
        native_output=saved,
        status="ready",
        width=1024,
        height=768,
        required=True,
        profile_version="pyramid-v1",
        source_snapshot=source_snapshot,
        source_generation=7,
        generation=1,
        building_generation=2,
        building_status="building",
        manifest_digest=manifest_digest,
        retryable=False,
        retry_after_ms=None,
        error_code=None,
    )
    reference = SimpleNamespace(
        pyramid=projection,
        manifest=manifest,
        overview_url="https://cdn.example/overview.webp",
        expires_in=900,
    )
    task = SimpleNamespace(id=task_id, file_type="image")
    owner = SimpleNamespace(
        id=task_id,
        kind="task",
        width=1024,
        height=768,
        file_type="image",
    )
    asset = SimpleNamespace(
        id=asset_id, profile_version="pyramid-v1", active_generation=1
    )
    generation = SimpleNamespace(
        asset_id=asset_id,
        generation=1,
        job_id=uuid.uuid4(),
        output_id=output_id,
        status="ready",
        width=1024,
        height=768,
        source_snapshot=source_snapshot,
        manifest_digest=manifest_digest,
        last_accessed_at=None,
    )
    generation.overview_key = target.prefix + "overview.webp"
    return SimpleNamespace(
        task=task,
        owner=owner,
        asset=asset,
        generation=generation,
        projection=projection,
        reference=reference,
        actor_id=actor_id,
        overview_path=target.prefix + "overview.webp",
    )


def _wire_owner(monkeypatch, fixture, *, owner_reference=None, owner_assets=None):
    calls = {"checks": 0, "references": [], "assets": []}

    async def load_task(db, task_id):
        return fixture.task

    async def visible(db, task, actor_id):
        return None

    async def authorize(db, task_id, actor, request):
        return SimpleNamespace(
            actor_id=fixture.actor_id, mode="http", owner_id=fixture.task.id
        )

    async def active(db, task):
        return fixture.owner, fixture.asset, fixture.generation

    async def projection(db, task, generation, actor_id):
        return fixture.projection

    async def check(db, authority, lock=False):
        calls["checks"] += 1

    async def forbidden(*args, **kwargs):
        raise AssertionError("Native reads must not use legacy physical I/O")

    async def reference(db, **kwargs):
        calls["references"].append(kwargs)
        return owner_reference or fixture.reference

    async def assets(db, **kwargs):
        calls["assets"].append(kwargs)
        return owner_assets

    monkeypatch.setattr(api, "_load_task_or_404", load_task)
    monkeypatch.setattr(api.task_access, "assert_task_visible", visible)
    monkeypatch.setattr(api.originals, "authorize", authorize)
    monkeypatch.setattr(api.originals, "check", check)
    monkeypatch.setattr(api, "get_active_generation_for_task", active)
    monkeypatch.setattr(api, "_owner_projection", projection)
    monkeypatch.setattr(api, "owner_generation_reference", reference)
    monkeypatch.setattr(api, "owner_asset_references", assets)
    monkeypatch.setattr(api.objects, "reading", forbidden)
    monkeypatch.setattr(api.objects, "transport", forbidden)
    return calls


async def test_native_ready_get_uses_owner_reference_and_data_building_state(
    monkeypatch,
):
    fixture = _native_fixture()
    calls = _wire_owner(monkeypatch, fixture)
    monkeypatch.setattr(
        api,
        "_invalidate_ready",
        lambda *args, **kwargs: pytest.fail("Native ORM state must not be rewritten"),
    )
    response = Response()

    result = await api.get_image_pyramid(
        fixture.task.id,
        SimpleNamespace(headers={}),
        response,
        db=SimpleNamespace(commit=AsyncMock()),
        current_user=SimpleNamespace(id=fixture.actor_id),
    )

    assert result.status == "ready"
    assert result.generation == 1
    assert result.source_snapshot == fixture.projection.source_snapshot
    assert result.width == 1024 and result.height == 768
    # Data, not the local ORM row, owns pending/building projection.
    assert result.building_generation == 2
    assert result.building_status == "building"
    assert result.manifest.width == 1024 and result.manifest.height == 768
    assert result.overview.url == "https://cdn.example/overview.webp"
    assert response.headers["ETag"].startswith('"pyramid-1-')
    assert calls["checks"] == 2
    assert len(calls["references"]) == 1
    assert calls["references"][0]["generation"] == 1
    assert calls["references"][0]["task_id"] == fixture.task.id
    assert (
        calls["references"][0]["expected_source_snapshot"]
        == fixture.projection.source_snapshot
    )
    assert calls["assets"] == []


async def test_native_ready_get_conditional_request_keeps_owner_authority_check(
    monkeypatch,
):
    fixture = _native_fixture()
    calls = _wire_owner(monkeypatch, fixture)
    etag = f'"pyramid-1-{fixture.projection.manifest_digest.removeprefix("sha256:")}"'

    result = await api.get_image_pyramid(
        fixture.task.id,
        SimpleNamespace(headers={"if-none-match": etag}),
        Response(),
        db=SimpleNamespace(commit=AsyncMock()),
        current_user=SimpleNamespace(id=fixture.actor_id),
    )

    assert result.status_code == 304
    assert result.headers["ETag"] == etag
    assert calls["checks"] == 2


async def test_native_asset_urls_use_owner_references_with_actual_lifetime(
    monkeypatch,
):
    fixture = _native_fixture()
    references = SimpleNamespace(
        expires_in=600,
        items=[
            SimpleNamespace(
                kind="overview",
                generation=1,
                level=None,
                x=None,
                y=None,
                url="https://cdn.example/overview.webp",
            ),
            SimpleNamespace(
                kind="tile",
                generation=1,
                level=0,
                x=0,
                y=0,
                url="https://cdn.example/tiles/0/0/0.webp",
            ),
        ],
    )
    calls = _wire_owner(monkeypatch, fixture, owner_assets=references)
    payload = api.ImagePyramidAssetUrlsRequest(
        items=[
            {"kind": "overview", "generation": 1},
            {"kind": "tile", "generation": 1, "level": 0, "x": 0, "y": 0},
            {"kind": "tile", "generation": 1, "level": 0, "x": 0, "y": 0},
        ]
    )

    result = await api._native_asset_url_response(
        SimpleNamespace(commit=AsyncMock()),
        fixture.task,
        fixture.generation,
        fixture.projection,
        payload,
        authority=SimpleNamespace(actor_id=fixture.actor_id),
        actor_id=fixture.actor_id,
    )

    assert result.generation == 1
    assert [item.url for item in result.items] == [
        "https://cdn.example/overview.webp",
        "https://cdn.example/tiles/0/0/0.webp",
    ]
    assert len(calls["assets"]) == 1
    assert len(calls["assets"][0]["items"]) == 2
    assert calls["assets"][0]["generation"] == 1
    assert calls["checks"] == 2
    assert calls["references"] == []


async def test_native_missing_members_preserve_owner_failure_without_orm_mutation(
    monkeypatch,
):
    fixture = _native_fixture()

    async def missing(db, **kwargs):
        raise ImageFailure("object_missing")

    calls = _wire_owner(monkeypatch, fixture)
    monkeypatch.setattr(api, "owner_generation_reference", missing)
    monkeypatch.setattr(
        api,
        "_invalidate_ready",
        lambda *args, **kwargs: pytest.fail("Native ORM state must not be rewritten"),
    )

    result = await api.get_image_pyramid(
        fixture.task.id,
        SimpleNamespace(headers={}),
        Response(),
        db=SimpleNamespace(commit=AsyncMock()),
        current_user=SimpleNamespace(id=fixture.actor_id),
    )

    assert result.status == "inconsistent"
    assert result.error_code == "image_pyramid_storage_missing"
    assert result.generation == 1
    assert calls["checks"] == 1
    assert calls["assets"] == []


async def test_common_thumbnail_pointer_never_signs_saved_native_output(monkeypatch):
    fixture = _native_fixture()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, key):
            return fixture.generation

        async def commit(self):
            return None

    async def load_owner(db, owner_kind, owner_id):
        return fixture.owner

    async def asset_for_owner(db, owner):
        return fixture.asset

    async def forbidden(*args, **kwargs):
        raise AssertionError("saved Native pointer must not use legacy credentials")

    monkeypatch.setattr(objects, "async_sessionmaker", lambda *args, **kwargs: Session)
    monkeypatch.setattr(objects.storage_access, "engine", lambda db: object())
    monkeypatch.setattr(pyramids, "load_owner", load_owner)
    monkeypatch.setattr(pyramids, "get_asset_for_owner", asset_for_owner)
    monkeypatch.setattr(objects, "reading", forbidden)
    monkeypatch.setattr(objects, "transport", forbidden)

    assert (
        await objects.thumbnail_url(
            object(),
            "task",
            fixture.task.id,
            fixture.overview_path,
            actor_id=fixture.actor_id,
        )
        is None
    )


async def test_legacy_reading_refuses_saved_native_output():
    generation = SimpleNamespace(job_id=uuid.uuid4(), output_id=uuid.uuid4())

    with pytest.raises(ContentError) as refusal:
        async with objects.reading(
            None, None, generation, keys=("manifest.json",)
        ):
            pytest.fail("saved Native output reached legacy physical reads")

    assert refusal.value.code == "image_pyramid_native_owner_required"
