from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.image_pyramid import (
    build_manifest,
    expected_tile_dimensions,
    get_asset_for_owner,
    owner_for_dataset_item,
    publish_generation,
    prepare_generation,
    pyramid_levels,
    sha256_digest,
    touch_generation_access,
)
from app.services.storage import StorageService
from app.workers import image_pyramid as image_pyramid_worker
from app.workers.media_materialization import SqlAlchemyMediaMaterializationWriter
from tests.factory import build_tool_bindings, create_user


async def _make_image_task(db_session, owner_id, *, width=8193, height=6145):
    project = Project(
        display_id=f"P-PYR-{uuid.uuid4().hex[:6]}",
        name="Pyramid project",
        type_key="image-segmentation",
        type_label="Image",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["defect"], unit="region"),
    )
    dataset = Dataset(
        display_id=f"D-PYR-{uuid.uuid4().hex[:6]}",
        name=f"pyramid-{uuid.uuid4().hex[:6]}",
        data_type="image",
        created_by=owner_id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="large.png",
        file_path=f"pyramid/{uuid.uuid4()}.png",
        file_type="image",
        file_size=1234,
        width=width,
        height=height,
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-PYR-{uuid.uuid4().hex[:6]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return task, item


async def test_publish_generation_projects_task_thumbnail_only_for_winning_lease(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_image_task(db_session, user.id)
    task.thumbnail_path = "thumbnails/original.webp"
    task.blurhash = "existing-blurhash"
    asset = ImagePyramidAsset(
        id=uuid.uuid4(),
        task_id=task.id,
        profile_version="pyramid-v1",
        building_generation=1,
    )
    winning_token = uuid.uuid4()
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=1,
        source_identity="etag:image:bytes:1234",
        status="building",
        lease_token=winning_token,
        normalization_version="exif-autorotate-srgb-v1",
    )
    db_session.add_all([asset, generation])
    await db_session.flush()

    publication = {
        "source_fingerprint": "sha256:source",
        "width": item.width,
        "height": item.height,
        "max_level": 0,
        "manifest_storage_key": f"image-pyramids/{asset.id}/g1/manifest.json",
        "manifest_digest": "sha256:manifest",
        "overview_storage_key": f"image-pyramids/{asset.id}/g1/overview.webp",
        "overview_width": 512,
        "overview_height": 384,
        "overview_digest": "sha256:overview",
        "tile_count": 1,
        "retained_bytes": 2048,
        "task_thumbnail_writer": SqlAlchemyMediaMaterializationWriter(db_session),
    }

    staged = await db_session.begin_nested()
    published = await publish_generation(
        db_session,
        asset_id=asset.id,
        generation_number=1,
        lease_token=winning_token,
        **publication,
    )
    assert published is True
    await staged.rollback()
    for row in (task, asset, generation):
        await db_session.refresh(row)
    assert task.thumbnail_path == "thumbnails/original.webp"
    assert task.blurhash == "existing-blurhash"
    assert asset.active_generation is None
    assert asset.building_generation == 1
    assert generation.status == "building"

    published = await publish_generation(
        db_session,
        asset_id=asset.id,
        generation_number=1,
        lease_token=winning_token,
        **publication,
    )

    assert published is True
    assert asset.active_generation == 1
    assert asset.building_generation is None
    assert generation.status == "ready"
    assert task.thumbnail_path == publication["overview_storage_key"]
    assert task.blurhash == "existing-blurhash"
    await db_session.commit()

    task.thumbnail_path = "thumbnails/before-losing-lease.webp"
    asset.building_generation = 2
    losing_generation_token = uuid.uuid4()
    db_session.add(
        ImagePyramidGeneration(
            asset_id=asset.id,
            generation=2,
            source_identity="etag:image:bytes:1234",
            status="building",
            lease_token=losing_generation_token,
            normalization_version="exif-autorotate-srgb-v1",
        )
    )
    await db_session.flush()

    published = await publish_generation(
        db_session,
        asset_id=asset.id,
        generation_number=2,
        lease_token=winning_token,
        **publication,
    )

    assert published is False
    assert task.thumbnail_path == "thumbnails/before-losing-lease.webp"
    assert asset.active_generation == 1
    assert asset.building_generation == 2


def test_levels_and_overlap_are_full_resolution_first():
    levels = pyramid_levels(1025, 513)
    assert levels[:3] == [
        {
            "level": 0,
            "scaleFactor": 1,
            "width": 1025,
            "height": 513,
            "columns": 3,
            "rows": 2,
        },
        {
            "level": 1,
            "scaleFactor": 2,
            "width": 513,
            "height": 257,
            "columns": 2,
            "rows": 1,
        },
        {
            "level": 2,
            "scaleFactor": 4,
            "width": 257,
            "height": 129,
            "columns": 1,
            "rows": 1,
        },
    ]
    assert levels[-1]["width"] == levels[-1]["height"] == 1
    assert expected_tile_dimensions(levels[0], 0, 0) == (513, 513)
    assert expected_tile_dimensions(levels[0], 1, 0) == (514, 513)
    assert expected_tile_dimensions(levels[0], 2, 1) == (2, 2)


def test_manifest_is_deterministic_and_does_not_expose_storage():
    manifest = build_manifest(
        generation=4,
        source_fingerprint="sha256:source",
        width=1025,
        height=513,
        overview_width=512,
        overview_height=256,
        overview_digest="sha256:overview",
    )
    encoded = str(manifest)
    assert manifest["schema"] == "aap-image-pyramid/v1"
    assert manifest["generation"] == 4
    assert "image-pyramids/" not in encoded
    assert "http://" not in encoded
    assert "https://" not in encoded
    assert "bucket" not in encoded.lower()


def test_image_dimensions_apply_exif_orientation_without_decode():
    image = Image.new("RGB", (7, 11), "red")
    exif = image.getexif()
    exif[274] = 6
    payload = io.BytesIO()
    image.save(payload, format="JPEG", exif=exif)

    assert StorageService.read_image_dimensions_from_bytes(payload.getvalue()) == (
        11,
        7,
    )


async def test_asset_owner_constraint_rejects_zero_or_two_owners(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_image_task(db_session, user.id)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ImagePyramidAsset(profile_version="pyramid-v1"))
            await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ImagePyramidAsset(
                    dataset_item_id=item.id,
                    task_id=task.id,
                    profile_version="pyramid-v1",
                )
            )
            await db_session.flush()


async def test_prepare_generation_is_singleflight_and_fences_source_replacement(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    _, item = await _make_image_task(db_session, user.id)
    owner = owner_for_dataset_item(item)
    identity = ["etag:a:bytes:1234"]
    monkeypatch.setattr(
        "app.services.image_pyramid.source_identity",
        lambda owner: (identity[0], 1234),
    )

    first = await prepare_generation(db_session, owner, force=False)
    duplicate = await prepare_generation(db_session, owner, force=False)
    assert first is not None
    assert duplicate is first
    assert first.status == "pending"

    asset = await get_asset_for_owner(db_session, owner)
    assert asset is not None
    first.status = "ready"
    first.width = item.width
    first.height = item.height
    first.format = "webp"
    asset.active_generation = first.generation
    asset.building_generation = None
    await db_session.flush()

    reused = await prepare_generation(db_session, owner, force=False)
    assert reused is first

    identity[0] = "etag:b:bytes:1234"
    replacement = await prepare_generation(db_session, owner, force=False)
    assert replacement is not None
    assert replacement.generation == first.generation + 1
    assert asset.active_generation is None


async def test_manifest_and_asset_url_endpoints_are_bounded_and_deduplicate(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_image_task(db_session, user.id)
    asset = ImagePyramidAsset(
        dataset_item_id=item.id,
        profile_version="pyramid-v1",
        active_generation=1,
    )
    db_session.add(asset)
    await db_session.flush()
    manifest = build_manifest(
        generation=1,
        source_fingerprint="sha256:source",
        width=item.width,
        height=item.height,
        overview_width=512,
        overview_height=384,
        overview_digest="sha256:overview",
    )
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=1,
        source_identity="etag:a:bytes:1234",
        source_fingerprint="sha256:source",
        status="ready",
        width=item.width,
        height=item.height,
        max_level=len(manifest["levels"]) - 1,
        format="webp",
        normalization_version="exif-autorotate-srgb-v1",
        manifest_key=f"image-pyramids/{asset.id}/g1/manifest.json",
        manifest_digest=sha256_digest(b"manifest"),
        overview_key=f"image-pyramids/{asset.id}/g1/overview.webp",
        overview_width=512,
        overview_height=384,
        overview_digest="sha256:overview",
        tile_count=1,
        retained_bytes=100,
    )
    db_session.add(generation)
    await db_session.flush()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.source_matches_generation",
        lambda owner, generation: True,
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.generation_manifest",
        lambda generation: manifest,
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.storage_service.verify_upload",
        lambda key, bucket=None: {"ContentLength": 1},
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.storage_service.generate_download_url",
        lambda key, **kwargs: f"https://storage.invalid/{key}",
    )
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr("app.api.v1.tasks.image_pyramid.utcnow", lambda: now[0])
    monkeypatch.setattr("app.services.image_pyramid.utcnow", lambda: now[0])
    headers = {"Authorization": f"Bearer {token}"}

    unauthorized_response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid"
    )
    assert unauthorized_response.status_code == 401

    manifest_response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid", headers=headers
    )
    assert manifest_response.status_code == 200
    body = manifest_response.json()
    assert body["status"] == "ready"
    assert body["manifest"] == manifest
    assert "image-pyramids/" not in str(body["manifest"])
    assert manifest_response.headers["etag"].startswith('"pyramid-1-')
    await db_session.refresh(generation)
    assert generation.last_accessed_at == now[0]

    now[0] += timedelta(minutes=10)
    throttled_response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid", headers=headers
    )
    assert throttled_response.status_code == 200
    assert throttled_response.headers["etag"] == manifest_response.headers["etag"]
    await db_session.refresh(generation)
    assert generation.last_accessed_at == now[0] - timedelta(minutes=10)

    now[0] += timedelta(microseconds=1)
    eligible_response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid", headers=headers
    )
    assert eligible_response.status_code == 200
    assert eligible_response.headers["etag"] == manifest_response.headers["etag"]
    await db_session.refresh(generation)
    assert generation.last_accessed_at == now[0]

    not_modified_response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid",
        headers={
            **headers,
            "If-None-Match": manifest_response.headers["etag"],
        },
    )
    assert not_modified_response.status_code == 304
    assert not not_modified_response.content

    await db_session.refresh(generation)
    assert generation.last_accessed_at == now[0]

    items = [
        {"kind": "overview", "generation": 1},
        {"kind": "overview", "generation": 1},
        {"kind": "tile", "generation": 1, "level": 0, "x": 0, "y": 0},
    ]
    urls_response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/asset-urls",
        headers=headers,
        json={"items": items},
    )
    assert urls_response.status_code == 200
    assert [row["kind"] for row in urls_response.json()["items"]] == [
        "overview",
        "tile",
    ]

    invalid_response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/asset-urls",
        headers=headers,
        json={
            "items": [{"kind": "tile", "generation": 1, "level": 0, "x": 999, "y": 0}]
        },
    )
    assert invalid_response.status_code == 422
    assert invalid_response.json()["detail"]["reason"] == "invalid_coordinate"

    stale_generation_response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/asset-urls",
        headers=headers,
        json={"items": [{"kind": "overview", "generation": 2}]},
    )
    assert stale_generation_response.status_code == 409
    assert stale_generation_response.json()["detail"] == {
        "reason": "stale_generation",
        "active_generation": 1,
    }

    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.storage_service.verify_upload",
        lambda key, bucket=None: False,
    )
    missing_object_response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/asset-urls",
        headers=headers,
        json={"items": [{"kind": "overview", "generation": 1}]},
    )
    assert missing_object_response.status_code == 409
    assert missing_object_response.json()["detail"]["reason"] == "inconsistent_ready"
    await db_session.refresh(asset)
    await db_session.refresh(generation)
    assert asset.active_generation is None
    assert generation.status == "failed"
    assert generation.error_code == "object_missing"


