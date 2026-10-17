from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from ai_annotation.cli import pipelines as cli_pipelines
from ai_annotation.cli.pipelines import app as pipelines_app
from ai_annotation.errors import APIStatusError
from ai_annotation.pipeline_models import (
    PreannotateRequest,
    PreannotationSubmission,
    ProjectPipeline,
    ProjectPipelineApplyRequest,
    ProjectPipelineCreate,
    ProjectPipelineUpdate,
)
from ai_annotation.pipelines import ProjectPipelines

API = "http://testserver/api/v1"
PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
PIPELINE_ID = UUID("87654321-4321-8765-4321-876543218765")
BACKEND_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
JOB_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def pipeline_response(**overrides):
    data = {
        "id": str(PIPELINE_ID),
        "scope": "private",
        "project_id": str(PROJECT_ID),
        "organization_id": None,
        "name": "Video objects",
        "stages": [
            {
                "stage": 7,
                "ml_backend_id": str(BACKEND_ID),
                "parent_stage": None,
                "source": {"kind": "dataset", "data_type": "video"},
                "params": {"score_threshold": 0.4},
            }
        ],
        "is_default": True,
        "created_by": str(PROJECT_ID),
        "created_by_name": "Annotator",
        "usage_count": 2,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
    return data | overrides


def test_list_and_get_use_server_filters_and_typed_responses(client, respx_mock):
    route = respx_mock.get(
        f"{API}/project-pipelines",
        params={"scope": "private", "project_id": str(PROJECT_ID)},
    ).mock(return_value=httpx.Response(200, json=[pipeline_response()]))
    get_route = respx_mock.get(f"{API}/project-pipelines/{PIPELINE_ID}").mock(
        return_value=httpx.Response(200, json=pipeline_response())
    )

    api = ProjectPipelines(client._http)
    listed = api.list(scope="private", project_id=PROJECT_ID)
    detail = api.get(PIPELINE_ID)

    assert route.called and get_route.called
    assert isinstance(listed[0], ProjectPipeline)
    assert isinstance(detail, ProjectPipeline)
    assert listed[0].stages[0].stage == 7
    assert listed[0].stages[0].source == {"kind": "dataset", "data_type": "video"}
    assert listed[0].stages[0].params == {"score_threshold": 0.4}


def test_create_validates_owner_scope_and_preserves_dynamic_stage_config(
    client, respx_mock
):
    with pytest.raises(ValueError, match="require project_id"):
        ProjectPipelineCreate(
            name="Invalid private pipeline",
            stages=[{"stage": 0, "ml_backend_id": BACKEND_ID}],
        )
    with pytest.raises(PydanticValidationError):
        ProjectPipelineCreate(
            name="Unknown stage field",
            project_id=PROJECT_ID,
            stages=[{"stage": 0, "ml_backend_id": BACKEND_ID, "unknown": True}],
        )

    body = ProjectPipelineCreate(
        name="Video objects",
        project_id=PROJECT_ID,
        stages=[
            {
                "stage": 7,
                "ml_backend_id": BACKEND_ID,
                "parent_stage": None,
                "source": {"kind": "dataset", "execution_unit": "frame"},
                "params": {"confidence": 0.37, "labels": ["cat", "dog"]},
            },
            {
                "stage": 11,
                "ml_backend_id": BACKEND_ID,
                "parent_stage": 7,
                "roi": {"mode": "crop", "pad": 0.1},
                "write": {"target": "attributes", "keys": ["color"]},
                "input": {"mode": "crop", "custom_model_option": True},
            },
        ],
    )
    route = respx_mock.post(f"{API}/project-pipelines").mock(
        return_value=httpx.Response(201, json=pipeline_response())
    )

    result = ProjectPipelines(client._http).create(body)

    sent = json.loads(route.calls.last.request.content)
    assert result.id == PIPELINE_ID
    assert sent["stages"][0]["stage"] == 7
    assert sent["stages"][0]["parent_stage"] is None
    assert sent["stages"][0]["source"]["execution_unit"] == "frame"
    assert sent["stages"][0]["params"] == {
        "confidence": 0.37,
        "labels": ["cat", "dog"],
    }
    assert sent["stages"][1]["parent_stage"] == 7
    assert sent["stages"][1]["input"]["custom_model_option"] is True


def test_update_preserves_explicit_null_and_omits_unset_fields(client, respx_mock):
    route = respx_mock.put(f"{API}/project-pipelines/{PIPELINE_ID}").mock(
        return_value=httpx.Response(200, json=pipeline_response())
    )
    api = ProjectPipelines(client._http)

    api.update(PIPELINE_ID, ProjectPipelineUpdate(name="Renamed"))
    api.update(
        PIPELINE_ID,
        ProjectPipelineUpdate(project_id=None, organization_id=None),
    )

    assert json.loads(route.calls[0].request.content) == {"name": "Renamed"}
    assert json.loads(route.calls[1].request.content) == {
        "project_id": None,
        "organization_id": None,
    }


def test_delete_and_apply_use_exact_routes_and_typed_copy_response(client, respx_mock):
    delete_route = respx_mock.delete(f"{API}/project-pipelines/{PIPELINE_ID}").mock(
        return_value=httpx.Response(204)
    )
    apply_route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/pipelines/apply").mock(
        return_value=httpx.Response(201, json=pipeline_response())
    )

    api = ProjectPipelines(client._http)
    assert api.delete(PIPELINE_ID) is None
    result = api.apply(
        PROJECT_ID,
        ProjectPipelineApplyRequest(pipeline_id=PIPELINE_ID, set_default=True),
    )

    assert delete_route.called and apply_route.called
    assert json.loads(apply_route.calls.last.request.content) == {
        "pipeline_id": str(PIPELINE_ID),
        "set_default": True,
    }
    assert isinstance(result, ProjectPipeline)


