"""Typed contracts for comments, history and bug-report SDK resources."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.annotation_history import (
    AnnotationHistory as AnnotationHistoryResource,
)
from ai_annotation.bug_report_models import (
    BugCommentCreate,
    BugReportCreate,
    BugReportUpdate,
    BugScreenshotUploadRequest,
)
from ai_annotation.bug_reports import BugReports
from ai_annotation.cli import comments as comments_cli
from ai_annotation.comment_models import (
    AnnotationCommentCreate,
    AnnotationCommentUpdate,
    CommentAttachmentInput,
    CommentAttachmentUploadRequest,
    CommentMentionInput,
)
from ai_annotation.comments import Comments
from ai_annotation.errors import AAPError, APIStatusError, NotFoundError

from .conftest import API

runner = CliRunner()
NOW = "2026-10-17T08:30:00Z"
ANNOTATION_ID = str(uuid4())
TASK_ID = str(uuid4())
COMMENT_ID = str(uuid4())
PROJECT_ID = str(uuid4())
REPORT_ID = str(uuid4())
REPORTER_ID = str(uuid4())


def _comment(**overrides) -> dict:
    return {
        "id": COMMENT_ID,
        "annotation_id": ANNOTATION_ID,
        "project_id": PROJECT_ID,
        "author_id": REPORTER_ID,
        "author_name": "Reporter",
        "body": "Found an issue",
        "is_resolved": False,
        "is_active": True,
        "mentions": [],
        "attachments": [],
        "canvas_drawing": None,
        "anchor": None,
        "created_at": NOW,
        "updated_at": NOW,
        **overrides,
    }


def _report(**overrides) -> dict:
    return {
        "id": REPORT_ID,
        "display_id": "BUG-1",
        "reporter_id": REPORTER_ID,
        "route": "/workbench",
        "user_role": "employee",
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "title": "Incorrect label",
        "description": "The label is missing",
        "severity": "medium",
        "status": "new",
        "duplicate_of_id": None,
        "browser_ua": None,
        "viewport": None,
        "recent_api_calls": [],
        "recent_console_errors": [],
        "screenshot_url": None,
        "attachments": [],
        "resolution": None,
        "fixed_in_version": None,
        "assigned_to_id": None,
        "created_at": NOW,
        "triaged_at": None,
        "fixed_at": None,
        "reopen_count": 0,
        "last_reopened_at": None,
        **overrides,
    }


def test_comment_legacy_array_pages_mixed_feed_and_sparse_counts(client, respx_mock):
    comments = Comments(client._http)
    legacy = respx_mock.get(f"{API}/annotations/{ANNOTATION_ID}/comments").mock(
        return_value=httpx.Response(200, json=[_comment()])
    )
    annotation_page = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/comments/page"
    ).mock(
        return_value=httpx.Response(
            200, json={"items": [_comment()], "next_cursor": "c1"}
        )
    )
    task_page = respx_mock.get(f"{API}/tasks/{TASK_ID}/comments/page").mock(
        return_value=httpx.Response(
            200, json={"items": [_comment()], "next_cursor": None}
        )
    )
    feedback_id = str(uuid4())
    discussion = respx_mock.get(f"{API}/tasks/{TASK_ID}/discussion/page").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "source": "feedback",
                        "data": {
                            "id": feedback_id,
                            "kind": "comment",
                            "anchor_type": "task",
                            "project_id": PROJECT_ID,
                            "task_id": TASK_ID,
                            "annotation_id": None,
                            "anchor_position": None,
                            "status": "open",
                            "severity": None,
                            "title": None,
                            "body": "Task discussion",
                            "author_id": REPORTER_ID,
                            "author_name": "Reporter",
                            "attachments": [],
                            "mentions": [],
                            "canvas_drawing": None,
                            "thread_parent_id": None,
                            "is_active": True,
                            "resolved_at": None,
                            "resolved_by_id": None,
                            "created_at": NOW,
                            "updated_at": NOW,
                            "actions": {"reply": True},
                        },
                        "actions": {"reply": True},
                    },
                    {
                        "source": "annotation_comment",
                        "data": _comment(),
                        "actions": {"edit": True, "delete": True},
                    },
                ],
                "next_cursor": "d1",
                "total": 2,
            },
        )
    )
    counts = respx_mock.get(f"{API}/tasks/{TASK_ID}/discussion/annotation-counts").mock(
        return_value=httpx.Response(200, json={"counts": {ANNOTATION_ID: 3}})
    )

    assert comments.list(ANNOTATION_ID)[0].id == UUID(COMMENT_ID)
    assert comments.page(ANNOTATION_ID, limit=12, cursor="old").next_cursor == "c1"
    assert dict(annotation_page.calls.last.request.url.params) == {
        "limit": "12",
        "cursor": "old",
    }
    assert comments.task_page(TASK_ID).items[0].annotation_id == UUID(ANNOTATION_ID)
    mixed = comments.discussion_page(
        TASK_ID, scope="annotation", annotation_id=ANNOTATION_ID
    )
    assert mixed.items[0].source == "feedback"
    assert mixed.items[0].data.id == UUID(feedback_id)
    assert mixed.items[0].actions.reply is True
    assert comments.annotation_counts(TASK_ID).counts == {ANNOTATION_ID: 3}
    assert legacy.called and task_page.called and discussion.called and counts.called


def test_comment_create_patch_aliases_and_upload_contract(client, respx_mock, tmp_path):
    comments = Comments(client._http)
    mention_id = str(uuid4())
    attachment_key = "comment-attachments/project/file.png"
    create = respx_mock.post(f"{API}/annotations/{ANNOTATION_ID}/comments").mock(
        return_value=httpx.Response(201, json=_comment())
    )
    patch = respx_mock.patch(f"{API}/comments/{COMMENT_ID}").mock(
        return_value=httpx.Response(200, json=_comment(is_resolved=False))
    )
    delete = respx_mock.delete(f"{API}/comments/{COMMENT_ID}").mock(
        return_value=httpx.Response(204)
    )
    upload = respx_mock.post(
        f"{API}/annotations/{ANNOTATION_ID}/comment-attachments/upload-init"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "storage_key": attachment_key,
                "upload_url": "https://storage.local/put?signature=secret",
                "expires_in": 900,
            },
        )
    )

    request = AnnotationCommentCreate(
        body="hello",
        mentions=[
            CommentMentionInput(
                user_id=mention_id, display_name="Alice", offset=0, length=5
            )
        ],
        attachments=[
            CommentAttachmentInput(
                storage_key=attachment_key,
                file_name="file.png",
                mime_type="image/png",
                size=3,
            )
        ],
    )
    assert comments.create(ANNOTATION_ID, request).body == "Found an issue"
    sent = json.loads(create.calls.last.request.content)
    assert sent["mentions"][0] == {
        "userId": mention_id,
        "displayName": "Alice",
        "offset": 0,
        "length": 5,
    }
    assert sent["attachments"][0]["storageKey"] == attachment_key

    comments.update(COMMENT_ID, AnnotationCommentUpdate(is_resolved=False))
    assert json.loads(patch.calls.last.request.content) == {"is_resolved": False}
    comments.delete(COMMENT_ID)
    assert delete.called

    signed = comments.init_attachment_upload(
        ANNOTATION_ID,
        CommentAttachmentUploadRequest(file_name="file.png", content_type="image/png"),
    )
    assert "secret" not in repr(signed)
    assert signed.model_dump(mode="json")["upload_url"].endswith("signature=secret")
    assert json.loads(upload.calls.last.request.content) == {
        "file_name": "file.png",
        "content_type": "image/png",
    }

    file_path = tmp_path / "uploaded.png"
    file_path.write_bytes(b"png")
    storage_put = respx_mock.put(host="storage.local", path="/put").mock(
        return_value=httpx.Response(200)
    )
    uploaded = comments.upload_attachment(ANNOTATION_ID, file_path)
    assert uploaded.storage_key == attachment_key
    assert uploaded.file_name == "uploaded.png" and uploaded.size == 3
    assert "Authorization" not in storage_put.calls.last.request.headers
    assert (
        AnnotationCommentCreate(body="", attachments=[uploaded]).attachments[0]
        is uploaded
    )


def test_comment_attachment_json_url_redacts_repr_and_redirect_download_is_bare(
    client, respx_mock, tmp_path
):
    comments = Comments(client._http)
    signed_url = "https://storage.local/comments/a.png?signature=private"
    url_route = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/comment-attachments/download",
        params={"key": "comment-attachments/a.png", "as_json": "true"},
    ).mock(return_value=httpx.Response(200, json={"download_url": signed_url}))
    result = comments.attachment_download_url(
        ANNOTATION_ID, "comment-attachments/a.png"
    )
    assert "private" not in repr(result)
    assert result.model_dump(mode="json")["download_url"] == signed_url
    assert url_route.called

    redirect = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/comment-attachments/download",
        params={"key": "comment-attachments/a.png"},
    ).mock(return_value=httpx.Response(302, headers={"Location": signed_url}))
    storage = respx_mock.get(host="storage.local", path="/comments/a.png").mock(
        return_value=httpx.Response(200, content=b"attachment")
    )
    destination = tmp_path / "attachment.png"
    assert (
        comments.download_attachment(
            ANNOTATION_ID, "comment-attachments/a.png", destination
        )
        == destination
    )
    assert redirect.calls.last.request.headers["Authorization"] == "Bearer ak_test"
    assert "Authorization" not in storage.calls.last.request.headers
    assert destination.read_bytes() == b"attachment"


@pytest.mark.parametrize(
    ("status", "location", "message"),
    [
        (301, "https://storage.local/a", "must return HTTP 302"),
        (302, "/relative", "absolute"),
        (302, "https://user:pass@storage.local/a", "absolute"),
    ],
)
def test_comment_redirect_rejects_non_302_or_unsafe_location(
    client, respx_mock, tmp_path, status, location, message
):
    comments = Comments(client._http)
    route = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/comment-attachments/download"
    ).mock(return_value=httpx.Response(status, headers={"Location": location}))
    error_type = APIStatusError if status != 302 else AAPError
    with pytest.raises(error_type, match=message):
        comments.download_attachment(
            ANNOTATION_ID, "comment-attachments/a", tmp_path / "a"
        )
    assert route.called


def test_history_routes_return_typed_entries_and_propagate_server_visibility(
    client, respx_mock
):
    history = AnnotationHistoryResource(client._http)
    annotation_route = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/history"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "annotation_id": ANNOTATION_ID,
                "task_id": TASK_ID,
                "entries": [
                    {
                        "kind": "comment",
                        "timestamp": NOW,
                        "actor": {
                            "id": REPORTER_ID,
                            "name": "Reporter",
                            "email": "r@example.com",
                            "role": "employee",
                        },
                        "action": None,
                        "detail": {"is_active": False},
                        "comment_id": COMMENT_ID,
                        "body": "old comment",
                    }
                ],
            },
        )
    )
    task_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/audit-history").mock(
        return_value=httpx.Response(
            200,
            json={
                "annotation_id": None,
                "task_id": TASK_ID,
                "entries": [],
            },
        )
    )
    entry = history.annotation(ANNOTATION_ID).entries[0]
    assert entry.kind == "comment" and entry.actor.name == "Reporter"
    assert history.task(TASK_ID).annotation_id is None
    assert annotation_route.called and task_route.called

    invisible = str(uuid4())
    respx_mock.get(f"{API}/tasks/{invisible}/audit-history").mock(
        return_value=httpx.Response(404, json={"detail": "Task not found"})
    )
    with pytest.raises(NotFoundError):
        history.task(invisible)


def test_bug_report_operations_preserve_metadata_nulls_and_markdown(
    client, respx_mock, tmp_path
):
    bugs = BugReports(client._http)
    list_route = respx_mock.get(f"{API}/bug_reports").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            text="# New reports",
        )
    )
    mine = respx_mock.get(f"{API}/bug_reports/mine").mock(
        return_value=httpx.Response(200, json={"items": [_report()], "total": 1})
    )
    create = respx_mock.post(f"{API}/bug_reports").mock(
        return_value=httpx.Response(201, json=_report())
    )
    report_route = respx_mock.get(f"{API}/bug_reports/{REPORT_ID}").mock(
        return_value=httpx.Response(200, json={**_report(), "comments": []})
    )
    patch = respx_mock.patch(f"{API}/bug_reports/{REPORT_ID}").mock(
        return_value=httpx.Response(200, json=_report(status="new"))
    )
    delete = respx_mock.delete(f"{API}/bug_reports/{REPORT_ID}").mock(
        return_value=httpx.Response(204)
    )
    cluster = respx_mock.post(f"{API}/bug_reports/cluster").mock(
        return_value=httpx.Response(
            200,
            json={
                "clusters": [{"report_id": REPORT_ID, "similar_ids": [str(uuid4())]}]
            },
        )
    )
    upload = respx_mock.post(f"{API}/bug_reports/screenshot/upload-init").mock(
        return_value=httpx.Response(
            200,
            json={
                "storage_key": "bug-report-attachments/u/screen.png",
                "upload_url": "https://storage.local/put?sig=secret",
                "expires_in": 900,
            },
        )
    )
    comment = respx_mock.post(f"{API}/bug_reports/{REPORT_ID}/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": str(uuid4()),
                "bug_report_id": REPORT_ID,
                "author_id": REPORTER_ID,
                "author_name": "Reporter",
                "author_role": "employee",
                "body": "More detail",
                "created_at": NOW,
            },
        )
    )

    assert bugs.list(format="markdown") == "# New reports"
    assert list_route.calls.last.request.url.params["format"] == "markdown"
    assert bugs.list_mine().items[0].project_id == UUID(PROJECT_ID)
    request = BugReportCreate(
        title="New issue",
        description="Repro details",
        project_id=PROJECT_ID,
        task_id=TASK_ID,
    )
    bugs.create(request)
    created = json.loads(create.calls.last.request.content)
    assert created["project_id"] == PROJECT_ID and created["task_id"] == TASK_ID
    assert bugs.get(REPORT_ID).display_id == "BUG-1"

    bugs.update(REPORT_ID, BugReportUpdate(status=None, resolution=None))
    assert json.loads(patch.calls.last.request.content) == {
        "status": None,
        "resolution": None,
    }
    bugs.delete(REPORT_ID)
    assert delete.called
    assert bugs.cluster().clusters[0].report_id == UUID(REPORT_ID)
    signed = bugs.init_screenshot_upload(
        BugScreenshotUploadRequest(file_name="screen.png", content_type="image/png")
    )
    assert "secret" not in repr(signed)
    assert signed.model_dump(mode="json")["upload_url"].endswith("sig=secret")
    screenshot_path = tmp_path / "screen.png"
    screenshot_path.write_bytes(b"png")
    storage_put = respx_mock.put(host="storage.local", path="/put").mock(
        return_value=httpx.Response(200)
    )
    screenshot = bugs.upload_screenshot(screenshot_path)
    assert screenshot.storage_key == "bug-report-attachments/u/screen.png"
    assert screenshot.size == 3
    report_with_attachment = BugReportCreate(
        title="Screenshot report",
        description="Includes screenshot",
        attachments=[screenshot],
    )
    assert report_with_attachment.attachments[0].storage_key == screenshot.storage_key
    assert screenshot.model_dump(mode="json", by_alias=True)["mimeType"] == "image/png"
    assert "Authorization" not in storage_put.calls.last.request.headers
    bugs.add_comment(REPORT_ID, BugCommentCreate(body="More detail"))
    assert json.loads(comment.calls.last.request.content) == {"body": "More detail"}
    assert report_route.called and mine.called and upload.called and cluster.called


def test_bug_report_attachment_redirect_uses_bare_downloader(
    client, respx_mock, tmp_path
):
    bugs = BugReports(client._http)
    report_route = respx_mock.get(
        f"{API}/bug_reports/{REPORT_ID}/attachments/download",
        params={"key": "bug-report-attachments/u/screen.png"},
    ).mock(
        return_value=httpx.Response(
            302,
            headers={
                "Location": "https://storage.local/bugs/screen.png?signature=hidden"
            },
        )
    )
    storage = respx_mock.get(host="storage.local", path="/bugs/screen.png").mock(
        return_value=httpx.Response(200, content=b"image")
    )
    destination = tmp_path / "screen.png"
    bugs.download_attachment(
        REPORT_ID, "bug-report-attachments/u/screen.png", destination
    )
    assert report_route.calls.last.request.headers["Authorization"] == "Bearer ak_test"
    assert "Authorization" not in storage.calls.last.request.headers
    assert storage.calls.last.request.url.params["signature"] == "hidden"
    assert destination.read_bytes() == b"image"


def test_comment_cli_requires_confirmation_before_any_write(
    client, respx_mock, tmp_path, monkeypatch
):
    payload = tmp_path / "comment.json"
    payload.write_text(json.dumps({"body": "hello"}), encoding="utf-8")
    comments_route = respx_mock.post(
        f"{API}/annotations/{ANNOTATION_ID}/comments"
    ).mock(return_value=httpx.Response(201, json=_comment()))
    client.comments = Comments(client._http)
    monkeypatch.setattr(comments_cli, "get_client", lambda _json: client)

    declined = runner.invoke(
        comments_cli.app,
        ["create", ANNOTATION_ID, "--request", str(payload), "--json"],
    )
    assert declined.exit_code == 2
    assert not comments_route.called

    accepted = runner.invoke(
        comments_cli.app,
        ["create", ANNOTATION_ID, "--request", str(payload), "--yes", "--json"],
    )
    assert accepted.exit_code == 0, accepted.output
    assert comments_route.called
    assert json.loads(comments_route.calls.last.request.content) == {"body": "hello"}


def test_comment_cli_legacy_array_has_json_safe_models(client, respx_mock, monkeypatch):
    respx_mock.get(f"{API}/annotations/{ANNOTATION_ID}/comments").mock(
        return_value=httpx.Response(200, json=[_comment()])
    )
    client.comments = Comments(client._http)
    monkeypatch.setattr(comments_cli, "get_client", lambda _json: client)

    result = runner.invoke(comments_cli.app, ["list", ANNOTATION_ID, "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["id"] == COMMENT_ID
    assert data[0]["annotation_id"] == ANNOTATION_ID


@pytest.mark.parametrize("field", ["title", "description", "severity"])
def test_bug_update_rejects_explicit_null_content(field):
    with pytest.raises(ValidationError):
        BugReportUpdate.model_validate({field: None})
    assert BugReportUpdate().model_dump(exclude_unset=True) == {}


def test_bug_nullable_admin_fields_can_be_cleared():
    fields = dict.fromkeys(
        ["duplicate_of_id", "assigned_to_id", "fixed_in_version", "resolution"]
    )
    assert (
        BugReportUpdate.model_validate(fields).model_dump(exclude_unset=True) == fields
    )


def test_oversized_screenshot_is_rejected_before_requests(client, respx_mock, tmp_path):
    path = tmp_path / "oversized.png"
    with path.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(AAPError, match="10 MiB"):
        client.bug_reports.upload_screenshot(path)
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "resource,parent,key,path",
    [
        (
            "comments",
            ANNOTATION_ID,
            "comment-attachments/a.png",
            f"/annotations/{ANNOTATION_ID}/comment-attachments/download",
        ),
        (
            "bug_reports",
            REPORT_ID,
            "bug-report-attachments/a.png",
            f"/bug_reports/{REPORT_ID}/attachments/download",
        ),
    ],
)
def test_signed_download_status_body_is_not_exposed(
    client, respx_mock, tmp_path, resource, parent, key, path
):
    respx_mock.get(API + path).respond(
        302, headers={"location": "https://storage.local/object?signature=hidden"}
    )
    respx_mock.get(host="storage.local", path="/object").respond(
        403, text="signature=hidden"
    )
    destination = tmp_path / "preserved.bin"
    destination.write_bytes(b"keep")
    with pytest.raises(APIStatusError) as caught:
        getattr(client, resource).download_attachment(parent, key, destination)
    assert "hidden" not in str(caught.value)
    assert "hidden" not in str(caught.value.detail)
    assert destination.read_bytes() == b"keep"


def test_bug_response_preserves_unknown_server_status(client, respx_mock):
    respx_mock.get(f"{API}/bug_reports/{REPORT_ID}").respond(
        200,
        json={
            **_report(),
            "status": "awaiting_evidence",
            "severity": "urgent",
            "comments": [],
        },
    )
    report = client.bug_reports.get(REPORT_ID)
    assert report.status == "awaiting_evidence"
    assert report.severity == "urgent"


@pytest.mark.parametrize(
    "resource,route,metadata",
    [
        (
            "comments",
            f"/annotations/{ANNOTATION_ID}/comment-attachments/upload-init",
            {"storage_key": "comment-attachments/u/a.png"},
        ),
        (
            "bug_reports",
            "/bug_reports/screenshot/upload-init",
            {"storage_key": "bug-report-attachments/u/a.png"},
        ),
    ],
)
def test_signed_upload_status_body_is_not_exposed(
    client, respx_mock, tmp_path, resource, route, metadata
):
    path = tmp_path / "a.png"
    path.write_bytes(b"png")
    respx_mock.post(API + route).respond(
        200,
        json={
            **metadata,
            "upload_url": "https://storage.local/upload?signature=hidden",
            "expires_in": 900,
        },
    )
    upload = respx_mock.put(host="storage.local", path="/upload").respond(
        403, text="signature=hidden"
    )
    with pytest.raises(APIStatusError) as caught:
        if resource == "comments":
            client.comments.upload_attachment(ANNOTATION_ID, path)
        else:
            client.bug_reports.upload_screenshot(path)
    assert caught.value.status_code == 403
    assert "hidden" not in str(caught.value.detail)
    assert "authorization" not in upload.calls.last.request.headers
