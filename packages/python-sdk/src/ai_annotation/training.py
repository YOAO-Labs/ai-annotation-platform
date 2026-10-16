"""Synchronous resources for frozen datasets, training runs and previews."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.errors import AAPError, ValidationError
from ai_annotation.training_models import (
    TrainingArtifactDownload,
    TrainingConfigurationResolve,
    TrainingConfigurationSnapshot,
    TrainingDatasetClassPage,
    TrainingDatasetCreate,
    TrainingDatasetDownload,
    TrainingDatasetInsightComparison,
    TrainingDatasetInsightOverview,
    TrainingDatasetOut,
    TrainingDatasetPage,
    TrainingDatasetPreflight,
    TrainingDatasetSampleDetail,
    TrainingDatasetSamplePage,
    TrainingDatasetSelection,
    TrainingEvaluation,
    TrainingHubCatalog,
    TrainingInitializationEligibility,
    TrainingLogPage,
    TrainingParameterSchema,
    TrainingPreviewCapabilities,
    TrainingPreviewCreate,
    TrainingPreviewOut,
    TrainingProjectPage,
    TrainingQueuePage,
    TrainingRecipeOut,
    TrainingRecipePage,
    TrainingRecipeWrite,
    TrainingRecovery,
    TrainingReuseCandidate,
    TrainingRunCreate,
    TrainingRunDetail,
    TrainingRunEvents,
    TrainingRunOut,
    TrainingRunPage,
    TrainingRunPreflight,
    TrainingTaskDatasetPage,
    TrainingTelemetryPage,
    UnrecognizedTrainingDatasetVersionPage,
)

IdLike = str | UUID


def _request_body(body: BaseModel) -> dict:
    return body.model_dump(mode="json", by_alias=True, exclude_unset=True)


class Training:
    """Global training catalog, manager-visible projects and run queue."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def catalog(self) -> TrainingHubCatalog:
        return TrainingHubCatalog.model_validate(
            self._http.request("GET", "/training/catalog").json()
        )

    def projects(
        self, search: str = "", limit: int = 20, offset: int = 0
    ) -> TrainingProjectPage:
        resp = self._http.request(
            "GET",
            "/training/projects",
            params={"search": search, "limit": limit, "offset": offset},
        )
        return TrainingProjectPage.model_validate(resp.json())

    def runs(
        self,
        state: Literal["all", "active", "finished", "failed"] = "active",
        project_id: IdLike | None = None,
        framework: Literal["ultralytics", "libreyolo"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> TrainingQueuePage:
        resp = self._http.request(
            "GET",
            "/training/runs",
            params=_drop_none(
                {
                    "state": state,
                    "project_id": project_id,
                    "framework": framework,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return TrainingQueuePage.model_validate(resp.json())


class TaskDatasets:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, project_id: IdLike) -> TrainingTaskDatasetPage:
        resp = self._http.request("GET", f"/projects/{project_id}/task-datasets")
        return TrainingTaskDatasetPage.model_validate(resp.json())

    def unrecognized_versions(
        self, project_id: IdLike, limit: int = 20, offset: int = 0
    ) -> UnrecognizedTrainingDatasetVersionPage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/task-datasets/unrecognized-versions",
            params={"limit": limit, "offset": offset},
        )
        return UnrecognizedTrainingDatasetVersionPage.model_validate(resp.json())


class TrainingDatasets:
    def __init__(self, http: HttpTransport):
        self._http = http

    def preflight(
        self, project_id: IdLike, body: TrainingDatasetSelection
    ) -> TrainingDatasetPreflight:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-datasets:preflight",
            json=_request_body(body),
        )
        return TrainingDatasetPreflight.model_validate(resp.json())

    def create(
        self, project_id: IdLike, body: TrainingDatasetCreate
    ) -> TrainingDatasetOut:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-datasets",
            json=_request_body(body),
        )
        return TrainingDatasetOut.model_validate(resp.json())

    def list(
        self,
        project_id: IdLike,
        limit: int = 20,
        offset: int = 0,
        status: Literal["pending", "running", "ready", "failed", "cancelled"]
        | None = None,
        task: Literal["detect", "segment"] | None = None,
        task_dataset_id: IdLike | None = None,
    ) -> TrainingDatasetPage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets",
            params=_drop_none(
                {
                    "limit": limit,
                    "offset": offset,
                    "status": status,
                    "task": task,
                    "task_dataset_id": task_dataset_id,
                }
            ),
        )
        return TrainingDatasetPage.model_validate(resp.json())

    def get(
        self, project_id: IdLike, version_id: IdLike, include_manifest: bool = True
    ) -> TrainingDatasetOut:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}",
            params={"include_manifest": include_manifest},
        )
        return TrainingDatasetOut.model_validate(resp.json())

    def retry(self, project_id: IdLike, version_id: IdLike) -> TrainingDatasetOut:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/training-datasets/{version_id}/retry"
        )
        return TrainingDatasetOut.model_validate(resp.json())

    def download(
        self, project_id: IdLike, version_id: IdLike, dest: str | Path
    ) -> Path:
        descriptor = TrainingDatasetDownload.model_validate(
            self._http.request(
                "GET", f"/projects/{project_id}/training-datasets/{version_id}/download"
            ).json()
        )
        return self._http.stream_download(
            descriptor.url, Path(dest), expected_sha256=descriptor.sha256
        )

    def delete(self, project_id: IdLike, version_id: IdLike) -> None:
        self._http.request(
            "DELETE", f"/projects/{project_id}/training-datasets/{version_id}"
        )

    def insights(
        self, project_id: IdLike, version_id: IdLike
    ) -> TrainingDatasetInsightOverview:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights",
        )
        return TrainingDatasetInsightOverview.model_validate(resp.json())

    def classes(
        self,
        project_id: IdLike,
        version_id: IdLike,
        limit: int = 25,
        offset: int = 0,
        search: str = "",
        order_by: Literal["index", "instances", "images"] = "index",
        split: Literal["train", "val"] | None = None,
    ) -> TrainingDatasetClassPage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/classes",
            params=_drop_none(
                {
                    "limit": limit,
                    "offset": offset,
                    "search": search,
                    "order_by": order_by,
                    "split": split,
                }
            ),
        )
        return TrainingDatasetClassPage.model_validate(resp.json())

    def compare(
        self, project_id: IdLike, version_id: IdLike, baseline_id: IdLike
    ) -> TrainingDatasetInsightComparison:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/compare",
            params={"baseline_id": baseline_id},
        )
        return TrainingDatasetInsightComparison.model_validate(resp.json())

    def samples(
        self,
        project_id: IdLike,
        version_id: IdLike,
        limit: int = 25,
        offset: int = 0,
        split: Literal["train", "val"] | None = None,
        class_index: int | None = None,
        content_sha256: str | None = None,
        kind: Literal["all", "positive", "negative", "excluded", "unavailable"] = "all",
        baseline_id: IdLike | None = None,
        change: Literal[
            "all",
            "added",
            "removed",
            "labels_changed",
            "split_changed",
            "multiplicity_changed",
            "task_ids_changed",
            "reintroduced",
            "unchanged",
            "unknown",
        ] = "all",
    ) -> TrainingDatasetSamplePage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/samples",
            params=_drop_none(
                {
                    "limit": limit,
                    "offset": offset,
                    "split": split,
                    "class_index": class_index,
                    "content_sha256": content_sha256,
                    "kind": kind,
                    "baseline_id": baseline_id,
                    "change": change,
                }
            ),
        )
        return TrainingDatasetSamplePage.model_validate(resp.json())

    def sample(
        self,
        project_id: IdLike,
        version_id: IdLike,
        task_id: IdLike,
        limit: int = 500,
        offset: int = 0,
    ) -> TrainingDatasetSampleDetail:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}",
            params={"limit": limit, "offset": offset},
        )
        return TrainingDatasetSampleDetail.model_validate(resp.json())

    def download_media(
        self,
        project_id: IdLike,
        version_id: IdLike,
        task_id: IdLike,
        dest: str | Path,
        *,
        preview: bool = False,
    ) -> Path:
        return self._http.stream_download(
            f"/api/v1/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}/media",
            Path(dest),
            params={"preview": preview},
        )


