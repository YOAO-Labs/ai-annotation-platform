"""Learning CLI contracts at file, confirmation, and HTTP boundaries."""

import json
import hashlib
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field
from typer.main import get_command
from typer.testing import CliRunner

from ai_annotation import AAPError
from ai_annotation.cli._output import read_json_body
from ai_annotation.cli.main import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "120",
}
PROJECT, VERSION, RUN, ATTEMPT, MODEL, ROUND = [str(uuid4()) for _ in range(6)]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1)


@pytest.mark.parametrize("content", ["[1]", "null", '"secret"', "{bad", ""])
def test_request_file_rejects_invalid_object_without_echo(tmp_path, content):
    path = tmp_path / "request.json"
    path.write_text(content)
    with pytest.raises(AAPError) as exc:
        read_json_body(path, Request)
    assert "secret" not in str(exc.value)


def test_request_file_errors_omit_input_values(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"count": "private-token-value"}))
    with pytest.raises(AAPError, match="count") as exc:
        read_json_body(path, Request)
    assert "private-token-value" not in str(exc.value)
    with pytest.raises(AAPError):
        read_json_body(tmp_path / "missing.json", Request)
    path.write_bytes(b"\xff")
    with pytest.raises(AAPError):
        read_json_body(path, Request)


def test_learning_commands_have_valid_help():
    command = get_command(app)
    learning = command.commands["learning"]
    assert set(learning.commands) == {
        "training",
        "task-datasets",
        "datasets",
        "runs",
        "recipes",
        "previews",
        "models",
        "releases",
        "policies",
        "evaluations",
        "studies",
        "active-learning",
    }
    count = 0
    for group, entry in learning.commands.items():
        for name in entry.commands:
            result = runner.invoke(app, ["learning", group, name, "--help"], env=ENV)
            assert result.exit_code == 0, (group, name, result.output, result.exception)
            assert "--json" in result.output
            count += 1
    assert count == 95


