"""v0.13.x · type_key 与 data_type 媒体维度一致性 invariant (收口 PR#30 review #5).

前端用 `type_key === "lidar"` 入 3D Stage, 后端 manifest 用 `data_type == "lidar"`
放行点云端点. 两侧落库不一致就会撕裂 (前端进 3D 台, 后端拒提供 manifest);
本 invariant 阻断这种 drift, 创建 / 更新接口必经此关.
"""

from __future__ import annotations

import pytest
from aap_workspace.configuration_policy import (
    ConfigurationPolicyError,
    assert_project_kind_consistent,
    data_type_from_type_key,
    legacy_type_key_from_data_type,
)
from tests.factory import create_project


# ── 纯单元: helper ───────────────────────────────────────────────────────────


def test_invariant_passes_when_consistent():
    assert_project_kind_consistent("lidar", "lidar")
    assert_project_kind_consistent("image-det", "image")
    assert_project_kind_consistent("image-seg", "image")
    assert_project_kind_consistent("video-track", "video")


def test_invariant_passes_when_either_missing():
    """cross-fill 会补缺, 此处不抛."""
    assert_project_kind_consistent(None, "lidar")
    assert_project_kind_consistent("lidar", None)
    assert_project_kind_consistent(None, None)


def test_invariant_raises_on_drift():
    with pytest.raises(ConfigurationPolicyError) as exc:
        assert_project_kind_consistent("lidar", "image")
    assert exc.value.status_code == 422
    assert "media" in exc.value.detail.lower() or "媒体" in exc.value.detail

    with pytest.raises(ConfigurationPolicyError):
        assert_project_kind_consistent("image-det", "lidar")
    with pytest.raises(ConfigurationPolicyError):
        assert_project_kind_consistent("video-track", "image")


def test_legacy_helpers_round_trip():
    """互推 helper 跟 invariant 同源 (避免再次出现新 type_key 漏更新派生表的漂移)."""
    for tk in ("lidar", "image-det", "image-seg", "video-track"):
        dt = data_type_from_type_key(tk)
        assert_project_kind_consistent(tk, dt)  # 任一边推得另一边, invariant 必过
    for dt in ("lidar", "image", "video"):
        tk = legacy_type_key_from_data_type(dt)
        assert_project_kind_consistent(tk, dt)


# ── 集成: POST/PATCH /projects ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_project_rejects_kind_drift(httpx_client, db_session, super_admin):
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "drift",
            "type_label": "drift",
            "type_key": "lidar",
            "data_type": "image",  # 与 type_key 不一致 → 422
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_create_project_cross_fill_still_works(
    httpx_client, db_session, super_admin
):
    """只给 data_type 时 cross-fill 补 type_key, 不触发 invariant."""
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "lidar-only-data-type",
            "type_label": "点云",
            "data_type": "lidar",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data_type"] == "lidar"
    assert body["type_key"] == "lidar"


@pytest.mark.asyncio
async def test_patch_project_rejects_kind_drift(httpx_client, db_session, super_admin):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.data_type = "image"
    await db_session.commit()

    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"type_key": "lidar"},  # 单改 type_key, 与现 data_type=image 撕裂
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422, res.text
    await db_session.refresh(proj)
    assert proj.type_key == "image-det"
    assert proj.data_type == "image"


@pytest.mark.asyncio
async def test_patch_project_kind_pair_change_passes(
    httpx_client, db_session, super_admin
):
    """同时改 type_key + data_type 且一致 → 200 (允许有意改造)."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.data_type = "image"
    await db_session.commit()

    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"type_key": "video-track", "data_type": "video"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
