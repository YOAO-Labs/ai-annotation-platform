from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from aap_assets_images.geometry import pyramid_levels
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.image_pyramid import (
    ImagePyramidAssetUrl,
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidLevel,
    ImagePyramidManifest,
    ImagePyramidOverviewManifest,
    ImagePyramidOverviewUrl,
    ImagePyramidResponse,
)
from app.services.image_pyramid_contract import (
    ImagePyramidManifestResult,
    ImagePyramidRetryPlan,
)
from tests.factory import build_tool_bindings

pytestmark = pytest.mark.asyncio


async def _make_image_task(db_session, owner_id):
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
        width=8193,
        height=6145,
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
    return task


def _manifest() -> ImagePyramidManifest:
    return ImagePyramidManifest(
        schema="aap-image-pyramid/v1",
        generation=1,
        sourceFingerprint="sha256:" + "a" * 64,
        normalizationVersion="exif-autorotate-srgb-v1",
        width=8193,
        height=6145,
        tileSize=512,
        overlap=1,
        format="webp",
        levels=[
            ImagePyramidLevel.model_validate(row) for row in pyramid_levels(8193, 6145)
        ],
        overview=ImagePyramidOverviewManifest(
            width=512, height=384, contentDigest="sha256:" + "b" * 64
        ),
    )


class _ImagePyramidApiStub:
    def __init__(self):
        self.manifest_calls = []
        self.asset_url_calls = []
        self.retry_calls = []
        self.task_id = None

    async def task_summaries(self, tasks, dimensions):
        return {}

    async def manifest(self, task, *, if_none_match):
        self.task_id = task.task_id
        self.manifest_calls.append(if_none_match)
        etag = '"pyramid-1-immutable"'
        if if_none_match == etag:
            return ImagePyramidManifestResult(
                None, etag, "private, max-age=60", "not_modified"
            )
        return ImagePyramidManifestResult(
            ImagePyramidResponse(
                task_id=task.task_id,
                status="ready",
                required=True,
                generation=1,
                manifest=_manifest(),
                overview=ImagePyramidOverviewUrl(
                    url="https://storage.invalid/overview.webp",
                    expires_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                ),
            ),
            etag,
            "private, max-age=60",
            "ready",
        )

    async def asset_urls(self, task, payload: ImagePyramidAssetUrlsRequest):
        self.task_id = task.task_id
        self.asset_url_calls.append(payload)
        return ImagePyramidAssetUrlsResponse(
            task_id=task.task_id,
            generation=1,
            expires_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            items=[
                ImagePyramidAssetUrl(
                    kind=item.kind,
                    generation=item.generation,
                    level=getattr(item, "level", None),
                    x=getattr(item, "x", None),
                    y=getattr(item, "y", None),
                    url=f"https://storage.invalid/{index}",
                )
                for index, item in enumerate(payload.items)
            ],
        )

    async def retry(self, task, *, operation_id):
        self.task_id = task.task_id
        self.retry_calls.append(operation_id)
        return ImagePyramidRetryPlan(
            status="pending",
            owner_kind="dataset_item",
            owner_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
        )


async def test_asset_owner_constraint_rejects_zero_or_two_owners(
    db_session, super_admin
):
    user, _ = super_admin
    task = await _make_image_task(db_session, user.id)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ImagePyramidAsset(profile_version="pyramid-v1"))
            await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ImagePyramidAsset(
                    dataset_item_id=task.dataset_item_id,
                    task_id=task.id,
                    profile_version="pyramid-v1",
                )
            )
            await db_session.flush()


async def test_image_pyramid_http_routes_preserve_cache_and_idempotency_contract(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _make_image_task(db_session, user.id)
    api = _ImagePyramidApiStub()
    monkeypatch.setattr(
        "app.api.v1.tasks.image_pyramid.bind_image_pyramid_api",
        lambda db, *, actor_id: api,
    )
    headers = {"Authorization": f"Bearer {token}"}

    unauthorized = await httpx_client.get(f"/api/v1/tasks/{task.id}/image-pyramid")
    assert unauthorized.status_code == 401

    manifest = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid", headers=headers
    )
    assert manifest.status_code == 200
    assert manifest.headers["cache-control"] == "private, max-age=60"
    assert manifest.headers["etag"] == '"pyramid-1-immutable"'
    assert manifest.json()["manifest"]["schema"] == "aap-image-pyramid/v1"

    not_modified = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/image-pyramid",
        headers={**headers, "If-None-Match": '"pyramid-1-immutable"'},
    )
    assert not_modified.status_code == 304
    assert not not_modified.content
    assert api.manifest_calls == [None, '"pyramid-1-immutable"']

    payload = {
        "items": [
            {"kind": "overview", "generation": 1},
            {"kind": "tile", "generation": 1, "level": 0, "x": 1, "y": 2},
        ]
    }
    asset_urls = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/asset-urls",
        headers=headers,
        json=payload,
    )
    assert asset_urls.status_code == 200
    assert len(asset_urls.json()["items"]) == 2
    assert len(api.asset_url_calls) == 1

    operation_id = uuid.uuid4()
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/image-pyramid/retry",
        headers={**headers, "Idempotency-Key": str(operation_id)},
    )
    assert accepted.status_code == 202
    assert api.retry_calls == [operation_id]
    assert accepted.json()["status"] == "pending"
    assert accepted.json()["job_id"] is not None
