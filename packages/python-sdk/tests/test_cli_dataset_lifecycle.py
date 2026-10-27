"""Dataset state filtering and reviewed CLI lifecycle changes."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli import datasets as datasets_cli
from ai_annotation.cli.main import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "140",
    "NO_COLOR": "1",
    "TERM": "dumb",
}
DATASET_ID = str(uuid4())
ACTOR_ID = str(uuid4())
COUNTS = {
    "items": 12,
    "sources": 2,
    "projects": 3,
    "tasks": 15,
    "annotations": 18,
    "active_scans": 0,
    "active_registrations": 0,
    "enabled_schedules": 1,
    "pending_jobs": 0,
    "ready_lineage_versions": 4,
    "unfinished_lineage_versions": 0,
}


def _routes(respx_mock, operation="archive", *, preview_fields=None, apply_status=200):
    now = datetime.now(timezone.utc)
    archived = operation == "restore"
    dataset = {
        "id": DATASET_ID,
        "display_id": "D-1",
        "name": "dataset",
        "lifecycle_revision": 7,
        "archived_at": now.isoformat() if archived else None,
        "archived_by": ACTOR_ID if archived else None,
    }
    get = respx_mock.get(f"{API}/datasets/{DATASET_ID}").mock(
        return_value=httpx.Response(200, json=dataset)
    )
    preview_data = {
        "dataset_id": DATASET_ID,
        "lifecycle_revision": 7,
        "state": "archived" if archived else "active",
        "operation": operation,
        "measured_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "impact_token": "exact-reviewed-token",
        "counts": COUNTS,
        "frozen_coverage": "recorded_dataset_membership_only",
        **(preview_fields or {}),
    }
    preview = respx_mock.post(f"{API}/datasets/{DATASET_ID}/lifecycle-preview").mock(
        return_value=httpx.Response(200, json=preview_data)
    )
    result = {
        "dataset_id": DATASET_ID,
        "lifecycle_revision": 8,
        "state": "archived" if operation == "archive" else "active",
        "archived_at": now.isoformat() if operation == "archive" else None,
        "archived_by": ACTOR_ID if operation == "archive" else None,
    }
    apply = respx_mock.post(f"{API}/datasets/{DATASET_ID}/lifecycle").mock(
        return_value=httpx.Response(apply_status, json=result)
    )
    return get, preview, apply, preview_data


@pytest.mark.parametrize("operation", ["archive", "restore"])
def test_lifecycle_json_applies_only_the_exact_preview(respx_mock, operation):
    get, preview, apply, review = _routes(respx_mock, operation)
    result = runner.invoke(
        app, ["datasets", operation, DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["dataset_id"] == DATASET_ID
    assert output["lifecycle_revision"] == 8
    assert output["state"] == ("archived" if operation == "archive" else "active")
    assert output["archived_by"] == (ACTOR_ID if operation == "archive" else None)
    assert json.loads(preview.calls.last.request.content) == {
        "operation": operation,
        "expected_revision": 7,
    }
    assert json.loads(apply.calls.last.request.content) == {
        "operation": operation,
        "expected_revision": 7,
        "impact_token": "exact-reviewed-token",
    }
    assert get.call_count == preview.call_count == apply.call_count == 1
    assert "recorded_dataset_membership_only" in result.stderr
    assert review["expires_at"] in result.stderr
    assert "关联项目" in result.stderr and "冻结版本" in result.stderr
    assert "exact-reviewed-token" not in result.output
    if operation == "restore":
        assert "不会恢复已撤销的权限" in result.stderr
        assert "不会自动恢复旧作业" in result.stderr


@pytest.mark.parametrize("operation", ["archive", "restore"])
def test_lifecycle_json_requires_yes_before_any_request(respx_mock, operation):
    result = runner.invoke(app, ["datasets", operation, DATASET_ID, "--json"], env=ENV)
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert len(respx_mock.calls) == 0


def test_lifecycle_noninteractive_requires_yes(respx_mock, monkeypatch):
    monkeypatch.setattr(
        datasets_cli,
        "sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False)),
    )
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID], env=ENV, input="y\n"
    )
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize("answer,apply_count", [("n\n", 0), ("y\n", 1)])
def test_interactive_lifecycle_previews_before_confirmation(
    respx_mock, monkeypatch, answer, apply_count
):
    monkeypatch.setattr(
        datasets_cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    _, preview, apply, _ = _routes(respx_mock)
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID], env=ENV, input=answer
    )
    assert result.exit_code == (0 if apply_count else 1), result.output
    assert preview.call_count == 1
    assert apply.call_count == apply_count
    assert result.output.index("影响预览") < result.output.index("确认归档")


def test_lifecycle_expired_preview_requires_new_command(respx_mock):
    _, preview, apply, _ = _routes(
        respx_mock,
        preview_fields={
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
        },
    )
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert "预览已过期" in result.stderr
    assert result.stdout == ""
    assert preview.call_count == 1
    assert not apply.called


@pytest.mark.parametrize(
    "fields",
    [
        {"dataset_id": str(uuid4())},
        {"lifecycle_revision": 8},
        {"operation": "restore"},
        {"state": "archived"},
    ],
)
def test_lifecycle_mismatched_review_is_never_applied(respx_mock, fields):
    _, preview, apply, _ = _routes(respx_mock, preview_fields=fields)
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert "预览与本次操作不一致" in result.stderr
    assert preview.call_count == 1
    assert not apply.called


@pytest.mark.parametrize(
    "code",
    [
        "dataset_lifecycle_preview_changed",
        "dataset_lifecycle_preview_expired",
        "dataset_lifecycle_preview_invalid",
        "dataset_lifecycle_changed",
        "dataset_busy",
    ],
)
def test_lifecycle_conflict_is_not_repreviewed_or_retried(respx_mock, code):
    get, preview, apply, _ = _routes(respx_mock)
    apply.mock(return_value=httpx.Response(409, json={"detail": {"code": code}}))
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert code in result.stderr
    assert result.stdout == ""
    assert get.call_count == preview.call_count == apply.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_lifecycle_preview_denial_never_applies(respx_mock, status):
    _, preview, apply, _ = _routes(respx_mock)
    preview.mock(return_value=httpx.Response(status, json={"detail": "denied"}))
    result = runner.invoke(
        app, ["datasets", "archive", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert str(status) in result.stderr
    assert preview.call_count == 1
    assert not apply.called


@pytest.mark.parametrize("state", [None, "active", "archived", "all"])
def test_datasets_list_state_pagination_and_json(respx_mock, state):
    route = respx_mock.get(f"{API}/datasets").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 10, "offset": 20}
        )
    )
    args = [
        "datasets",
        "list",
        "--search",
        "image",
        "--data-type",
        "image",
        "--limit",
        "10",
        "--offset",
        "20",
        "--json",
    ]
    if state is not None:
        args.extend(["--state", state])
    result = runner.invoke(app, args, env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "items": [],
        "total": 0,
        "limit": 10,
        "offset": 20,
    }
    assert dict(route.calls.last.request.url.params) == {
        "search": "image",
        "data_type": "image",
        "limit": "10",
        "offset": "20",
        "state": state or "active",
    }


def test_datasets_list_invalid_state_sends_no_request(respx_mock):
    result = runner.invoke(app, ["datasets", "list", "--state", "unknown"], env=ENV)
    assert result.exit_code == 2
    assert len(respx_mock.calls) == 0


def test_state_option_is_not_accepted_by_other_resources(respx_mock):
    result = runner.invoke(app, ["projects", "list", "--state", "archived"], env=ENV)
    assert result.exit_code == 2
    assert len(respx_mock.calls) == 0
