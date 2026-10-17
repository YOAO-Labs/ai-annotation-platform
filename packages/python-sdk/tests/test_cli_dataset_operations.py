"""CLI contracts for dataset maintenance and guide asset transfers."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
from typer.main import get_command
from typer.testing import CliRunner

from ai_annotation.cli import datasets as datasets_cli
from ai_annotation.cli import guide_assets as guide_assets_cli
from ai_annotation.cli.datasets import app as datasets_app
from ai_annotation.cli.guide_assets import app as guide_assets_app
from ai_annotation.guide_assets import GuideAssets

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1", "TERM": "dumb"}
DATASET_ID = str(uuid4())
PROJECT_ID = str(uuid4())


class _ClientContext:
    def __init__(self, client):
        self.datasets = client.datasets
        self.guide_assets = GuideAssets(client._http)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _patch_cli_clients(monkeypatch, client):
    def factory(_json_mode=False):
        return _ClientContext(client)

    monkeypatch.setattr(datasets_cli, "get_client", factory)
    monkeypatch.setattr(guide_assets_cli, "get_client", factory)


def test_new_cli_commands_have_valid_help():
    for app in (datasets_app, guide_assets_app):
        command = get_command(app)
        assert command.commands is not None
        for name in command.commands:
            result = runner.invoke(app, [name, "--help"])
            assert result.exit_code == 0, (name, result.output, result.exception)


def test_dimension_failures_preserve_receipt_and_fail_cli(respx_mock):
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-dimensions").respond(
        200, json={"processed": 2, "failed": 1, "remaining_hint": True}
    )
    result = runner.invoke(
        datasets_app, ["backfill-dimensions", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "processed": 2,
        "failed": 1,
        "remaining_hint": True,
    }
    assert result.stderr == "" and route.call_count == 1


def test_import_from_connection_cli_requires_confirmation_and_returns_job(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    job_id = str(uuid4())
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/import-from-connection").mock(
        return_value=httpx.Response(202, json={"job_id": job_id})
    )
    body = json.dumps(
        {
            "connection_id": str(uuid4()),
            "source_path": "incoming/images",
            "recursive": False,
            "include_globs": ["*.jpg"],
        }
    )

    refused = runner.invoke(
        datasets_app,
        ["import-from-connection", DATASET_ID, "--body", "-", "--json"],
        input=body,
        env=ENV,
    )
    assert refused.exit_code != 0
    assert not route.called

    accepted = runner.invoke(
        datasets_app,
        [
            "import-from-connection",
            DATASET_ID,
            "--body",
            "-",
            "--json",
            "--yes",
        ],
        input=body,
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout) == {"job_id": job_id}
    assert (
        json.loads(route.calls.last.request.content)["source_path"] == "incoming/images"
    )


def test_guide_asset_put_cli_requires_confirmation_and_does_not_complete(
    client, monkeypatch, respx_mock, tmp_path
):
    _patch_cli_clients(monkeypatch, client)
    source = tmp_path / "hint.png"
    source.write_bytes(b"png-bytes")
    upload_url = "http://storage.local/put/hint.png?signature=temporary"
    route = respx_mock.put(upload_url).mock(return_value=httpx.Response(201))
    body = json.dumps(
        {
            "upload_url": upload_url,
            "file_path": str(source),
            "content_type": "image/png",
        }
    )

    refused = runner.invoke(
        guide_assets_app,
        ["upload-bytes", "--body", "-", "--json"],
        input=body,
        env=ENV,
    )
    assert refused.exit_code != 0
    assert not route.called

    accepted = runner.invoke(
        guide_assets_app,
        ["upload-bytes", "--body", "-", "--json", "--yes"],
        input=body,
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout) == {"uploaded": True}
    assert route.calls.last.request.content == b"png-bytes"
    assert route.calls.last.request.headers["content-type"] == "image/png"
    assert "authorization" not in route.calls.last.request.headers
    assert len(respx_mock.calls) == 1


def test_signed_guide_urls_are_only_printed_in_json(client, monkeypatch, respx_mock):
    _patch_cli_clients(monkeypatch, client)
    upload_url = "http://storage.local/put/hint.png?signature=temporary"
    init = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/guide-assets/upload-init"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"key": "projects/key", "upload_url": upload_url, "expires_in": 900},
        )
    )
    body = json.dumps({"filename": "hint.png", "content_type": "image/png", "size": 10})
    result = runner.invoke(
        guide_assets_app,
        ["upload-init", PROJECT_ID, "--body", "-"],
        input=body,
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert upload_url not in result.stdout
    assert init.called
