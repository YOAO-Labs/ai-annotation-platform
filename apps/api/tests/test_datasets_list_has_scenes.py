from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, Scene
from app.services.dataset import DatasetService


@pytest.mark.asyncio
async def test_list_datasets_filters_by_derived_has_scenes(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    suffix = uuid.uuid4().hex[:6]
    plain = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-HS-P-{suffix}",
        name="plain dataset",
        data_type="image",
        created_by=user.id,
    )
    scene_ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-HS-S-{suffix}",
        name="scene dataset",
        data_type="image",
        created_by=user.id,
    )
    db_session.add_all([plain, scene_ds])
    await db_session.flush()
    db_session.add(
        Scene(
            id=uuid.uuid4(),
            display_id=f"S-HS-{suffix}",
            dataset_id=scene_ds.id,
            name="scene-a",
        )
    )
    await db_session.flush()

    svc = DatasetService(db_session)
    scene_items, _ = await svc.list(has_scenes=True)
    plain_items, _ = await svc.list(has_scenes=False)

    assert str(scene_ds.id) in {item["id"] for item in scene_items}
    assert str(plain.id) not in {item["id"] for item in scene_items}
    assert str(plain.id) in {item["id"] for item in plain_items}
    assert str(scene_ds.id) not in {item["id"] for item in plain_items}
    assert all(item["has_scenes"] is True for item in scene_items)
    assert all(item["has_scenes"] is False for item in plain_items)


@pytest.mark.asyncio
async def test_list_datasets_data_type_filter_uses_media_kind(
    db_session: AsyncSession, super_admin
):
    """data_type 过滤按 media kind 归一:查 "lidar" 命中存成 "point_cloud" 的数据集
    (与 link 硬门同口径),且不串到 image。"""
    user, _ = super_admin
    suffix = uuid.uuid4().hex[:6]
    pc_ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-PC-{suffix}",
        name="point cloud dataset",
        data_type="point_cloud",
        created_by=user.id,
    )
    img_ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-IMG-{suffix}",
        name="image dataset",
        data_type="image",
        created_by=user.id,
    )
    db_session.add_all([pc_ds, img_ds])
    await db_session.flush()

    svc = DatasetService(db_session)
    lidar_items, _ = await svc.list(data_type="lidar")
    pc_items, _ = await svc.list(data_type="point_cloud")
    image_items, _ = await svc.list(data_type="image")

    lidar_ids = {item["id"] for item in lidar_items}
    pc_ids = {item["id"] for item in pc_items}
    image_ids = {item["id"] for item in image_items}

    assert str(pc_ds.id) in lidar_ids
    assert str(pc_ds.id) in pc_ids
    assert str(pc_ds.id) not in image_ids
    assert str(img_ds.id) not in lidar_ids
    assert str(img_ds.id) in image_ids


@pytest.mark.asyncio
async def test_dataset_list_api_returns_filtered_pages_and_empty_envelope(
    httpx_client, super_admin
):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    prefix = f"list-envelope-{uuid.uuid4().hex}"
    created = set()
    for suffix in ("first", "second"):
        response = await httpx_client.post(
            "/api/v1/datasets",
            headers=headers,
            json={"name": f"{prefix}-{suffix}", "data_type": "image"},
        )
        assert response.status_code == 201, response.text
        created.add(response.json()["id"])

    returned = []
    for offset in (0, 1):
        response = await httpx_client.get(
            "/api/v1/datasets",
            headers=headers,
            params={"search": prefix, "limit": 1, "offset": offset},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert (page["total"], page["limit"], page["offset"]) == (2, 1, offset)
        assert len(page["items"]) == 1
        returned.append(page["items"][0]["id"])
    assert set(returned) == created

    response = await httpx_client.get(
        "/api/v1/datasets",
        headers=headers,
        params={"search": f"{prefix}-absent", "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0, "limit": 1, "offset": 0}
