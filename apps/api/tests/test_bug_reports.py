"""v0.6.8 · BUG 反馈闭环测试

覆盖 add_comment 自动 reopen + 评论端点鉴权。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.bug_report import BugReport
from app.services.bug_report import BugReportService
from app.services.display_id import next_display_id


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_bug(
    db: AsyncSession, reporter_id: uuid.UUID, status: str = "fixed"
) -> BugReport:
    display_id = await next_display_id(db, "bug_reports")
    report = BugReport(
        id=uuid.uuid4(),
        display_id=display_id,
        reporter_id=reporter_id,
        route="/foo",
        user_role="annotator",
        title="test bug",
        description="d",
        severity="medium",
        status=status,
        reopen_count=0,
    )
    db.add(report)
    await db.flush()
    return report


@pytest.mark.asyncio
async def test_reporter_comment_on_fixed_triggers_reopen(db_session, annotator):
    """提交者在 fixed 状态评论 → 自动回 triaged + reopen_count++"""
    user, _ = annotator
    report = await _seed_bug(db_session, user.id, status="fixed")

    svc = BugReportService(db_session)
    result = await svc.add_comment(report.id, user.id, "依然有问题")
    assert result is not None
    comment, was_reopened, name, role = result

    assert was_reopened is True
    assert role == "employee"
    assert name == "Annotator"

    refreshed = await db_session.get(BugReport, report.id)
    assert refreshed.status == "triaged"
    assert refreshed.reopen_count == 1
    assert refreshed.last_reopened_at is not None
    assert refreshed.triaged_at is not None


@pytest.mark.asyncio
async def test_admin_comment_on_fixed_does_not_reopen(
    db_session, annotator, super_admin
):
    """管理员评论不触发 reopen，状态保持 fixed。"""
    reporter, _ = annotator
    admin, _ = super_admin
    report = await _seed_bug(db_session, reporter.id, status="fixed")

    svc = BugReportService(db_session)
    result = await svc.add_comment(report.id, admin.id, "已修复，请验收")
    assert result is not None
    _, was_reopened, _, _ = result
    assert was_reopened is False

    refreshed = await db_session.get(BugReport, report.id)
    assert refreshed.status == "fixed"
    assert refreshed.reopen_count == 0


@pytest.mark.asyncio
async def test_bug_report_update_can_clear_nullable_fields(db_session, annotator):
    """PATCH 使用 exclude_unset 后，显式 null 应能清空 nullable 字段。"""
    user, _ = annotator
    report = await _seed_bug(db_session, user.id, status="fixed")
    report.resolution = "已在 v1 修复"
    await db_session.flush()

    svc = BugReportService(db_session)
    updated = await svc.update(report.id, resolution=None)

    assert updated is not None
    assert updated.resolution is None


@pytest.mark.asyncio
async def test_reporter_comment_on_new_does_not_reopen(db_session, annotator):
    """非终态评论不触发 reopen。"""
    user, _ = annotator
    report = await _seed_bug(db_session, user.id, status="new")

    svc = BugReportService(db_session)
    result = await svc.add_comment(report.id, user.id, "补充信息")
    assert result is not None
    _, was_reopened, _, _ = result
    assert was_reopened is False

    refreshed = await db_session.get(BugReport, report.id)
    assert refreshed.status == "new"
    assert refreshed.reopen_count == 0


@pytest.mark.asyncio
async def test_reopen_increments_on_repeated_cycles(db_session, annotator, super_admin):
    """fixed → reopen → fixed → reopen，reopen_count 累加。"""
    reporter, _ = annotator
    admin, _ = super_admin
    report = await _seed_bug(db_session, reporter.id, status="fixed")

    svc = BugReportService(db_session)

    # 第一次 reopen
    await svc.add_comment(report.id, reporter.id, "no.1")
    refreshed = await db_session.get(BugReport, report.id)
    assert refreshed.reopen_count == 1

    # 管理员重新关掉
    refreshed.status = "fixed"
    await db_session.flush()

    # 第二次 reopen
    await svc.add_comment(report.id, reporter.id, "no.2")
    refreshed2 = await db_session.get(BugReport, report.id)
    assert refreshed2.reopen_count == 2
    assert refreshed2.status == "triaged"


@pytest.mark.asyncio
async def test_third_party_cannot_comment(
    httpx_client, db_session, annotator, reviewer
):
    """非提交者非管理员调评论端点 → 403。"""
    reporter, _ = annotator
    other, other_token = reviewer  # reviewer 不是项目管理员
    report = await _seed_bug(db_session, reporter.id, status="new")
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/bug_reports/{report.id}/comments",
        json={"body": "ping"},
        headers=_bearer(other_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reporter_can_comment_via_http(httpx_client, db_session, annotator):
    """提交者经 HTTP 评论 → 200，author 信息回传。"""
    reporter, token = annotator
    report = await _seed_bug(db_session, reporter.id, status="fixed")
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/bug_reports/{report.id}/comments",
        json={"body": "依然不行"},
        headers=_bearer(token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["body"] == "依然不行"
    assert data["author_role"] == "employee"
    assert data["author_name"] == "Annotator"

    # 再 GET 详情，确认 reopen_count + status
    detail = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}", headers=_bearer(token)
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "triaged"
    assert body["reopen_count"] == 1
    assert body["last_reopened_at"] is not None


@pytest.mark.asyncio
async def test_create_bug_report_accepts_multi_image_attachments(
    httpx_client, annotator, db_session, monkeypatch
):
    """提交 BUG 绑定自己的已核验回执，详情保留服务端实际元数据。"""
    from app.services import bug_attachment_uploads as uploads
    from tests.test_bug_attachment_uploads import patch_objects, seed_receipt

    user, token = annotator
    objects = patch_objects(monkeypatch)
    rows = [await seed_receipt(db_session, objects, user.id) for _ in range(2)]
    attachments = [uploads.attachment(row).model_dump(by_alias=True) for row in rows]

    resp = await httpx_client.post(
        "/api/v1/bug_reports",
        json={
            "title": "markdown bug",
            "description": "**bold** detail",
            "severity": "high",
            "attachments": attachments,
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["attachments"] == attachments
    assert data["screenshot_url"] == attachments[0]["storageKey"]
    for row in rows:
        await db_session.refresh(row)
        assert row.bound_report_id == uuid.UUID(data["id"])

    detail = await httpx_client.get(
        f"/api/v1/bug_reports/{data['id']}", headers=_bearer(token)
    )
    assert detail.status_code == 200
    assert detail.json()["attachments"] == attachments


@pytest.mark.asyncio
async def test_bug_report_attachment_invalid_key_rejected(httpx_client, annotator):
    """附件 key 必须在 BUG 附件前缀下，防止任意对象 key 注入。"""
    _, token = annotator
    resp = await httpx_client.post(
        "/api/v1/bug_reports",
        json={
            "title": "bad attachment",
            "description": "d",
            "attachments": [
                {
                    "storageKey": "comment-attachments/not-a-bug.png",
                    "fileName": "x.png",
                    "mimeType": "image/png",
                    "size": 1,
                }
            ],
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bug_report_rejects_oversized_description(httpx_client, annotator):
    """描述长度有硬上限，避免前端捕获内容被无限放大。"""
    _, token = annotator
    resp = await httpx_client.post(
        "/api/v1/bug_reports",
        json={
            "title": "too long",
            "description": "x" * 20001,
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bug_report_rejects_too_many_recent_api_calls(httpx_client, annotator):
    """最近请求记录最多保留 100 条。"""
    _, token = annotator
    resp = await httpx_client.post(
        "/api/v1/bug_reports",
        json={
            "title": "too many calls",
            "description": "d",
            "recent_api_calls": [{"url": "/x"} for _ in range(101)],
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bug_comment_rejects_oversized_body(httpx_client, db_session, annotator):
    """评论正文有硬上限，仍允许普通 Markdown 长评论。"""
    reporter, token = annotator
    report = await _seed_bug(db_session, reporter.id, status="new")
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/bug_reports/{report.id}/comments",
        json={"body": "x" * 10001},
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bug_report_upload_init_rejects_non_image(httpx_client, annotator):
    _, token = annotator
    resp = await httpx_client.post(
        "/api/v1/bug_reports/screenshot/upload-init",
        json={"file_name": "x.txt", "content_type": "text/plain"},
        headers=_bearer(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bug_report_attachment_download_checks_registered_key(
    httpx_client, db_session, annotator, reviewer, monkeypatch
):
    reporter, token = annotator
    _, other_token = reviewer
    from app.services import bug_attachment_uploads as uploads
    from tests.test_bug_attachment_uploads import (
        patch_objects,
        seed_receipt,
        image_bytes,
    )

    objects = patch_objects(monkeypatch)
    report = await _seed_bug(db_session, reporter.id, status="new")
    row = await seed_receipt(db_session, objects, reporter.id, bound=report.id)
    key = uploads.object_key(row)
    report.attachments = [uploads.attachment(row).model_dump(by_alias=True)]
    await db_session.commit()

    ok = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}/attachments/download",
        params={"key": key},
        headers=_bearer(token),
    )
    assert ok.status_code == 200
    assert ok.content == image_bytes()
    assert "location" not in ok.headers

    bad_key = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}/attachments/download",
        params={"key": f"bug-report-attachments/{reporter.id}/{uuid.uuid4()}-y.png"},
        headers=_bearer(token),
    )
    assert bad_key.status_code == 400

    forbidden = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}/attachments/download",
        params={"key": key},
        headers=_bearer(other_token),
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_bug_report_upload_init_is_deprecated_without_new_put(
    httpx_client, annotator
):
    response = await httpx_client.post(
        "/api/v1/bug_reports/screenshot/upload-init",
        json={"file_name": "x.png", "content_type": "image/png"},
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "bug_attachment_upload_init_retired",
        "upload_path": "/api/v1/bug_reports/screenshot/upload",
    }


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "foreign",
        "mismatch",
        "mime",
        "name",
        "duplicate",
        "alias",
        "bound",
        "pending",
        "expired",
    ],
)
async def test_report_create_rejects_unowned_or_inconsistent_receipts(
    case, httpx_client, annotator, reviewer, db_session, monkeypatch
):
    from app.services import bug_attachment_uploads as uploads
    from tests.test_bug_attachment_uploads import patch_objects, seed_receipt, created

    objects = patch_objects(monkeypatch)
    row = await seed_receipt(
        db_session,
        objects,
        reviewer[0].id if case == "foreign" else annotator[0].id,
        state="pending" if case == "pending" else "ready",
        expired=case == "expired",
    )
    value = uploads.attachment(row).model_dump(by_alias=True)
    if case == "bound":
        await created(httpx_client, annotator, [value])
    if case == "unknown":
        value["storageKey"] = "bug-screenshots/unknown.png"
    elif case == "mismatch":
        value["size"] += 1
    elif case == "mime":
        value["mimeType"] = "image/webp"
    elif case == "name":
        value["fileName"] = "claimed-name.png"
    fields = {
        "title": "attachment rejection",
        "description": "d",
        "attachments": [value],
    }
    if case == "duplicate":
        fields["attachments"].append(value)
    elif case == "alias":
        fields["screenshot_url"] = "bug-screenshots/other.png"
    response = await httpx_client.post(
        "/api/v1/bug_reports", json=fields, headers=_bearer(annotator[1])
    )
    assert response.status_code == (
        403 if case == "foreign" else 410 if case == "expired" else 409
    ), response.text
    assert not objects.reads and not objects.deleted


async def test_screenshot_only_expands_known_receipt_and_rejects_unknown(
    httpx_client, annotator, db_session, monkeypatch
):
    from app.services import bug_attachment_uploads as uploads
    from tests.test_bug_attachment_uploads import patch_objects, seed_receipt, created

    objects = patch_objects(monkeypatch)
    row = await seed_receipt(db_session, objects, annotator[0].id)
    report = await created(
        httpx_client, annotator, screenshot_url=uploads.object_key(row)
    )
    assert report["attachments"] == [uploads.attachment(row).model_dump(by_alias=True)]
    assert report["screenshot_url"] == uploads.object_key(row)
    response = await httpx_client.post(
        "/api/v1/bug_reports",
        json={
            "title": "legacy",
            "description": "d",
            "screenshot_url": "https://unknown.invalid/old.png",
        },
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == 409


async def test_unknown_legacy_attachment_metadata_stays_readable_but_no_io(
    httpx_client, annotator, db_session, monkeypatch
):
    from tests.test_bug_attachment_uploads import patch_objects

    objects = patch_objects(monkeypatch)
    report = await _seed_bug(db_session, annotator[0].id, "new")
    legacy = {
        "storageKey": "bug-screenshots/unknown.png",
        "fileName": "legacy.png",
        "mimeType": "image/png",
        "size": 10,
    }
    report.attachments = [legacy]
    await db_session.commit()
    detail = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}", headers=_bearer(annotator[1])
    )
    assert detail.status_code == 200 and detail.json()["attachments"] == [legacy]
    download = await httpx_client.get(
        f"/api/v1/bug_reports/{report.id}/attachments/download",
        params={"key": legacy["storageKey"]},
        headers=_bearer(annotator[1]),
    )
    assert download.status_code == 409
    deleted = await httpx_client.delete(
        f"/api/v1/bug_reports/{report.id}", headers=_bearer(annotator[1])
    )
    assert deleted.status_code == 204 and not objects.reads and not objects.deleted
