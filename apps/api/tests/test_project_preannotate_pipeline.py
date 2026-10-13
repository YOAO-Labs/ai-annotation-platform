"""v0.18.27 · 项目级「已保存的编排」(Project.preannotate_pipeline, 方案 A)。

PATCH /projects/{id} 读写 preannotate_pipeline:
- 单 / 多阶段 → persist + GET 原样返回。
- 显式 null → 清除。
- 不传 → 不动 (exclude_unset)。
- 非法 stage (重复序号 / 无源阶段 / 坏 uuid) → 422。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from tests.conftest import create_registry_with_pool


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(db: AsyncSession, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-{suffix}",
        name=f"pipe-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        ai_enabled=True,
    )
    db.add(proj)
    await db.flush()
    # v0.19.0 ADR-0044 · 全局注册项 + 项目启用关联 (编排阶段引用 registry id)。
    detect, detect_pool = await create_registry_with_pool(
        db,
        name="detect",
        url="http://detect/",
        is_interactive=False,
        state="connected",
    )
    classify, classify_pool = await create_registry_with_pool(
        db,
        name="classify",
        url="http://classify/",
        is_interactive=False,
        state="connected",
    )
    db.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=detect_pool.id, enabled=True)
    )
    db.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=classify_pool.id, enabled=True)
    )
    proj.ml_backend_pool_id = detect_pool.id
    await db.commit()
    return proj, detect, classify


def _stages(detect_id, classify_id):
    return [
        {"stage": 0, "ml_backend_id": str(detect_id), "model_id": "detect"},
        {
            "stage": 1,
            "ml_backend_id": str(classify_id),
            "model_id": "va",
            "task_type": "classification",
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0.05},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]


@pytest.mark.asyncio
async def test_patch_persists_and_get_returns_pipeline(
    httpx_client, super_admin, db_session
):
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    stages = _stages(detect.id, classify.id)

    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["preannotate_pipeline"] == stages

    got = await httpx_client.get(f"/api/v1/projects/{proj.id}", headers=_bearer(token))
    assert got.status_code == 200, got.text
    assert got.json()["preannotate_pipeline"] == stages


@pytest.mark.asyncio
async def test_patch_single_stage_pipeline(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, detect, _ = await _seed(db_session, owner.id)
    stages = [{"stage": 0, "ml_backend_id": str(detect.id), "model_id": "detect"}]
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["preannotate_pipeline"] == stages


@pytest.mark.asyncio
async def test_patch_null_clears_pipeline(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": _stages(detect.id, classify.id)},
    )
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["preannotate_pipeline"] is None


@pytest.mark.asyncio
async def test_patch_omit_keeps_pipeline(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    stages = _stages(detect.id, classify.id)
    await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    # 不传 preannotate_pipeline, 改别的字段 → 编排保持不变。
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"name": "renamed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["preannotate_pipeline"] == stages


@pytest.mark.asyncio
async def test_patch_rejects_duplicate_stage(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    stages = _stages(detect.id, classify.id)
    stages[1]["stage"] = 0  # 重复 stage 序号
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_rejects_no_root_stage(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    stages = _stages(detect.id, classify.id)
    stages[0]["parent_stage"] = 1  # 无 parent_stage=None 的源阶段
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_rejects_bad_uuid(httpx_client, super_admin, db_session):
    owner, token = super_admin
    proj, _, _ = await _seed(db_session, owner.id)
    stages = [{"stage": 0, "ml_backend_id": "not-a-uuid", "model_id": "detect"}]
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": stages},
    )
    assert resp.status_code == 422, resp.text


# ---------- v0.19.3 WS1 · 保存路径能力软提示 (不挡, 与 dispatch 422 同判据) ----------


async def _set_caps(db: AsyncSession, backend, models: list[dict]):
    backend.health_meta = {"capabilities": {"models": models}}
    db.add(backend)
    await db.commit()


@pytest.mark.asyncio
async def test_patch_emits_capability_warnings_not_blocked(
    httpx_client, super_admin, db_session
):
    # 源模型自报 batchable=false → 保存仍 200 (软提示), 响应回带 capability_warnings。
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    await _set_caps(
        db_session, detect, [{"id": "detect", "resource_profile": {"batchable": False}}]
    )
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": _stages(detect.id, classify.id)},
    )
    assert resp.status_code == 200, resp.text
    warnings = resp.json()["capability_warnings"]
    assert any("batchable=false" in w for w in warnings), warnings


@pytest.mark.asyncio
async def test_patch_no_warnings_when_capable(httpx_client, super_admin, db_session):
    # 零退化: 无能力快照 (老 backend) → 保存 200 且 capability_warnings 为空。
    owner, token = super_admin
    proj, detect, classify = await _seed(db_session, owner.id)
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}",
        headers=_bearer(token),
        json={"preannotate_pipeline": _stages(detect.id, classify.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["capability_warnings"] == []
