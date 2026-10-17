"""Mask-format SDK and CLI contracts using intercepted HTTP only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.cli import mask_formats as mask_formats_cli
from ai_annotation.errors import APIStatusError, PermissionDeniedError
from ai_annotation.mask_format_models import (
    MaskFormatExportPreflightRequest,
    MaskFormatImportBatch,
    MaskFormatImportExecuteRequest,
    MaskFormatImportPreflightRequest,
    MaskFormatImportPreflightResult,
    MaskFormatUploadInitRequest,
    MaskFormatUploadInitResult,
)
from ai_annotation.mask_formats import MaskFormats

from .conftest import API

runner = CliRunner()
PROJECT_ID = str(uuid4())
IMPORT_ID = str(uuid4())
NOW = datetime.now(timezone.utc).isoformat()
RECEIPT = "mfi_" + "r" * 43
DIGEST = "a" * 64


def _plan(direction: str = "import") -> dict:
    return {
        "format_id": "coco-rle",
        "direction": direction,
        "adapter_version": "2.1.0",
        "manifest_version": "3",
        "media_type": "image",
        "loss_class": "lossy",
        "staged_object_key": "mask-formats/project/user/object/source.json",
        "staged_sha256": DIGEST,
        "mapping_digest": "b" * 64,
        "options_digest": "c" * 64,
        "items": [
            {
                "item_id": "item-1",
                "task_id": str(uuid4()),
                "media_path": "source/image.png",
                "source_index": 0,
                "loss_class": "lossy",
                "estimated_objects": 2,
                "estimated_files": 1,
                "estimated_bytes": 128,
                "losses": [
                    {
                        "code": "attributes_dropped",
                        "message": "attributes are omitted",
                        "detail": {},
                    }
                ],
                "skips": [],
                "warnings": [],
            }
        ],
        "unknown_labels": [],
        "size_conflicts": [],
        "overlap_conflicts": [],
        "id_mapping": {"source": "task"},
        "frame_mapping": {},
        "estimated_objects": 2,
        "estimated_files": 1,
        "estimated_bytes": 128,
        "losses": [],
        "skips": [],
        "warnings": [],
        "plan_digest": DIGEST,
    }


def _batch(status: str = "partial") -> dict:
    return {
        "id": IMPORT_ID,
        "project_id": PROJECT_ID,
        "async_job_id": str(uuid4()),
        "format_id": "coco-rle",
        "adapter_version": "2.1.0",
        "manifest_version": "3",
        "staged_sha256": DIGEST,
        "plan_digest": DIGEST,
        "status": status,
        "result": {
            "items": {
                "item-1": {"status": "completed", "annotation_ids": ["ann-1"]},
                "item-2": {"status": "failed", "error_code": "invalid_shape"},
            }
        },
        "receipt_expires_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
        "future_server_field": {"kept": True},
    }


def test_sdk_calls_all_seven_routes_and_preserves_import_receipts(client, respx_mock):
    api = MaskFormats(client._http)
    descriptor = {
        "format_id": "coco-rle",
        "label": "COCO RLE",
        "adapter_version": "2.1.0",
        "manifest_version": "3",
        "media_types": ["image"],
        "import_capability": {
            "supported": True,
            "verified": True,
            "enabled_for_ui": True,
        },
        "export_capability": {
            "supported": True,
            "verified": True,
            "enabled_for_ui": True,
        },
        "option_schema": {"overlap_policy": {"enum": ["error", "last_wins"]}},
        "future_descriptor_field": "preserved",
    }
    respx_mock.get(f"{API}/projects/{PROJECT_ID}/mask-formats").mock(
        return_value=httpx.Response(200, json=[descriptor])
    )
    formats = api.list(PROJECT_ID)
    assert formats[0].format_id == "coco-rle"
    assert formats[0].model_extra == {"future_descriptor_field": "preserved"}

    export_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/exports:preflight"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "plans": [_plan("export")],
                "loss_class": "lossy",
                "estimated_objects": 2,
                "estimated_files": 1,
                "estimated_bytes": 128,
                "losses": [],
                "warnings": [],
                "preflight_digest": DIGEST,
            },
        )
    )
    export = api.export_preflight(
        PROJECT_ID,
        MaskFormatExportPreflightRequest(
            targets=["coco-rle"], options={"overlap_policy": "last_wins"}
        ),
    )
    export_body = json.loads(export_route.calls.last.request.content)
    assert export_body["targets"] == ["coco-rle"]
    assert export_body["options"] == {"overlap_policy": "last_wins"}
    assert export.preflight_digest == DIGEST

    scope_export = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/exports:preflight"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "plans": [_plan("export")],
                "loss_class": "lossy",
                "estimated_objects": 2,
                "estimated_files": 1,
                "estimated_bytes": 128,
                "preflight_digest": DIGEST,
            },
        )
    )
    from ai_annotation.export_models import VideoExportScopeRequest

    api.export_preflight(
        PROJECT_ID,
        MaskFormatExportPreflightRequest(
            targets=["coco-rle"],
            scope=VideoExportScopeRequest.model_validate(
                {
                    "task_id": str(uuid4()),
                    "selection": {"kind": "frames", "from_frame": 0, "to_frame": 4},
                }
            ),
        ),
    )
    scope_payload = json.loads(scope_export.calls.last.request.content)
    assert scope_payload["scope"]["selection"]["kind"] == "frames"

    respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/imports:upload-init"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "object_key": "mask-formats/project/user/object/source.json",
                "upload_url": "https://storage.test/object?signature=private",
                "expires_in": 900,
            },
        )
    )
    upload_init = api.upload_init(
        PROJECT_ID,
        MaskFormatUploadInitRequest(
            file_name="source.json", content_type="application/json"
        ),
    )
    assert upload_init.upload_url.endswith("signature=private")
    assert "signature=private" not in repr(upload_init)
    assert upload_init.model_dump(mode="json")["upload_url"] == upload_init.upload_url

    preflight_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/imports:preflight"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "import_id": IMPORT_ID,
                "receipt": RECEIPT,
                "receipt_expires_at": NOW,
                "plan": _plan(),
            },
        )
    )
    preflight = api.import_preflight(
        PROJECT_ID,
        MaskFormatImportPreflightRequest(
            format_id="coco-rle",
            staged_object_key="mask-formats/project/user/object/source.json",
            staged_sha256=DIGEST,
            mapping={"foreground": "cat"},
            options={"overlap_policy": "error"},
        ),
    )
    preflight_payload = json.loads(preflight_route.calls.last.request.content)
    assert preflight_payload["mapping"] == {"foreground": "cat"}
    assert RECEIPT not in repr(preflight)
    assert preflight.model_dump(mode="json")["receipt"] == RECEIPT

    execute_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/imports"
    ).mock(return_value=httpx.Response(202, json=_batch("pending")))
    executed = api.execute_import(
        PROJECT_ID,
        MaskFormatImportExecuteRequest(
            receipt=RECEIPT, plan_digest=DIGEST, confirm_lossy=True
        ),
    )
    assert json.loads(execute_route.calls.last.request.content) == {
        "receipt": RECEIPT,
        "plan_digest": DIGEST,
        "confirm_lossy": True,
    }
    assert executed.status == "pending"
    assert executed.result["items"]["item-2"]["status"] == "failed"
    assert executed.model_extra == {"future_server_field": {"kept": True}}

    respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/mask-formats/imports/{IMPORT_ID}"
    ).mock(return_value=httpx.Response(200, json=_batch()))
    read = api.get_import(PROJECT_ID, IMPORT_ID)
    assert read.status == "partial"
    assert read.plan_digest == DIGEST
    assert read.adapter_version == "2.1.0" and read.manifest_version == "3"

    resume_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-formats/imports/{IMPORT_ID}/resume"
    ).mock(return_value=httpx.Response(202, json=_batch("pending")))
    resumed = api.resume_import(PROJECT_ID, IMPORT_ID)
    assert resumed.status == "pending"
    assert resume_route.calls.call_count == 1


def test_export_preflight_propagates_project_permission_denial(client, respx_mock):
    respx_mock.post(f"{API}/projects/{PROJECT_ID}/mask-formats/exports:preflight").mock(
        return_value=httpx.Response(
            403, json={"detail": {"reason": "export_annotations_required"}}
        )
    )

    with pytest.raises(PermissionDeniedError) as caught:
        MaskFormats(client._http).export_preflight(
            PROJECT_ID,
            MaskFormatExportPreflightRequest(targets=["coco-rle"]),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {"reason": "export_annotations_required"}


def test_presigned_put_uses_bare_client_and_rejects_redirects_without_leaking_url(
    client, respx_mock, tmp_path
):
    path = tmp_path / "source.json"
    path.write_bytes(b"{}")
    signed_url = "https://storage.test/object?signature=private-value"
    accepted = respx_mock.put(signed_url).mock(return_value=httpx.Response(204))

    MaskFormats(client._http).upload_bytes(signed_url, path, "application/json")

    request = accepted.calls.last.request
    assert request.headers["content-type"] == "application/json"
    assert request.headers.get("authorization") is None
    assert request.content == b"{}"

    rejected_url = "https://storage.test/object?signature=do-not-show"
    redirect = respx_mock.put(rejected_url).mock(
        return_value=httpx.Response(
            302,
            headers={"Location": "https://storage.test/other?signature=also-private"},
        )
    )
    with pytest.raises(APIStatusError) as caught:
        MaskFormats(client._http).upload_bytes(rejected_url, path, "application/json")
    assert caught.value.status_code == 302
    assert "signature=" not in str(caught.value)
    assert redirect.calls.call_count == 1


def test_request_models_forbid_unknown_input_and_hide_credentials_only_from_repr():
    with pytest.raises(ValidationError):
        MaskFormatImportPreflightRequest.model_validate(
            {
                "format_id": "coco-rle",
                "staged_object_key": "key",
                "staged_sha256": DIGEST,
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        MaskFormatUploadInitRequest(file_name="../source.json")
    with pytest.raises(ValidationError):
        MaskFormatImportExecuteRequest(
            receipt=RECEIPT,
            plan_digest=DIGEST,
            unexpected=True,
        )
    request = MaskFormatImportExecuteRequest(receipt=RECEIPT, plan_digest=DIGEST)
    assert RECEIPT not in repr(request)


class _ClientContext:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return None


def test_cli_upload_returns_digest_but_never_prints_the_signed_url(
    monkeypatch, tmp_path
):
    source = tmp_path / "source.json"
    source.write_bytes(b'{"annotations":[]}')
    signed_url = "https://storage.test/object?signature=secret-cli-value"

    class Resource:
        def __init__(self):
            self.uploaded = None

        def upload_init(self, project_id, body):
            assert project_id == PROJECT_ID
            assert body.file_name == "source.json"
            return MaskFormatUploadInitResult(
                object_key="mask-formats/project/user/object/source.json",
                upload_url=signed_url,
                expires_in=900,
            )

        def upload_bytes(self, url, file_path, content_type):
            self.uploaded = (url, file_path, content_type)

    class FakeClient:
        def __init__(self):
            self.mask_formats = Resource()

    client = FakeClient()
    monkeypatch.setattr(
        mask_formats_cli, "get_client", lambda *_args, **_kwargs: _ClientContext(client)
    )
    result = runner.invoke(
        mask_formats_cli.app,
        ["upload", PROJECT_ID, str(source), "--yes", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["staged_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert payload["object_key"] == "mask-formats/project/user/object/source.json"
    assert "upload_url" not in payload
    assert "secret-cli-value" not in result.output
    assert client.mask_formats.uploaded == (signed_url, source, "application/json")


def test_cli_receipt_is_explicit_json_and_execute_preserves_partial_batch(
    monkeypatch, tmp_path
):
    preflight_body = tmp_path / "preflight.json"
    preflight_body.write_text(
        json.dumps(
            {
                "format_id": "coco-rle",
                "staged_object_key": "mask-formats/project/user/object/source.json",
                "staged_sha256": DIGEST,
                "mapping": {},
                "options": {},
            }
        ),
        encoding="utf-8",
    )
    execute_body = tmp_path / "execute.json"
    execute_body.write_text(
        json.dumps({"receipt": RECEIPT, "plan_digest": DIGEST}), encoding="utf-8"
    )
    receipt_result = MaskFormatImportPreflightResult.model_validate(
        {
            "import_id": IMPORT_ID,
            "receipt": RECEIPT,
            "receipt_expires_at": NOW,
            "plan": _plan(),
        }
    )

    class Resource:
        def __init__(self):
            self.execute_calls = []

        def import_preflight(self, project_id, body):
            assert project_id == PROJECT_ID and body.format_id == "coco-rle"
            return receipt_result

        def execute_import(self, project_id, body):
            self.execute_calls.append((project_id, body))
            return MaskFormatImportBatch.model_validate(_batch("partial"))

    class FakeClient:
        def __init__(self):
            self.mask_formats = Resource()

    client = FakeClient()
    monkeypatch.setattr(
        mask_formats_cli, "get_client", lambda *_args, **_kwargs: _ClientContext(client)
    )

    as_json = runner.invoke(
        mask_formats_cli.app,
        [
            "import-preflight",
            PROJECT_ID,
            "--body",
            str(preflight_body),
            "--yes",
            "--json",
        ],
    )
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["receipt"] == RECEIPT

    as_text = runner.invoke(
        mask_formats_cli.app,
        ["import-preflight", PROJECT_ID, "--body", str(preflight_body), "--yes"],
    )
    assert as_text.exit_code == 0, as_text.output
    assert RECEIPT not in as_text.output

    executed = runner.invoke(
        mask_formats_cli.app,
        [
            "execute-import",
            PROJECT_ID,
            "--body",
            str(execute_body),
            "--yes",
            "--json",
        ],
    )
    assert executed.exit_code == 0, executed.output
    payload = json.loads(executed.stdout)
    assert payload["status"] == "partial"
    assert payload["result"]["items"]["item-2"]["status"] == "failed"
    assert len(client.mask_formats.execute_calls) == 1
    sent_project, sent_body = client.mask_formats.execute_calls[0]
    assert sent_project == PROJECT_ID
    assert sent_body.receipt == RECEIPT and sent_body.plan_digest == DIGEST
    assert sent_body.confirm_lossy is False


@pytest.mark.parametrize(
    ("command", "body_kind"),
    [
        ("upload", None),
        ("import-preflight", "import"),
        ("execute-import", "execute"),
        ("resume-import", None),
    ],
)
def test_cli_confirmation_refusal_sends_no_mutation(
    monkeypatch, tmp_path, command, body_kind
):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    import_body = tmp_path / "import.json"
    import_body.write_text(
        json.dumps(
            {
                "format_id": "coco-rle",
                "staged_object_key": "mask-formats/project/user/object/source.json",
                "staged_sha256": DIGEST,
                "mapping": {},
                "options": {},
            }
        ),
        encoding="utf-8",
    )
    execute_body = tmp_path / "execute.json"
    execute_body.write_text(
        json.dumps({"receipt": RECEIPT, "plan_digest": DIGEST}), encoding="utf-8"
    )

    def unexpected_client(*_args, **_kwargs):
        pytest.fail("confirmation refusal must happen before client construction")

    monkeypatch.setattr(mask_formats_cli, "get_client", unexpected_client)
    command_args = [command]
    if command == "upload":
        command_args.extend([PROJECT_ID, str(source)])
    elif command in {"import-preflight", "execute-import"}:
        body_path = import_body if body_kind == "import" else execute_body
        command_args.extend([PROJECT_ID, "--body", str(body_path)])
    else:
        command_args.extend([PROJECT_ID, IMPORT_ID])
    command_args.append("--json")
    result = runner.invoke(mask_formats_cli.app, command_args)

    assert result.exit_code == 2
    assert "--yes" in result.output
