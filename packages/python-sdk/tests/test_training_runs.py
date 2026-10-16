from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from ai_annotation.errors import AAPError, ValidationError
from ai_annotation.training import (
    Training,
    TrainingPreviews,
    TrainingRecipes,
    TrainingRuns,
)
from ai_annotation.training_models import (
    TrainingConfiguration,
    TrainingConfigurationResolve,
    TrainingPreviewCreate,
    TrainingRecipeWrite,
    TrainingRunCreate,
)


def _id():
    return str(uuid4())


def _run(project_id, dataset_id):
    return {
        "id": _id(),
        "project_id": project_id,
        "creator_id": None,
        "dataset_id": dataset_id,
        "async_job_id": None,
        "name": "run 1",
        "status": "succeeded",
        "task": "detect",
        "media_type": "image",
        "config": {"framework": "ultralytics"},
        "config_digest": "a" * 64,
        "current_attempt_id": None,
        "metrics": {"map50": 0.7},
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
    }


def _catalog():
    return {
        "enabled": True,
        "online": True,
        "busy": False,
        "resource_id": "gpu-0",
        "executor_id": None,
        "last_seen_at": None,
        "items": [],
    }


def _recipe(project_id, recipe_id):
    return {
        "id": recipe_id,
        "project_id": project_id,
        "head_revision": 1,
        "archived": False,
        "revision": {
            "id": _id(),
            "recipe_id": recipe_id,
            "project_id": project_id,
            "creator_id": None,
            "ordinal": 1,
            "name": "recipe",
            "description": "",
            "snapshot": {},
            "snapshot_digest": "b" * 64,
            "origin": None,
            "initialization_model_id": None,
            "created_at": "2026-10-16T00:00:00Z",
        },
        "compatible": True,
        "incompatibility": None,
        "source_available": True,
    }


def _preview(project_id, body):
    return {
        "id": _id(),
        "project_id": project_id,
        "async_job_id": None,
        "request": body.model_dump(mode="json"),
        "input_digest": "c" * 64,
        "config_digest": "d" * 64,
        "configuration": {},
        "status": "queued",
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "started_at": None,
        "completed_at": None,
        "expires_at": "2026-10-17T00:00:00Z",
        "report": None,
    }


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


