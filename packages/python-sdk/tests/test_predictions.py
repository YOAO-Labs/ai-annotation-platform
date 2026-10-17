import json
from uuid import uuid4

import httpx
import pytest

from ai_annotation.management_operation_models import (
    PredictionPurgeRequest,
    PredictionPurgeResponse,
)
from ai_annotation.models import ImportResult

from .conftest import API

PROJECT_ID = str(uuid4())


def test_import_file_multipart(client, respx_mock, tmp_path):
    f = tmp_path / "preds.json"
    f.write_text('{"schema_version": "1.2", "tasks": []}')
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/predictions/import").mock(
        return_value=httpx.Response(
            200, json={"imported": 5, "skipped": 1, "errors": [], "dry_run": False}
        )
    )
    result = client.predictions.import_file(
        PROJECT_ID, f, format="aap_json", model_version="v3", dry_run=False
    )
    req = route.calls.last.request
    assert req.url.params["format"] == "aap_json"
    assert req.url.params["dry_run"] == "false"
    # 缺省不传 yolo_variant (走后端默认 det)
    assert "yolo_variant" not in req.url.params
    content = req.content
    # multipart 字段名必须是 file (后端 alias)
    assert b'name="file"' in content
    assert b'filename="preds.json"' in content
    assert b'name="model_version"' in content and b"v3" in content
    # SDK 缺省 overwrite_existing=False, 显式发送 (后端缺省是 True)
    assert b'name="overwrite_existing"' in content and b"false" in content
    assert isinstance(result, ImportResult)
    assert result.imported == 5


def test_import_file_yolo_params(client, respx_mock, tmp_path):
    f = tmp_path / "labels.zip"
    f.write_bytes(b"PK\x03\x04")
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/predictions/import").mock(
        return_value=httpx.Response(
            200, json={"imported": 0, "skipped": 0, "errors": [], "dry_run": True}
        )
    )
    result = client.predictions.import_file(
        PROJECT_ID,
        f,
        format="yolo",
        yolo_variant="obb",
        dry_run=True,
        overwrite_existing=True,
    )
    req = route.calls.last.request
    assert req.url.params["format"] == "yolo"
    assert req.url.params["yolo_variant"] == "obb"
    assert req.url.params["dry_run"] == "true"
    assert b'name="overwrite_existing"' in req.content and b"true" in req.content
    assert result.dry_run is True


def test_purge_predictions_sends_typed_request_once(client, respx_mock):
    task_id = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/predictions/purge").mock(
        return_value=httpx.Response(
            200,
            json={
                "source_scope": "external_import",
                "task_ids": [task_id],
                "dry_run": True,
                "counts": {
                    "ml_backend": 0,
                    "external_import": 3,
                    "unknown": 0,
                    "total": 3,
                },
            },
        )
    )
    result = client.predictions.purge(
        PROJECT_ID,
        PredictionPurgeRequest(
            source_scope="external_import", task_ids=[task_id], dry_run=True
        ),
    )

    assert isinstance(result, PredictionPurgeResponse)
    assert result.counts.external_import == 3
    assert route.calls.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "source_scope": "external_import",
        "task_ids": [task_id],
        "dry_run": True,
    }


def test_purge_invalid_source_fails_before_http(client, respx_mock):
    with pytest.raises(ValueError):
        PredictionPurgeRequest(source_scope="predictions")
    with pytest.raises(ValueError):
        PredictionPurgeRequest(dry_rnu=True)
    assert not respx_mock.calls
