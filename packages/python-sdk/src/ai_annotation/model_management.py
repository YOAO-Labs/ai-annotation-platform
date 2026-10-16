"""Client resources for controlled models, learning policies, and evaluations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.model_management_models import (
    EvaluationComparison,
    EvaluationQualification,
    EvaluationReport,
    EvaluationSampleResultPage,
    EvaluationSource,
    LearningPolicy,
    LearningPolicySaveRequest,
    LearningTriggerPage,
    ModelDecision,
    ModelDecisionCreateRequest,
    ModelDecisionPage,
    ModelDeployment,
    ModelDeploymentCreateRequest,
    ModelDownload,
    ModelLineage,
    ModelRelease,
    ModelReleaseCreateRequest,
    ModelReleasePage,
    NodeKind,
    SampleEvaluation,
    SampleEvaluationCreateRequest,
    SampleEvaluationPage,
    TrainedModel,
    TrainedModelCreateRequest,
    TrainedModelDetail,
    TrainedModelPage,
)

IdLike = str | UUID


def _body(request: Any) -> dict[str, Any]:
    return request.model_dump(mode="json", by_alias=True, exclude_unset=True)


class TrainedModels:
    def __init__(self, http: HttpTransport):
        self._http = http

    def create(
        self, project_id: IdLike, request: TrainedModelCreateRequest
    ) -> TrainedModel:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/trained-models",
            json=_body(request),
        )
        return TrainedModel.model_validate(response.json())

    def list(
        self,
        project_id: IdLike,
        limit: int = 20,
        offset: int = 0,
        archive: Literal["exclude", "include", "only"] = "exclude",
    ) -> TrainedModelPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/trained-models",
            params={"limit": limit, "offset": offset, "archive": archive},
        )
        return TrainedModelPage.model_validate(response.json())

    def get(self, project_id: IdLike, version_id: IdLike) -> TrainedModelDetail:
        response = self._http.request(
            "GET", f"/projects/{project_id}/trained-models/{version_id}"
        )
        return TrainedModelDetail.model_validate(response.json())

    def download(
        self, project_id: IdLike, version_id: IdLike, dest: str | Path
    ) -> Path:
        response = self._http.request(
            "GET", f"/projects/{project_id}/trained-models/{version_id}/download"
        )
        descriptor = ModelDownload.model_validate(response.json())
        return self._http.stream_download(
            descriptor.url,
            Path(dest),
            expected_sha256=descriptor.sha256,
            expected_size=descriptor.size_bytes,
        )

    def deploy(
        self,
        project_id: IdLike,
        version_id: IdLike,
        request: ModelDeploymentCreateRequest,
    ) -> ModelDeployment:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/trained-models/{version_id}/deploy",
            json=_body(request),
        )
        return ModelDeployment.model_validate(response.json())

    def lineage(
        self,
        project_id: IdLike,
        kind: NodeKind,
        node_id: IdLike,
        relation: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ModelLineage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/model-lineage/{kind}/{node_id}",
            params=_drop_none({"relation": relation, "cursor": cursor, "limit": limit}),
        )
        return ModelLineage.model_validate(response.json())

    def decisions(
        self,
        project_id: IdLike,
        version_id: IdLike,
        before: int | None = None,
        limit: int = 20,
    ) -> ModelDecisionPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/trained-models/{version_id}/decisions",
            params=_drop_none({"before": before, "limit": limit}),
        )
        return ModelDecisionPage.model_validate(response.json())

    def decide(
        self,
        project_id: IdLike,
        version_id: IdLike,
        request: ModelDecisionCreateRequest,
    ) -> ModelDecision:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/trained-models/{version_id}/decisions",
            json=_body(request),
        )
        return ModelDecision.model_validate(response.json())


class ModelReleases:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self, project_id: IdLike, limit: int = 20, offset: int = 0
    ) -> ModelReleasePage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/model-releases",
            params={"limit": limit, "offset": offset},
        )
        return ModelReleasePage.model_validate(response.json())

    def create(
        self, project_id: IdLike, request: ModelReleaseCreateRequest
    ) -> ModelRelease:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/model-releases",
            json=_body(request),
        )
        return ModelRelease.model_validate(response.json())


class LearningPolicies:
    def __init__(self, http: HttpTransport):
        self._http = http

    def get(self, project_id: IdLike) -> LearningPolicy | None:
        response = self._http.request("GET", f"/projects/{project_id}/learning-policy")
        value = response.json()
        return LearningPolicy.model_validate(value) if value is not None else None

    def save(
        self, project_id: IdLike, request: LearningPolicySaveRequest
    ) -> LearningPolicy:
        response = self._http.request(
            "PUT",
            f"/projects/{project_id}/learning-policy",
            json=_body(request),
        )
        return LearningPolicy.model_validate(response.json())

    def check(self, project_id: IdLike) -> LearningPolicy:
        response = self._http.request(
            "POST", f"/projects/{project_id}/learning-policy/check"
        )
        return LearningPolicy.model_validate(response.json())

    def disable(self, project_id: IdLike, expected_revision: int) -> LearningPolicy:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/learning-policy/disable",
            params={"expected_revision": expected_revision},
        )
        return LearningPolicy.model_validate(response.json())

    def delete(self, project_id: IdLike) -> None:
        self._http.request("DELETE", f"/projects/{project_id}/learning-policy")

    def triggers(
        self, project_id: IdLike, limit: int = 20, offset: int = 0
    ) -> LearningTriggerPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/learning-policy/triggers",
            params={"limit": limit, "offset": offset},
        )
        return LearningTriggerPage.model_validate(response.json())


class SampleEvaluations:
    def __init__(self, http: HttpTransport):
        self._http = http

    def qualification(
        self,
        project_id: IdLike,
        model_version_id: IdLike,
        deployment_id: IdLike,
    ) -> EvaluationQualification:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations/qualification",
            params={
                "model_version_id": str(model_version_id),
                "deployment_id": str(deployment_id),
            },
        )
        return EvaluationQualification.model_validate(response.json())

    def create(
        self, project_id: IdLike, request: SampleEvaluationCreateRequest
    ) -> SampleEvaluation:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/sample-evaluations",
            json=_body(request),
        )
        return SampleEvaluation.model_validate(response.json())

    def list(
        self, project_id: IdLike, limit: int = 20, offset: int = 0
    ) -> SampleEvaluationPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations",
            params={"limit": limit, "offset": offset},
        )
        return SampleEvaluationPage.model_validate(response.json())

    def get(self, project_id: IdLike, evaluation_id: IdLike) -> SampleEvaluation:
        response = self._http.request(
            "GET", f"/projects/{project_id}/sample-evaluations/{evaluation_id}"
        )
        return SampleEvaluation.model_validate(response.json())

    def compare(
        self, project_id: IdLike, left_id: IdLike, right_id: IdLike
    ) -> EvaluationComparison:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations/compare",
            params={"left_id": str(left_id), "right_id": str(right_id)},
        )
        return EvaluationComparison.model_validate(response.json())

    def report(
        self, project_id: IdLike, evaluation_id: IdLike, attempt_id: IdLike
    ) -> EvaluationReport:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/report",
        )
        return EvaluationReport.model_validate(response.json())

    def samples(
        self,
        project_id: IdLike,
        evaluation_id: IdLike,
        attempt_id: IdLike,
        class_index: int | None = None,
        error: Literal[
            "false_positive", "false_negative", "confusion", "localization", "failed"
        ]
        | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        offset: int = 0,
        limit: int = 12,
    ) -> EvaluationSampleResultPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples",
            params=_drop_none(
                {
                    "class_index": class_index,
                    "error": error,
                    "confidence_min": confidence_min,
                    "confidence_max": confidence_max,
                    "offset": offset,
                    "limit": limit,
                }
            ),
        )
        return EvaluationSampleResultPage.model_validate(response.json())

    def source(
        self,
        project_id: IdLike,
        evaluation_id: IdLike,
        attempt_id: IdLike,
        ordinal: int,
    ) -> EvaluationSource:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/source",
        )
        return EvaluationSource.model_validate(response.json())

    def download_image(
        self,
        project_id: IdLike,
        evaluation_id: IdLike,
        attempt_id: IdLike,
        ordinal: int,
        dest: str | Path,
    ) -> Path:
        return self._http.stream_download(
            f"/api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/image",
            Path(dest),
        )

    def cancel(self, project_id: IdLike, evaluation_id: IdLike) -> SampleEvaluation:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/sample-evaluations/{evaluation_id}/cancel",
        )
        return SampleEvaluation.model_validate(response.json())

    def retry(self, project_id: IdLike, evaluation_id: IdLike) -> SampleEvaluation:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/sample-evaluations/{evaluation_id}/retry",
        )
        return SampleEvaluation.model_validate(response.json())

    def delete(self, project_id: IdLike, evaluation_id: IdLike) -> None:
        self._http.request(
            "DELETE", f"/projects/{project_id}/sample-evaluations/{evaluation_id}"
        )
