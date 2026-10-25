"""v0.10.37 · ML Backend 能力协商落库 + 绑定按 data_type 校验 (epic 阶段 1).

覆盖:
- check_health 探 /setup 把能力快照落进 health_meta["capabilities"] + is_interactive 派生对账.
- PATCH /projects/{id} 绑定 backend 时按 project.data_type 校验模态:
  - 视频项目绑只支持图片的 backend → 422.
  - 视频项目绑 video-capable backend → 200.
  - /setup 探测失败 → fail-open 放行.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from tests.factory import create_project

_IMAGE_SETUP = {
    "name": "grounded-sam2",
    "is_interactive": True,
    "supported_prompts": ["point", "bbox", "text"],
    "supported_text_outputs": ["box", "mask", "both"],
}
_VIDEO_SETUP = {
    "name": "grounded-sam2-video",
    "is_interactive": True,
    "supported_prompts": ["point", "bbox", "text"],
    "supported_trackers": ["sam2_video"],
}


async def _seed_backend(db, project_id, *, name="bk") -> MLBackendRegistry:
    """ADR-0044 · backend 现为全局注册项 (无 project_id); 绑定经 PATCH ml_backend_id,
    模态校验走 db.get(MLBackendRegistry, id)。project_id 仅保留签名兼容, 不入库。
    v0.23.3 ADR-0050 · 同时建 singleton pool (绑定需经 pool 层)。"""
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name=name,
        url=f"http://example-{uuid.uuid4().hex[:8]}/",
        is_interactive=False,
        state="connected",
    )
    db.add(b)
    await db.flush()
    from app.services.ml_backend import MLBackendService

    await MLBackendService(db)._create_singleton_pool(b)
    return b


@pytest.mark.asyncio
async def test_check_health_persists_capabilities_and_derives_is_interactive(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    from app.services.ml_backend import MLBackendService

    async def fake_health_meta(self):
        return True, {"model_version": "v-test"}

    async def fake_setup(self):
        return _VIDEO_SETUP

    with (
        patch(
            "app.services.ml_client.MLBackendClient.health_meta", new=fake_health_meta
        ),
        patch("app.services.ml_client.MLBackendClient.setup", new=fake_setup),
    ):
        svc = MLBackendService(db_session)
        ok = await svc.check_health(backend.id)
        await db_session.commit()

    assert ok is True
    await db_session.refresh(backend)
    caps = backend.health_meta["capabilities"]
    assert caps["supported_trackers"] == ["sam2_video"]
    assert "video" in caps["modalities"]
    assert "image" in caps["modalities"]
    assert backend.is_interactive is True  # 派生对账


@pytest.mark.asyncio
async def test_bind_image_only_backend_to_video_project_rejected(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(locator, *, timeout):
        return _IMAGE_SETUP

    with patch("aap_inference.probe.fetch_backend_setup", new=fake_setup):
        res = await httpx_client.patch(
            f"/api/v1/projects/{proj.id}",
            json={"ml_backend_id": str(backend.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 422, res.text
    assert "video" in res.json()["detail"]


@pytest.mark.asyncio
async def test_bind_video_backend_to_video_project_ok(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(locator, *, timeout):
        return _VIDEO_SETUP

    with patch("aap_inference.probe.fetch_backend_setup", new=fake_setup):
        res = await httpx_client.patch(
            f"/api/v1/projects/{proj.id}",
            json={"ml_backend_id": str(backend.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_bind_failopen_when_setup_unreachable(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(locator, *, timeout):
        raise RuntimeError("connection refused")

    with patch("aap_inference.probe.fetch_backend_setup", new=fake_setup):
        res = await httpx_client.patch(
            f"/api/v1/projects/{proj.id}",
            json={"ml_backend_id": str(backend.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200, res.text  # fail-open


@pytest.mark.asyncio
async def test_bind_backend_with_empty_modalities_failopen(
    httpx_client, db_session, super_admin
):
    """探测成功但能力快照不含模态信号 (无 prompt/tracker) → fail-open 放行, 不误拦纯批量检测后端."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)  # image 项目
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(locator, *, timeout):
        return {"name": "plain-detector", "is_interactive": False}

    with patch("aap_inference.probe.fetch_backend_setup", new=fake_setup):
        res = await httpx_client.patch(
            f"/api/v1/projects/{proj.id}",
            json={"ml_backend_id": str(backend.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200, res.text  # 空模态 → fail-open


@pytest.mark.asyncio
async def test_create_video_project_with_image_backend_rejected(
    httpx_client, db_session, super_admin
):
    """创建即绑定 backend 也走模态校验 (与 PATCH 对称, 防止 create 路径绕过)."""
    user, token = super_admin
    # 绑定的 backend 必须先存在 (挂在任意已有项目上)
    dummy = await create_project(db_session, owner_id=user.id)
    backend = await _seed_backend(db_session, dummy.id, name="image-bk")
    await db_session.commit()

    async def fake_setup(locator, *, timeout):
        return _IMAGE_SETUP

    with patch("aap_inference.probe.fetch_backend_setup", new=fake_setup):
        res = await httpx_client.post(
            "/api/v1/projects",
            json={
                "name": "新视频项目",
                "type_label": "视频-追踪",
                "type_key": "video-track",
                "ai_enabled": True,
                "ml_backend_id": str(backend.id),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 422, res.text
    assert "video" in res.json()["detail"]