def test_preannotate_sends_optional_idempotency_header_and_returns_job_ack(
    client, respx_mock
):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/preannotate").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": str(JOB_ID),
                "status": "queued",
                "total_tasks": None,
                "channel": f"project:{PROJECT_ID}:preannotate",
                "celery_task_id": str(JOB_ID),
                "warnings": [],
            },
        )
    )
    body = PreannotateRequest(
        task_ids=[UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")],
        pipeline_stages=[
            {
                "stage": 7,
                "ml_backend_id": BACKEND_ID,
                "source": {"kind": "dataset", "execution_unit": "frame"},
                "params": {"threshold": 0.2},
            }
        ],
    )

    result = ProjectPipelines(client._http).preannotate(
        PROJECT_ID, body, idempotency_key="retry-key / exact"
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer ak_test"
    assert request.headers["Idempotency-Key"] == "retry-key / exact"
    payload = json.loads(request.content)
    assert payload["pipeline_stages"][0]["stage"] == 7
    assert payload["pipeline_stages"][0]["source"]["execution_unit"] == "frame"
    assert payload["pipeline_stages"][0]["params"] == {"threshold": 0.2}
    assert "ml_backend_id" not in payload
    assert isinstance(result, PreannotationSubmission)
    assert result.job_id == JOB_ID
    assert result.total_tasks is None


def test_preannotate_requires_backend_only_for_flat_requests():
    with pytest.raises(PydanticValidationError, match="ml_backend_id is required"):
        PreannotateRequest()
    PreannotateRequest(pipeline_stages=[{"stage": 13, "ml_backend_id": BACKEND_ID}])


def test_preannotate_without_key_does_not_retry_post_or_add_key(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/preannotate").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )

    with pytest.raises(APIStatusError):
        ProjectPipelines(client._http).preannotate(
            PROJECT_ID, PreannotateRequest(ml_backend_id=BACKEND_ID)
        )

    assert route.call_count == 1
    assert "Idempotency-Key" not in route.calls.last.request.headers


@pytest.mark.parametrize(
    ("command", "needs_body"),
    [("delete", False), ("apply", True), ("preannotate", True)],
)
def test_pipeline_cli_refuses_json_mutation_without_yes_before_client(
    monkeypatch, tmp_path, command, needs_body
):
    body_file = tmp_path / "body.json"
    body_file.write_text(
        json.dumps(
            {
                "pipeline_id": str(PIPELINE_ID),
            }
            if command == "apply"
            else {"ml_backend_id": str(BACKEND_ID)}
        ),
        encoding="utf-8",
    )
    client_opened = False

    def fail_if_opened(*args, **kwargs):
        nonlocal client_opened
        client_opened = True
        raise AssertionError("confirmation refusal must happen before client creation")

    monkeypatch.setattr(cli_pipelines, "get_client", fail_if_opened)
    args = [command]
    if command == "delete":
        args.append(str(PIPELINE_ID))
    else:
        args.extend(["--project", str(PROJECT_ID)])
        if needs_body:
            args.extend(["--body", str(body_file)])
    args.append("--json")
    result = CliRunner().invoke(pipelines_app, args)

    assert result.exit_code == 2
    assert not client_opened
    assert "--yes" in result.output


def test_preannotate_cli_reads_stdin_and_prints_ack_without_wait(monkeypatch):
    calls = []
    acknowledgement = PreannotationSubmission(
        job_id=JOB_ID,
        status="queued",
        total_tasks=None,
        channel=f"project:{PROJECT_ID}:preannotate",
        celery_task_id=str(JOB_ID),
    )

    class PipelineAPI:
        def preannotate(self, project_id, body, *, idempotency_key=None):
            calls.append((project_id, body, idempotency_key))
            return acknowledgement

    class FakeClient:
        pipelines = PipelineAPI()

    class ClientContext:
        def __enter__(self):
            return FakeClient()

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli_pipelines, "get_client", lambda _: ClientContext())
    result = CliRunner().invoke(
        pipelines_app,
        [
            "preannotate",
            "--project",
            str(PROJECT_ID),
            "--body",
            "-",
            "--idempotency-key",
            "retry-me",
            "--yes",
            "--json",
        ],
        input=json.dumps({"ml_backend_id": str(BACKEND_ID)}),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "queued"
    assert json.loads(result.stdout)["job_id"] == str(JOB_ID)
    assert calls[0][0] == str(PROJECT_ID)
    assert calls[0][1].ml_backend_id == BACKEND_ID
    assert calls[0][2] == "retry-me"


def test_submission_response_accepts_replay_without_warnings():
    result = PreannotationSubmission.model_validate(
        {
            "job_id": str(JOB_ID),
            "status": "running",
            "total_tasks": 3,
            "channel": f"project:{PROJECT_ID}:preannotate",
            "celery_task_id": str(JOB_ID),
        }
    )
    assert result.warnings == []