async def test_generation_access_commit_is_visible_and_rollback_preserves_touch(
    test_engine, monkeypatch
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    asset_id = uuid.uuid4()
    fixture_committed = False
    try:
        async with sessions() as setup:
            user = await create_user(
                setup, "super_admin", f"pyramid-access-{suffix}@test.local", "Pyramid"
            )
            task, item = await _make_image_task(setup, user.id, width=64, height=48)
            asset = ImagePyramidAsset(
                id=asset_id,
                dataset_item_id=item.id,
                profile_version="pyramid-v1",
                active_generation=1,
            )
            generation = ImagePyramidGeneration(
                asset_id=asset.id,
                generation=1,
                source_identity="etag:access:bytes:1234",
                status="ready",
                width=item.width,
                height=item.height,
                normalization_version="exif-autorotate-srgb-v1",
            )
            setup.add_all([asset, generation])
            await setup.flush()
            user_id = user.id
            project_id = task.project_id
            dataset_id = item.dataset_id
            item_id = item.id
            task_id = task.id
            await setup.commit()
            fixture_committed = True

        async with sessions() as command:
            generation = await command.get(ImagePyramidGeneration, (asset_id, 1))
            assert generation is not None
            await touch_generation_access(command, generation)
            committed_touch = generation.last_accessed_at

        assert committed_touch is not None

        async def read_committed_touch():
            async with sessions() as observer:
                return await observer.scalar(
                    select(ImagePyramidGeneration.last_accessed_at).where(
                        ImagePyramidGeneration.asset_id == asset_id,
                        ImagePyramidGeneration.generation == 1,
                    )
                )

        assert await read_committed_touch() == committed_touch

        async with sessions() as rollback_session:
            generation = await rollback_session.get(
                ImagePyramidGeneration, (asset_id, 1)
            )
            assert generation is not None
            monkeypatch.setattr(
                "app.services.image_pyramid.utcnow",
                lambda: committed_touch + timedelta(minutes=11),
            )

            async def unavailable_commit():
                await rollback_session.flush()
                raise RuntimeError("controlled access commit failure")

            monkeypatch.setattr(rollback_session, "commit", unavailable_commit)
            with pytest.raises(RuntimeError):
                await touch_generation_access(rollback_session, generation)
            assert await read_committed_touch() == committed_touch
            await rollback_session.rollback()

        assert await read_committed_touch() == committed_touch
    finally:
        if fixture_committed:
            async with sessions.begin() as cleanup:
                await cleanup.execute(
                    delete(ImagePyramidGeneration).where(
                        ImagePyramidGeneration.asset_id == asset_id
                    )
                )
                await cleanup.execute(
                    delete(ImagePyramidAsset).where(ImagePyramidAsset.id == asset_id)
                )
                await cleanup.execute(delete(Task).where(Task.id == task_id))
                await cleanup.execute(
                    delete(DatasetItem).where(DatasetItem.id == item_id)
                )
                await cleanup.execute(delete(Dataset).where(Dataset.id == dataset_id))
                await cleanup.execute(delete(Project).where(Project.id == project_id))
                await cleanup.execute(delete(User).where(User.id == user_id))


async def test_source_replacement_invalidates_ready_generation(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_image_task(db_session, user.id)
    asset = ImagePyramidAsset(
        dataset_item_id=item.id,
        profile_version="pyramid-v1",
        active_generation=1,
    )
    db_session.add(asset)
    await db_session.flush()
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=1,
        source_identity="etag:old:bytes:1234",
        source_fingerprint="sha256:source",
        status="ready",
        width=item.width,
        height=item.height,
        max_level=14,
        format="webp",
        normalization_version="exif-autorotate-srgb-v1",
        tile_count=400,
        retained_bytes=1000,
    )
    db_session.add(generation)
    await db_session.flush()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.source_matches_generation",
        lambda owner, generation: False,
    )

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["error_code"] == "source_changed"
    await db_session.refresh(asset)
    await db_session.refresh(generation)
    assert asset.active_generation is None
    assert generation.status == "failed"
    assert generation.last_accessed_at is None