def test_training_discovery_and_run_operations(client, monkeypatch, tmp_path):
    project_id, dataset_id, model_id, run_id, attempt_id = (
        _id(),
        _id(),
        _id(),
        _id(),
        _id(),
    )
    run = _run(project_id, dataset_id)
    eligibility = {
        "qualified": False,
        "reason": "not_qualified",
        "qualification": None,
    }
    observation = {
        "attempt_id": attempt_id,
        "stream_id": None,
        "earliest_cursor": None,
        "latest_cursor": 0,
        "next_cursor": 0,
        "has_more": False,
        "gaps": [],
        "summary": None,
    }
    routes = {
        ("GET", "/training/catalog"): _catalog(),
        ("GET", "/training/projects"): {"total": 0, "items": []},
        ("GET", "/training/runs"): {
            "total": 0,
            "counts": {
                "active": 0,
                "queued": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
            },
            "items": [],
        },
        ("GET", f"/projects/{project_id}/training-runs/catalog"): _catalog(),
        ("GET", f"/projects/{project_id}/training-runs/parameter-schema"): {
            "schema_id": "ultralytics:yolo11n",
            "schema_digest": "e" * 64,
            "framework": "ultralytics",
            "framework_version": "1.0",
            "adapter_revision": 2,
            "task": "detect",
            "model_family": "yolo11",
            "source": "catalog",
            "source_sha256": "f" * 64,
            "groups": {},
            "fields": [],
        },
        (
            "GET",
            f"/projects/{project_id}/training-runs/initialization-sources/{model_id}",
        ): {
            "model_version_id": model_id,
            "eligible": False,
            "reasons": ["incompatible"],
            "pin": None,
            "storage_verified": False,
        },
        ("POST", f"/projects/{project_id}/training-runs:preflight"): {
            "config": {"framework": "ultralytics"},
            "config_digest": "a" * 64,
            "origin": None,
        },
        ("POST", f"/projects/{project_id}/training-runs"): run,
        ("GET", f"/projects/{project_id}/training-runs"): {"items": [run], "total": 1},
        ("GET", f"/projects/{project_id}/training-runs/{run_id}"): {
            "run": run,
            "attempts": [],
            "registration_eligibility": eligibility,
        },
        ("GET", f"/projects/{project_id}/training-runs/{run_id}/events"): {
            "attempt_id": attempt_id,
            "items": [{"sequence": 1, "kind": "training", "epoch": 1}],
        },
        (
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/evaluation",
        ): {
            "task": "detect",
            "identity_status": "verified",
            "reasons": [],
            "descriptor": None,
            "compatibility_digest": None,
            "summary": {"map50": 0.7},
            "per_class_available": False,
            "per_class_total": 0,
            "per_class": [],
            "provenance": {},
        },
        (
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/telemetry",
        ): {
            **observation,
            "items": [],
            "metric_definitions": {},
        },
        (
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/logs",
        ): {
            **observation,
            "items": [],
        },
        (
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/artifacts/weights/download",
        ): {
            "url": "https://storage.test/weights.pt",
            "sha256": "1" * 64,
            "size_bytes": 42,
        },
        ("GET", f"/projects/{project_id}/training-runs/{run_id}/recovery"): {
            "attempt_id": attempt_id,
            "eligible": False,
            "reasons": ["completed"],
            "executor_online": False,
            "runtime_compatible": False,
            "checkpoint_epoch": None,
        },
        ("POST", f"/projects/{project_id}/training-runs/{run_id}/cancel"): run,
        ("POST", f"/projects/{project_id}/training-runs/{run_id}/resume"): run,
        ("DELETE", f"/projects/{project_id}/training-runs/{run_id}"): {},
    }
    calls, downloads = _install_http(client, monkeypatch, routes)
    training, runs = Training(client._http), TrainingRuns(client._http)
    assert training.catalog().enabled
    assert training.projects(search="vision", limit=4, offset=2).total == 0
    assert (
        training.runs(
            state="failed",
            project_id=project_id,
            framework="libreyolo",
            limit=5,
            offset=1,
        ).total
        == 0
    )
    assert runs.catalog(project_id).online
    assert runs.parameter_schema(project_id, "ultralytics", "yolo11n").fields == []
    assert not runs.initialization_source(
        project_id, model_id, "ultralytics", "yolo11n", dataset_id
    ).eligible
    body = TrainingRunCreate(
        name="run 1",
        dataset_id=dataset_id,
        request_id=uuid4(),
        preflight_digest="a" * 64,
    )
    assert runs.preflight(project_id, body).config_digest == "a" * 64
    assert runs.create(project_id, body).status == "succeeded"
    assert runs.list(project_id, limit=10, offset=3).total == 1
    detail = runs.get(project_id, run_id)
    assert detail.run.current_attempt_id is None
    assert detail.registration_eligibility.reason == "not_qualified"
    assert (
        runs.events(project_id, run_id, attempt_id, after=1, latest=True)
        .items[0]
        .sequence
        == 1
    )
    assert (
        runs.evaluation(project_id, run_id, attempt_id, offset=2, limit=7).summary[
            "map50"
        ]
        == 0.7
    )
    assert (
        runs.telemetry(project_id, run_id, attempt_id, after=4, limit=5).next_cursor
        == 0
    )
    assert runs.logs(project_id, run_id, attempt_id, before=9, limit=8).items == []
    with pytest.raises(ValidationError):
        runs.logs(project_id, run_id, attempt_id, after=1, before=2)
    assert len(calls) == 14
    dest = tmp_path / "weights.pt"
    assert (
        runs.download_artifact(project_id, run_id, attempt_id, "weights", dest) == dest
    )
    assert runs.recovery(project_id, run_id).reasons == ["completed"]
    assert runs.cancel(project_id, run_id).status == "succeeded"
    assert runs.resume(project_id, run_id).status == "succeeded"
    assert runs.delete(project_id, run_id) is None

    paths = [path for _method, path, _kwargs in calls]
    assert paths == [path for _method, path in routes]
    assert calls[2][2]["params"] == {
        "state": "failed",
        "project_id": project_id,
        "framework": "libreyolo",
        "limit": 5,
        "offset": 1,
    }
    assert calls[5][2]["params"]["dataset_id"] == dataset_id
    assert calls[13][2]["params"] == {"before": 9, "limit": 8}
    assert downloads == [
        (
            "https://storage.test/weights.pt",
            dest,
            {"expected_sha256": "1" * 64, "expected_size": 42},
        )
    ]


