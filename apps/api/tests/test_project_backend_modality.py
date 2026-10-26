"""ML Backend capability negotiation + project binding modality gate.

覆盖:
- check_health 探 backend、把能力快照写进 health_meta["capabilities"] + is_interactive 派生对账;
  公开 registry facts 是冻结 Mapping/tuple, 不再返回可变 ORM 行.
- PATCH /projects/{id} 绑定 backend 时按 project.data_type 校验模态:
  - 视频项目绑只支持图片的 backend → 422.
  - 视频项目绑 video-capable backend → 200.
  - 探测返回 null 模态 (体检目标不可达) → fail-open 放行.
"""

from __future__ import annotations

import uuid

import pytest

from aap_inference import extract_capabilities
from app.services.ml_backend import MLBackendService
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


async def _seed_backend(db, *, name="bk"):
    """ADR-0044 · backend 是全局注册项; 创建即带 singleton pool + member."""
    return await MLBackendService(db).create_registry(
        name, f"http://example-{uuid.uuid4().hex[:8]}.invalid"
    )


def _probe_modalities(setup):
    capabilities = extract_capabilities(setup)

    async def probe(registry_id, probe_subject, *, timeout, limits):
        return list(capabilities["modalities"]) if capabilities else []

    return probe


def _fake_health(monkeypatch):
    class Client:
        async def health_meta(self, *, gpu_health_challenge=None):
            return True, {"model_version": "v-test"}

        async def setup(self):
            return _VIDEO_SETUP

    def factory(db):
        def create(backend):
            return Client()

        return create

    monkeypatch.setattr(
        "app.services.inference_backend_control.health_observation_client_factory",
        factory,
    )


@pytest.mark.asyncio
async def test_check_health_persists_capabilities_and_derives_is_interactive(
    db_session, monkeypatch
):
    backend = await _seed_backend(db_session)
    await db_session.commit()
    _fake_health(monkeypatch)

    ok = await MLBackendService(db_session).check_health(backend.id)
    await db_session.commit()

    assert ok is True
    facts = await MLBackendService(db_session).get_registry(backend.id)
    assert facts.state == "connected"
    caps = facts.health_meta["capabilities"]
    assert caps["supported_trackers"] == ("sam2_video",)
    assert "video" in caps["modalities"]
    assert "image" in caps["modalities"]
    assert facts.is_interactive is True  # 派生对账


@pytest.mark.asyncio
async def test_bind_image_only_backend_to_video_project_rejected(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.inference_backend_probe.backend_probe",
        lambda: _probe_modalities(_IMAGE_SETUP),
    )
    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"ml_backend_id": str(backend.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422, res.text
    assert "video" in res.json()["detail"]


@pytest.mark.asyncio
async def test_bind_video_backend_to_video_project_ok(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.inference_backend_probe.backend_probe",
        lambda: _probe_modalities(_VIDEO_SETUP),
    )
    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"ml_backend_id": str(backend.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_bind_failopen_when_setup_unreachable(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend = await _seed_backend(db_session)
    await db_session.commit()

    async def probe(registry_id, probe_subject, *, timeout, limits):
        # The Inference probe owner reports null modalities when /setup is unreachable.
        return None

    monkeypatch.setattr(
        "app.services.inference_backend_probe.backend_probe", lambda: probe
    )
    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"ml_backend_id": str(backend.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text  # fail-open


@pytest.mark.asyncio
async def test_bind_backend_with_empty_modalities_failopen(
    httpx_client, db_session, super_admin, monkeypatch
):
    """探测成功但能力快照不含模态信号 (无 prompt/tracker) → fail-open 放行, 不误拦纯批量检测后端."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)  # image 项目
    backend = await _seed_backend(db_session)
    await db_session.commit()

    async def probe(registry_id, probe_subject, *, timeout, limits):
        return []

    monkeypatch.setattr(
        "app.services.inference_backend_probe.backend_probe", lambda: probe
    )
    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        json={"ml_backend_id": str(backend.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text  # 空模态 → fail-open


@pytest.mark.asyncio
async def test_create_video_project_with_image_backend_rejected(
    httpx_client, db_session, super_admin, monkeypatch
):
    """创建即绑定 backend 也走模态校验 (与 PATCH 对称, 防止 create 路径绕过)."""
    user, token = super_admin
    backend = await _seed_backend(db_session, name="image-bk")
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.inference_backend_probe.backend_probe",
        lambda: _probe_modalities(_IMAGE_SETUP),
    )
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