def test_dataset_list_forwards_task_and_uuid_filter(respx_mock):
    route = respx_mock.get(f"{API}/projects/{PROJECT}/training-datasets").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 7})
    )
    result = runner.invoke(
        app,
        [
            "learning",
            "datasets",
            "list",
            "--project",
            PROJECT,
            "--task",
            "segment",
            "--task-dataset-id",
            VERSION,
            "--limit",
            "5",
            "--offset",
            "5",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total"] == 7
    params = route.calls.last.request.url.params
    assert dict(params) == {
        "task": "segment",
        "task_dataset_id": VERSION,
        "limit": "5",
        "offset": "5",
    }


def test_policy_disable_revision_is_query_and_guarded(respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/learning-policy/disable").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "revision_changed"}})
    )
    args = [
        "learning",
        "policies",
        "disable",
        "--project",
        PROJECT,
        "--expected-revision",
        "3",
        "--json",
    ]
    refused = runner.invoke(app, args, env=ENV)
    assert refused.exit_code == 2 and "--yes" in refused.output
    assert not route.called
    accepted = runner.invoke(app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 1
    assert "revision_changed" in accepted.stderr
    assert accepted.stdout == ""
    assert route.call_count == 1
    assert route.calls.last.request.url.params["expected_revision"] == "3"
    assert not route.calls.last.request.content


@pytest.mark.parametrize(
    "group,resource",
    [("runs", RUN), ("evaluations", VERSION), ("active-learning", ROUND)],
)
def test_cancel_declined_sends_no_request(group, resource, respx_mock):
    result = runner.invoke(
        app,
        ["learning", group, "cancel", resource, "--project", PROJECT],
        input="n\n",
        env=ENV,
    )
    assert result.exit_code != 0
    assert len(respx_mock.calls) == 0


def test_invalid_write_body_never_calls_api(tmp_path, respx_mock):
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps({"name": "secret-request-name", "request_id": "invalid"})
    )
    result = runner.invoke(
        app,
        [
            "learning",
            "models",
            "create",
            "--project",
            PROJECT,
            "--body",
            str(path),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "secret-request-name" not in result.output
    assert "Traceback" not in result.output
    assert len(respx_mock.calls) == 0


def test_excessively_nested_body_fails_without_traceback(tmp_path, respx_mock):
    path = tmp_path / "deep.json"
    path.write_text('{"count":' + "[" * 2000 + "0" + "]" * 2000 + "}")
    result = runner.invoke(
        app,
        [
            "learning",
            "runs",
            "create",
            "--project",
            PROJECT,
            "--body",
            str(path),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1
    assert result.stdout == "" and "Traceback" not in result.output
    assert len(respx_mock.calls) == 0


def test_run_submission_keeps_id_and_omits_legacy_parameters(tmp_path, respx_mock):
    payload = {
        "name": "single-epoch",
        "request_id": str(uuid4()),
        "dataset_id": VERSION,
        "configuration": {
            "schema_id": "current",
            "schema_digest": "a" * 64,
            "values": {"epochs": 1},
        },
        "preflight_digest": "b" * 64,
        "origin": None,
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))
    route = respx_mock.post(f"{API}/projects/{PROJECT}/training-runs").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "preflight_changed"}})
    )
    result = runner.invoke(
        app,
        [
            "learning",
            "runs",
            "create",
            "--project",
            PROJECT,
            "--body",
            str(path),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1 and "preflight_changed" in result.stderr
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == payload


def test_failed_download_integrity_preserves_file_and_credentials(tmp_path, respx_mock):
    descriptor = respx_mock.get(
        f"{API}/projects/{PROJECT}/trained-models/{MODEL}/download"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://storage.test/weights?signature=short-lived",
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "size_bytes": 8,
            },
        )
    )
    storage = respx_mock.get("https://storage.test/weights?signature=short-lived").mock(
        return_value=httpx.Response(200, content=b"corrupt")
    )
    out = tmp_path / "weights.pt"
    out.write_bytes(b"previous")
    result = runner.invoke(
        app,
        [
            "learning",
            "models",
            "download",
            MODEL,
            "--project",
            PROJECT,
            "--out",
            str(out),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert result.stdout == ""
    assert out.read_bytes() == b"previous"
    assert descriptor.call_count == storage.call_count == 1
    assert descriptor.calls.last.request.headers["authorization"] == "Bearer ak_test"
    assert "authorization" not in storage.calls.last.request.headers
    assert not list(tmp_path.glob(".aap-download-*"))


def test_model_decision_request_id_and_body_preserved(tmp_path, respx_mock):
    payload = {
        "request_id": str(uuid4()),
        "disposition": "research",
        "note": "human review",
    }
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(payload))
    route = respx_mock.post(
        f"{API}/projects/{PROJECT}/trained-models/{MODEL}/decisions"
    ).mock(return_value=httpx.Response(403, json={"detail": "manager required"}))
    result = runner.invoke(
        app,
        [
            "learning",
            "models",
            "decide",
            MODEL,
            "--project",
            PROJECT,
            "--body",
            str(path),
            "--yes",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1 and "403" in result.stderr
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == payload


def test_binary_download_auth_query_and_json_path(tmp_path, respx_mock):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT}/training-datasets/{VERSION}/insights/samples/{RUN}/media"
    ).mock(return_value=httpx.Response(200, content=b"frozen-image"))
    out = tmp_path / "image.png"
    result = runner.invoke(
        app,
        [
            "learning",
            "datasets",
            "download-media",
            VERSION,
            RUN,
            "--project",
            PROJECT,
            "--preview",
            "--out",
            str(out),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"path": str(out)}
    assert out.read_bytes() == b"frozen-image"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer ak_test"
    assert request.url.params["preview"] == "true"


def test_log_query_conflict_fails_without_request(respx_mock):
    result = runner.invoke(
        app,
        [
            "learning",
            "runs",
            "logs",
            RUN,
            ATTEMPT,
            "--project",
            PROJECT,
            "--after",
            "1",
            "--before",
            "2",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code != 0 and "Traceback" not in result.output
    assert len(respx_mock.calls) == 0


def test_learning_human_output_is_readable(respx_mock):
    respx_mock.get(f"{API}/projects/{PROJECT}/training-runs").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    result = runner.invoke(
        app, ["learning", "runs", "list", "--project", PROJECT], env=ENV
    )
    assert result.exit_code == 0
    assert '"items"' in result.output and '"total"' in result.output
