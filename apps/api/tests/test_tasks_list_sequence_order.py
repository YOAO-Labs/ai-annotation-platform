"""GET /tasks 排序回归:点云 scene 按帧分包时,同 scene 的 task 是同一刻批量创建的、
created_at 全相同,旧的 (created_at, id) 排序会退化为按随机 UUID id 乱序。本测试固定
created_at、打乱插入顺序,断言接口按 sequence_order 返回,并验证游标分页跨新排序正确。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import storage_service
from app.db.models.project import Project
from app.db.models.task import Task

# 所有 task 共用同一创建时刻(复现 batch 批量建 task 的场景)。
_FIXED_TS = datetime(2026, 6, 10, 13, 10, 31, 12768, tzinfo=timezone.utc)


async def _seed_sequence(db: AsyncSession, owner_id: uuid.UUID, seqs: list[int | None]):
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-SEQ-{uuid.uuid4().hex[:6]}",
        name="seq-order-test",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    for seq in seqs:
        db.add(
            Task(
                id=uuid.uuid4(),
                project_id=p.id,
                display_id=f"T-SEQ-{uuid.uuid4().hex[:6]}",
                file_name="x.pcd",
                file_path="/tmp/x.pcd",
                file_type="point_cloud",
                tags=[],
                status="pending",
                sequence_order=seq,
                created_at=_FIXED_TS,
            )
        )
    await db.flush()
    return p


@pytest.mark.asyncio
async def test_list_orders_by_sequence_order_despite_identical_created_at(
    httpx_client, db_session, super_admin
):
    admin_user, token = super_admin
    # 打乱插入,期望接口仍按 sequence_order 升序返回。
    p = await _seed_sequence(db_session, admin_user.id, [3, 0, 4, 1, 2])
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    seqs = [it["sequence_order"] for it in resp.json()["items"]]
    assert seqs == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_null_sequence_order_sorts_last(httpx_client, db_session, super_admin):
    admin_user, token = super_admin
    # 含 NULL(非序列任务):应排在有序帧之后,且不破坏排序。
    p = await _seed_sequence(db_session, admin_user.id, [1, None, 0, None])
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    seqs = [it["sequence_order"] for it in resp.json()["items"]]
    assert seqs == [0, 1, None, None]


@pytest.mark.asyncio
async def test_cursor_pagination_preserves_sequence_order(
    httpx_client, db_session, super_admin
):
    admin_user, token = super_admin
    p = await _seed_sequence(db_session, admin_user.id, [4, 2, 0, 5, 1, 3])
    await db_session.commit()

    # limit=2 翻页,拼接所有页,断言整体仍是 0..5 且无重复/缺漏。
    collected: list[int] = []
    cursor: str | None = None
    for _ in range(10):  # 上限保护
        url = f"/api/v1/tasks?project_id={p.id}&limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        resp = await httpx_client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        collected.extend(it["sequence_order"] for it in body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            break

    assert collected == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_point_cloud_task_without_dataset_item_uses_datasets_bucket(
    httpx_client, db_session, super_admin, monkeypatch
):
    admin_user, token = super_admin
    project = await _seed_sequence(db_session, admin_user.id, [0])
    await db_session.commit()

    signed: list[tuple[str, str | None]] = []

    def _fake_download_url(key: str, **kwargs) -> str:
        bucket = kwargs.get("bucket")
        signed.append((key, bucket))
        return f"http://storage.local/{bucket}/{key.lstrip('/')}"

    monkeypatch.setattr(storage_service, "generate_download_url", _fake_download_url)

    response = await httpx_client.get(
        f"/api/v1/tasks?project_id={project.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["file_url"].startswith(
        f"http://storage.local/{storage_service.datasets_bucket}/"
    )
    assert signed == [("/tmp/x.pcd", storage_service.datasets_bucket)]
