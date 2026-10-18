"""Frozen public contract for Assets/Workspace association reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from aap_workspace_contract.project_read import WorkspaceProjectSnapshot

ASSOCIATIONS_SCHEMA = "aap-assets-associations/v1"

AssociationFailureCode = Literal[
    "invalid_request",
    "account_not_active",
    "invalid_account_role",
    "project_missing",
    "invalid_project_role",
    "incompatible_project_role",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListDatasetProjectsRequest(_FrozenModel):
    actor_id: UUID
    dataset_id: UUID


class ListProjectDatasetsRequest(_FrozenModel):
    actor_id: UUID
    project_id: UUID


class LinkedProjectSnapshot(WorkspaceProjectSnapshot):
    """Raw Workspace fields plus current Labeling counters for ProjectOut."""

    total_tasks: StrictInt = Field(ge=0)
    completed_tasks: StrictInt = Field(ge=0)
    review_tasks: StrictInt = Field(ge=0)
    in_progress_tasks: StrictInt = Field(ge=0)
    batch_summary: dict[str, Any]

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetProjectsResult(_FrozenModel):
    dataset_id: UUID
    projects: list[LinkedProjectSnapshot]

    @model_validator(mode="after")
    def unique_project_ids(self) -> DatasetProjectsResult:
        ids = [project.id for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset project projection contains duplicate ids")
        return self


class ProjectDatasetSummary(_FrozenModel):
    id: UUID
    display_id: StrictStr
    name: StrictStr
    data_type: StrictStr
    linked_at: datetime | None
    items_count: StrictInt = Field(ge=0)
    tasks_in_project: StrictInt = Field(ge=0)

    @field_validator("linked_at")
    @classmethod
    def utc_linked_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("linked_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("linked_at")
    def serialize_linked_at(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


class ProjectDatasetsResult(_FrozenModel):
    project_id: UUID
    datasets: list[ProjectDatasetSummary]

    @model_validator(mode="after")
    def unique_dataset_ids(self) -> ProjectDatasetsResult:
        ids = [dataset.id for dataset in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("project dataset projection contains duplicate ids")
        return self


_ASSOCIATION_FAILURES = frozenset(
    {
        "invalid_request",
        "account_not_active",
        "invalid_account_role",
        "project_missing",
        "invalid_project_role",
        "incompatible_project_role",
    }
)
_AssociationResult = TypeVar("_AssociationResult", bound=BaseModel)


class AssociationFailure(Exception):
    """Expected, versioned failure from an Assets association projection."""

    def __init__(self, code: AssociationFailureCode):
        if code not in _ASSOCIATION_FAILURES:
            raise ValueError(f"unknown Assets association failure code: {code!r}")
        self.code = code
        super().__init__(code)


def decode_associations_reply(
    payload: Any,
    result_type: type[_AssociationResult],
) -> _AssociationResult:
    """Validate an exact aap-assets-associations/v1 reply branch."""
    if not isinstance(payload, dict) or payload.get("schema") != ASSOCIATIONS_SCHEMA:
        raise ValueError("Assets associations reply has an unknown schema")

    if payload.get("ok") is True:
        if set(payload) != {"schema", "ok", "data"}:
            raise ValueError("Assets associations success reply has an invalid shape")
        return result_type.model_validate(payload["data"])

    if payload.get("ok") is False:
        if set(payload) != {"schema", "ok", "error"}:
            raise ValueError("Assets associations failure reply has an invalid shape")
        error = payload["error"]
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or not isinstance(error.get("code"), str)
            or error["code"] not in _ASSOCIATION_FAILURES
        ):
            raise ValueError("Assets associations failure reply has an unknown error")
        raise AssociationFailure(error["code"])

    raise ValueError("Assets associations reply must contain a boolean ok field")
