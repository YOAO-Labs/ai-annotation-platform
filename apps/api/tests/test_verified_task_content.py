import hashlib
import io
import uuid

from PIL import Image
import pytest

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services import dataset_content as svc
from app.services.dataset_assets import register_item_asset
from app.services.storage import storage_service


def headers(actor):
    return {"Authorization": f"Bearer {actor[1]}"}


def picture(color):
    output = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
async def stored_world(db_session, project_admin, annotator):
    actor = project_admin[0]
    dataset = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:10]}", name="Stored", created_by=actor.id
    )
    project = Project(
        display_id=f"pr-{uuid.uuid4().hex[:10]}",
        name="Stored",
        owner_id=actor.id,
        type_key="bbox",
        type_label="Bounding box",
    )
    db_session.add_all([dataset, project])
    await db_session.flush()
    data = picture("red")
    key = f"content-acceptance/{uuid.uuid4().hex}/image.png"
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="image.png",
        file_path=key,
        file_type="image",
        file_size=len(data),
    )
    db_session.add(item)
    await db_session.flush()
    asset = await register_item_asset(
        db_session, item, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )
    task = Task(
        display_id=f"ta-{uuid.uuid4().hex[:10]}",
        project_id=project.id,
        dataset_item_id=item.id,
        file_name="image.png",
        file_path=key,
        file_type="image",
        status="pending",
        assignee_id=annotator[0].id,
    )
    db_session.add(task)
    member = ProjectMember(
        project_id=project.id, user_id=annotator[0].id, role="annotator"
    )
    db_session.add(member)
    await db_session.flush()
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=data
    )
    try:
        yield task, asset, member, data, key
    finally:
        storage_service.client.delete_object(
            Bucket=storage_service.datasets_bucket, Key=key
        )


async def test_real_stored_object_is_verified_and_overwrite_is_rejected(
    httpx_client, db_session, project_admin, stored_world
):
    task, asset, _, data, key = stored_world
    url = f"/api/v1/files/tasks/{task.id}/content"
    sha = asset.checksum_value
    identity = await httpx_client.get(url + "-identity", headers=headers(project_admin))
    assert identity.status_code == 200 and identity.json()["sha256"] == sha
    assert identity.json()["original_availability"] == "unchecked"
    response = await httpx_client.get(
        url, params={"sha256": sha}, headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    assert response.content == data and response.headers["x-content-sha256"] == sha
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=picture("blue")
    )
    response = await httpx_client.get(
        url, params={"sha256": sha}, headers=headers(project_admin)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] in {"source_changed", "content_mismatch"}
    await db_session.refresh(asset)
    assert asset.checksum_value == sha


async def test_task_authorization_and_legacy_url_share_visibility(
    httpx_client,
    db_session,
    project_admin,
    annotator,
    reviewer,
    stored_world,
    monkeypatch,
):
    task, asset, member, data, _ = stored_world
    base = f"/api/v1/files/tasks/{task.id}"
    response = await httpx_client.get(
        base + "/content",
        params={"sha256": asset.checksum_value},
        headers=headers(annotator),
    )
    assert response.status_code == 200 and response.content == data
    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        lambda *args, **kw: "http://storage.test/legacy",
    )
    response = await httpx_client.get(base + "/file-url", headers=headers(annotator))
    assert (
        response.status_code == 200
        and response.json()["url"] == "http://storage.test/legacy"
    )
    for suffix in ["/file-url", "/content?sha256=" + asset.checksum_value]:
        assert (
            await httpx_client.get(base + suffix, headers=headers(reviewer))
        ).status_code in {403, 404}
    await db_session.delete(member)
    await db_session.flush()
    assert (
        await httpx_client.get(
            base + "/content",
            params={"sha256": asset.checksum_value},
            headers=headers(annotator),
        )
    ).status_code in {403, 404}


@pytest.mark.parametrize(
    "dataset_bound,file_type,bucket_attribute",
    [
        (True, "image", "datasets_bucket"),
        (False, "point_cloud", "datasets_bucket"),
        (False, "image", "bucket"),
    ],
)
async def test_file_url_uses_the_tasks_actual_purpose_bucket(
    httpx_client,
    db_session,
    annotator,
    stored_world,
    monkeypatch,
    dataset_bound,
    file_type,
    bucket_attribute,
):
    task = stored_world[0]
    if not dataset_bound:
        task.dataset_item_id = None
    task.file_type = file_type
    await db_session.flush()
    signed = []

    def sign(key, **kwargs):
        signed.append((key, kwargs["bucket"]))
        return "http://storage.test/correct-purpose"

    monkeypatch.setattr(storage_service, "generate_download_url", sign)
    response = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/file-url", headers=headers(annotator)
    )
    assert response.status_code == 200, response.text
    assert signed == [(task.file_path, getattr(storage_service, bucket_attribute))]


async def test_membership_revocation_after_copy_closes_snapshot(
    db_session, annotator, stored_world, monkeypatch
):
    task, asset, member, _, _ = stored_world
    original = svc.snapshots.capture
    snapshots = []

    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        snapshots.append(result)
        await db_session.delete(member)
        await db_session.flush()
        return result

    monkeypatch.setattr(svc.snapshots, "capture", capture)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await svc.task_snapshot(db_session, task.id, annotator[0], asset.checksum_value)
    assert snapshots[0].file.closed


async def test_limited_api_key_cannot_read_task_content(
    httpx_client, project_admin, stored_world
):
    task, asset, _, _, _ = stored_world
    result = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "limited", "scopes": ["annotations:read"]},
    )
    assert result.status_code == 201
    response = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content",
        params={"sha256": asset.checksum_value},
        headers={"Authorization": "Bearer " + result.json()["plaintext"]},
    )
    assert response.status_code == 403
