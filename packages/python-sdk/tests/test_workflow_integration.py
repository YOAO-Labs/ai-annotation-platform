"""Cross-surface contracts for externally driven SDK workflows."""

import json
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation import describe_operation
from ai_annotation.cli.main import app

from .conftest import API, BASE


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_reads_existing_job_without_waiting_or_mutating(respx_mock, json_output):
    job_id = str(uuid4())
    payload = {
        "id": job_id,
        "kind": "batch_predict",
        "status": "running",
        "progress_pct": 12,
        "payload": {},
        "result": {},
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
    route = respx_mock.get(f"{API}/async-jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    args = ["jobs", "get", job_id, *(["--json"] if json_output else [])]
    result = CliRunner().invoke(
        app, args, env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if json_output:
        data = json.loads(result.stdout)
        assert data["id"] == job_id and data["status"] == "running"
    else:
        assert job_id in result.stdout and "running" in result.stdout
    assert route.call_count == 1 and len(respx_mock.calls) == 1


@pytest.mark.parametrize("json_output", [False, True])
def test_registered_pipeline_list_serializes_nonempty_typed_rows(
    respx_mock, json_output
):
    pipeline_id, project_id, backend_id, author_id = [str(uuid4()) for _ in range(4)]
    row = {
        "id": pipeline_id,
        "scope": "private",
        "project_id": project_id,
        "name": "Detector",
        "stages": [
            {"stage": 7, "ml_backend_id": backend_id, "params": {"threshold": 0.2}}
        ],
        "created_by": author_id,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
    route = respx_mock.get(f"{API}/project-pipelines").mock(
        return_value=httpx.Response(200, json=[row])
    )
    result = CliRunner().invoke(
        app,
        [
            "pipelines",
            "list",
            "--scope",
            "private",
            "--project",
            project_id,
            *(["--json"] if json_output else []),
        ],
        env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if json_output:
        data = json.loads(result.stdout)
        assert data[0]["id"] == pipeline_id
        assert data[0]["stages"][0]["params"] == {"threshold": 0.2}
        assert data[0]["stages"][0]["ml_backend_id"] == backend_id
    else:
        assert pipeline_id in result.stdout and "Detector" in result.stdout
    assert dict(route.calls.last.request.url.params) == {
        "scope": "private",
        "project_id": project_id,
    }
    assert route.call_count == 1


@pytest.mark.parametrize("status", [200, 409])
def test_registered_preannotation_stdin_requires_confirmation_and_keeps_identity(
    respx_mock, status
):
    project_id, task_id, backend_id, job_id = [str(uuid4()) for _ in range(4)]
    key = "caller-persisted-identity"
    payload = {
        "task_ids": [task_id],
        "output_mode": "box",
        "batch_id": None,
        "pipeline_stages": [
            {
                "stage": 7,
                "ml_backend_id": backend_id,
                "parent_stage": None,
                "params": {"threshold": 0.2},
            }
        ],
    }
    acknowledgement = {
        "job_id": job_id,
        "status": "queued",
        "total_tasks": None,
        "channel": f"project:{project_id}:preannotate",
    }
    route = respx_mock.post(f"{API}/projects/{project_id}/preannotate").mock(
        return_value=httpx.Response(
            status,
            json=acknowledgement
            if status == 200
            else {"detail": {"code": "idempotency_conflict"}},
        )
    )
    args = [
        "--error-format=json",
        "pipelines",
        "preannotate",
        "--project",
        project_id,
        "--body",
        "-",
        "--idempotency-key",
        key,
        "--json",
    ]
    env = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}
    refused = CliRunner().invoke(app, args, input=json.dumps(payload), env=env)
    assert refused.exit_code == 2
    assert json.loads(refused.stderr)["kind"] == "usage_error"
    assert not route.called
    result = CliRunner().invoke(
        app, [*args, "--yes"], input=json.dumps(payload), env=env
    )
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == key
    assert request.headers["Authorization"] == "Bearer ak_test"
    assert json.loads(request.content) == payload
    if status == 200:
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["job_id"] == job_id and data["total_tasks"] is None
        assert result.stderr == ""
    else:
        assert result.exit_code == 1 and result.stdout == ""
        assert json.loads(result.stderr)["code"] == "idempotency_conflict"
    assert len(respx_mock.calls) == 1


def test_registered_notification_preference_preserves_other_channels(respx_mock):
    route = respx_mock.put(f"{API}/notification-preferences").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = CliRunner().invoke(
        app,
        ["notifications", "update-preference", "--body", "-", "--json"],
        input='{"type":"future-type","toast":false}',
        env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
    assert json.loads(route.calls.last.request.content) == {
        "type": "future-type",
        "toast": False,
    }


def test_workflow_discovery_explains_scope_null_and_idempotency_limits():
    preannotate = describe_operation("pipelines.preannotate")
    assert any("Idempotency-Key" in note for note in preannotate["constraints"])
    assert any("HTTP 200" in note for note in preannotate["constraints"])
    assert preannotate["cli"][0]["confirmation"] is True
    assert preannotate["sdk"][0]["request_schemas"][0]["model"] == "PreannotateRequest"
    reply = describe_operation("feedbacks.reply")
    assert any("no idempotency key" in note for note in reply["constraints"])
    patch = describe_operation("feedbacks.patch")
    assert any("null as no change" in note for note in patch["constraints"])


def test_pipeline_scope_rejected_before_http(respx_mock):
    result = CliRunner().invoke(
        app, ["pipelines", "list", "--scope", "invalid", "--json"]
    )
    assert result.exit_code == 2
    assert "scope must be" in result.output
    assert not respx_mock.calls
