"""Typed user and group resource contracts plus sensitive CLI workflows."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.cli import groups as groups_cli
from ai_annotation.cli import users as users_cli
from ai_annotation.errors import ConflictError
from ai_annotation.groups import Groups
from ai_annotation.user_management_models import (
    AdminResetPasswordResponse,
    BulkGroupAssignmentPreview,
    BulkGroupAssignmentRequest,
    BulkGroupAssignmentResponse,
    BulkInviteRequest,
    BulkInviteResponse,
    BulkInviteResultItem,
    InvitationCreate,
    InvitationCreated,
    OffboardingCommitRequest,
)
from ai_annotation.users import Users

from .conftest import API

runner = CliRunner()
NOW = "2026-10-17T00:00:00Z"


def _user(user_id: str | None = None, **extra) -> dict:
    return {
        "id": user_id or str(uuid4()),
        "email": "person@example.test",
        "name": "Person",
        "role": "employee",
        "group_name": None,
        "group_id": None,
        "status": "active",
        "is_active": True,
        "created_at": NOW,
        **extra,
    }


def _group(group_id: str | None = None, **extra) -> dict:
    return {
        "id": group_id or str(uuid4()),
        "name": "workers",
        "description": None,
        "member_count": 3,
        "created_at": NOW,
        **extra,
    }


def test_invitation_request_keeps_platform_and_project_roles_separate():
    project_id = uuid4()
    request = InvitationCreate(
        email=" Person@Example.Test ",
        role="employee",
        project_id=project_id,
        project_member_role="annotator",
    )
    assert request.email == "person@example.test"
    assert request.role == "employee"
    assert request.project_member_role == "annotator"

    with pytest.raises(ValidationError, match="project_member_role is required"):
        InvitationCreate(
            email="person@example.test", role="employee", project_id=project_id
        )
    with pytest.raises(ValidationError, match="requires project_id"):
        InvitationCreate(
            email="person@example.test",
            role="employee",
            project_member_role="annotator",
        )


def test_bulk_group_request_requires_group_id_and_preserves_explicit_null(
    client, respx_mock
):
    user_id = uuid4()
    with pytest.raises(ValidationError, match="group_id"):
        BulkGroupAssignmentRequest(user_ids=[user_id])

    request = BulkGroupAssignmentRequest(user_ids=[user_id], group_id=None)
    preview_route = respx_mock.post(f"{API}/users/groups/bulk/preview").mock(
        return_value=httpx.Response(
            200,
            json={
                "group_id": None,
                "group_name": None,
                "items": [{"user_id": str(user_id), "ok": True}],
                "applicable": 1,
                "blocked": 0,
            },
        )
    )
    apply_route = respx_mock.post(f"{API}/users/groups/bulk").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"user_id": str(user_id), "ok": True}],
                "succeeded": 1,
                "failed": 0,
            },
        )
    )
    users = Users(client._http)

    users.preview_bulk_group_assignment(request)
    users.bulk_group_assignment(request)

    expected = {"user_ids": [str(user_id)], "group_id": None}
    assert json.loads(preview_route.calls.last.request.content) == expected
    assert json.loads(apply_route.calls.last.request.content) == expected


def test_one_time_credentials_are_hidden_from_model_repr():
    invite = InvitationCreated(
        invite_url="https://example.test/register?token=secret-invite",
        token="secret-invite",
        expires_at=NOW,
    )
    bulk_item = BulkInviteResultItem(
        index=0,
        email="person@example.test",
        ok=True,
        token="secret-bulk",
        invite_url="https://example.test/register?token=secret-bulk",
    )
    password = AdminResetPasswordResponse(
        temp_password="Once-Only-Password-7!",
        message="deliver securely",
        target_email="person@example.test",
    )

    representation = repr((invite, bulk_item, password))
    for secret in (
        "secret-invite",
        "secret-bulk",
        "Once-Only-Password-7!",
        "https://example.test/register?token=",
    ):
        assert secret not in representation


def test_users_query_keeps_page_visibility_flags_and_bulk_invite_partial_results(
    client, respx_mock
):
    user_id = str(uuid4())
    query_route = respx_mock.get(f"{API}/users/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [_user(user_id, is_managed=False, is_lifecycle_managed=False)],
                "total": 101,
                "page": 2,
                "page_size": 50,
                "pages": 3,
            },
        )
    )
    users = Users(client._http)
    page = users.query(page=2, page_size=50, group_id=str(uuid4()), status="all")
    assert page.items[0].is_managed is False
    assert page.items[0].is_lifecycle_managed is False
    assert page.total == 101 and page.pages == 3
    assert query_route.calls.last.request.url.params["page"] == "2"
    assert query_route.calls.last.request.url.params["status"] == "all"

    invite_route = respx_mock.post(f"{API}/users/bulk-invite").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "index": 0,
                        "email": "one@example.test",
                        "ok": True,
                        "retryable": False,
                        "invitation_id": str(uuid4()),
                        "token": "one-time-token",
                        "invite_url": "https://example.test/register?token=one-time-token",
                    },
                    {
                        "index": 1,
                        "email": "bad@example.test",
                        "ok": False,
                        "retryable": True,
                        "error": "invalid role",
                    },
                ],
                "succeeded": 1,
                "failed": 1,
                "preview": False,
            },
        )
    )
    body = BulkInviteRequest(
        items=[
            {"email": "one@example.test", "role": "employee"},
            {"email": "bad@example.test", "role": "unknown"},
        ]
    )
    result = users.bulk_invite(body)
    assert result.succeeded == 1 and result.failed == 1
    assert result.items[0].token == "one-time-token"
    sent = json.loads(invite_route.calls.last.request.content)
    assert sent == {
        "items": [
            {"email": "one@example.test", "role": "employee"},
            {"email": "bad@example.test", "role": "unknown"},
        ]
    }


def test_offboarding_commit_sends_the_supplied_preview_version_once(client, respx_mock):
    user_id = str(uuid4())
    route = respx_mock.post(f"{API}/users/{user_id}/offboarding").mock(
        return_value=httpx.Response(409, json={"detail": "preview is stale"})
    )
    users = Users(client._http)
    request = OffboardingCommitRequest(
        preview_version="v-current", reason="handoff", mode="handoff", projects=[]
    )
    with pytest.raises(ConflictError):
        users.offboard(user_id, request)
    assert json.loads(route.calls.last.request.content) == {
        "preview_version": "v-current",
        "reason": "handoff",
        "mode": "handoff",
        "projects": [],
    }
    assert route.call_count == 1


def test_offboarding_parses_the_captured_public_api_responses(client, respx_mock):
    captured = json.loads(
        (Path(__file__).parent / "fixtures/offboarding_api_response.json").read_text()
    )
    expected_preview, expected_result = captured["preview"], captured["result"]
    user_id = expected_preview["user"]["id"]
    preview_route = respx_mock.get(f"{API}/users/{user_id}/offboarding-preview").mock(
        return_value=httpx.Response(200, json=expected_preview)
    )
    commit_route = respx_mock.post(f"{API}/users/{user_id}/offboarding").mock(
        return_value=httpx.Response(200, json=expected_result)
    )
    users = Users(client._http)
    preview = users.offboarding_preview(user_id)
    assert preview.preview_version == expected_preview["preview_version"]
    assert preview.can_commit and len(preview.projects) == 1
    assert preview.projects[0].tasks["annotator"]["rejected"] == 1
    assert (
        preview.projects[0].roles["owner"].receiver_options[0].role == "project_admin"
    )
    assert (
        preview.api_keys[0].key_prefix == expected_preview["api_keys"][0]["key_prefix"]
    )
    result = users.offboard(
        user_id,
        OffboardingCommitRequest(
            preview_version=preview.preview_version, reason="交接 😀", projects=[]
        ),
    )
    assert result.status == "suspended" and result.mode == "handoff"
    assert not result.user.is_active and result.user.disabled_reason == "交接 😀"
    assert [item.role for item in result.transfers] == ["owner", "annotator"]
    assert result.transfers[1].task_count == 2
    assert (
        len(result.revoked_api_key_ids) == 1
        and result.audit_id == expected_result["audit_id"]
    )
    assert preview_route.call_count == commit_route.call_count == 1


def test_user_export_streams_wrapped_json_to_an_existing_directory(
    client, respx_mock, tmp_path
):
    content = b'{"_export_meta":{"count":1},"users":[]}'
    route = respx_mock.get(f"{API}/users/export").mock(
        return_value=httpx.Response(200, content=content)
    )
    dest = tmp_path / "users.json"
    result = Users(client._http).export(dest, format="json", status="all")
    assert result == dest
    assert dest.read_bytes() == content
    assert route.calls.last.request.url.params["format"] == "json"
    assert route.calls.last.request.url.params["status"] == "all"


def test_groups_keep_legacy_array_and_paged_query_contracts(client, respx_mock):
    list_route = respx_mock.get(f"{API}/groups").mock(
        return_value=httpx.Response(200, json=[_group()])
    )
    query_route = respx_mock.get(f"{API}/groups/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [_group()],
                "total": 1,
                "page": 1,
                "page_size": 50,
                "pages": 1,
            },
        )
    )
    groups = Groups(client._http)
    assert groups.list()[0].member_count == 3
    page = groups.query(search="work")
    assert page.items[0].name == "workers"
    assert (
        list_route.called
        and query_route.calls.last.request.url.params["search"] == "work"
    )


class _ClientContext:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False


def test_offboarding_cli_confirms_and_does_not_retry_a_stale_preview(
    client, respx_mock, monkeypatch, tmp_path
):
    user_id = str(uuid4())
    request_file = tmp_path / "offboarding.json"
    request_file.write_text(json.dumps({"preview_version": "supplied-preview"}))
    route = respx_mock.post(f"{API}/users/{user_id}/offboarding").mock(
        return_value=httpx.Response(
            409,
            json={
                "detail": {
                    "code": "offboarding_preview_stale",
                    "expected_preview_version": "supplied-preview",
                    "current_preview_version": "a" * 64,
                    "blockers": [],
                }
            },
        )
    )
    monkeypatch.setattr(users_cli, "get_client", lambda _json: _ClientContext(client))
    args = ["offboard", user_id, str(request_file), "--json"]
    refused = runner.invoke(users_cli.app, args)
    assert refused.exit_code == 2 and not route.called
    assert "--yes" in refused.output
    stale = runner.invoke(users_cli.app, [*args, "--yes"])
    assert stale.exit_code != 0 and route.call_count == 1
    assert (
        json.loads(route.calls.last.request.content)["preview_version"]
        == "supplied-preview"
    )


class _PasswordOps:
    def __init__(self):
        self.calls = 0

    def admin_reset_password(self, _user_id):
        self.calls += 1
        return AdminResetPasswordResponse(
            temp_password="Once-Only-Password-7!",
            message="deliver securely",
            target_email="person@example.test",
        )


def test_password_reset_requires_secret_opt_in_before_call(monkeypatch):
    ops = _PasswordOps()
    monkeypatch.setattr(
        users_cli,
        "get_client",
        lambda _json: _ClientContext(type("C", (), {"users": ops})()),
    )

    hidden = runner.invoke(users_cli.app, ["admin-reset-password", "user-1", "--yes"])
    assert hidden.exit_code == 2
    assert ops.calls == 0

    shown = runner.invoke(
        users_cli.app,
        ["admin-reset-password", "user-1", "--yes", "--show-secret"],
    )
    assert shown.exit_code == 0, shown.output
    assert "Once-Only-Password-7!" in shown.stdout
    assert ops.calls == 1


@pytest.mark.parametrize("preview", [False, True])
def test_bulk_invite_cli_reports_partial_receipt_and_only_fails_apply(
    monkeypatch, tmp_path, preview
):
    result = BulkInviteResponse(
        items=[
            BulkInviteResultItem(
                index=0,
                email="ok@example.test",
                ok=True,
                invitation_id=uuid4(),
                token=None if preview else "fresh-token",
                invite_url=(
                    None
                    if preview
                    else "https://example.test/register?token=fresh-token"
                ),
            ),
            BulkInviteResultItem(
                index=1,
                email="bad@example.test",
                ok=False,
                retryable=True,
                error="invalid role",
            ),
        ],
        succeeded=1,
        failed=1,
        preview=preview,
    )

    class _InviteOps:
        def bulk_invite(self, _request):
            return result

        def preview_bulk_invites(self, _request):
            return result

    client = type("C", (), {"users": _InviteOps()})()
    monkeypatch.setattr(users_cli, "get_client", lambda _json: _ClientContext(client))
    request_file = tmp_path / "invites.json"
    request_file.write_text(
        '{"items":[{"email":"ok@example.test","role":"employee"}]}',
        encoding="utf-8",
    )
    command = "bulk-invite-preview" if preview else "bulk-invite"
    args = [command, str(request_file), "--json"]
    if not preview:
        args.append("--yes")
    output = runner.invoke(users_cli.app, args)
    assert output.exit_code == (0 if preview else 1), output.output
    decoded = json.loads(output.stdout)
    assert (decoded["succeeded"], decoded["failed"]) == (1, 1)
    assert len(decoded["items"]) == 2
    assert decoded["items"][1]["error"] == "invalid role"
    assert decoded["items"][0]["token"] == (None if preview else "fresh-token")


@pytest.mark.parametrize("preview", [False, True])
def test_bulk_group_cli_reports_all_rows_and_only_fails_apply(
    monkeypatch, tmp_path, preview
):
    user_ids = [uuid4(), uuid4()]
    if preview:
        result = BulkGroupAssignmentPreview(
            group_id=None,
            group_name=None,
            items=[
                {"user_id": user_ids[0], "ok": True},
                {"user_id": user_ids[1], "ok": False, "error": "outside scope"},
            ],
            applicable=1,
            blocked=1,
        )
    else:
        result = BulkGroupAssignmentResponse(
            items=[
                {"user_id": user_ids[0], "ok": True},
                {
                    "user_id": user_ids[1],
                    "ok": False,
                    "retryable": True,
                    "error": "outside scope",
                },
            ],
            succeeded=1,
            failed=1,
        )

    class _GroupOps:
        def bulk_group_assignment(self, _request):
            return result

        def preview_bulk_group_assignment(self, _request):
            return result

    client = type("C", (), {"users": _GroupOps()})()
    monkeypatch.setattr(users_cli, "get_client", lambda _json: _ClientContext(client))
    request_file = tmp_path / f"groups-{preview}.json"
    request_file.write_text(
        json.dumps({"user_ids": [str(item) for item in user_ids], "group_id": None}),
        encoding="utf-8",
    )
    command = "bulk-group-preview" if preview else "bulk-group-apply"
    args = [command, str(request_file), "--json"]
    if not preview:
        args.append("--yes")
    output = runner.invoke(users_cli.app, args)

    assert output.exit_code == (0 if preview else 1), output.output
    decoded = json.loads(output.stdout)
    assert len(decoded["items"]) == 2
    assert decoded["items"][1]["error"] == "outside scope"


@pytest.mark.parametrize(
    ("group_id", "expected_action"),
    [
        (None, "清除 2 个账号的数据组关联"),
        ("8c79d7d0-04e3-4eb9-9c02-3cd88bb777fd", "设置到数据组"),
    ],
)
def test_bulk_group_confirmation_distinguishes_clear_from_assignment(
    monkeypatch, tmp_path, group_id, expected_action
):
    user_ids = [uuid4(), uuid4()]
    response = BulkGroupAssignmentResponse(
        items=[{"user_id": user_id, "ok": True} for user_id in user_ids],
        succeeded=2,
        failed=0,
    )

    class _GroupOps:
        def bulk_group_assignment(self, request):
            assert (str(request.group_id) if request.group_id else None) == group_id
            return response

    client = type("C", (), {"users": _GroupOps()})()
    confirmation_messages = []
    monkeypatch.setattr(users_cli, "get_client", lambda _json: _ClientContext(client))
    monkeypatch.setattr(
        users_cli,
        "confirm_destructive",
        lambda message, _yes, _json: confirmation_messages.append(message),
    )
    request_file = tmp_path / f"group-update-{group_id}.json"
    request_file.write_text(
        json.dumps(
            {"user_ids": [str(item) for item in user_ids], "group_id": group_id}
        ),
        encoding="utf-8",
    )

    output = runner.invoke(
        users_cli.app,
        ["bulk-group-apply", str(request_file), "--yes", "--json"],
    )

    assert output.exit_code == 0, output.output
    assert expected_action in confirmation_messages[0]


def test_group_delete_requires_confirmation_before_api_call(monkeypatch):
    class _GroupOps:
        calls = 0

        def delete(self, _group_id):
            self.calls += 1

    ops = _GroupOps()
    monkeypatch.setattr(
        groups_cli,
        "get_client",
        lambda _json: _ClientContext(type("C", (), {"groups": ops})()),
    )
    denied = runner.invoke(groups_cli.app, ["delete", "group-1", "--json"])
    assert denied.exit_code == 2
    assert "--yes" in denied.output
    assert ops.calls == 0

    accepted = runner.invoke(groups_cli.app, ["delete", "group-1", "--json", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout) == {"deleted": True, "group_id": "group-1"}
    assert ops.calls == 1
