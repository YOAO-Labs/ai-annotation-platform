"""Self-service account contracts without live account mutations."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation._http import HttpTransport
from ai_annotation.account import Account
from ai_annotation.account_models import (
    AvatarRefUpdate,
    DeactivationRequest,
    PasswordChange,
    ProfileUpdate,
    TaskEventBatchRequest,
    TaskEventBatchResult,
    UserPreferencesPatch,
)
from ai_annotation.avatars import Avatars
from ai_annotation.cli import account as account_cli
from ai_annotation.errors import AAPError, NotFoundError

from .conftest import API, BASE

runner = CliRunner()
USER_ID = str(uuid4())
TASK_ID = str(uuid4())
PROJECT_ID = str(uuid4())
EVENT_ID = str(uuid4())
AVATAR_TOKEN = "a" * 32
USER = {
    "id": USER_ID,
    "email": "agent@example.test",
    "name": "Agent",
    "role": "employee",
    "group_name": None,
    "status": "online",
    "is_active": True,
    "preferences": {},
    "avatar_ref": None,
    "created_at": "2026-10-17T00:00:00Z",
}


class _Response:
    def __init__(self, value=None, status_code=200):
        self._value = value
        self.status_code = status_code

    def json(self):
        return self._value


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0)


def _task_event(**overrides):
    start = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    return {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "kind": "annotate",
        "started_at": start,
        "ended_at": start + timedelta(seconds=2),
        "duration_ms": 2000,
        "client_id": EVENT_ID,
        **overrides,
    }


def test_account_methods_preserve_payloads_and_concrete_responses():
    preferences = {
        "workbench": {"layout": {"workspace": {"future": {"opaque": True}}}},
        "namedPresetsRevision": "b" * 32,
    }
    event_result = {
        "accepted": 1,
        "queued_async": True,
        "discarded": [],
    }
    transport = _Transport(
        [
            _Response(USER),
            _Response(preferences),
            _Response(preferences),
            _Response(status_code=204),
            _Response(USER),
            _Response(USER),
            _Response(USER),
            _Response(USER),
            _Response(status_code=204),
            _Response(event_result),
        ]
    )
    account = Account(transport)
    password = PasswordChange(old_password="OldPass123", new_password="NewPass456")
    preference_patch = {
        "namedPresetsRevision": "b" * 32,
        "workbench": {"layout": {"workspace": {"namedPresets": {}}}},
        "ui": {"nullableFuturePreference": None},
    }

    profile = account.update_profile(ProfileUpdate(name="Agent Two"))
    fetched_preferences = account.get_preferences()
    updated_preferences = account.update_preferences(preference_patch)
    account.change_password(password)
    preset_avatar = account.set_avatar_ref(AvatarRefUpdate(avatar_ref="preset:fox"))
    cleared_avatar = account.clear_avatar()
    account.request_deactivation(DeactivationRequest(reason="Leaving"))
    account.cancel_deactivation()
    account.heartbeat()
    events = account.submit_task_events(TaskEventBatchRequest(events=[_task_event()]))

    assert str(profile.id) == str(preset_avatar.id) == str(cleared_avatar.id) == USER_ID
    assert fetched_preferences == preferences
    assert updated_preferences == preferences
    assert events == TaskEventBatchResult.model_validate(event_result)
    assert "OldPass123" not in repr(password)
    assert "NewPass456" not in repr(password)
    assert [call[:2] for call in transport.calls] == [
        ("PATCH", "/auth/me"),
        ("GET", "/auth/me/preferences"),
        ("PATCH", "/auth/me/preferences"),
        ("POST", "/auth/me/password"),
        ("PATCH", "/auth/me/avatar"),
        ("DELETE", "/auth/me/avatar"),
        ("POST", "/auth/me/deactivation-request"),
        ("DELETE", "/auth/me/deactivation-request"),
        ("POST", "/auth/me/heartbeat"),
        ("POST", "/auth/me/task-events:batch"),
    ]
    assert transport.calls[2][2]["json"] == preference_patch
    assert transport.calls[3][2]["json"] == {
        "old_password": "OldPass123",
        "new_password": "NewPass456",
    }
    assert transport.calls[4][2]["json"] == {"avatar_ref": "preset:fox"}
    assert "json" not in transport.calls[5][2]
    assert transport.calls[9][2]["json"]["events"][0]["client_id"] == EVENT_ID


def test_preferences_root_model_keeps_null_cas_and_opaque_values():
    value = {
        "namedPresetsRevision": "c" * 32,
        "workbench": {
            "layout": {"workspace": {"namedPresets": {"preset-a": {"x": 1}}}}
        },
        "future": {"nullable": None, "opaque": [1, "two"]},
    }

    patch = UserPreferencesPatch.model_validate(value)

    assert patch.root == value
    assert patch.model_dump(mode="json") == value


def test_password_change_rejects_reusing_the_current_password():
    with pytest.raises(ValidationError, match="must differ"):
        PasswordChange(old_password="SamePass123", new_password="SamePass123")


@pytest.mark.parametrize(
    "event, message",
    [
        ({**_task_event(), "started_at": datetime(2026, 1, 1)}, "timezone"),
        ({**_task_event(), "duration_ms": 1}, "duration_ms"),
        (
            {**_task_event(), "duration_ms": 4 * 60 * 60 * 1000 + 1},
            "less than or equal",
        ),
    ],
)
def test_task_events_validate_closed_aware_intervals(event, message):
    with pytest.raises(ValidationError, match=message):
        TaskEventBatchRequest(events=[event])


def test_task_events_do_not_generate_client_ids():
    event = _task_event()
    event.pop("client_id")
    transport = _Transport(
        [_Response({"accepted": 1, "queued_async": False, "discarded": []})]
    )

    Account(transport).submit_task_events(TaskEventBatchRequest(events=[event]))

    assert "client_id" not in transport.calls[0][2]["json"]["events"][0]


def test_avatar_upload_uses_multipart_and_refuses_oversize_before_request(
    tmp_path, respx_mock
):
    transport = HttpTransport(BASE, "ak_test")
    route = respx_mock.post(f"{API}/auth/me/avatar").mock(
        return_value=httpx.Response(200, json=USER)
    )
    path = tmp_path / "avatar.png"
    path.write_bytes(b"not-decoded-by-the-mock")

    try:
        result = Account(transport).upload_avatar(path)
        request = route.calls.last.request
        assert str(result.id) == USER_ID
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="file"; filename="avatar.png"' in request.content
        assert b"not-decoded-by-the-mock" in request.content

        oversized = tmp_path / "oversized.png"
        oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with pytest.raises(AAPError, match="2 MiB"):
            Account(transport).upload_avatar(oversized)
        assert route.call_count == 1
    finally:
        transport.close()


def test_avatar_download_uses_api_path_and_preserves_destination_on_failure(
    tmp_path, respx_mock
):
    transport = HttpTransport(BASE, "ak_test")
    path = tmp_path / "avatar.webp"
    path.write_bytes(b"old-avatar")
    route = respx_mock.get(f"{API}/avatars/{AVATAR_TOKEN}").mock(
        return_value=httpx.Response(404, json={"detail": "avatar not found"})
    )

    try:
        with pytest.raises(NotFoundError):
            Avatars(transport).download(AVATAR_TOKEN, path)
        assert route.called
        assert path.read_bytes() == b"old-avatar"
        assert not route.calls.last.request.headers.get("if-none-match")
        with pytest.raises(ValueError, match="32 lowercase"):
            Avatars(transport).download("../invalid", path)
    finally:
        transport.close()


def test_avatar_download_streams_success_to_destination(tmp_path, respx_mock):
    transport = HttpTransport(BASE, "ak_test")
    path = tmp_path / "avatar.webp"
    respx_mock.get(f"{API}/avatars/{AVATAR_TOKEN}").mock(
        return_value=httpx.Response(200, content=b"webp-bytes")
    )

    try:
        result = Avatars(transport).download(AVATAR_TOKEN, path)
        assert result == path
        assert path.read_bytes() == b"webp-bytes"
    finally:
        transport.close()


def _install_fake_client(monkeypatch, *, account=None, avatars=None):
    @contextmanager
    def fake_client(_json_mode):
        yield type("ClientStub", (), {"account": account, "avatars": avatars})()

    monkeypatch.setattr(account_cli, "get_client", fake_client)


def test_preferences_cli_keeps_cas_and_null_from_stdin(monkeypatch):
    expected = {
        "workbench": {"shortcuts": {"common": {"common.task.next": None}}},
        "ui": {"theme": "dark"},
    }

    class AccountStub:
        patch = None

        def update_preferences(self, patch):
            self.patch = patch
            return patch

    account = AccountStub()
    _install_fake_client(monkeypatch, account=account)
    result = runner.invoke(
        account_cli.app,
        ["preferences", "patch", "--body", "-", "--json"],
        input=json.dumps(expected),
    )

    assert result.exit_code == 0, result.output
    assert account.patch == expected
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "command, body",
    [
        (
            ["avatar", "set", "preset:fox"],
            None,
        ),
        (
            ["deactivation", "request"],
            None,
        ),
        (
            ["password", "change", "--body", "-"],
            {"old_password": "OldPass123", "new_password": "NewPass456"},
        ),
    ],
)
def test_sensitive_cli_writes_require_confirmation_before_client(
    monkeypatch, command, body
):
    def fail_if_opened(_json_mode):
        raise AssertionError("confirmation must happen before the API client opens")

    monkeypatch.setattr(account_cli, "get_client", fail_if_opened)
    result = runner.invoke(
        account_cli.app,
        [*command, "--json"],
        input=json.dumps(body) if body is not None else None,
    )

    assert result.exit_code != 0
    assert "--yes" in result.output


def test_password_cli_accepts_stdin_without_echoing_secrets(monkeypatch):
    class AccountStub:
        body = None

        def change_password(self, body):
            self.body = body

    account = AccountStub()
    _install_fake_client(monkeypatch, account=account)
    result = runner.invoke(
        account_cli.app,
        ["password", "change", "--body", "-", "--yes", "--json"],
        input=json.dumps({"old_password": "OldPass123", "new_password": "NewPass456"}),
    )

    assert result.exit_code == 0, result.output
    assert "OldPass123" not in result.stdout + result.stderr
    assert "NewPass456" not in result.stdout + result.stderr
    assert account.body.old_password.get_secret_value() == "OldPass123"


def test_avatar_upload_cli_requires_yes_before_opening_client(monkeypatch, tmp_path):
    path = tmp_path / "avatar.png"
    path.write_bytes(b"mock image")

    def fail_if_opened(_json_mode):
        raise AssertionError("confirmation must happen before the API client opens")

    monkeypatch.setattr(account_cli, "get_client", fail_if_opened)
    result = runner.invoke(
        account_cli.app,
        ["avatar", "upload", str(path), "--json"],
    )

    assert result.exit_code != 0
    assert "--yes" in result.output


def test_event_cli_reports_partial_receipt_then_exits_nonzero(monkeypatch, tmp_path):
    event_result = TaskEventBatchResult(
        accepted=1,
        queued_async=False,
        discarded=[{"index": 1, "client_id": str(uuid4()), "reason": "task_not_found"}],
    )

    class AccountStub:
        def submit_task_events(self, _body):
            return event_result

    body_path = tmp_path / "events.json"
    body_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "task_id": TASK_ID,
                        "project_id": PROJECT_ID,
                        "kind": "annotate",
                        "started_at": "2026-10-17T08:00:00Z",
                        "ended_at": "2026-10-17T08:00:02Z",
                        "duration_ms": 2000,
                        "client_id": EVENT_ID,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _install_fake_client(monkeypatch, account=AccountStub())

    result = runner.invoke(
        account_cli.app,
        ["events", "submit", "--body", str(body_path), "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["accepted"] == 1
    assert json.loads(result.stdout)["discarded"][0]["reason"] == "task_not_found"
