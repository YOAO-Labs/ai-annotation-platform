import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli import notifications as notification_cli
from ai_annotation.notifications import Notifications

from .conftest import API, BASE

runner = CliRunner()
NOTICE_ID = str(uuid4())
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}


@pytest.fixture
def cli_client(client, monkeypatch):
    client.notifications = Notifications(client._http)

    @contextmanager
    def get_client(_json_mode=False):
        yield client

    monkeypatch.setattr(notification_cli, "get_client", get_client)
    return client


@pytest.mark.parametrize(
    ("args", "method", "path", "payload", "stdin"),
    [
        (
            ["list", "--unread-only", "--limit", "5", "--offset", "2", "--json"],
            "GET",
            "/notifications",
            {"items": [], "total": 0, "unread": 0},
            "",
        ),
        (
            ["unread-count", "--json"],
            "GET",
            "/notifications/unread-count",
            {"unread": 4},
            "",
        ),
        (
            ["mark-read", NOTICE_ID, "--json"],
            "POST",
            f"/notifications/{NOTICE_ID}/read",
            {"ok": True},
            "",
        ),
        (
            ["mark-all-read", "--yes", "--json"],
            "POST",
            "/notifications/mark-all-read",
            {"updated": 4},
            "",
        ),
        (
            ["delete", NOTICE_ID, "--yes", "--json"],
            "DELETE",
            f"/notifications/{NOTICE_ID}",
            {"ok": True},
            "",
        ),
        (
            ["clear-read", "--yes", "--json"],
            "POST",
            "/notifications/clear-read",
            {"deleted": 2},
            "",
        ),
        (
            ["preferences", "--json"],
            "GET",
            "/notification-preferences",
            {"items": []},
            "",
        ),
        (
            ["update-preference", "--body", "-", "--json"],
            "PUT",
            "/notification-preferences",
            {"ok": True},
            '{"type":"future_type","toast":true}',
        ),
    ],
)
def test_notification_cli_json_commands(
    cli_client, respx_mock, args, method, path, payload, stdin
):
    route = respx_mock.request(method, f"{API}{path}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    result = runner.invoke(notification_cli.app, args, env=ENV, input=stdin)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert route.called
    if method == "GET" and path == "/notifications":
        assert route.calls.last.request.url.params["unread_only"] == "true"


def test_notification_cli_json_preserves_native_payload_values(cli_client, respx_mock):
    payload = ["legacy", None, 2**100, {"label": "雪"}]
    item = {
        "id": NOTICE_ID,
        "type": "legacy.notification",
        "target_type": "task",
        "target_id": NOTICE_ID,
        "payload": payload,
        "created_at": "2026-10-17T00:00:00Z",
    }
    respx_mock.get(f"{API}/notifications").mock(
        return_value=httpx.Response(
            200,
            json={"items": [item], "total": 1, "unread": 1},
        )
    )

    result = runner.invoke(
        notification_cli.app,
        ["list", "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["items"][0]["payload"] == payload


@pytest.mark.parametrize(
    "args",
    [
        ["mark-all-read", "--json"],
        ["delete", NOTICE_ID, "--json"],
        ["clear-read", "--json"],
    ],
)
def test_cli_refuses_broad_or_destructive_mutation_without_confirmation(
    cli_client, respx_mock, args
):
    result = runner.invoke(notification_cli.app, args, env=ENV)

    assert result.exit_code != 0
    assert not respx_mock.calls


def test_cli_rejects_invalid_preference_stdin_before_http(cli_client, respx_mock):
    result = runner.invoke(
        notification_cli.app,
        ["update-preference", "--body", "-", "--json"],
        env=ENV,
        input='{"type":"future_type","toast":null}',
    )

    assert result.exit_code == 1
    assert "toast" in result.output
    assert not respx_mock.calls


def test_cli_list_rejects_server_exceeding_limit_before_http(cli_client, respx_mock):
    result = runner.invoke(
        notification_cli.app,
        ["list", "--limit", "101", "--json"],
        env=ENV,
    )

    assert result.exit_code != 0
    assert not respx_mock.calls
