"""Synchronous study and active-learning resources."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.errors import AAPError
from ai_annotation.learning_automation_models import (
    ActiveLearningCandidateList,
    ActiveLearningDecision,
    ActiveLearningDecisionCreate,
    ActiveLearningDecisionList,
    ActiveLearningFeedback,
    ActiveLearningFeedbackCreate,
    ActiveLearningFeedbackList,
    ActiveLearningPoolPage,
    ActiveLearningPreview,
    ActiveLearningReport,
    ActiveLearningRound,
    ActiveLearningRoundCreate,
    ActiveLearningRoundPage,
    ActiveLearningSelection,
    TrainingStudyCreate,
    TrainingStudyDetail,
    TrainingStudyPage,
    TrainingStudyPreview,
)

IdLike = str | UUID


class TrainingStudies:
    def __init__(self, http: HttpTransport):
        self._http = http

    def preview(
        self, project_id: IdLike, body: TrainingStudyCreate
    ) -> TrainingStudyPreview:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/training-studies:preview",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return TrainingStudyPreview.model_validate(response.json())

    def create(
        self, project_id: IdLike, body: TrainingStudyCreate
    ) -> TrainingStudyDetail:
        if body.preview_digest is None:
            raise AAPError("training study creation requires a preview_digest")
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/training-studies",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return TrainingStudyDetail.model_validate(response.json())

    def list(
        self, project_id: IdLike, *, limit: int = 20, offset: int = 0
    ) -> TrainingStudyPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/training-studies",
            params=_drop_none({"limit": limit, "offset": offset}),
        )
        return TrainingStudyPage.model_validate(response.json())

    def get(self, project_id: IdLike, study_id: IdLike) -> TrainingStudyDetail:
        response = self._http.request(
            "GET", f"/projects/{project_id}/training-studies/{study_id}"
        )
        return TrainingStudyDetail.model_validate(response.json())

    def cancel(self, project_id: IdLike, study_id: IdLike) -> TrainingStudyDetail:
        response = self._http.request(
            "POST", f"/projects/{project_id}/training-studies/{study_id}/cancel"
        )
        return TrainingStudyDetail.model_validate(response.json())


class ActiveLearning:
    def __init__(self, http: HttpTransport):
        self._http = http

    def pool(
        self, project_id: IdLike, *, limit: int = 32, offset: int = 0
    ) -> ActiveLearningPoolPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds/pool",
            params=_drop_none({"limit": limit, "offset": offset}),
        )
        return ActiveLearningPoolPage.model_validate(response.json())

    def preview(
        self, project_id: IdLike, body: ActiveLearningSelection
    ) -> ActiveLearningPreview:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/active-learning-rounds:preview",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return ActiveLearningPreview.model_validate(response.json())

    def create(
        self, project_id: IdLike, body: ActiveLearningRoundCreate
    ) -> ActiveLearningRound:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/active-learning-rounds",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return ActiveLearningRound.model_validate(response.json())

    def list(
        self, project_id: IdLike, *, limit: int = 20, offset: int = 0
    ) -> ActiveLearningRoundPage:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds",
            params=_drop_none({"limit": limit, "offset": offset}),
        )
        return ActiveLearningRoundPage.model_validate(response.json())

    def get(self, project_id: IdLike, round_id: IdLike) -> ActiveLearningRound:
        response = self._http.request(
            "GET", f"/projects/{project_id}/active-learning-rounds/{round_id}"
        )
        return ActiveLearningRound.model_validate(response.json())

    def report(
        self, project_id: IdLike, round_id: IdLike, attempt_id: IdLike
    ) -> ActiveLearningReport:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/attempts/{attempt_id}/report",
        )
        return ActiveLearningReport.model_validate(response.json())

    def candidates(
        self, project_id: IdLike, round_id: IdLike
    ) -> ActiveLearningCandidateList:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/candidates",
        )
        return ActiveLearningCandidateList.model_validate(response.json())

    def download_image(
        self,
        project_id: IdLike,
        round_id: IdLike,
        ordinal: int,
        dest: str | Path,
    ) -> Path:
        return self._http.stream_download(
            f"/api/v1/projects/{project_id}/active-learning-rounds/{round_id}/candidates/{ordinal}/image",
            Path(dest),
        )

    def decisions(
        self,
        project_id: IdLike,
        round_id: IdLike,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> ActiveLearningDecisionList:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/decisions",
            params=_drop_none({"limit": limit, "offset": offset}),
        )
        return ActiveLearningDecisionList.model_validate(response.json())

    def decide(
        self,
        project_id: IdLike,
        round_id: IdLike,
        body: ActiveLearningDecisionCreate,
    ) -> ActiveLearningDecision:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/decisions",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return ActiveLearningDecision.model_validate(response.json())

    def feedback(
        self,
        project_id: IdLike,
        round_id: IdLike,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> ActiveLearningFeedbackList:
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/feedback",
            params=_drop_none({"limit": limit, "offset": offset}),
        )
        return ActiveLearningFeedbackList.model_validate(response.json())

    def create_feedback(
        self,
        project_id: IdLike,
        round_id: IdLike,
        body: ActiveLearningFeedbackCreate,
    ) -> ActiveLearningFeedback:
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/active-learning-rounds/{round_id}/feedback",
            json=body.model_dump(mode="json", exclude_unset=True, by_alias=True),
        )
        return ActiveLearningFeedback.model_validate(response.json())

    def cancel(self, project_id: IdLike, round_id: IdLike) -> ActiveLearningRound:
        response = self._http.request(
            "POST", f"/projects/{project_id}/active-learning-rounds/{round_id}/cancel"
        )
        return ActiveLearningRound.model_validate(response.json())

    def retry(self, project_id: IdLike, round_id: IdLike) -> ActiveLearningRound:
        response = self._http.request(
            "POST", f"/projects/{project_id}/active-learning-rounds/{round_id}/retry"
        )
        return ActiveLearningRound.model_validate(response.json())
