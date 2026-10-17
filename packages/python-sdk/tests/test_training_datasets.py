from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import APIStatusError
from ai_annotation.training import TaskDatasets, TrainingDatasets
from ai_annotation.training_models import (
    TrainingDatasetCreate,
    TrainingDatasetSelection,
)

from .conftest import API


def _id():
    return str(uuid4())


def _version(project_id, *, full=True):
    value = {
        "id": _id(),
        "project_id": project_id,
        "name": "frozen v1",
        "status": "ready",
        "progress_pct": 100,
        "snapshot_digest": "a" * 64,
        "created_at": "2026-10-16T00:00:00Z",
        "task_dataset_id": None,
        "version_ordinal": None,
        "manifest": {} if full else None,
    }
    return value


def _install_http(client, monkeypatch, responses):
    calls = []
    downloads = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return Response(responses[(method, path)])

    def stream_download(url, dest, **kwargs):
        downloads.append((url, Path(dest), kwargs))
        return Path(dest)

    monkeypatch.setattr(client._http, "request", request)
    monkeypatch.setattr(client._http, "stream_download", stream_download)
    return calls, downloads


def test_task_dataset_streams_and_version_lifecycle(client, monkeypatch, tmp_path):
    project_id = _id()
    version = _version(project_id)
    routes = {
        ("GET", f"/projects/{project_id}/task-datasets"): {
            "items": [],
            "pending_membership_count": 2,
            "unrecognized_version_count": 1,
        },
        ("GET", f"/projects/{project_id}/task-datasets/unrecognized-versions"): {
            "items": [],
            "total": 0,
        },
        ("POST", f"/projects/{project_id}/training-datasets:preflight"): {
            "ready": True,
            "digest": "b" * 64,
            "classes": ["car"],
            "summary": {"train": 2},
            "issues": [],
            "excluded": [],
        },
        ("POST", f"/projects/{project_id}/training-datasets"): version,
        ("GET", f"/projects/{project_id}/training-datasets"): {
            "items": [version],
            "total": 1,
        },
        ("GET", f"/projects/{project_id}/training-datasets/{version['id']}"): version,
        (
            "POST",
            f"/projects/{project_id}/training-datasets/{version['id']}/retry",
        ): version,
        ("GET", f"/projects/{project_id}/training-datasets/{version['id']}/download"): {
            "url": "https://storage.test/frozen.zip",
            "sha256": "c" * 64,
        },
        ("DELETE", f"/projects/{project_id}/training-datasets/{version['id']}"): {},
    }
    calls, downloads = _install_http(client, monkeypatch, routes)
    streams = TaskDatasets(client._http)
    datasets = TrainingDatasets(client._http)
    task_page = streams.list(project_id)
    assert task_page.pending_membership_count == 2
    assert streams.unrecognized_versions(project_id, limit=7, offset=3).total == 0

    selection = TrainingDatasetSelection(task="segment", seed=11)
    assert datasets.preflight(project_id, selection).digest == "b" * 64
    create = TrainingDatasetCreate(
        name="v1",
        request_id=uuid4(),
        preflight_digest="b" * 64,
        task="segment",
    )
    assert datasets.create(project_id, create).status == "ready"
    assert (
        datasets.list(
            project_id, limit=9, offset=4, status="ready", task="segment"
        ).total
        == 1
    )
    assert (
        datasets.get(project_id, version["id"], include_manifest=False).manifest == {}
    )
    assert str(datasets.retry(project_id, version["id"]).id) == version["id"]
    destination = tmp_path / "version.zip"
    assert datasets.download(project_id, version["id"], destination) == destination
    assert datasets.delete(project_id, version["id"]) is None

    assert [path for _method, path, _kwargs in calls] == [
        path for _method, path in routes
    ]
    assert calls[1][2]["params"] == {"limit": 7, "offset": 3}
    assert calls[4][2]["params"] == {
        "limit": 9,
        "offset": 4,
        "status": "ready",
        "task": "segment",
    }
    assert calls[5][2]["params"] == {"include_manifest": False}
    assert downloads == [
        ("https://storage.test/frozen.zip", destination, {"expected_sha256": "c" * 64})
    ]


