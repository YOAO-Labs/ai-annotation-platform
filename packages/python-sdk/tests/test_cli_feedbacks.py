"""Feedback CLI request-file, confirmation, and JSON contracts."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.feedbacks import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "120",
}
FEEDBACK_ID = str(uuid4())


def _feedback(**extra):
    return {
        "id": FEEDBACK_ID,
        "kind": "issue",
        "anchor_type": "task",
        "project_id": str(uuid4()),
        "task_id": str(uuid4()),
        "status": "open",
        "body": "Review this task",
        "author_id": str(uuid4()),
        "created_at": "2026-10-17T00:00:00Z",
        "actions": {"edit": True, "change_status": True, "delete": True, "reply": True},
        **extra,
    }


def test_reply_reads_json_from_stdin_and_returns_bare_json(respx_mock):
    route = respx_mock.post(f"{API}/feedbacks/{FEEDBACK_ID}/replies").mock(
        return_value=httpx.Response(200, json=_feedback(thread_parent_id=FEEDBACK_ID))
    )

    result = runner.invoke(
        app,
        ["reply", FEEDBACK_ID, "--body", "-", "--json"],
        input='{"body":"Thanks","attachments":[]}',
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {
        "body": "Thanks",
        "attachments": [],
    }
    assert json.loads(result.stdout)["thread_parent_id"] == FEEDBACK_ID


def test_thread_forwards_cursor_and_outputs_root_page(respx_mock):
    route = respx_mock.get(f"{API}/feedbacks/{FEEDBACK_ID}/thread").mock(
        return_value=httpx.Response(
            200,
            json={
                "root": _feedback(),
                "items": [_feedback(thread_parent_id=FEEDBACK_ID)],
                "next_cursor": "opaque-next",
                "total": 1,
            },
        )
    )

    result = runner.invoke(
        app,
        ["thread", FEEDBACK_ID, "--cursor", "opaque-old==", "--limit", "2", "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert dict(route.calls.last.request.url.params) == {
        "cursor": "opaque-old==",
        "limit": "2",
    }
    output = json.loads(result.stdout)
    assert output["root"]["id"] == FEEDBACK_ID
    assert output["items"][0]["thread_parent_id"] == FEEDBACK_ID
    assert output["next_cursor"] == "opaque-next"


def test_patch_stdin_preserves_explicit_null_and_empty_mentions(respx_mock):
    route = respx_mock.patch(f"{API}/feedbacks/{FEEDBACK_ID}").mock(
        return_value=httpx.Response(200, json=_feedback())
    )

    result = runner.invoke(
        app,
        ["patch", FEEDBACK_ID, "--body", "-", "--json"],
        input='{"title":null,"mentions":[]}',
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {
        "title": None,
        "mentions": [],
    }


def test_delete_requires_confirmation_and_reports_204(respx_mock):
    route = respx_mock.delete(f"{API}/feedbacks/{FEEDBACK_ID}").mock(
        return_value=httpx.Response(204)
    )

    refused = runner.invoke(app, ["delete", FEEDBACK_ID, "--json"], env=ENV)
    assert refused.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(app, ["delete", FEEDBACK_ID, "--yes", "--json"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert route.call_count == 1
    assert json.loads(accepted.stdout) == {
        "deleted": True,
        "feedback_id": FEEDBACK_ID,
    }
