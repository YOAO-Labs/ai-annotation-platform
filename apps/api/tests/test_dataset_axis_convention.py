"""Dataset.metadata_["axis_convention"] 读写覆盖。

覆盖:
  1. POST /datasets 不传 axis_convention → 返回 None, DB metadata = {}
  2. POST /datasets 传 sustechpoints_demo → 返回该值, metadata 含 key
  3. PUT /datasets/{id} 仅传 name → axis_convention 不被改 (保留 apollo)
  4. PUT /datasets/{id} 显式传 None → 清除 (返回 None, metadata 无 key)
  5. PUT /datasets/{id} 传无效字符串 → 422

ADR: docs/adr/0034-lidar-axis-convention.md
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.dataset import Dataset
from app.services.storage import storage_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    """create_dataset 会调 ensure_bucket / create_folder, 测试里 stub 掉避免接 MinIO."""
    monkeypatch.setattr(storage_service, "ensure_bucket", lambda *a, **k: None)
    monkeypatch.setattr(storage_service, "create_folder", lambda *a, **k: None)


async def test_create_dataset_without_axis_convention_defaults_to_none(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    name = f"ds-noaxis-{uuid.uuid4().hex[:6]}"
    resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": name, "description": "no axis"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["axis_convention"] is None

    ds_id = uuid.UUID(body["id"])
    ds = await db_session.get(Dataset, ds_id)
    assert ds is not None
    assert ds.metadata_ == {}


async def test_create_dataset_with_axis_convention_persists(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    name = f"ds-axis-{uuid.uuid4().hex[:6]}"
    resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={
            "name": name,
            "description": "sustech demo",
            "data_type": "lidar",
            "axis_convention": "sustechpoints_demo",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["axis_convention"] == "sustechpoints_demo"

    ds_id = uuid.UUID(body["id"])
    get_resp = await httpx_client.get(
        f"/api/v1/datasets/{ds_id}", headers=_bearer(token)
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["axis_convention"] == "sustechpoints_demo"

    ds = await db_session.get(Dataset, ds_id)
    assert ds is not None
    assert ds.metadata_ == {"axis_convention": "sustechpoints_demo"}


async def test_update_dataset_without_axis_field_preserves_existing(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    name = f"ds-keep-{uuid.uuid4().hex[:6]}"
    create_resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": name, "axis_convention": "apollo"},
    )
    assert create_resp.status_code == 201
    ds_id = uuid.UUID(create_resp.json()["id"])

    new_name = f"{name}-renamed"
    update_resp = await httpx_client.put(
        f"/api/v1/datasets/{ds_id}",
        headers=_bearer(token),
        json={"name": new_name},
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()
    assert body["name"] == new_name
    assert body["axis_convention"] == "apollo"

    db_session.expire_all()
    ds = await db_session.get(Dataset, ds_id)
    assert ds is not None
    assert ds.metadata_ == {"axis_convention": "apollo"}


async def test_update_dataset_explicit_none_clears_axis(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    name = f"ds-clear-{uuid.uuid4().hex[:6]}"
    create_resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": name, "axis_convention": "kitti_camera"},
    )
    assert create_resp.status_code == 201
    ds_id = uuid.UUID(create_resp.json()["id"])

    update_resp = await httpx_client.put(
        f"/api/v1/datasets/{ds_id}",
        headers=_bearer(token),
        json={"axis_convention": None},
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()
    assert body["axis_convention"] is None

    db_session.expire_all()
    ds = await db_session.get(Dataset, ds_id)
    assert ds is not None
    assert "axis_convention" not in (ds.metadata_ or {})


async def test_update_dataset_rejects_invalid_axis_convention(
    httpx_client, super_admin
):
    _, token = super_admin
    name = f"ds-bad-{uuid.uuid4().hex[:6]}"
    create_resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": name},
    )
    assert create_resp.status_code == 201
    ds_id = uuid.UUID(create_resp.json()["id"])

    resp = await httpx_client.put(
        f"/api/v1/datasets/{ds_id}",
        headers=_bearer(token),
        json={"axis_convention": "nonsense_axis"},
    )
    assert resp.status_code == 422