class TrainingRuns:
    def __init__(self, http: HttpTransport):
        self._http = http

    def catalog(self, project_id: IdLike) -> TrainingHubCatalog:
        return TrainingHubCatalog.model_validate(
            self._http.request(
                "GET", f"/projects/{project_id}/training-runs/catalog"
            ).json()
        )

    def parameter_schema(
        self,
        project_id: IdLike,
        framework: Literal["ultralytics", "libreyolo"],
        base_model: str,
    ) -> TrainingParameterSchema:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/parameter-schema",
            params={"framework": framework, "base_model": base_model},
        )
        return TrainingParameterSchema.model_validate(resp.json())

    def initialization_source(
        self,
        project_id: IdLike,
        model_id: IdLike,
        framework: Literal["ultralytics", "libreyolo"],
        base_model: str,
        dataset_id: IdLike | None = None,
    ) -> TrainingInitializationEligibility:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/initialization-sources/{model_id}",
            params=_drop_none(
                {
                    "framework": framework,
                    "base_model": base_model,
                    "dataset_id": dataset_id,
                }
            ),
        )
        return TrainingInitializationEligibility.model_validate(resp.json())

    def preflight(
        self, project_id: IdLike, body: TrainingRunCreate
    ) -> TrainingRunPreflight:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-runs:preflight",
            json=_request_body(body),
        )
        return TrainingRunPreflight.model_validate(resp.json())

    def create(self, project_id: IdLike, body: TrainingRunCreate) -> TrainingRunOut:
        if body.preflight_digest is None:
            raise AAPError("Training run creation requires a fresh preflight_digest")
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-runs",
            json=_request_body(body),
        )
        return TrainingRunOut.model_validate(resp.json())

    def list(
        self, project_id: IdLike, limit: int = 20, offset: int = 0
    ) -> TrainingRunPage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs",
            params={"limit": limit, "offset": offset},
        )
        return TrainingRunPage.model_validate(resp.json())

    def get(self, project_id: IdLike, run_id: IdLike) -> TrainingRunDetail:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/training-runs/{run_id}"
        )
        return TrainingRunDetail.model_validate(resp.json())

    def events(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike | None = None,
        after: int = 0,
        latest: bool = False,
    ) -> TrainingRunEvents:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/events",
            params=_drop_none(
                {"attempt_id": attempt_id, "after": after, "latest": latest}
            ),
        )
        return TrainingRunEvents.model_validate(resp.json())

    def evaluation(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike,
        offset: int = 0,
        limit: int = 50,
    ) -> TrainingEvaluation:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/evaluation",
            params={"offset": offset, "limit": limit},
        )
        return TrainingEvaluation.model_validate(resp.json())

    def telemetry(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike,
        after: int | None = None,
        limit: int = 120,
    ) -> TrainingTelemetryPage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/telemetry",
            params=_drop_none({"after": after, "limit": limit}),
        )
        return TrainingTelemetryPage.model_validate(resp.json())

    def logs(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike,
        after: int | None = None,
        before: int | None = None,
        limit: int = 120,
    ) -> TrainingLogPage:
        if after is not None and before is not None:
            raise ValidationError(422, "Logs cannot use both after and before cursors")
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/logs",
            params=_drop_none({"after": after, "before": before, "limit": limit}),
        )
        return TrainingLogPage.model_validate(resp.json())

    def download_artifact(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike,
        name: str,
        dest: str | Path,
    ) -> Path:
        descriptor = TrainingArtifactDownload.model_validate(
            self._http.request(
                "GET",
                f"/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/artifacts/{name}/download",
            ).json()
        )
        return self._http.stream_download(
            descriptor.url,
            Path(dest),
            expected_sha256=descriptor.sha256,
            expected_size=descriptor.size_bytes,
        )

    def recovery(self, project_id: IdLike, run_id: IdLike) -> TrainingRecovery:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/training-runs/{run_id}/recovery"
        )
        return TrainingRecovery.model_validate(resp.json())

    def cancel(self, project_id: IdLike, run_id: IdLike) -> TrainingRunOut:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/training-runs/{run_id}/cancel"
        )
        return TrainingRunOut.model_validate(resp.json())

    def resume(self, project_id: IdLike, run_id: IdLike) -> TrainingRunOut:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/training-runs/{run_id}/resume"
        )
        return TrainingRunOut.model_validate(resp.json())

    def delete(self, project_id: IdLike, run_id: IdLike) -> None:
        self._http.request("DELETE", f"/projects/{project_id}/training-runs/{run_id}")


