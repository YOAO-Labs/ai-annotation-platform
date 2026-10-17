"""Template/source CLI creation preserves server-owned defaults."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.main import app

from .conftest import API, BASE
from .test_projects import PROJECT

PARENT = "11111111-1111-4111-8111-111111111111"
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


@pytest.mark.parametrize(
    "option,field",
    [("--template", "template_id"), ("--source-project", "source_project_id")],
)
def test_cli_inherits_type_without_an_implicit_image(respx_mock, option, field):
    route = respx_mock.post(f"{API}/projects").mock(
        return_value=httpx.Response(201, json=PROJECT)
    )
    result = CliRunner().invoke(
        app, ["projects", "create", "--name", "copy", option, PARENT, "--json"], env=ENV
    )
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {
        "name": "copy",
        field: PARENT,
    }


@pytest.mark.parametrize(
    "options",
    [
        ["--template", PARENT, "--source-project", PARENT],
        ["--template", PARENT, "--copy-annotation-guide"],
        ["--type", "video", "--copy-annotation-guide"],
        [],
    ],
)
def test_cli_invalid_inheritance_fails_before_http(respx_mock, options):
    result = CliRunner().invoke(
        app,
        [
            "--error-format",
            "json",
            "projects",
            "create",
            "--name",
            "copy",
            *options,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["kind"] == "usage_error"
    assert not respx_mock.calls


def test_cli_explicit_type_and_label_override_template(respx_mock):
    route = respx_mock.post(f"{API}/projects").respond(201, json=PROJECT)
    result = CliRunner().invoke(
        app,
        [
            "projects",
            "create",
            "--name",
            "copy",
            "--template",
            PARENT,
            "--type",
            "lidar",
            "--type-label",
            "Point cloud",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    body = json.loads(route.calls.last.request.content)
    assert body["data_type"] == "lidar" and body["type_label"] == "Point cloud"
    assert "type_key" not in body
