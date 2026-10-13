"""issue #124 · 可预标批次列表与改派广播的端到端覆盖。

守护 `/projects/{id}/batches?status=...` 的多状态过滤（避免全量历史拉取）与
标注员 / 质检员改派触发的 `batch.assignment_changed` 广播（改派不改 status,
否则其它管理员的「可预标」列表会保留过期项）。
"""

from __future__ import annotations

import pytest

from tests.factory import create_membership, create_batch, create_project, create_user


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_batches_multi_status_filter(httpx_client, db_session, super_admin):
    """status=active,draft 只返回这两类, 不再拉取全量批次历史。"""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    await create_batch(db_session, project_id=proj.id, name="act", status="active")
    await create_batch(db_session, project_id=proj.id, name="drf", status="draft")
    await create_batch(db_session, project_id=proj.id, name="arc", status="archived")
    await db_session.commit()

    url = f"/api/v1/projects/{proj.id}/batches"
    multi = await httpx_client.get(f"{url}?status=active,draft", headers=_bearer(token))
    assert multi.status_code == 200, multi.text
    assert sorted(b["name"] for b in multi.json()) == ["act", "drf"]

    # 单状态行为不变
    single = await httpx_client.get(f"{url}?status=active", headers=_bearer(token))
    assert single.status_code == 200, single.text
    assert [b["name"] for b in single.json()] == ["act"]

    # 无 status 仍返回全部 (向后兼容)
    all_batches = await httpx_client.get(url, headers=_bearer(token))
    assert all_batches.status_code == 200, all_batches.text
    assert len(all_batches.json()) == 3


@pytest.mark.asyncio
async def test_patch_assignment_broadcasts_batch_event(
    httpx_client, db_session, super_admin, monkeypatch
):
    """PATCH 改派后广播 batch.assignment_changed, 让可预标列表实时收敛。"""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    anno = await create_user(db_session, "employee", "bcast-anno@e.test", "Anno")
    await create_membership(
        db_session,
        project_id=proj.id,
        user_id=anno.id,
        role="annotator",
        assigned_by=user.id,
    )
    draft = await create_batch(
        db_session, project_id=proj.id, name="bcast", status="draft"
    )
    await db_session.commit()

    calls: list[tuple[str, list[str]]] = []

    async def fake_publish(project_id: str, batch_ids: list[str]) -> None:
        calls.append((project_id, list(batch_ids)))

    monkeypatch.setattr(
        "app.services.batch.publish_batch_assignment_change", fake_publish
    )

    res = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}/batches/{draft.id}",
        json={"annotator_id": str(anno.id)},
        headers=_bearer(token),
    )
    assert res.status_code == 200, res.text
    assert calls == [(str(proj.id), [str(draft.id)])]
