"""v0.9.7 · /admin/projects/:id/alias-frequency 端到端测试."""

from __future__ import annotations

import uuid

import pytest

from app.db.models.prediction import Prediction
from tests.factory import create_project, create_task


async def _make_prediction(db, *, project_id, task_id, result):
    p = Prediction(
        project_id=project_id,
        task_id=task_id,
        result=result,
        score=0.9,
    )
    db.add(p)
    await db.flush()
    return p


@pytest.mark.asyncio
async def test_alias_freq_requires_admin(httpx_client, annotator):
    _, token = annotator
    res = await httpx_client.get(
        f"/api/v1/admin/projects/{uuid.uuid4()}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_alias_freq_requires_project_manager(
    httpx_client, db_session, super_admin, project_admin
):
    super_user, super_token = super_admin
    owner, owner_token = project_admin
    owned = await create_project(db_session, owner_id=owner.id, name="owned")
    foreign = await create_project(db_session, owner_id=super_user.id, name="foreign")
    await db_session.commit()

    owned_result = await httpx_client.get(
        f"/api/v1/admin/projects/{owned.id}/alias-frequency",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert owned_result.status_code == 200, owned_result.text

    foreign_result = await httpx_client.get(
        f"/api/v1/admin/projects/{foreign.id}/alias-frequency",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert foreign_result.status_code == 404

    super_result = await httpx_client.get(
        f"/api/v1/admin/projects/{foreign.id}/alias-frequency",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert super_result.status_code == 200, super_result.text


@pytest.mark.asyncio
async def test_alias_freq_404_when_project_missing(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.get(
        f"/api/v1/admin/projects/{uuid.uuid4()}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_alias_freq_empty_for_project_without_predictions(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="Empty")

    res = await httpx_client.get(
        f"/api/v1/admin/projects/{proj.id}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_predictions"] == 0
    assert body["frequency"] == {}


@pytest.mark.asyncio
async def test_alias_freq_aggregates_polygon_labels(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="Poly")
    task = await create_task(db_session, project_id=proj.id)

    # 3 个 polygon prediction: 2 person + 1 car
    await _make_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[
            {"type": "polygonlabels", "value": {"labels": ["person"], "points": []}},
            {"type": "polygonlabels", "value": {"labels": ["person"], "points": []}},
            {"type": "polygonlabels", "value": {"labels": ["car"], "points": []}},
        ],
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/admin/projects/{proj.id}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_predictions"] == 1
    assert body["frequency"] == {"person": 2, "car": 1}


@pytest.mark.asyncio
async def test_alias_freq_falls_back_to_value_class(
    httpx_client, db_session, super_admin
):
    """rectanglelabels 偶发用 value.class 而非 value.labels[]; 端点要兼容."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="Rect")
    task = await create_task(db_session, project_id=proj.id)

    await _make_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[
            {"type": "rectanglelabels", "value": {"class": "dog", "x": 0, "y": 0}},
            {"type": "rectanglelabels", "value": {"class": "dog", "x": 0, "y": 0}},
        ],
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/admin/projects/{proj.id}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["frequency"] == {"dog": 2}


@pytest.mark.asyncio
async def test_alias_freq_mixed_label_types_merged(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="Mixed")
    task = await create_task(db_session, project_id=proj.id)

    await _make_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[
            {"type": "polygonlabels", "value": {"labels": ["person"], "points": []}},
            {"type": "rectanglelabels", "value": {"class": "person", "x": 0}},
            {"type": "rectanglelabels", "value": {"class": "car", "x": 0}},
        ],
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/admin/projects/{proj.id}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["frequency"] == {"person": 2, "car": 1}


@pytest.mark.asyncio
async def test_alias_freq_handles_non_array_result(
    httpx_client, db_session, super_admin
):
    """偶发 mock data 把 result 写成对象而非数组; 端点不应 500."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="Mock")
    task = await create_task(db_session, project_id=proj.id)

    await _make_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result={"corrupted": "object"},  # 非数组 — 应被守卫跳过
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/admin/projects/{proj.id}/alias-frequency",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_predictions"] == 1
    assert body["frequency"] == {}
