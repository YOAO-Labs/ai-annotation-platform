"""Project template CLI stdin, JSON, and confirmation boundaries."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from typer.testing import CliRunner

from ai_annotation.cli import templates as templates_cli
from ai_annotation.project_template_models import ProjectTemplate

TEMPLATE_ID = str(uuid4())
TEMPLATE = ProjectTemplate.model_validate(
    {
        "id": TEMPLATE_ID,
        "display_id": "PT-1",
        "name": "Vehicles",
        "type_label": "Object detection",
        "type_key": "image-det",
        "created_by": str(uuid4()),
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
)

runner = CliRunner()


def test_list_help_exposes_scope_and_repeatable_type_filter():
    result = runner.invoke(templates_cli.app, ["list", "--help"])

    assert result.exit_code == 0, result.output
    assert "private" in result.output
    assert "organization" in result.output
    assert "public" in result.output
    assert "--type-key" in result.output


def test_create_reads_typed_request_from_stdin(monkeypatch):
    received = []
    api = SimpleNamespace(
        create=lambda body: received.append(body) or TEMPLATE,
    )
    monkeypatch.setattr(
        templates_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(project_templates=api)),
    )

    result = runner.invoke(
        templates_cli.app,
        ["create", "--body", "-", "--json"],
        input=json.dumps(
            {
                "source_project_id": str(uuid4()),
                "tool_bindings": {"bbox": {"enabled": True}},
            }
        ),
    )

    assert result.exit_code == 0, result.output
    assert len(received) == 1
    assert received[0].source_project_id is not None
    assert received[0].tool_bindings == {"bbox": {"enabled": True}}
    assert json.loads(result.stdout)["id"] == TEMPLATE_ID


def test_delete_json_refusal_does_not_open_client(monkeypatch):
    client_requests = []
    monkeypatch.setattr(
        templates_cli,
        "get_client",
        lambda *_args: client_requests.append("opened"),
    )

    result = runner.invoke(templates_cli.app, ["delete", TEMPLATE_ID, "--json"])

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert client_requests == []


def test_delete_with_yes_calls_api_once_and_prints_json(monkeypatch):
    deleted = []
    api = SimpleNamespace(delete=lambda template_id: deleted.append(template_id))
    monkeypatch.setattr(
        templates_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(project_templates=api)),
    )

    result = runner.invoke(
        templates_cli.app, ["delete", TEMPLATE_ID, "--json", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert deleted == [TEMPLATE_ID]
    assert json.loads(result.stdout) == {"deleted": True, "template_id": TEMPLATE_ID}
