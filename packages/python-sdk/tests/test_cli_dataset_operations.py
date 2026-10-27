"""CLI contracts for dataset maintenance and guide asset transfers."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
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


def test_zip_and_scan_cli_pass_saved_ingest_keys(
    client, monkeypatch, respx_mock, tmp_path
):
    _patch_cli_clients(monkeypatch, client)
    key = str(uuid4())
    zip_path = tmp_path / "data.zip"
    zip_path.write_bytes(b"PK\x03\x04fakezip")
    upload = respx_mock.post(f"{API}/datasets/{DATASET_ID}/items/upload-zip").mock(
        return_value=httpx.Response(
            200,
            json={
                "added": 0,
                "deduped": 0,
                "skipped": 0,
                "errors": [],
                "total_in_zip": 0,
                "linked_tasks": 0,
                "scene_inference_notes": [],
            },
        )
    )
    scan = respx_mock.post(f"{API}/datasets/{DATASET_ID}/items/scan").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "new_items": 0, "linked_tasks": 0}
        )
    )

    uploaded = runner.invoke(
        datasets_app,
        [
            "upload",
            DATASET_ID,
            str(zip_path),
            "--zip",
            "--idempotency-key",
            key,
            "--json",
        ],
        env=ENV,
    )
    scanned = runner.invoke(
        datasets_app,
        ["scan-items", DATASET_ID, "--yes", "--idempotency-key", key, "--json"],
        env=ENV,
    )

    assert uploaded.exit_code == 0, uploaded.output
    assert scanned.exit_code == 0, scanned.output
    assert json.loads(uploaded.stdout) == {
        "added": 0,
        "deduped": 0,
        "skipped": 0,
        "errors": [],
        "total_in_zip": 0,
        "linked_tasks": 0,
        "scene_inference_notes": [],
    }
    assert json.loads(scanned.stdout) == {
        "status": "ok",
        "new_items": 0,
        "linked_tasks": 0,
    }
    assert upload.calls.last.request.headers["Idempotency-Key"] == key
    assert scan.calls.last.request.headers["Idempotency-Key"] == key


def test_invalid_ingest_cli_key_fails_before_any_request(
    client, monkeypatch, respx_mock, tmp_path
):
    _patch_cli_clients(monkeypatch, client)
    zip_path = tmp_path / "data.zip"
    zip_path.write_bytes(b"PK\x03\x04fakezip")
    upload = respx_mock.post(f"{API}/datasets/{DATASET_ID}/items/upload-zip").mock(
        return_value=httpx.Response(200, json={})
    )
    scan = respx_mock.post(f"{API}/datasets/{DATASET_ID}/items/scan").mock(
        return_value=httpx.Response(200, json={})
    )

    uploaded = runner.invoke(
        datasets_app,
        ["upload", DATASET_ID, str(zip_path), "--zip", "--idempotency-key", "bad"],
        env=ENV,
    )
    scanned = runner.invoke(
        datasets_app,
        ["scan-items", DATASET_ID, "--idempotency-key", "bad"],
        env=ENV,
    )

    assert uploaded.exit_code == 2
    assert scanned.exit_code == 2
    assert upload.call_count == 0
    assert scan.call_count == 0


def test_dimension_backfill_cli_reports_accepted_job(respx_mock):
    operation_id = str(uuid4())
    job_id = str(uuid4())
    idempotency_key = str(uuid4())
    receipt = {
        "dataset_id": DATASET_ID,
        "operation_id": operation_id,
        "job_id": job_id,
        "purpose": "dimensions",
        "status": "pending",
        "accepted_before": "2026-10-18T12:34:56Z",
    }
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-dimensions").respond(
        202, json=receipt
    )
    result = runner.invoke(
        datasets_app,
        [
            "backfill-dimensions",
            DATASET_ID,
            "--batch",
            "25",
            "--yes",
            "--idempotency-key",
            idempotency_key,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == receipt
    request = route.calls.last.request
    assert request.url.params["batch"] == "25"
    assert request.headers["Idempotency-Key"] == idempotency_key
    assert result.stderr == "" and route.call_count == 1


def test_failed_dimension_backfill_replay_prints_receipt_and_exits_one(respx_mock):
    receipt = {
        "dataset_id": DATASET_ID,
        "operation_id": str(uuid4()),
        "job_id": str(uuid4()),
        "purpose": "dimensions",
        "status": "failed",
        "accepted_before": "2026-10-18T12:34:56Z",
    }
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-dimensions").respond(
        202, json=receipt
    )

    result = runner.invoke(
        datasets_app,
        ["backfill-dimensions", DATASET_ID, "--yes", "--json"],
        env=ENV,
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == receipt
    assert route.call_count == 1


def test_media_backfill_cli_prints_job_receipt(respx_mock):
    operation_id = str(uuid4())
    job_id = str(uuid4())
    idempotency_key = str(uuid4())
    receipt = {
        "dataset_id": DATASET_ID,
        "operation_id": operation_id,
        "job_id": job_id,
        "purpose": "thumbnail",
        "status": "running",
        "accepted_before": "2026-10-18T12:34:56Z",
    }
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-media").respond(
        202, json=receipt
    )

    result = runner.invoke(
        datasets_app,
        [
            "backfill-media",
            DATASET_ID,
            "--yes",
            "--idempotency-key",
            idempotency_key,
            "--json",
        ],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == receipt
    assert route.calls.last.request.headers["Idempotency-Key"] == idempotency_key


def test_failed_media_backfill_replay_prints_receipt_and_exits_one(respx_mock):
    receipt = {
        "dataset_id": DATASET_ID,
        "operation_id": str(uuid4()),
        "job_id": str(uuid4()),
        "purpose": "thumbnail",
        "status": "canceled",
        "accepted_before": "2026-10-18T12:34:56Z",
    }
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-media").respond(
        202, json=receipt
    )

    result = runner.invoke(
        datasets_app,
        ["backfill-media", DATASET_ID, "--yes", "--json"],
        env=ENV,
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == receipt
    assert route.call_count == 1


def test_backfill_cli_rejects_invalid_key_before_request(respx_mock):
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/backfill-media").respond(
        202, json={}
    )

    result = runner.invoke(
        datasets_app,
        [
            "backfill-media",
            DATASET_ID,
            "--yes",
            "--idempotency-key",
            "invalid",
            "--json",
        ],
        env=ENV,
    )

    assert result.exit_code == 2
    assert "UUID" in result.output
    assert route.call_count == 0


def test_import_from_connection_cli_requires_confirmation_and_returns_job(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    job_id = str(uuid4())
    idempotency_key = str(uuid4())
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
            "--idempotency-key",
            idempotency_key,
        ],
        input=body,
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout) == {"job_id": job_id}
    assert (
        json.loads(route.calls.last.request.content)["source_path"] == "incoming/images"
    )
    assert route.calls.last.request.headers["Idempotency-Key"] == idempotency_key


def test_import_from_connection_cli_reuses_saved_key_after_lost_response(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    idempotency_key = str(uuid4())
    job_id = str(uuid4())
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/import-from-connection").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the job"),
            httpx.Response(202, json={"job_id": job_id}),
        ]
    )
    body = json.dumps({"connection_id": str(uuid4()), "source_path": "incoming"})
    args = [
        "import-from-connection",
        DATASET_ID,
        "--body",
        "-",
        "--json",
        "--yes",
        "--idempotency-key",
        idempotency_key,
    ]

    first = runner.invoke(datasets_app, args, input=body, env=ENV)
    second = runner.invoke(datasets_app, args, input=body, env=ENV)

    assert first.exit_code == 1
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout) == {"job_id": job_id}
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        idempotency_key,
        idempotency_key,
    ]


def test_import_from_connection_cli_rejects_invalid_key_before_request(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    route = respx_mock.post(f"{API}/datasets/{DATASET_ID}/import-from-connection").mock(
        return_value=httpx.Response(202, json={"job_id": str(uuid4())})
    )

    result = runner.invoke(
        datasets_app,
        [
            "import-from-connection",
            DATASET_ID,
            "--body",
            "-",
            "--json",
            "--yes",
            "--idempotency-key",
            "invalid",
        ],
        input=json.dumps({"connection_id": str(uuid4())}),
        env=ENV,
    )

    assert result.exit_code != 0
    assert not route.called


@pytest.mark.parametrize("operation", ["create", "update"])
def test_dataset_mutation_cli_reuses_saved_key_after_lost_response(
    operation, client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    key = str(uuid4())
    dataset = {
        "id": DATASET_ID,
        "display_id": "D-1",
        "name": "ds",
        "description": "updated",
        "data_type": "image",
        "file_count": 2,
        "total_size": 100,
        "created_at": "2026-06-11T00:00:00Z",
    }
    if operation == "create":
        route = respx_mock.post(f"{API}/datasets").mock(
            side_effect=[
                httpx.ReadTimeout(
                    "response lost after the server accepted the request"
                ),
                httpx.Response(201, json=dataset),
            ]
        )
        args = [
            "create",
            "--name",
            "ds",
            "--idempotency-key",
            key,
            "--json",
        ]
        expected_body = {"name": "ds", "data_type": "image"}
    else:
        route = respx_mock.put(f"{API}/datasets/{DATASET_ID}").mock(
            side_effect=[
                httpx.ReadTimeout(
                    "response lost after the server accepted the request"
                ),
                httpx.Response(200, json=dataset),
            ]
        )
        args = [
            "update",
            DATASET_ID,
            "--description",
            "updated",
            "--idempotency-key",
            key,
            "--json",
        ]
        expected_body = {"description": "updated"}

    first = runner.invoke(datasets_app, args, env=ENV)
    assert first.exit_code == 1, first.output
    second = runner.invoke(datasets_app, args, env=ENV)

    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["id"] == DATASET_ID
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        key,
        key,
    ]
    assert [json.loads(call.request.content) for call in route.calls] == [
        expected_body,
        expected_body,
    ]


@pytest.mark.parametrize("operation", ["create", "update"])
def test_dataset_mutation_cli_rejects_bad_key_before_request(operation, respx_mock):
    if operation == "create":
        route = respx_mock.post(f"{API}/datasets").mock(
            return_value=httpx.Response(201, json={})
        )
        args = ["create", "--name", "ds"]
    else:
        route = respx_mock.put(f"{API}/datasets/{DATASET_ID}").mock(
            return_value=httpx.Response(200, json={})
        )
        args = ["update", DATASET_ID, "--description", "updated"]
    args.extend(["--idempotency-key", "not-a-uuid", "--json"])

    result = runner.invoke(datasets_app, args, env=ENV)

    assert result.exit_code != 0
    assert "UUID" in result.output
    assert route.call_count == 0


def test_unlink_cli_reuses_saved_key_after_lost_response(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    idempotency_key = str(uuid4())
    route = respx_mock.delete(f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the request"),
            httpx.Response(
                200,
                json={
                    "deleted_tasks": 1,
                    "deleted_annotations": 0,
                    "deleted_batches": 0,
                    "deleted_batch_ids": [],
                    "soft": False,
                },
            ),
        ]
    )
    args = [
        "unlink",
        DATASET_ID,
        PROJECT_ID,
        "--json",
        "--yes",
        "--idempotency-key",
        idempotency_key,
    ]

    first = runner.invoke(datasets_app, args, env=ENV)
    assert first.exit_code == 1, first.output
    second = runner.invoke(datasets_app, args, env=ENV)
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["deleted_tasks"] == 1
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        idempotency_key,
        idempotency_key,
    ]


def test_unlink_cli_confirmation_passes_idempotency_key(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    idempotency_key = str(uuid4())
    respx_mock.get(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}/preview-unlink"
    ).respond(
        200,
        json={
            "will_delete_tasks": 3,
            "will_delete_annotations": 2,
            "will_delete_batches": 0,
        },
    )
    route = respx_mock.delete(f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}").respond(
        200,
        json={
            "deleted_tasks": 3,
            "deleted_annotations": 2,
            "deleted_batches": 0,
            "deleted_batch_ids": [],
            "soft": False,
        },
    )

    result = runner.invoke(
        datasets_app,
        [
            "unlink",
            DATASET_ID,
            PROJECT_ID,
            "--idempotency-key",
            idempotency_key,
        ],
        input="y\n",
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert route.called
    assert route.calls.last.request.headers["Idempotency-Key"] == idempotency_key


def test_unlink_cli_rejects_invalid_key_before_requests(
    client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    preview = respx_mock.get(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}/preview-unlink"
    ).respond(200, json={})
    unlink = respx_mock.delete(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}"
    ).respond(200, json={})

    result = runner.invoke(
        datasets_app,
        [
            "unlink",
            DATASET_ID,
            PROJECT_ID,
            "--json",
            "--yes",
            "--idempotency-key",
            "not-a-uuid",
        ],
        env=ENV,
    )

    assert result.exit_code != 0
    assert "UUID" in result.output
    assert not preview.called
    assert not unlink.called


@pytest.mark.parametrize("delete_item", [False, True])
def test_source_delete_cli_reuses_saved_key_after_lost_response(
    delete_item, client, monkeypatch, respx_mock
):
    _patch_cli_clients(monkeypatch, client)
    item_id = str(uuid4())
    path = (
        f"{API}/datasets/{DATASET_ID}/items/{item_id}"
        if delete_item
        else f"{API}/datasets/{DATASET_ID}"
    )
    command = "delete-item" if delete_item else "delete"
    key = str(uuid4())
    route = respx_mock.delete(path).mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the request"),
            httpx.Response(204),
        ]
    )
    args = [command, DATASET_ID]
    if delete_item:
        args.append(item_id)
    args.extend(["--json", "--yes", "--idempotency-key", key])

    first = runner.invoke(datasets_app, args, env=ENV)
    assert first.exit_code == 1, first.output
    second = runner.invoke(datasets_app, args, env=ENV)

    expected = {"deleted": True, "dataset_id": DATASET_ID}
    if delete_item:
        expected["item_id"] = item_id
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout) == expected
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        key,
        key,
    ]


@pytest.mark.parametrize("delete_item", [False, True])
def test_source_delete_cli_rejects_bad_key_before_any_request(delete_item, respx_mock):
    item_id = str(uuid4())
    path = (
        f"{API}/datasets/{DATASET_ID}/items/{item_id}"
        if delete_item
        else f"{API}/datasets/{DATASET_ID}"
    )
    command = "delete-item" if delete_item else "delete"
    route = respx_mock.delete(path).mock(return_value=httpx.Response(204))
    args = [command, DATASET_ID]
    if delete_item:
        args.append(item_id)
    args.extend(["--yes", "--json", "--idempotency-key", "not-a-uuid"])

    result = runner.invoke(datasets_app, args, env=ENV)

    assert result.exit_code != 0
    assert "UUID" in result.output
    assert route.call_count == 0


@pytest.mark.parametrize("delete_item", [False, True])
def test_source_delete_json_requires_confirmation_even_with_a_key(
    delete_item, respx_mock
):
    item_id = str(uuid4())
    path = (
        f"{API}/datasets/{DATASET_ID}/items/{item_id}"
        if delete_item
        else f"{API}/datasets/{DATASET_ID}"
    )
    command = "delete-item" if delete_item else "delete"
    route = respx_mock.delete(path).mock(return_value=httpx.Response(204))
    args = [command, DATASET_ID]
    if delete_item:
        args.append(item_id)
    args.extend(["--json", "--idempotency-key", str(uuid4())])

    result = runner.invoke(datasets_app, args, env=ENV)

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert route.call_count == 0


def test_guide_asset_upload_cli_requires_confirmation_and_posts_bytes(
    client, monkeypatch, respx_mock, tmp_path
):
    _patch_cli_clients(monkeypatch, client)
    source = tmp_path / "hint.png"
    source.write_bytes(b"png-bytes")
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/guide-assets/upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "projects/key",
                "original_name": "hint.png",
                "content_type": "image/png",
                "size": 9,
                "uploaded_at": "2026-10-17T00:00:00Z",
            },
        )
    )

    refused = runner.invoke(
        guide_assets_app,
        ["upload", PROJECT_ID, str(source), "--json"],
        env=ENV,
    )
    assert refused.exit_code != 0
    assert not route.called

    accepted = runner.invoke(
        guide_assets_app,
        ["upload", PROJECT_ID, str(source), "--json", "--yes"],
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["key"] == "projects/key"
    assert route.calls.last.request.content == b"png-bytes"
    assert route.calls.last.request.headers["content-type"] == "image/png"
    assert route.calls.last.request.url.params["file_name"] == "hint.png"
    assert len(respx_mock.calls) == 1


def test_guide_asset_content_cli_writes_authenticated_bytes(
    client, monkeypatch, respx_mock, tmp_path
):
    _patch_cli_clients(monkeypatch, client)
    respx_mock.get(f"{API}/projects/{PROJECT_ID}/guide-assets/content").mock(
        return_value=httpx.Response(
            200, content=b"png-bytes", headers={"content-type": "image/png"}
        )
    )
    output = tmp_path / "downloaded.png"

    result = runner.invoke(
        guide_assets_app,
        ["content", PROJECT_ID, "--key", "projects/key", "-o", str(output), "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"png-bytes"
    assert json.loads(result.stdout)["bytes"] == 9