def test_recipe_reuse_resolution_and_revision_operations(client, monkeypatch):
    project_id, recipe_id, run_id = _id(), _id(), _id()
    recipe = _recipe(project_id, recipe_id)
    routes = {
        ("GET", f"/projects/{project_id}/training-recipes"): {
            "items": [recipe],
            "total": 1,
        },
        ("POST", f"/projects/{project_id}/training-recipes"): recipe,
        ("GET", f"/projects/{project_id}/training-recipes/{recipe_id}"): recipe,
        ("GET", f"/projects/{project_id}/training-recipes/{recipe_id}/revisions"): {
            "items": [recipe],
            "total": 1,
        },
        (
            "POST",
            f"/projects/{project_id}/training-recipes/{recipe_id}/revisions",
        ): recipe,
        ("POST", f"/projects/{project_id}/training-recipes:resolve"): {
            "framework": "ultralytics",
            "framework_version": "1.0",
            "image_digest": "f" * 64,
            "base_model": {"id": "yolo11n"},
            "configuration": {"epochs": 10},
        },
        ("GET", f"/projects/{project_id}/training-recipes/clone/{run_id}"): {
            "snapshot": {},
            "snapshot_digest": "b" * 64,
            "origin": {"kind": "clone", "run_id": run_id, "digest": "b" * 64},
            "label": "run",
            "compatible": True,
            "incompatibility": None,
            "source_dataset_id": None,
        },
        ("POST", f"/projects/{project_id}/training-recipes/{recipe_id}/archive"): {
            **recipe,
            "archived": True,
        },
    }
    calls, _downloads = _install_http(client, monkeypatch, routes)
    recipes = TrainingRecipes(client._http)
    assert (
        recipes.list(project_id, limit=11, offset=2, include_archived=True).total == 1
    )
    write = TrainingRecipeWrite(
        name="recipe",
        request_id=uuid4(),
        configuration=TrainingConfigurationResolve(
            framework="ultralytics",
            base_model="yolo11n",
            configuration=TrainingConfiguration(schema_id="s", schema_digest="a" * 64),
        ),
    )
    assert recipes.create(project_id, write).head_revision == 1
    assert str(recipes.get(project_id, recipe_id).id) == recipe_id
    assert recipes.revisions(project_id, recipe_id, limit=8, offset=4).total == 1
    assert recipes.create_revision(project_id, recipe_id, write).head_revision == 1
    resolved = recipes.resolve(project_id, write.configuration)
    assert resolved.configuration == {"epochs": 10}
    assert recipes.clone_candidate(project_id, run_id).origin.kind == "clone"
    assert recipes.archive(project_id, recipe_id, expected_head_revision=3).archived
    assert [path for _method, path, _kwargs in calls] == [
        path for _method, path in routes
    ]
    assert calls[0][2]["params"]["include_archived"] is True
    assert calls[-1][2]["params"] == {"expected_head_revision": 3}


def test_augmentation_preview_lifecycle_and_image_download(
    client, monkeypatch, tmp_path
):
    project_id, dataset_id = _id(), _id()
    body = TrainingPreviewCreate(
        dataset_id=dataset_id,
        framework="ultralytics",
        base_model="yolo11n",
        configuration=TrainingConfiguration(schema_id="s", schema_digest="a" * 64),
        request_id=uuid4(),
    )
    preview = _preview(project_id, body)
    preview_id = preview["id"]
    routes = {
        ("GET", f"/projects/{project_id}/training-previews/capabilities"): {
            "qualified": True,
            "online": True,
            "busy": False,
            "resource_id": "gpu-0",
            "reason": None,
            "policy_digest": "b" * 64,
            "policy": {},
        },
        ("POST", f"/projects/{project_id}/training-previews"): preview,
        ("GET", f"/projects/{project_id}/training-previews/{preview_id}"): preview,
        ("POST", f"/projects/{project_id}/training-previews/{preview_id}/cancel"): {
            **preview,
            "status": "cancelling",
        },
        ("DELETE", f"/projects/{project_id}/training-previews/{preview_id}"): {},
    }
    calls, downloads = _install_http(client, monkeypatch, routes)
    previews = TrainingPreviews(client._http)
    assert previews.capabilities(project_id).qualified
    assert previews.create(project_id, body).status == "queued"
    assert str(previews.get(project_id, preview_id).id) == preview_id
    dest = tmp_path / "preview.png"
    assert previews.download_image(project_id, preview_id, 1, dest) == dest
    assert previews.cancel(project_id, preview_id).status == "cancelling"
    assert previews.delete(project_id, preview_id) is None
    assert [path for _method, path, _kwargs in calls] == [
        path for _method, path in routes
    ]
    assert downloads == [
        (
            f"/api/v1/projects/{project_id}/training-previews/{preview_id}/images/1",
            dest,
            {},
        )
    ]


def test_run_create_requires_preflight_digest_before_http(client, monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("create must reject before making a request")

    monkeypatch.setattr(client._http, "request", request)
    body = TrainingRunCreate(name="run", dataset_id=uuid4(), request_id=uuid4())
    with pytest.raises(AAPError, match="preflight_digest"):
        TrainingRuns(client._http).create(_id(), body)
    assert calls == []