async def test_reconcile_removes_processed_failed_generation(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    _, item = await _make_image_task(db_session, user.id)
    asset = ImagePyramidAsset(dataset_item_id=item.id, profile_version="pyramid-v1")
    db_session.add(asset)
    await db_session.flush()
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=1,
        source_identity="etag:failed:bytes:1234",
        status="failed",
        normalization_version="exif-autorotate-srgb-v1",
        updated_at=image_pyramid_worker.utcnow() - timedelta(days=2),
    )
    db_session.add(generation)
    await db_session.flush()

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Engine:
        async def dispose(self):
            pass

    class Client:
        def list_objects_v2(self, **kwargs):
            return {"Contents": []}

    class Storage:
        media_cache_bucket = "cache"
        client = Client()

    deleted_prefixes: list[str] = []
    monkeypatch.setattr(
        image_pyramid_worker,
        "create_async_engine",
        lambda *args, **kwargs: Engine(),
    )
    monkeypatch.setattr(
        image_pyramid_worker,
        "async_sessionmaker",
        lambda *args, **kwargs: lambda: SessionContext(),
    )
    monkeypatch.setattr(image_pyramid_worker, "StorageService", Storage)
    monkeypatch.setattr(
        image_pyramid_worker,
        "_delete_prefix",
        lambda _storage, prefix: deleted_prefixes.append(prefix),
    )

    await image_pyramid_worker._reconcile_image_pyramids()

    assert deleted_prefixes == [image_pyramid_worker.generation_prefix(asset.id, 1)]
    assert (
        await db_session.get(
            ImagePyramidGeneration,
            {"asset_id": asset.id, "generation": 1},
        )
        is None
    )


