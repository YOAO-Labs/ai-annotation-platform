"""Project transactions preserve guide references, revisions and final authority."""

import asyncio
from copy import deepcopy
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import projects as routes
from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.services import guide_asset_uploads as uploads
from app.services.ml_backend import MLBackendService
from tests.test_comment_attachment_uploads import bearer
from tests.test_guide_assets import (
    content,
    objects as objects,
    project as project,
    upload,
)
from tests.test_projects_clone import _seed_backend


def project_url(identifier):
    return f"/api/v1/projects/{identifier}"


async def clone(client, actor, source_id, **extra):
    return await client.post(
        "/api/v1/projects",
        headers=bearer(actor[1]),
        json={
            "name": f"Guide clone {uuid.uuid4().hex}",
            "type_label": "图像-检测",
            "type_key": "image-det",
            "source_project_id": str(source_id),
            "copy_annotation_guide": True,
            **extra,
        },
    )


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"guide_assets": []}, 409),
        ({"guide_assets": [], "guide_assets_revision": 1}, 409),
        ({"guide_assets": None}, 422),
        ({"guide_assets_revision": 2}, 422),
        ({"guide_assets": [], "guide_assets_revision": None}, 422),
        ({"guide_assets": [], "guide_assets_revision": True}, 422),
        ({"guide_assets": [], "guide_assets_revision": "2"}, 422),
        ({"guide_assets": [], "guide_assets_revision": 2.0}, 422),
    ],
)
async def test_explicit_list_rejects_missing_stale_or_noninteger_revision_without_unlinking(
    httpx_client, project_admin, project, objects, db_session, patch, status
):
    identifier = project.id
    value = await upload(httpx_client, project_admin, identifier)
    response = await httpx_client.patch(
        project_url(identifier), headers=bearer(project_admin[1]), json=patch
    )
    assert response.status_code == status, response.text
    current = await httpx_client.get(
        project_url(identifier), headers=bearer(project_admin[1])
    )
    assert current.status_code == 200 and current.json()["guide_assets"] == [value]
    assert current.json()["guide_assets_revision"] == 2
    assert (
        await content(httpx_client, project_admin, identifier, value["key"])
    ).status_code == 200
    assert not objects.deleted


