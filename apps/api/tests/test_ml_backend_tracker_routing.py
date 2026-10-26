"""v0.21.25 (阶段 R) · get_tracker_backend 按 tracker 能力选后端。

回归: sam3_video 此前被 get_project_backend 静默路由到项目绑定的 grounded-sam2
(只支持 sam2_video)。按 health_meta.capabilities.supported_trackers 路由后,
sam3_video → 声明了它的 backend, 与项目绑定无关。
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


async def _seed(db, *, name: str, trackers: list[str]) -> MLBackendRegistry:
    # v0.23.3 ADR-0050 · 每 registry 需要 singleton pool 才能 set_enabled / 绑定项目。
    registry, _pool = await create_registry_with_pool(
        db,
        id=uuid.uuid4(),
        name=name,
        url=f"http://example-{uuid.uuid4().hex[:8]}/",
        is_interactive=True,
        state="connected",
        health_meta={"capabilities": {"supported_trackers": trackers}},
    )
    return registry


@pytest.mark.asyncio
async def test_get_tracker_backend_routes_by_capability_not_binding(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    sam2 = await _seed(db_session, name="grounded-sam2", trackers=["sam2_video"])
    sam3 = await _seed(db_session, name="sam3-backend", trackers=["sam3_video"])
    svc = MLBackendService(db_session)
    await svc.set_enabled(proj.id, sam2.id, True)
    await svc.set_enabled(proj.id, sam3.id, True)
    # 项目显式绑定到 sam2 (复现现网「绑定 grounded-sam2」配置)。
    # v0.23.3: 项目主绑定存 pool id (经 sam2 的 singleton pool 解析)。
    proj.ml_backend_pool_id = await svc.pool_id_for_registry(sam2.id)
    await db_session.flush()

    # sam3_video 按能力挑 sam3-backend, 不受「项目绑定 sam2」影响 (核心回归)。
    got3 = await svc.get_tracker_backend(proj.id, "sam3_video")
    assert got3 is not None and got3.id == sam3.id
    # sam2_video 挑 sam2 (绑定 + 支持)。
    got2 = await svc.get_tracker_backend(proj.id, "sam2_video")
    assert got2 is not None and got2.id == sam2.id
    # 无 backend 声明 mock_bbox → None (交由 mock adapter 无需 backend 处理)。
    assert await svc.get_tracker_backend(proj.id, "mock_bbox") is None


@pytest.mark.asyncio
async def test_get_tracker_backend_none_when_capable_backend_not_enabled(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    # 注册但未对本项目启用 → 挑不到 (显式失败, 而非静默错投到别的 backend)。
    await _seed(db_session, name="sam3-backend", trackers=["sam3_video"])
    await db_session.flush()
    svc = MLBackendService(db_session)
    assert await svc.get_tracker_backend(proj.id, "sam3_video") is None


@pytest.mark.asyncio
async def test_get_tracker_backend_excludes_disconnected_backend(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend = await _seed(db_session, name="sam3-backend", trackers=["sam3_video"])
    backend.state = "disconnected"
    svc = MLBackendService(db_session)
    await svc.set_enabled(proj.id, backend.id, True)

    assert await svc.get_tracker_backend(proj.id, "sam3_video") is None


@pytest.mark.asyncio
async def test_combo_requires_one_backend_with_both_capabilities(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    discover = await _seed(db_session, name="discover", trackers=["sam3_video"])
    propagate = await _seed(
        db_session, name="propagate", trackers=["sam3_video_interactive"]
    )
    svc = MLBackendService(db_session)
    await svc.set_enabled(proj.id, discover.id, True)
    await svc.set_enabled(proj.id, propagate.id, True)

    assert (
        await svc.get_tracker_backend_for_capabilities(
            proj.id, ["sam3_video", "sam3_video_interactive"]
        )
        is None
    )