def test_training_dataset_insights_keep_frozen_sample_identity(
    client, monkeypatch, tmp_path
):
    project_id, version_id, task_id, baseline_id = _id(), _id(), _id(), _id()
    sample = {
        "version_id": version_id,
        "task_id": task_id,
        "included": True,
        "exclusion_reason": None,
        "issue": None,
        "sha256": "d" * 64,
        "label_sha256": "e" * 64,
        "split": "train",
        "width": 640,
        "height": 480,
        "size_bytes": 12,
        "instances": 1,
        "is_negative": False,
        "explicit_negative": False,
        "class_ids": [0],
        "media_state": "verified",
        "media_checked_at": None,
    }
    routes = {
        ("GET", f"/projects/{project_id}/training-datasets/{version_id}/insights"): {
            "version_id": version_id,
            "schema_version": 1,
            "input_digest": "a" * 64,
            "snapshot_digest": "b" * 64,
            "manifest_digest": "c" * 64,
            "status": "complete",
        },
        (
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/classes",
        ): {
            "items": [],
            "total": 0,
            "limit": 13,
            "offset": 2,
            "order_by": "instances",
            "split": "val",
        },
        (
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/compare",
        ): {
            "baseline_id": baseline_id,
            "version_id": version_id,
            "status": "complete",
            "counts": {"added": 1},
            "count_unit": "unique_contents",
            "baseline_members": 1,
            "target_members": 2,
        },
        (
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/samples",
        ): {
            "source_version_id": baseline_id,
            "items": [sample],
            "total": 1,
            "limit": 15,
            "offset": 5,
        },
        (
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}",
        ): {
            "sample": sample,
            "labels": [],
            "total": 1,
            "limit": 30,
            "offset": 8,
        },
    }
    calls, downloads = _install_http(client, monkeypatch, routes)
    datasets = TrainingDatasets(client._http)
    assert datasets.insights(project_id, version_id).status == "complete"
    datasets.classes(project_id, version_id, 13, 2, "car", "instances", "val")
    assert (
        str(datasets.compare(project_id, version_id, baseline_id).baseline_id)
        == baseline_id
    )
    page = datasets.samples(
        project_id,
        version_id,
        limit=15,
        offset=5,
        split="train",
        class_index=0,
        content_sha256="d" * 64,
        baseline_id=baseline_id,
        change="removed",
    )
    assert str(page.source_version_id) == baseline_id
    assert (
        datasets.sample(project_id, version_id, task_id, limit=30, offset=8).total == 1
    )
    destination = tmp_path / "sample.jpg"
    assert (
        datasets.download_media(
            project_id, version_id, task_id, destination, preview=True
        )
        == destination
    )

    params = [kwargs.get("params") for _method, _path, kwargs in calls]
    assert params[1] == {
        "limit": 13,
        "offset": 2,
        "search": "car",
        "order_by": "instances",
        "split": "val",
    }
    assert params[3]["baseline_id"] == baseline_id
    assert params[3]["change"] == "removed"
    assert downloads == [
        (
            f"/api/v1/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}/media",
            destination,
            {"params": {"preview": True}},
        )
    ]


@pytest.mark.parametrize("status", [401, 403, 409, 422])
def test_training_dataset_errors_keep_transport_status(client, respx_mock, status):
    project_id = _id()
    route = respx_mock.get(f"{API}/projects/{project_id}/task-datasets").mock(
        return_value=httpx.Response(status, json={"detail": {"code": "blocked"}})
    )
    with pytest.raises(APIStatusError) as error:
        TaskDatasets(client._http).list(project_id)
    assert error.value.status_code == status
    assert route.called
