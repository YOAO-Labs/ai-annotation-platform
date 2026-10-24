"""Pyramid business contracts; independent authority tests live in HTTP tests."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError

from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from app.services import image_pyramid as pyramids
from app.services import image_pyramid_objects as objects
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from app.services.content_snapshot import ContentError
from app.services.storage import StorageService
from tests.image_pyramid_fixtures import (
    PhysicalTransport,
    make_image_task,
    ready_generation,
    same_session_reading,
)


@pytest.fixture
def transport(monkeypatch):
    same_session_reading(monkeypatch)
    return PhysicalTransport(monkeypatch)


def test_levels_and_overlap_are_full_resolution_first():
    levels = pyramids.pyramid_levels(1025, 513)
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
    assert pyramids.expected_tile_dimensions(levels[0], 0, 0) == (513, 513)
    assert pyramids.expected_tile_dimensions(levels[0], 1, 0) == (514, 513)
    assert pyramids.expected_tile_dimensions(levels[0], 2, 1) == (2, 2)


def test_manifest_is_deterministic_and_does_not_expose_storage():
    manifest = pyramids.build_manifest(
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
    assert not any(
        private in encoded.lower()
        for private in ("image-pyramids/", "http://", "https://", "bucket")
    )
    assert pyramids.canonical_manifest_bytes(
        manifest
    ) == pyramids.canonical_manifest_bytes(dict(reversed(list(manifest.items()))))


def test_image_dimensions_apply_exif_orientation_without_decode():
    with Image.new("RGB", (7, 11), "red") as image, io.BytesIO() as payload:
        exif = image.getexif()
        exif[274] = 6
        image.save(payload, format="JPEG", exif=exif)
        assert StorageService.read_image_dimensions_from_bytes(payload.getvalue()) == (
            11,
            7,
        )


async def test_asset_owner_constraint_rejects_zero_or_two_owners(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await make_image_task(db_session, user.id)
    for values in ({}, {"dataset_item_id": item.id, "task_id": task.id}):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(
                    ImagePyramidAsset(profile_version="pyramid-v1", **values)
                )
                await db_session.flush()


async def test_prepare_generation_is_singleflight_and_fences_metadata_rebinding(
    db_session, super_admin
):
    user, _ = super_admin
    _, item = await make_image_task(db_session, user.id)
    owner = await pyramids.owner_for_dataset_item(db_session, item)
    delegation = originals.system_authorization("dataset_item", item.id, owner.source)
    first = await pyramids.prepare_generation(
        db_session, owner, force=False, delegation=delegation
    )
    duplicate = await pyramids.prepare_generation(
        db_session, owner, force=False, delegation=delegation
    )
    assert first is not None and duplicate is first and first.status == "pending"
    asset = await pyramids.get_asset_for_owner(db_session, owner)
    await ready_generation(db_session, item, asset=asset, generation=first)
    assert (
        await pyramids.prepare_generation(
            db_session, owner, force=False, delegation=delegation
        )
        is first
    )

    # Dimensions may be corrected without replacing immutable verified content.
    # They are still part of the exact input receipt owned by an earlier job.
    item.width += 1
    await db_session.flush()
    replacement_owner = await pyramids.owner_for_dataset_item(db_session, item)
    with pytest.raises(ContentError, match="content_binding_changed"):
        await pyramids.prepare_generation(
            db_session, replacement_owner, force=False, delegation=delegation
        )
    replacement = await pyramids.prepare_generation(
        db_session,
        replacement_owner,
        force=False,
        delegation=originals.system_authorization(
            "dataset_item", item.id, replacement_owner.source
        ),
    )
    assert replacement.generation == first.generation + 1
    assert replacement.original_ref != first.original_ref
    assert asset.active_generation is None


async def test_manifest_and_asset_url_endpoints_are_bounded_and_deduplicate(
    db_session, httpx_client, super_admin, transport, monkeypatch
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    asset, generation, output, manifest = await ready_generation(db_session, item)
    transport.register(output, manifest)
    headers, route = (
        {"Authorization": f"Bearer {token}"},
        f"/api/v1/tasks/{task.id}/image-pyramid",
    )
    assert (await httpx_client.get(route)).status_code == 401
    response = await httpx_client.get(route, headers=headers)
    assert response.status_code == 200
    assert (
        response.json()["status"] == "ready" and response.json()["manifest"] == manifest
    )
    assert "image-pyramids/" not in str(response.json()["manifest"])
    assert response.headers["etag"].startswith('"pyramid-1-')
    response304 = await httpx_client.get(
        route, headers={**headers, "If-None-Match": response.headers["etag"]}
    )
    assert response304.status_code == 304 and not response304.content

    transport.verified.clear()
    items = [
        {"kind": "overview", "generation": 1},
        {"kind": "overview", "generation": 1},
        {"kind": "tile", "generation": 1, "level": 0, "x": 0, "y": 0},
    ]
    urls = await httpx_client.post(
        route + "/asset-urls", headers=headers, json={"items": items}
    )
    assert urls.status_code == 200
    assert [row["kind"] for row in urls.json()["items"]] == ["overview", "tile"]
    assert len(transport.verified) == 2
    assert all(
        options["bucket"] == output.storage_ref["bucket"]
        for _, options in transport.signed
    )

    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.settings.image_pyramid_asset_url_batch_max", 2
    )
    bounded = await httpx_client.post(
        route + "/asset-urls", headers=headers, json={"items": items}
    )
    assert (
        bounded.status_code == 422
        and bounded.json()["detail"]["reason"] == "batch_too_large"
    )
    invalid = await httpx_client.post(
        route + "/asset-urls",
        headers=headers,
        json={
            "items": [{"kind": "tile", "generation": 1, "level": 0, "x": 999, "y": 0}]
        },
    )
    assert (
        invalid.status_code == 422
        and invalid.json()["detail"]["reason"] == "invalid_coordinate"
    )
    stale = await httpx_client.post(
        route + "/asset-urls",
        headers=headers,
        json={"items": [{"kind": "overview", "generation": 2}]},
    )
    assert stale.status_code == 409 and stale.json()["detail"] == {
        "reason": "stale_generation",
        "active_generation": 1,
    }

    transport.missing.add(generation.overview_key)
    missing = await httpx_client.post(
        route + "/asset-urls",
        headers=headers,
        json={"items": [{"kind": "overview", "generation": 1}]},
    )
    assert missing.status_code == 409
    assert missing.json()["detail"]["reason"] == "inconsistent_ready"
    await db_session.refresh(asset)
    await db_session.refresh(generation)
    assert asset.active_generation is None and generation.status == "failed"


async def test_original_metadata_rebinding_invalidates_ready_generation(
    db_session, httpx_client, super_admin, transport
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    asset, generation, output, manifest = await ready_generation(db_session, item)
    transport.register(output, manifest)
    item.width += 1
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json()["status"] == "stale"
    assert response.json()["error_code"] == "source_changed"
    await db_session.refresh(asset)
    await db_session.refresh(generation)
    assert asset.active_generation is None and generation.status == "failed"
    assert not transport.verified and not transport.signed


async def test_reconcile_retains_unpinned_legacy_generation_without_storage_scans(
    db_session, super_admin
):
    user, _ = super_admin
    _, item = await make_image_task(db_session, user.id)
    asset, generation, _, _ = await ready_generation(db_session, item, pinned=False)
    generation.status, generation.updated_at = (
        "failed",
        pyramids.utcnow() - timedelta(days=2),
    )
    asset.active_generation = None
    await db_session.flush()

    class ForbiddenStorage:
        def __getattr__(self, name):
            pytest.fail(f"Unowned legacy generation must never touch storage: {name}")

    assert await objects.reconcile(db_session, ForbiddenStorage()) == 0
    assert (
        await db_session.get(ImagePyramidGeneration, (asset.id, generation.generation))
        is not None
    )


@pytest.mark.parametrize("operation", ["get", "retry"])
async def test_unpinned_legacy_ready_is_stale_or_regenerated(
    db_session, httpx_client, super_admin, monkeypatch, operation
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    _, generation, _, _ = await ready_generation(db_session, item, pinned=False)
    queued = []
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "new-job",
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.settings.image_pyramid_auto_generate", False
    )
    headers, route = (
        {"Authorization": f"Bearer {token}"},
        f"/api/v1/tasks/{task.id}/image-pyramid",
    )
    response = (
        await httpx_client.get(route, headers=headers)
        if operation == "get"
        else await httpx_client.post(route + "/retry", headers=headers)
    )
    assert response.status_code == (200 if operation == "get" else 202)
    assert response.json()["status"] == ("stale" if operation == "get" else "queued")
    assert generation.output_id is None
    if operation == "retry":
        assert queued[0][0] == ("task", task.id)
        assert queued[0][1]["delegation"]["original"]["content_id"]


async def test_retry_is_idempotent_while_generation_is_pending(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    owner = await pyramids.owner_for_dataset_item(db_session, item)
    await pyramids.prepare_generation(
        db_session,
        owner,
        force=False,
        delegation=originals.system_authorization(
            "dataset_item", item.id, owner.source
        ),
    )
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: pytest.fail("Pending generation must not enqueue"),
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


async def test_retry_failed_generation_keeps_cooldown(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    asset, generation, _, _ = await ready_generation(db_session, item, pinned=False)
    asset.active_generation = None
    generation.status = "failed"
    generation.updated_at = pyramids.utcnow()
    await db_session.flush()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.enqueue_image_pyramid",
        lambda *args, **kwargs: pytest.fail("Cooldown must not enqueue"),
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 429
    assert response.json()["detail"]["reason"] == "retry_cooldown"
    assert int(response.headers["retry-after"]) > 0


@pytest.mark.parametrize("pinned", [True, False])
async def test_task_summary_is_lightweight(
    db_session, httpx_client, super_admin, monkeypatch, pinned
):
    user, token = super_admin
    task, item = await make_image_task(db_session, user.id)
    await ready_generation(db_session, item, pinned=pinned)
    monkeypatch.setattr(
        physical,
        "read_manifest",
        lambda *args: pytest.fail("Summary must not read manifest"),
    )
    monkeypatch.setattr(
        physical,
        "verify_object",
        lambda *args: pytest.fail("Summary must not read tiles"),
    )
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    summary = response.json()["image_pyramid"]
    assert summary == {
        "status": "ready" if pinned else "failed",
        "generation": 1,
        "width": item.width,
        "height": item.height,
        "tile_size": 512,
        "format": "webp",
        "required": True,
    }
    assert "levels" not in summary and "url" not in summary