async def test_cas_reorder_remove_and_new_upload_preserve_only_current_exact_entries(
    httpx_client, project_admin, project, objects, db_session
):
    identifier = project.id
    first = await upload(httpx_client, project_admin, identifier)
    second = await upload(httpx_client, project_admin, identifier)
    url, headers = project_url(identifier), bearer(project_admin[1])
    response = await httpx_client.patch(
        url,
        headers=headers,
        json={"guide_assets": [second, first], "guide_assets_revision": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["guide_assets_revision"] == 4
    third = await upload(httpx_client, project_admin, identifier)
    stale = await httpx_client.patch(
        url,
        headers=headers,
        json={"guide_assets": [second], "guide_assets_revision": 4},
    )
    assert stale.status_code == 409
    current = (await httpx_client.get(url, headers=headers)).json()
    assert (
        current["guide_assets"] == [second, first, third]
        and current["guide_assets_revision"] == 5
    )
    for assets, status in (
        ([second, second], 422),
        ([{**second, "size_bytes": True}], 409),
    ):
        result = await httpx_client.patch(
            url,
            headers=headers,
            json={"guide_assets": assets, "guide_assets_revision": 5},
        )
        assert result.status_code == status, result.text
    result = await httpx_client.patch(
        url,
        headers=headers,
        json={"guide_assets": [third, second], "guide_assets_revision": 5},
    )
    assert result.status_code == 200 and result.json()["guide_assets_revision"] == 6
    first_id = uuid.UUID(first["key"].split("/")[-1])
    row = await db_session.get(Upload, first_id, populate_existing=True)
    assert row.state == "retired" and not objects.deleted
    reinjection = await httpx_client.patch(
        url,
        headers=headers,
        json={"guide_assets": [first, third, second], "guide_assets_revision": 6},
    )
    assert reinjection.status_code == 409


async def test_http_clone_survives_original_project_deletion_until_last_reference(
    httpx_client, project_admin, project, objects, db_session
):
    source_id = project.id
    value = await upload(httpx_client, project_admin, source_id)
    response = await clone(httpx_client, project_admin, source_id)
    assert response.status_code == 200, response.text
    target_id = uuid.UUID(response.json()["id"])
    owner_id = uuid.UUID(value["key"].split("/")[-1])
    assert response.json()["guide_assets"] == [value]
    assert response.json()["guide_assets_revision"] == 2
    for identifier in (source_id, target_id):
        assert await db_session.get(Reference, (identifier, owner_id)) is not None
    deleted = await httpx_client.delete(
        project_url(source_id), headers=bearer(project_admin[1])
    )
    assert deleted.status_code == 204, deleted.text
    assert await db_session.get(Project, source_id, populate_existing=True) is None
    owner = await db_session.get(Upload, owner_id, populate_existing=True)
    assert owner.project_id == source_id and owner.state == "ready"
    assert (
        await content(httpx_client, project_admin, target_id, value["key"])
    ).content == b"image\x00bytes"
    assert not objects.deleted
    result = await httpx_client.delete(
        project_url(target_id), headers=bearer(project_admin[1])
    )
    assert result.status_code == 204, result.text
    await db_session.refresh(owner)
    assert owner.state == "retired"
    cleaned = await uploads.cleanup(db_session, identifiers=[owner_id])
    assert cleaned["objects"] == 1 and len(objects.deleted) == 1


@pytest.mark.parametrize("backend_source", ["source", "explicit"])
async def test_copy_final_refresh_keeps_new_backend_binding(
    httpx_client,
    project_admin,
    project,
    objects,
    db_session,
    monkeypatch,
    backend_source,
):
    identifier = project.id
    backend = await _seed_backend(db_session, identifier)
    backend_id = backend.id
    pool = await MLBackendService(db_session)._pool_for_registry(backend_id)
    pool_id = pool.id
    if backend_source == "source":
        project.ml_backend_pool_id = pool_id
    await db_session.commit()
    await upload(httpx_client, project_admin, identifier)
    validate = AsyncMock(return_value=None)
    monkeypatch.setattr(routes, "_validate_backend_modality", validate)
    extra = {"ml_backend_id": str(backend_id)} if backend_source == "explicit" else {}
    result = await clone(httpx_client, project_admin, identifier, **extra)
    assert result.status_code == 200, result.text
    target_id = uuid.UUID(result.json()["id"])
    assert result.json()["ml_backend_id"] == str(backend_id)
    target = await db_session.get(Project, target_id, populate_existing=True)
    assert target.ml_backend_pool_id == pool_id
    assert await MLBackendService(db_session).is_enabled(target_id, backend_id)
    validate.assert_awaited_once()


@pytest.mark.parametrize(
    "change,status", [("body", 409), ("list", 409), ("owner", 404), ("actor", 403)]
)
async def test_clone_rechecks_source_and_original_actor_after_remote_setup_before_publish(
    httpx_client,
    project_admin,
    super_admin,
    project,
    objects,
    db_session,
    monkeypatch,
    change,
    status,
):
    identifier, actor_id = project.id, project_admin[0].id
    backend = await _seed_backend(db_session, identifier)
    pool = await MLBackendService(db_session)._pool_for_registry(backend.id)
    project.ml_backend_pool_id = pool.id
    await db_session.commit()
    await upload(httpx_client, project_admin, identifier)
    before = await db_session.scalar(select(func.count()).select_from(Project))

    async def validate(*args):
        # No target row exists while the remote capability check is in progress.
        assert (
            await db_session.scalar(select(func.count()).select_from(Project)) == before
        )
        if change == "body":
            project.annotation_guide = "Changed during setup"
        elif change == "list":
            project.guide_assets = [
                *deepcopy(project.guide_assets),
                {"key": "legacy/changed"},
            ]
        elif change == "owner":
            project.owner_id = super_admin[0].id
        else:
            project_admin[0].is_active = False
        await db_session.flush()

    monkeypatch.setattr(routes, "_validate_backend_modality", validate)
    result = await clone(httpx_client, project_admin, identifier)
    assert result.status_code == status, result.text
    # The failed final check never commits a clone or creates a second reference.
    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(Project.id).where(
                Project.owner_id == actor_id, Project.id != identifier
            )
        )
        is None
    )


async def test_combined_guide_and_backend_patch_returns_busy_instead_of_waiting_on_pool(
    httpx_client, project_admin, project, objects, db_session, test_engine, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    backend_id = pool_id = None
    async with maker() as catalog:
        backend = await _seed_backend(catalog, project.id)
        backend_id = backend.id
        pool_id = (await MLBackendService(catalog)._pool_for_registry(backend_id)).id
        await catalog.commit()
    try:
        value = await upload(httpx_client, project_admin, project.id)
        identifier = project.id
        monkeypatch.setattr(
            routes, "_validate_backend_modality", AsyncMock(return_value=None)
        )
        async with maker() as holder:
            await holder.scalar(
                select(MLBackendServicePool)
                .where(MLBackendServicePool.id == pool_id)
                .with_for_update()
            )
            try:
                response = await asyncio.wait_for(
                    httpx_client.patch(
                        project_url(identifier),
                        headers=bearer(project_admin[1]),
                        json={
                            "guide_assets": [value],
                            "guide_assets_revision": 2,
                            "ml_backend_id": str(backend_id),
                        },
                    ),
                    timeout=2,
                )
                assert response.status_code == 409, response.text
                assert response.json()["detail"]["code"] == "guide_asset_busy"
            finally:
                await holder.rollback()
        await db_session.rollback()
    finally:
        async with maker() as catalog:
            await catalog.execute(
                delete(MLBackendServicePool).where(MLBackendServicePool.id == pool_id)
            )
            await catalog.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
            )
            await catalog.commit()
