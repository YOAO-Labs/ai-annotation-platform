"""v0.6.9 · 通知中心 + BUG 反馈接入。"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.bug_report import BugReport
from app.db.models.notification import Notification
from app.db.models.notification_preference import NotificationPreference
from app.services.display_id import next_display_id
from app.services.notification import NotificationService
from app.services.notification_preferences import (
    KNOWN_NOTIFICATION_TYPES,
    default_toast_for,
)
from sqlalchemy import select


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_bug(
    db,
    reporter_id: uuid.UUID,
    status: str = "new",
    assigned_to: uuid.UUID | None = None,
) -> BugReport:
    display_id = await next_display_id(db, "bug_reports")
    report = BugReport(
        id=uuid.uuid4(),
        display_id=display_id,
        reporter_id=reporter_id,
        route="/foo",
        user_role="annotator",
        title="t",
        description="d",
        severity="medium",
        status=status,
        assigned_to_id=assigned_to,
    )
    db.add(report)
    await db.flush()
    return report


@pytest.mark.asyncio
async def test_notify_writes_row_and_unread_count(db_session, annotator):
    user, _ = annotator
    svc = NotificationService(db_session)
    await svc.notify(
        user_id=user.id,
        type="bug_report.commented",
        target_type="bug_report",
        target_id=uuid.uuid4(),
        payload={"display_id": "B-1"},
    )
    assert await svc.unread_count(user.id) == 1

    items, total, unread = await svc.list_for_user(user.id)
    assert total == 1 and unread == 1
    assert items[0].read_at is None


@pytest.mark.asyncio
async def test_mark_read_and_mark_all(db_session, annotator):
    user, _ = annotator
    svc = NotificationService(db_session)
    n1 = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )

    assert await svc.mark_read(user.id, n1.id) is True
    assert await svc.unread_count(user.id) == 1

    # 已读再标 → False（rowcount=0）
    assert await svc.mark_read(user.id, n1.id) is False

    # 标记全部
    n = await svc.mark_all_read(user.id)
    assert n == 1  # 只剩 n2 未读
    assert await svc.unread_count(user.id) == 0


@pytest.mark.asyncio
async def test_read_and_delete_service_methods_do_not_publish(
    db_session, annotator, monkeypatch
):
    """Patch B：服务层只在事务内改行，不发布 sync 事件。

    发布责任在 API handler，且必须在 ``db.commit()`` 成功之后（见
    ``test_read_and_delete_endpoints_publish_after_commit``）。
    """
    user, _ = annotator
    svc = NotificationService(db_session)
    n1 = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    n2 = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )

    publish_calls: list[dict] = []

    async def fail_publish(*, user_id, message):
        publish_calls.append(message)

    monkeypatch.setattr("app.services.notification._publish", fail_publish)

    assert await svc.mark_read(user.id, n1.id) is True
    assert await svc.mark_all_read(user.id) == 1
    assert await svc.delete_for_user(user.id, n1.id) is True
    assert await svc.clear_read(user.id) == 1
    assert publish_calls == []
    assert n2.id


@pytest.mark.asyncio
async def test_admin_status_change_notifies_reporter(
    httpx_client, db_session, annotator, super_admin
):
    reporter, _ = annotator
    admin, admin_token = super_admin
    report = await _seed_bug(db_session, reporter.id, status="new")
    await db_session.commit()

    resp = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        json={"status": "fixed", "resolution": "ok"},
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 200

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == reporter.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    n = rows[0]
    assert n.type == "bug_report.status_changed"
    assert n.payload["from_status"] == "new"
    assert n.payload["to_status"] == "fixed"
    assert n.payload["display_id"] == report.display_id


@pytest.mark.asyncio
async def test_reporter_reopen_notifies_assignee(
    httpx_client, db_session, annotator, super_admin
):
    reporter, reporter_token = annotator
    admin, _ = super_admin
    report = await _seed_bug(
        db_session, reporter.id, status="fixed", assigned_to=admin.id
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/bug_reports/{report.id}/comments",
        json={"body": "依然有问题"},
        headers=_bearer(reporter_token),
    )
    assert resp.status_code == 201

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == admin.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    n = rows[0]
    assert n.type == "bug_report.reopened"
    assert n.payload.get("reopen") is True
    assert n.payload.get("reopen_count") == 1
    assert n.payload.get("snippet") == "依然有问题"


@pytest.mark.asyncio
async def test_admin_comment_notifies_reporter(
    httpx_client, db_session, annotator, super_admin
):
    reporter, _ = annotator
    admin, admin_token = super_admin
    report = await _seed_bug(
        db_session, reporter.id, status="triaged", assigned_to=admin.id
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/bug_reports/{report.id}/comments",
        json={"body": "在跟进"},
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 201

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == reporter.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].type == "bug_report.commented"
    assert rows[0].payload["actor_role"] == "super_admin"


@pytest.mark.asyncio
async def test_notifications_endpoints_only_return_own(
    httpx_client, db_session, annotator, reviewer
):
    user_a, token_a = annotator
    user_b, _ = reviewer
    svc = NotificationService(db_session)
    await svc.notify(
        user_id=user_a.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await svc.notify(
        user_id=user_b.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()

    resp = await httpx_client.get("/api/v1/notifications", headers=_bearer(token_a))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["unread"] == 1

    cnt = await httpx_client.get(
        "/api/v1/notifications/unread-count", headers=_bearer(token_a)
    )
    assert cnt.status_code == 200
    assert cnt.json() == {"unread": 1}


@pytest.mark.asyncio
async def test_delete_notification_owner_scoped(
    httpx_client, db_session, annotator, reviewer
):
    user_a, token_a = annotator
    user_b, _ = reviewer
    svc = NotificationService(db_session)
    own = await svc.notify(
        user_id=user_a.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    other = await svc.notify(
        user_id=user_b.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()

    resp = await httpx_client.delete(
        f"/api/v1/notifications/{own.id}", headers=_bearer(token_a)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
    assert await svc.unread_count(user_a.id) == 0

    forbidden = await httpx_client.delete(
        f"/api/v1/notifications/{other.id}", headers=_bearer(token_a)
    )
    assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_clear_read_deletes_only_current_users_read_notifications(
    httpx_client, db_session, annotator, reviewer
):
    user_a, token_a = annotator
    user_b, _ = reviewer
    svc = NotificationService(db_session)
    unread = await svc.notify(
        user_id=user_a.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    read = await svc.notify(
        user_id=user_a.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    other_read = await svc.notify(
        user_id=user_b.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await svc.mark_read(user_a.id, read.id)
    await svc.mark_read(user_b.id, other_read.id)
    await db_session.commit()

    resp = await httpx_client.post(
        "/api/v1/notifications/clear-read", headers=_bearer(token_a)
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1}

    own_rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in own_rows] == [unread.id]
    assert own_rows[0].read_at is None

    other_rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user_b.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in other_rows] == [other_read.id]


@pytest.mark.asyncio
async def test_self_action_does_not_notify_self(httpx_client, db_session, super_admin):
    """super_admin 自己改自己的状态不应通知自己（reporter == admin 同一人）。"""
    admin, admin_token = super_admin
    report = await _seed_bug(db_session, admin.id, status="new")
    await db_session.commit()

    resp = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        json={"status": "triaged"},
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 200

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == admin.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0


# ── Patch B：提交后发布 notifications.sync ─────────────────────────────


def _commit_spy(db_session, monkeypatch):
    """Record handler commits so tests can assert publish happens after them."""
    commits: list[int] = []
    original_commit = db_session.commit

    async def spy_commit():
        commits.append(1)
        await original_commit()

    monkeypatch.setattr(db_session, "commit", spy_commit)
    return commits


def _record_publish(monkeypatch, sink: list, *, user_id=None):
    async def fake_publish(*, user_id: uuid.UUID, message: dict):
        if user_id is not None:
            assert user_id == user_id
        sink.append(message)

    monkeypatch.setattr("app.services.notification._publish", fake_publish)


@pytest.mark.asyncio
async def test_read_and_delete_endpoints_publish_after_commit(
    httpx_client, db_session, annotator, monkeypatch
):
    user, token = annotator
    svc = NotificationService(db_session)
    n1 = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    n2 = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()

    messages: list[dict] = []
    _record_publish(monkeypatch, messages, user_id=user.id)
    commits = _commit_spy(db_session, monkeypatch)

    resp = await httpx_client.post(
        f"/api/v1/notifications/{n1.id}/read", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert commits and messages == [{"type": "notifications.sync", "reason": "read"}]

    # 已读行存在 → clear-read 删除并发布 deleted 同步
    messages.clear()
    resp = await httpx_client.post(
        "/api/v1/notifications/clear-read", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1}
    assert messages == [{"type": "notifications.sync", "reason": "deleted"}]

    # no-op（没有已读行）不发布；已删除的行再删 → 404 不发布
    messages.clear()
    resp = await httpx_client.post(
        "/api/v1/notifications/clear-read", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}
    resp = await httpx_client.delete(
        f"/api/v1/notifications/{n1.id}", headers=_bearer(token)
    )
    assert resp.status_code == 404
    assert messages == []

    # 已读再标 → 404，不发布
    await svc.mark_read(user.id, n2.id)
    await db_session.commit()
    resp = await httpx_client.post(
        f"/api/v1/notifications/{n2.id}/read", headers=_bearer(token)
    )
    assert resp.status_code == 404
    assert messages == []


@pytest.mark.asyncio
async def test_mark_all_read_publishes_only_with_changes(
    httpx_client, db_session, annotator, monkeypatch
):
    user, token = annotator
    messages: list[dict] = []
    _record_publish(monkeypatch, messages, user_id=user.id)
    _commit_spy(db_session, monkeypatch)

    resp = await httpx_client.post(
        "/api/v1/notifications/mark-all-read", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 0}
    assert messages == []

    svc = NotificationService(db_session)
    await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()
    # notify 自身发布的是新通知消息，不是 sync；这里只关心 sync 事件
    messages.clear()
    resp = await httpx_client.post(
        "/api/v1/notifications/mark-all-read", headers=_bearer(token)
    )
    assert resp.json() == {"updated": 1}
    assert messages == [{"type": "notifications.sync", "reason": "read"}]


@pytest.mark.asyncio
async def test_failed_commit_does_not_publish(
    httpx_client, db_session, annotator, monkeypatch
):
    user, token = annotator
    svc = NotificationService(db_session)
    row = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()

    messages: list[dict] = []
    _record_publish(monkeypatch, messages, user_id=user.id)

    async def failing_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)

    with pytest.raises(RuntimeError):
        await httpx_client.post(
            f"/api/v1/notifications/{row.id}/read", headers=_bearer(token)
        )
    assert messages == []


@pytest.mark.asyncio
async def test_publication_failure_does_not_fail_committed_request(
    httpx_client, db_session, annotator, monkeypatch
):
    user, token = annotator
    svc = NotificationService(db_session)
    row = await svc.notify(
        user_id=user.id, type="t", target_type="bug_report", target_id=uuid.uuid4()
    )
    await db_session.commit()

    async def broken_publish(*, user_id, message):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.services.notification._publish", broken_publish)

    resp = await httpx_client.post(
        f"/api/v1/notifications/{row.id}/read", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ── 通知偏好：接收(in_app)与弹出(toast)契约 ───────────────────────────


def _pref_by_type(payload: dict) -> dict[str, dict]:
    return {item["type"]: item for item in payload["items"]}


@pytest.mark.asyncio
async def test_get_preferences_defaults_for_account_without_rows(
    httpx_client, annotator
):
    user, token = annotator
    resp = await httpx_client.get(
        "/api/v1/notification-preferences", headers=_bearer(token)
    )
    assert resp.status_code == 200
    by_type = _pref_by_type(resp.json())
    assert set(by_type) == set(KNOWN_NOTIFICATION_TYPES)
    for t in KNOWN_NOTIFICATION_TYPES:
        item = by_type[t]
        assert item["in_app"] is True
        assert item["email"] is False
        assert item["toast"] is default_toast_for(t)
    assert by_type["task.rejected"]["toast"] is True
    assert by_type["task.approved"]["toast"] is False


@pytest.mark.asyncio
async def test_legacy_row_without_toast_uses_type_default(
    httpx_client, db_session, annotator
):
    user, token = annotator
    db_session.add(
        NotificationPreference(
            user_id=user.id,
            type="task.rejected",
            channels={"in_app": True, "email": False},
        )
    )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/notification-preferences", headers=_bearer(token)
    )
    item = _pref_by_type(resp.json())["task.rejected"]
    assert item["toast"] is True  # default_toast_for("task.rejected")


@pytest.mark.asyncio
async def test_receipt_only_update_keeps_stored_toast(
    httpx_client, db_session, annotator
):
    """旧客户端只发 {type, in_app}：不得清除已存的弹出选择。"""
    user, token = annotator
    resp = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "task.rejected", "toast": False},
        headers=_bearer(token),
    )
    assert resp.status_code == 200

    resp = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "task.rejected", "in_app": False},
        headers=_bearer(token),
    )
    assert resp.status_code == 200

    item = _pref_by_type(
        (
            await httpx_client.get(
                "/api/v1/notification-preferences", headers=_bearer(token)
            )
        ).json()
    )["task.rejected"]
    assert item["in_app"] is False
    assert item["toast"] is False  # 保留显式关闭的弹出选择


@pytest.mark.asyncio
async def test_toast_only_update_keeps_receipt_and_reserved_email(
    httpx_client, db_session, annotator
):
    user, token = annotator
    db_session.add(
        NotificationPreference(
            user_id=user.id,
            type="job.failed",
            channels={"in_app": False, "email": True},
        )
    )
    await db_session.commit()

    resp = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "job.failed", "toast": True},
        headers=_bearer(token),
    )
    assert resp.status_code == 200

    item = _pref_by_type(
        (
            await httpx_client.get(
                "/api/v1/notification-preferences", headers=_bearer(token)
            )
        ).json()
    )["job.failed"]
    assert item["in_app"] is False
    assert item["email"] is True
    assert item["toast"] is True


@pytest.mark.asyncio
async def test_sequential_updates_merge_channel_keys_atomically(
    httpx_client, annotator
):
    """并发标签页写不同键（测试中以两次顺序请求模拟）互不覆盖。"""
    user, token = annotator
    for body in ({"in_app": False}, {"toast": True}):
        resp = await httpx_client.put(
            "/api/v1/notification-preferences",
            json={"type": "batch.rejected", **body},
            headers=_bearer(token),
        )
        assert resp.status_code == 200

    item = _pref_by_type(
        (
            await httpx_client.get(
                "/api/v1/notification-preferences", headers=_bearer(token)
            )
        ).json()
    )["batch.rejected"]
    assert item["in_app"] is False
    assert item["toast"] is True


@pytest.mark.asyncio
async def test_preference_update_validation(httpx_client, annotator):
    user, token = annotator
    base = "/api/v1/notification-preferences"

    # 未知类型
    resp = await httpx_client.put(
        base, json={"type": "nope.nope", "in_app": True}, headers=_bearer(token)
    )
    assert resp.status_code == 400
    # 两个开关都缺
    resp = await httpx_client.put(
        base, json={"type": "task.rejected"}, headers=_bearer(token)
    )
    assert resp.status_code == 400
    # 显式 null 值拒绝（pydantic 校验失败 → 422）
    resp = await httpx_client.put(
        base,
        json={"type": "task.rejected", "in_app": None},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    resp = await httpx_client.put(
        base,
        json={"type": "task.rejected", "in_app": True, "toast": None},
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preferences_are_owner_scoped(httpx_client, annotator, reviewer):
    user_a, token_a = annotator
    user_b, token_b = reviewer
    resp = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "task.rejected", "in_app": False, "toast": False},
        headers=_bearer(token_a),
    )
    assert resp.status_code == 200

    item_a = _pref_by_type(
        (
            await httpx_client.get(
                "/api/v1/notification-preferences", headers=_bearer(token_a)
            )
        ).json()
    )["task.rejected"]
    item_b = _pref_by_type(
        (
            await httpx_client.get(
                "/api/v1/notification-preferences", headers=_bearer(token_b)
            )
        ).json()
    )["task.rejected"]
    assert item_a["in_app"] is False and item_a["toast"] is False
    assert item_b["in_app"] is True and item_b["toast"] is True


@pytest.mark.asyncio
async def test_preference_update_publishes_preferences_sync_after_commit(
    httpx_client, db_session, annotator, monkeypatch
):
    user, token = annotator
    messages: list[dict] = []
    _record_publish(monkeypatch, messages, user_id=user.id)
    commits = _commit_spy(db_session, monkeypatch)

    resp = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "export.failed", "toast": False},
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    assert commits
    assert messages == [{"type": "notifications.sync", "reason": "preferences"}]

    # 偏好变更不产生通知行，也不改变未读数
    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
