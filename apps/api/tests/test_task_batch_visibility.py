"""B-16 · 标注员仅能在 GET /tasks / /tasks/{id} / /annotations / /predictions 上看到
被分派批次内的任务。super_admin / project owner 越权放行。
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.display_id import next_display_id
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_project_with_two_batches(
    db, owner_id: uuid.UUID, annotator_id: uuid.UUID
):
    """建一个项目 + 两个批次：B-MINE 分派给 annotator，B-OTHER 分派给别人。各 1 个任务。"""
    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="vis test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(p)
    await db.flush()

    await create_membership(
        db, project_id=pid, user_id=annotator_id, role="annotator", assigned_by=owner_id
    )

    # 创建一个真实的"别人"作为 b_other 的 annotator（FK 约束需要 user 存在）
    from app.db.models.user import User
    from app.core.security import hash_password

    other_user = User(
        id=uuid.uuid4(),
        email=f"other-{uuid.uuid4().hex[:6]}@test.local",
        name="OtherAnnotator",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    db.add(other_user)
    await db.flush()
    other_id = other_user.id

    b_mine = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id="B-MINE",
        name="my batch",
        status="active",
        annotator_id=annotator_id,
        assigned_user_ids=[str(annotator_id)],
    )
    b_other = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id="B-OTHER",
        name="other batch",
        status="active",
        annotator_id=other_id,
        assigned_user_ids=[str(other_id)],
    )
    db.add(b_mine)
    db.add(b_other)
    await db.flush()

    t_mine = Task(
        id=uuid.uuid4(),
        project_id=pid,
        batch_id=b_mine.id,
        display_id="T-MINE",
        file_name="m.jpg",
        file_path="/tmp/m.jpg",
        file_type="image",
        status="pending",
    )
    t_other = Task(
        id=uuid.uuid4(),
        project_id=pid,
        batch_id=b_other.id,
        display_id="T-OTHER",
        file_name="o.jpg",
        file_path="/tmp/o.jpg",
        file_type="image",
        status="pending",
    )
    db.add(t_mine)
    db.add(t_other)
    await db.flush()
    return p, b_mine, b_other, t_mine, t_other


@pytest.mark.asyncio
async def test_annotator_list_only_sees_assigned_batch(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, token = annotator
    p, b_mine, b_other, t_mine, t_other = await _seed_project_with_two_batches(
        db_session,
        owner.id,
        user.id,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&limit=200",
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(t_mine.id) in ids
    assert str(t_other.id) not in ids
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_annotator_get_other_batch_task_404(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, token = annotator
    _, _, _, _, t_other = await _seed_project_with_two_batches(
        db_session,
        owner.id,
        user.id,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{t_other.id}",
        headers=_bearer(token),
    )
    assert resp.status_code == 404

    resp = await httpx_client.get(
        f"/api/v1/tasks/{t_other.id}/annotations",
        headers=_bearer(token),
    )
    assert resp.status_code == 404

    resp = await httpx_client.get(
        f"/api/v1/tasks/{t_other.id}/predictions",
        headers=_bearer(token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_annotator_can_get_assigned_batch_task(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, token = annotator
    _, _, _, t_mine, _ = await _seed_project_with_two_batches(
        db_session,
        owner.id,
        user.id,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{t_mine.id}",
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(t_mine.id)


@pytest.mark.asyncio
async def test_super_admin_sees_all_batches(
    httpx_client, db_session, super_admin, annotator
):
    """B-16 修复后 super_admin 仍能越权看全部任务（owner / 监管视角）。"""
    owner, owner_token = super_admin
    user, _ = annotator
    p, _, _, t_mine, t_other = await _seed_project_with_two_batches(
        db_session,
        owner.id,
        user.id,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&limit=200",
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(t_mine.id) in ids
    assert str(t_other.id) in ids


@pytest.mark.asyncio
async def test_draft_batch_hidden_from_annotator_even_if_unassigned(
    httpx_client, db_session, super_admin, annotator
):
    """B-16 P-4 复现：draft 批次 + assigned_user_ids=[] 不应对标注员可见。
    历史 BUG：unassigned 规则未限制 batch.status，导致草稿批次也被当成开放批次。"""
    owner, _ = super_admin
    user, token = annotator

    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db_session, "projects"),
        name="P-4 repro",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(p)
    await db_session.flush()
    await create_membership(
        db_session,
        project_id=pid,
        user_id=user.id,
        role="annotator",
        assigned_by=owner.id,
    )

    b_active_mine = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id="BT-A",
        name="active mine",
        status="active",
        assigned_user_ids=[str(user.id)],
    )
    b_draft_open = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id="BT-D",
        name="draft open",
        status="draft",
        assigned_user_ids=[],
    )
    db_session.add(b_active_mine)
    db_session.add(b_draft_open)
    await db_session.flush()

    t_mine = Task(
        id=uuid.uuid4(),
        project_id=pid,
        batch_id=b_active_mine.id,
        display_id="T-A",
        file_name="m.jpg",
        file_path="/tmp/m.jpg",
        file_type="image",
        status="pending",
    )
    t_draft = Task(
        id=uuid.uuid4(),
        project_id=pid,
        batch_id=b_draft_open.id,
        display_id="T-D",
        file_name="d.jpg",
        file_path="/tmp/d.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(t_mine)
    db_session.add(t_draft)
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&limit=200",
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(t_mine.id) in ids
    assert str(t_draft.id) not in ids, "draft 批次任务不应对非特权用户可见"
    assert data["total"] == 1

    # 单独 GET 也应 404
    resp = await httpx_client.get(
        f"/api/v1/tasks/{t_draft.id}",
        headers=_bearer(token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unassigned_batch_visible_to_all_members(
    httpx_client, db_session, super_admin, annotator
):
    """assigned_user_ids = [] 且 status=active 的批次对所有成员可见（开放标注池）。"""
    owner, _ = super_admin
    user, token = annotator

    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db_session, "projects"),
        name="vis test 2",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(p)
    await db_session.flush()
    await create_membership(
        db_session,
        project_id=pid,
        user_id=user.id,
        role="annotator",
        assigned_by=owner.id,
    )

    b_open = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id="B-OPEN",
        name="open batch",
        status="active",
        assigned_user_ids=[],
    )
    db_session.add(b_open)
    await db_session.flush()

    t_open = Task(
        id=uuid.uuid4(),
        project_id=pid,
        batch_id=b_open.id,
        display_id="T-OPEN",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(t_open)
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&limit=200",
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(t_open.id) in ids


@pytest.mark.asyncio
async def test_unbatched_filter_returns_only_orphan_tasks(
    httpx_client, db_session, super_admin, annotator
):
    """v0.12.0 B5 · list_tasks?unbatched=true 仅返回 batch_id IS NULL 的未归类任务。

    B-OTHER 内的已归类任务 + 一条 batch_id=NULL 的未归类孤儿，
    super_admin 用 unbatched=true 应只看见后者。
    """
    owner, owner_token = super_admin
    user, _ = annotator
    p, _, _, t_mine, t_other = await _seed_project_with_two_batches(
        db_session,
        owner.id,
        user.id,
    )

    t_orphan = Task(
        id=uuid.uuid4(),
        project_id=p.id,
        batch_id=None,
        display_id="T-ORPHAN",
        file_name="orphan.jpg",
        file_path="/tmp/orphan.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(t_orphan)
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&unbatched=true&limit=200",
        headers=_bearer(owner_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {item["id"] for item in data["items"]}
    assert ids == {str(t_orphan.id)}
    assert str(t_mine.id) not in ids
    assert str(t_other.id) not in ids
    assert data["total"] == 1