class TrainingRecipes:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        project_id: IdLike,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> TrainingRecipePage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-recipes",
            params={
                "limit": limit,
                "offset": offset,
                "include_archived": include_archived,
            },
        )
        return TrainingRecipePage.model_validate(resp.json())

    def create(
        self, project_id: IdLike, body: TrainingRecipeWrite
    ) -> TrainingRecipeOut:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-recipes",
            json=_request_body(body),
        )
        return TrainingRecipeOut.model_validate(resp.json())

    def get(
        self,
        project_id: IdLike,
        recipe_id: IdLike,
        revision_id: IdLike | None = None,
    ) -> TrainingRecipeOut:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-recipes/{recipe_id}",
            params=_drop_none({"revision_id": revision_id}),
        )
        return TrainingRecipeOut.model_validate(resp.json())

    def revisions(
        self, project_id: IdLike, recipe_id: IdLike, limit: int = 20, offset: int = 0
    ) -> TrainingRecipePage:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-recipes/{recipe_id}/revisions",
            params={"limit": limit, "offset": offset},
        )
        return TrainingRecipePage.model_validate(resp.json())

    def create_revision(
        self, project_id: IdLike, recipe_id: IdLike, body: TrainingRecipeWrite
    ) -> TrainingRecipeOut:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-recipes/{recipe_id}/revisions",
            json=_request_body(body),
        )
        return TrainingRecipeOut.model_validate(resp.json())

    def resolve(
        self, project_id: IdLike, body: TrainingConfigurationResolve
    ) -> TrainingConfigurationSnapshot:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-recipes:resolve",
            json=_request_body(body),
        )
        return TrainingConfigurationSnapshot.model_validate(resp.json())

    def clone_candidate(
        self,
        project_id: IdLike,
        run_id: IdLike,
        attempt_id: IdLike | None = None,
    ) -> TrainingReuseCandidate:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/training-recipes/clone/{run_id}",
            params=_drop_none({"attempt_id": attempt_id}),
        )
        return TrainingReuseCandidate.model_validate(resp.json())

    def archive(
        self,
        project_id: IdLike,
        recipe_id: IdLike,
        expected_head_revision: int,
    ) -> TrainingRecipeOut:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-recipes/{recipe_id}/archive",
            params={"expected_head_revision": expected_head_revision},
        )
        return TrainingRecipeOut.model_validate(resp.json())