async def test_retry_is_idempotent_while_generation_is_pending(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_image_task(db_session, user.id)
    asset = ImagePyramidAsset(
        dataset_item_id=item.id,
        profile_version="pyramid-v1",
        building_generation=1,
    )
    db_session.add(asset)
    await db_session.flush()
    db_session.add(
        ImagePyramidGeneration(
            asset_id=asset.id,
            generation=1,
            source_identity="etag:a:bytes:1234",
            status="pending",
            normalization_version="exif-autorotate-srgb-v1",
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: pytest.fail("pending generation must not enqueue"),
    )

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "task_id": str(task.id),
        "status": "pending",
        "celery_task_id": None,
    }


async def test_task_summary_is_lightweight(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_image_task(db_session, user.id)
    asset = ImagePyramidAsset(
        dataset_item_id=item.id,
        profile_version="pyramid-v1",
        active_generation=1,
    )
    db_session.add(asset)
    await db_session.flush()
    db_session.add(
        ImagePyramidGeneration(
            asset_id=asset.id,
            generation=1,
            source_identity="etag:a:bytes:1234",
            source_fingerprint="sha256:source",
            status="ready",
            width=item.width,
            height=item.height,
            max_level=14,
            format="webp",
            normalization_version="exif-autorotate-srgb-v1",
            tile_count=400,
            retained_bytes=1000,
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        "app.api.v1.tasks._shared.storage_service.generate_download_url",
        lambda *args, **kwargs: "https://storage.invalid/source",
    )

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    summary = response.json()["image_pyramid"]
    assert summary == {
        "status": "ready",
        "generation": 1,
        "width": item.width,
        "height": item.height,
        "tile_size": 512,
        "format": "webp",
        "required": True,
    }
    assert "levels" not in summary
    assert "url" not in summary
