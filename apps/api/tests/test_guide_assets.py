"""Guide bytes are authenticated by current project references and saved owners."""

from copy import deepcopy
import hashlib
import uuid

import pytest
from sqlalchemy import inspect, select, text

from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.services import guide_asset_references as references
from app.services import guide_asset_storage as storage, guide_asset_uploads as uploads
from app.services.managed_original_retirement import managed_storage_identity
from tests._owned_s3 import patch_owned_client
from tests.factory import create_membership, create_project
from tests.test_comment_attachment_uploads import Objects, bearer


@pytest.fixture
def objects(monkeypatch):
    value = Objects()
    patch_owned_client(monkeypatch, value.client)
    monkeypatch.setattr(storage, "storage_service", value)
    monkeypatch.setattr(uploads, "storage_service", value)
    return value


@pytest.fixture
async def project(db_session, project_admin):
    result = await create_project(db_session, owner_id=project_admin[0].id)
    await db_session.commit()
    return result


def endpoint(project, suffix=""):
    state = inspect(project, raiseerr=False)
    identifier = state.identity[0] if state is not None else project
    return f"/api/v1/projects/{identifier}/guide-assets" + (
        f"/{suffix}" if suffix else ""
    )


async def upload(
    client,
    actor,
    project,
    *,
    data=b"image\x00bytes",
    mime="image/png",
    name="参考 图%一.png",
):
    response = await client.post(
        endpoint(project, "upload"),
        params={"file_name": name},
        content=data,
        headers={**bearer(actor[1]), "Content-Type": mime},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def content(client, actor, project, key):
    return await client.get(
        endpoint(project, "content"), params={"key": key}, headers=bearer(actor[1])
    )


async def unlink(client, actor, project, key):
    return await client.delete(
        endpoint(project), params={"key": key}, headers=bearer(actor[1])
    )


@pytest.mark.parametrize(
    "mime,size",
    [
        ("image/png", 1),
        ("image/jpeg", 12),
        ("image/webp", 12),
        ("image/gif", 12),
        ("image/svg+xml", 12),
        ("image/png", 5 * 1024**2),
    ],
)
async def test_upload_atomic_reference_revision_and_authenticated_exact_bytes(
    httpx_client, project_admin, project, objects, db_session, mime, size
):
    project_id, uploader_id = project.id, project_admin[0].id
    await db_session.refresh(project)
    initial = project.guide_assets_revision
    data = b"x" * size
    value = await upload(httpx_client, project_admin, project_id, data=data, mime=mime)
    row = await db_session.scalar(select(Upload).where(Upload.project_id == project_id))
    assert (
        row.state == "ready"
        and row.project_id == project_id
        and row.owner_id == uploader_id
    )
    assert row.sha256 == hashlib.sha256(data).hexdigest() and row.size_bytes == size
    assert value == storage.entry(row)
    assert value["key"] == f"projects/{project_id}/guide/{row.id}"
    assert await db_session.get(Reference, (project_id, row.id)) is not None
    await db_session.refresh(project)
    assert (
        project.guide_assets == [value] and project.guide_assets_revision == initial + 1
    )
    result = await content(httpx_client, project_admin, project_id, value["key"])
    assert result.status_code == 200 and result.content == data
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["content-disposition"].startswith("attachment;")
    assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize(
    "kind,status", [("empty", 400), ("oversize", 413), ("mime", 422), ("encoding", 415)]
)
async def test_invalid_raw_body_never_creates_owner_or_object(
    httpx_client, project_admin, project, objects, db_session, kind, status
):
    data = (
        b""
        if kind == "empty"
        else b"x" * (storage.MAX_BYTES + 1 if kind == "oversize" else 1)
    )
    headers = {
        **bearer(project_admin[1]),
        "Content-Type": "text/plain" if kind == "mime" else "image/png",
    }
    if kind == "encoding":
        headers["Content-Encoding"] = "gzip"
    response = await httpx_client.post(
        endpoint(project, "upload"),
        params={"file_name": "x.png"},
        content=data,
        headers=headers,
    )
    assert response.status_code == status, response.text
    assert await db_session.scalar(select(Upload.id)) is None
    assert not objects.puts and not objects.deleted


@pytest.mark.parametrize("operation", ["upload-init", "upload-complete", "sign-url"])
async def test_old_transfer_routes_do_not_issue_final_key_urls(
    httpx_client, project_admin, project, objects, db_session, operation
):
    key = f"projects/{project.id}/guide/{uuid.uuid4()}-legacy.png"
    project.guide_assets = [
        {
            "key": key,
            "original_name": "legacy.png",
            "content_type": "image/png",
            "size": 1,
            "uploaded_at": "2026-01-01T00:00:00Z",
        }
    ]
    await db_session.commit()
    if operation == "sign-url":
        result = await httpx_client.get(
            endpoint(project, operation),
            params={"key": key},
            headers=bearer(project_admin[1]),
        )
    else:
        payload = (
            {"filename": "x.png", "size": 1, "content_type": "image/png"}
            if operation == "upload-init"
            else {
                "key": key,
                "original_name": "legacy.png",
                "content_type": "image/png",
            }
        )
        result = await httpx_client.post(
            endpoint(project, operation), json=payload, headers=bearer(project_admin[1])
        )
    assert result.status_code == 409 and "content_path" in result.json()["detail"]
    assert "upload_url" not in result.text and not objects.puts and not objects.reads


@pytest.mark.parametrize("role", ["annotator", "reviewer", "viewer"])
async def test_current_members_read_but_cannot_manage(
    httpx_client, project_admin, annotator, project, objects, db_session, role
):
    value = await upload(httpx_client, project_admin, project)
    member = await create_membership(
        db_session,
        project_id=project.id,
        user_id=annotator[0].id,
        role=role,
        assigned_by=project_admin[0].id,
    )
    await db_session.commit()
    project_id = project.id
    allowed = await content(httpx_client, annotator, project_id, value["key"])
    assert allowed.status_code == 200, allowed.text
    denied = await unlink(httpx_client, annotator, project_id, value["key"])
    assert denied.status_code == 403
    await db_session.refresh(member)
    await db_session.delete(member)
    await db_session.commit()
    assert (
        await content(httpx_client, annotator, project_id, value["key"])
    ).status_code == 404


async def test_invisible_nonmember_gets_404_on_read_and_write(
    httpx_client, project_admin, annotator, project, objects
):
    key = (await upload(httpx_client, project_admin, project))["key"]
    project_id = project.id
    assert (await content(httpx_client, annotator, project_id, key)).status_code == 404
    assert (await unlink(httpx_client, annotator, project_id, key)).status_code == 404
    result = await httpx_client.post(
        endpoint(project_id, "upload"),
        params={"file_name": "x.png"},
        content=b"x",
        headers={**bearer(annotator[1]), "Content-Type": "image/png"},
    )
    assert result.status_code == 404


async def test_unknown_legacy_can_be_unlinked_without_claiming_or_deleting_bytes(
    httpx_client, project_admin, project, objects, db_session
):
    project_id = project.id
    key = f"projects/{project_id}/guide/{uuid.uuid4()}-旧 图%.svg"
    project.guide_assets = [
        {
            "key": key,
            "original_name": "旧 图%.svg",
            "content_type": "image/svg+xml",
            "size": 3,
            "uploaded_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    await db_session.commit()
    objects.objects[objects.bucket, key] = b"old"
    result = await content(httpx_client, project_admin, project_id, key)
    assert (
        result.status_code == 409
        and result.json()["detail"]["code"] == "guide_asset_storage_unpinned"
    )
    assert not objects.reads
    assert (
        await unlink(httpx_client, project_admin, project_id, key)
    ).status_code == 200
    assert objects.objects[objects.bucket, key] == b"old" and not objects.deleted
    assert await db_session.scalar(select(Upload.id)) is None
    await db_session.refresh(project)
    assert project.guide_assets == []


async def test_shared_copy_keeps_original_key_after_source_unlink_until_last_reference(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    target = await create_project(db_session, owner_id=project_admin[0].id)
    target_id = target.id
    current = await uploads.lock_projects(
        db_session, project_admin[0], [project_id, target_id], write_ids=[target_id]
    )
    await references.copy_references(
        db_session, current[project_id], current[target_id]
    )
    await db_session.commit()
    identifier = uuid.UUID(value["key"].split("/")[-1])
    assert (
        await unlink(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 200
    row = await db_session.get(Upload, identifier, populate_existing=True)
    assert row.state == "ready" and not objects.deleted
    assert (
        await content(httpx_client, project_admin, target_id, value["key"])
    ).content == b"image\x00bytes"
    assert (
        await unlink(httpx_client, project_admin, target_id, value["key"])
    ).status_code == 200
    await db_session.refresh(row)
    assert row.state == "retired" and not objects.deleted
    result = await uploads.cleanup(db_session, identifiers=[identifier])
    assert result["objects"] == 1 and await db_session.get(Upload, identifier) is None
    assert len(objects.deleted) == 1


async def test_same_json_without_reference_does_not_authorize_known_owner(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    target = await create_project(db_session, owner_id=project_admin[0].id)
    target.guide_assets = [deepcopy(value)]
    await db_session.commit()
    target_id = target.id
    before = len(objects.reads)
    result = await content(httpx_client, project_admin, target_id, value["key"])
    assert result.status_code == 409 and len(objects.reads) == before
    assert (
        await unlink(httpx_client, project_admin, target_id, value["key"])
    ).status_code == 200
    assert (
        await content(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 200


async def test_saved_bucket_exact_read_corruption_and_missing_are_not_current_bucket_fallbacks(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    saved = deepcopy(row.storage_ref)
    mapped = {
        **saved,
        "bucket": "guide-saved-test-bucket",
        "storage_identity": managed_storage_identity(
            saved["endpoint_url"], "guide-saved-test-bucket"
        ),
    }
    await db_session.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    row.storage_ref = mapped
    await db_session.commit()
    objects.objects[mapped["bucket"], value["key"]] = b"image\x00bytes"
    objects.objects[saved["bucket"], value["key"]] = b"current trap"
    result = await content(httpx_client, project_admin, project_id, value["key"])
    assert result.status_code == 200 and result.content == b"image\x00bytes"
    objects.objects[mapped["bucket"], value["key"]] = b"other\x00bytes"
    assert (
        await content(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 409
    del objects.objects[mapped["bucket"], value["key"]]
    assert (
        await content(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 404
    assert objects.objects[saved["bucket"], value["key"]] == b"current trap"


async def test_write_failure_keeps_pending_and_does_not_publish_or_compensate(
    httpx_client, project_admin, project, objects, db_session
):
    project_id = project.id
    objects.fail_put = True
    result = await httpx_client.post(
        endpoint(project_id, "upload"),
        params={"file_name": "x.png"},
        content=b"x",
        headers={**bearer(project_admin[1]), "Content-Type": "image/png"},
    )
    assert result.status_code == 503
    row = await db_session.scalar(select(Upload).where(Upload.project_id == project_id))
    assert row.state == "pending" and not objects.deleted
    assert await db_session.get(Reference, (project_id, row.id)) is None
    await db_session.refresh(project)
    assert project.guide_assets == []


async def test_body_autosave_does_not_replace_guide_assets(
    httpx_client, project_admin, project, objects
):
    value = await upload(httpx_client, project_admin, project)
    result = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        headers=bearer(project_admin[1]),
        json={"annotation_guide": "# 指引\n\n保留**图片**"},
    )
    assert result.status_code == 200 and result.json()["guide_assets"] == [value]