class TrainingPreviews:
    def __init__(self, http: HttpTransport):
        self._http = http

    def capabilities(self, project_id: IdLike) -> TrainingPreviewCapabilities:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/training-previews/capabilities"
        )
        return TrainingPreviewCapabilities.model_validate(resp.json())

    def create(
        self, project_id: IdLike, body: TrainingPreviewCreate
    ) -> TrainingPreviewOut:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/training-previews",
            json=_request_body(body),
        )
        return TrainingPreviewOut.model_validate(resp.json())

    def get(self, project_id: IdLike, preview_id: IdLike) -> TrainingPreviewOut:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/training-previews/{preview_id}"
        )
        return TrainingPreviewOut.model_validate(resp.json())

    def download_image(
        self, project_id: IdLike, preview_id: IdLike, index: int, dest: str | Path
    ) -> Path:
        return self._http.stream_download(
            f"/api/v1/projects/{project_id}/training-previews/{preview_id}/images/{index}",
            Path(dest),
        )

    def cancel(self, project_id: IdLike, preview_id: IdLike) -> TrainingPreviewOut:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/training-previews/{preview_id}/cancel"
        )
        return TrainingPreviewOut.model_validate(resp.json())

    def delete(self, project_id: IdLike, preview_id: IdLike) -> None:
        self._http.request(
            "DELETE", f"/projects/{project_id}/training-previews/{preview_id}"
        )
