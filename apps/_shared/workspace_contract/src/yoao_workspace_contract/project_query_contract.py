"""Closed requests and typed read facts for the Workspace Project query owner."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .wire import ClosedModel, _json_bytes, _reject_constant, _unique_object

PROJECT_QUERY_PROTOCOL = "aap-workspace-project-query/v1"
ProjectQueryOperation: TypeAlias = Literal[
    "list_projects",
    "query_projects",
    "get_project",
    "project_stats",
    "project_readiness",
    "get_project_projection",
    "get_project_access",
    "require_project_manager",
    "require_project_visible",
    "search_projects",
]
ProjectQueryAction: TypeAlias = Literal[
    "list_rows",
    "count_rows",
    "visible_rows",
    "get_row",
    "actor_status",
    "member_access",
    "access_member_rows",
    "ai_counts",
    "member_count",
    "pool",
    "my_roles",
    "annotation_count",
    "task_series",
    "annotation_series",
    "dataset_ids",
    "dataset_item_count",
    "linked_task_count",
    "task_count",
    "job_counts",
    "latest_job_status",
    "batches",
    "batch_task_counts",
    "active_members",
    "projection_row",
    "search_rows",
]
DecimalCount: TypeAlias = Annotated[StrictStr, Field(pattern=r"^(0|[1-9][0-9]*)$")]
PositiveDecimal: TypeAlias = Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$")]
SignedMicroseconds: TypeAlias = Annotated[
    StrictStr, Field(pattern=r"^(0|-?[1-9][0-9]*)$", max_length=19)
]

PROJECT_ROW_COLUMNS = (
    "id",
    "organization_id",
    "display_id",
    "name",
    "type_label",
    "type_key",
    "data_type",
    "owner_id",
    "status",
    "ai_enabled",
    "ml_backend_pool_id",
    "default_variants",
    "preannotate_pipeline",
    "label_config",
    "tool_bindings",
    "sampling",
    "maximum_annotations",
    "show_overlap_first",
    "iou_dedup_threshold",
    "box_threshold",
    "text_threshold",
    "ai_interactive_enabled",
    "raster_mask_native_editing_enabled",
    "mask_qc_config",
    "point_cloud_quality_config",
    "rendering_config",
    "annotation_guide",
    "guide_assets",
    "guide_assets_revision",
    "model_version",
    "task_lock_ttl_seconds",
    "total_tasks",
    "completed_tasks",
    "review_tasks",
    "in_progress_tasks",
    "batch_summary",
    "video_sampling",
    "video_collaboration",
    "scene_mode",
    "prefer_same_scene_continuation",
    "scene_continuation_window_min",
    "due_date",
    "created_at",
    "updated_at",
)
PROJECT_JSON_COLUMNS = (
    "default_variants",
    "preannotate_pipeline",
    "label_config",
    "tool_bindings",
    "mask_qc_config",
    "point_cloud_quality_config",
    "rendering_config",
    "guide_assets",
    "batch_summary",
    "video_sampling",
    "video_collaboration",
)
PROJECT_ENRICHMENT_COLUMNS = (
    "ml_backend_id",
    "owner_name",
    "owner_avatar_ref",
    "member_count",
    "ai_completed_tasks",
)


class _ProjectQueryModel(ClosedModel):
    @field_validator("ok", mode="before", check_fields=False)
    @classmethod
    def exact_json_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a JSON boolean")
        return value

    @model_validator(mode="before")
    @classmethod
    def schema_alias_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class ProjectQueryActor(ClosedModel):
    id: uuid.UUID
    role: StrictStr


class ProjectQueryPagination(ClosedModel):
    page: PositiveDecimal
    page_size: Annotated[StrictInt, Field(ge=1, le=100)]


class ProjectConfigurationEvidence(ClosedModel):
    source: Literal["configuration"]
    operation_id: uuid.UUID
    operation: Literal["create_project", "update_project"]


class ProjectOwnerTransferEvidence(ClosedModel):
    source: Literal["owner_transfer"]
    operation_id: uuid.UUID
    new_owner_id: uuid.UUID


class ProjectClassRenameEvidence(ClosedModel):
    source: Literal["class_rename"]
    operation_id: uuid.UUID
    source_operation: Literal["rename_class"]
    actor_id: uuid.UUID
    project_id: uuid.UUID
    owner_id: uuid.UUID
    platform_role: StrictStr
    is_active: StrictBool


ProjectProjectionEvidence: TypeAlias = Annotated[
    ProjectConfigurationEvidence
    | ProjectOwnerTransferEvidence
    | ProjectClassRenameEvidence,
    Field(discriminator="source"),
]


class ProjectQueryRequest(_ProjectQueryModel):
    protocol: Literal["aap-workspace-project-query/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: ProjectQueryOperation
    admitted_actor: ProjectQueryActor
    target_id: uuid.UUID | None
    filters_ascii: StrictStr | None
    pagination: ProjectQueryPagination | None
    projection_evidence: ProjectProjectionEvidence | None

    @field_validator("filters_ascii")
    @classmethod
    def opaque_ascii_filters(cls, value):
        if value is not None and (
            not value.isascii() or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("Filters require ASCII JSON text without raw controls")
        return value

    @model_validator(mode="after")
    def operation_fields_match(self):
        listed = self.operation in (
            "list_projects",
            "query_projects",
            "search_projects",
        )
        targeted = self.operation in (
            "get_project",
            "project_readiness",
            "get_project_projection",
            "get_project_access",
            "require_project_manager",
            "require_project_visible",
        )
        if (self.filters_ascii is not None) != listed:
            raise ValueError("Filters do not match the operation")
        if (self.target_id is not None) != targeted:
            raise ValueError("Target does not match the operation")
        if (self.pagination is not None) != (self.operation == "query_projects"):
            raise ValueError("Pagination does not match the operation")
        if (self.projection_evidence is not None) != (
            self.operation == "get_project_projection"
        ):
            raise ValueError("Projection evidence does not match the operation")
        evidence = self.projection_evidence
        if isinstance(evidence, ProjectClassRenameEvidence) and (
            evidence.actor_id != self.admitted_actor.id
            or evidence.project_id != self.target_id
        ):
            raise ValueError("Accepted class rename facts do not match the request")
        return self


class ProjectQueryFilters(ClosedModel):
    status: StrictStr | None
    search: StrictStr | None
    type_key: list[StrictStr] | None
    data_type: list[StrictStr] | None
    member_id: uuid.UUID | None
    project_role: StrictStr | None
    created_from: StrictStr | None
    created_to: StrictStr | None


class ProjectSearchFilters(ClosedModel):
    q: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    limit: Annotated[StrictInt, Field(ge=1, le=20)]


class ProjectQueryPolicy(_ProjectQueryModel):
    protocol: Literal["aap-workspace-project-query/v1"] = Field(alias="schema")


class ProjectQueryError(ClosedModel):
    status_code: Literal[401, 403, 404, 422, 503]
    detail: Literal[
        "账号已停用或不存在",
        "账号角色无效，访问被拒绝",
        "项目不存在",
        "项目职责无效，访问被拒绝",
        "账号平台角色与项目职责不兼容",
        "仅项目负责人或超级管理员可执行",
        "非法项目职责",
        "创建日期须为 ISO 日期或时间",
        "创建开始日期不能晚于结束日期",
        "身份认证服务暂不可用",
        "身份资料服务暂不可用",
    ]
    headers: dict[Literal["WWW-Authenticate"], Literal["Bearer"]] | None

    @model_validator(mode="after")
    def valid_error_pair(self):
        expected = {
            "账号已停用或不存在": 401,
            "账号角色无效，访问被拒绝": 403,
            "项目不存在": 404,
            "项目职责无效，访问被拒绝": 403,
            "账号平台角色与项目职责不兼容": 403,
            "仅项目负责人或超级管理员可执行": 403,
            "非法项目职责": 422,
            "创建日期须为 ISO 日期或时间": 422,
            "创建开始日期不能晚于结束日期": 422,
            "身份认证服务暂不可用": 503,
            "身份资料服务暂不可用": 503,
        }
        if self.status_code != expected[self.detail]:
            raise ValueError("Unexpected status/detail pair")
        headers = {"WWW-Authenticate": "Bearer"} if self.status_code == 401 else None
        if self.headers != headers:
            raise ValueError("Unexpected authentication headers")
        return self


class ProjectQuerySuccess(_ProjectQueryModel):
    protocol: Literal["aap-workspace-project-query/v1"] = Field(alias="schema")
    ok: Literal[True]
    request: ProjectQueryRequest
    data_json: StrictStr


class ProjectQueryFailure(_ProjectQueryModel):
    protocol: Literal["aap-workspace-project-query/v1"] = Field(alias="schema")
    ok: Literal[False]
    request: ProjectQueryRequest
    error: ProjectQueryError


ProjectQueryReply: TypeAlias = ProjectQuerySuccess | ProjectQueryFailure


class ProjectQueryRowFacts(ClosedModel):
    row_json: StrictStr | None


class ProjectQueryRowsFacts(ClosedModel):
    rows_json: StrictStr


class ProjectQueryCountFacts(ClosedModel):
    count: DecimalCount


class ProjectQueryAccountStatus(ClosedModel):
    actor_id: uuid.UUID
    platform_role: StrictStr
    is_active: StrictBool


class ProjectQueryActorFacts(ClosedModel):
    actor: ProjectQueryAccountStatus | None


class ProjectQueryMembership(ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: StrictStr | None


class ProjectQueryMemberFacts(ClosedModel):
    member: ProjectQueryMembership | None


class ProjectQueryAccessMemberRowsFacts(ClosedModel):
    rows_json: StrictStr


class ProjectQueryProjectCount(ClosedModel):
    project_id: uuid.UUID
    count: DecimalCount


class ProjectQueryAiCountsFacts(ClosedModel):
    counts: list[ProjectQueryProjectCount]


class ProjectQueryPoolFacts(ClosedModel):
    legacy_instance_id: uuid.UUID | None


class ProjectQueryProjectRole(ClosedModel):
    project_id: uuid.UUID
    role: StrictStr | None


class ProjectQueryMyRolesFacts(ClosedModel):
    memberships: list[ProjectQueryProjectRole]


class ProjectQueryTaskSeriesFacts(ClosedModel):
    total_data: Annotated[list[DecimalCount], Field(min_length=12, max_length=12)]
    completed: Annotated[list[DecimalCount], Field(min_length=12, max_length=12)]
    pending_review: Annotated[list[DecimalCount], Field(min_length=12, max_length=12)]


class ProjectQueryAnnotationSeriesFacts(ClosedModel):
    total: Annotated[list[DecimalCount], Field(min_length=12, max_length=12)]
    ai_derived: Annotated[list[DecimalCount], Field(min_length=12, max_length=12)]


class ProjectQueryDatasetIdsFacts(ClosedModel):
    dataset_ids: list[uuid.UUID]


class ProjectQueryJobStatusCount(ClosedModel):
    status: StrictStr
    count: DecimalCount


class ProjectQueryJobCountsFacts(ClosedModel):
    statuses: list[ProjectQueryJobStatusCount]


class ProjectQueryLatestJobFacts(ClosedModel):
    status: StrictStr | None


class ProjectQueryBatch(ClosedModel):
    id: uuid.UUID
    status: StrictStr
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None


class ProjectQueryBatchesFacts(ClosedModel):
    batches: list[ProjectQueryBatch]


class ProjectQueryBatchCount(ClosedModel):
    batch_id: uuid.UUID | None
    count: DecimalCount


class ProjectQueryBatchCountsFacts(ClosedModel):
    batch_task_counts: list[ProjectQueryBatchCount]


class ProjectQueryActiveMember(ClosedModel):
    user_id: uuid.UUID
    project_role: StrictStr


class ProjectQueryActiveMembersFacts(ClosedModel):
    members: list[ProjectQueryActiveMember]


ProjectQueryFacts: TypeAlias = (
    ProjectQueryRowFacts
    | ProjectQueryRowsFacts
    | ProjectQueryCountFacts
    | ProjectQueryActorFacts
    | ProjectQueryMemberFacts
    | ProjectQueryAccessMemberRowsFacts
    | ProjectQueryAiCountsFacts
    | ProjectQueryPoolFacts
    | ProjectQueryMyRolesFacts
    | ProjectQueryTaskSeriesFacts
    | ProjectQueryAnnotationSeriesFacts
    | ProjectQueryDatasetIdsFacts
    | ProjectQueryJobCountsFacts
    | ProjectQueryLatestJobFacts
    | ProjectQueryBatchesFacts
    | ProjectQueryBatchCountsFacts
    | ProjectQueryActiveMembersFacts
)


class ProjectQueryDateBounds(ClosedModel):
    created_from_us: SignedMicroseconds | None
    created_to_us: SignedMicroseconds | None


class ProjectQueryProjectIds(ClosedModel):
    project_ids: list[uuid.UUID]


class ProjectQueryProjectId(ClosedModel):
    project_id: uuid.UUID


class ProjectQueryPoolId(ClosedModel):
    pool_id: uuid.UUID


class ProjectQueryAnnotationCountParameters(ProjectQueryProjectIds):
    ai_only: StrictBool


class ProjectQuerySeriesParameters(ProjectQueryProjectIds):
    bucket_ends: Annotated[list[StrictStr], Field(min_length=12, max_length=12)]


class ProjectQueryDatasetIds(ClosedModel):
    dataset_ids: list[uuid.UUID]


ProjectQueryParameters: TypeAlias = (
    ProjectQueryDateBounds
    | ProjectQueryProjectIds
    | ProjectQueryProjectId
    | ProjectQueryPoolId
    | ProjectQueryAnnotationCountParameters
    | ProjectQuerySeriesParameters
    | ProjectQueryDatasetIds
    | None
)

# Each action binds both its argument shape and its reply, including repeated reads.
PROJECT_QUERY_STEPS = {
    "list_rows": (ProjectQueryDateBounds, ProjectQueryRowsFacts),
    "count_rows": (ProjectQueryDateBounds, ProjectQueryCountFacts),
    "visible_rows": (type(None), ProjectQueryRowsFacts),
    "get_row": (type(None), ProjectQueryRowFacts),
    "actor_status": (type(None), ProjectQueryActorFacts),
    "member_access": (type(None), ProjectQueryMemberFacts),
    "access_member_rows": (type(None), ProjectQueryAccessMemberRowsFacts),
    "ai_counts": (ProjectQueryProjectIds, ProjectQueryAiCountsFacts),
    "member_count": (ProjectQueryProjectId, ProjectQueryCountFacts),
    "pool": (ProjectQueryPoolId, ProjectQueryPoolFacts),
    "my_roles": (ProjectQueryProjectIds, ProjectQueryMyRolesFacts),
    "annotation_count": (ProjectQueryAnnotationCountParameters, ProjectQueryCountFacts),
    "task_series": (ProjectQuerySeriesParameters, ProjectQueryTaskSeriesFacts),
    "annotation_series": (
        ProjectQuerySeriesParameters,
        ProjectQueryAnnotationSeriesFacts,
    ),
    "dataset_ids": (type(None), ProjectQueryDatasetIdsFacts),
    "dataset_item_count": (ProjectQueryDatasetIds, ProjectQueryCountFacts),
    "linked_task_count": (ProjectQueryDatasetIds, ProjectQueryCountFacts),
    "task_count": (type(None), ProjectQueryCountFacts),
    "job_counts": (type(None), ProjectQueryJobCountsFacts),
    "latest_job_status": (type(None), ProjectQueryLatestJobFacts),
    "batches": (type(None), ProjectQueryBatchesFacts),
    "batch_task_counts": (type(None), ProjectQueryBatchCountsFacts),
    "active_members": (type(None), ProjectQueryActiveMembersFacts),
    "projection_row": (type(None), ProjectQueryRowFacts),
    "search_rows": (type(None), ProjectQueryRowsFacts),
}
_SERIALIZATION_ACTIONS = {"ai_counts", "member_count", "pool"}
_ACCESS_ACTIONS = {"get_row", "actor_status", "member_access"}
_PROJECT_ACCESS_ACTIONS = {"get_row", "actor_status", "access_member_rows"}
PROJECT_QUERY_OPERATION_ACTIONS = {
    "list_projects": {"list_rows", "my_roles"} | _SERIALIZATION_ACTIONS,
    "query_projects": {"count_rows", "list_rows", "my_roles"} | _SERIALIZATION_ACTIONS,
    "get_project": _ACCESS_ACTIONS | _SERIALIZATION_ACTIONS | {"my_roles"},
    "get_project_access": _PROJECT_ACCESS_ACTIONS,
    "require_project_manager": _PROJECT_ACCESS_ACTIONS,
    "require_project_visible": _PROJECT_ACCESS_ACTIONS,
    "project_stats": {
        "visible_rows",
        "annotation_count",
        "task_series",
        "annotation_series",
    },
    "project_readiness": _ACCESS_ACTIONS
    | {
        "dataset_ids",
        "dataset_item_count",
        "linked_task_count",
        "task_count",
        "job_counts",
        "latest_job_status",
        "batches",
        "batch_task_counts",
        "active_members",
    },
    "get_project_projection": {"projection_row"} | _SERIALIZATION_ACTIONS,
    "search_projects": {"search_rows"},
}


class ProjectQueryStep(_ProjectQueryModel):
    protocol: Literal["aap-workspace-project-query/v1"] = Field(alias="schema")
    request: ProjectQueryRequest
    action: ProjectQueryAction
    parameters: ProjectQueryParameters
    facts: ProjectQueryFacts

    @model_validator(mode="after")
    def action_shapes_match(self):
        parameter_type, fact_type = PROJECT_QUERY_STEPS[self.action]
        # Both actions carry the same closed rows_json object; Pydantic resolves
        # that structural union to ProjectQueryRowsFacts at runtime.
        equivalent_rows_fact = (
            self.action == "access_member_rows"
            and type(self.facts) is ProjectQueryRowsFacts
        )
        if type(self.parameters) is not parameter_type or (
            type(self.facts) is not fact_type and not equivalent_rows_fact
        ):
            raise ValueError("Read action arguments/facts do not match")
        if self.action not in PROJECT_QUERY_OPERATION_ACTIONS[self.request.operation]:
            raise ValueError("Read action does not match its operation")
        if (
            isinstance(self.facts, ProjectQueryActorFacts)
            and self.facts.actor is not None
        ):
            if self.facts.actor.actor_id != self.request.admitted_actor.id:
                raise ValueError("Current account does not match the admitted actor")
        if (
            isinstance(self.facts, ProjectQueryMemberFacts)
            and self.facts.member is not None
        ):
            if (
                self.facts.member.project_id != self.request.target_id
                or self.facts.member.user_id != self.request.admitted_actor.id
            ):
                raise ValueError("Current membership does not match the actor/project")
        if isinstance(
            self.facts, (ProjectQueryAiCountsFacts, ProjectQueryMyRolesFacts)
        ):
            records = (
                self.facts.counts
                if isinstance(self.facts, ProjectQueryAiCountsFacts)
                else self.facts.memberships
            )
            ids = [record.project_id for record in records]
            if len(set(ids)) != len(ids) or not set(ids).issubset(
                self.parameters.project_ids
            ):
                raise ValueError("Grouped facts do not match the requested projects")
        return self


class ProjectQueryUnavailable(Exception):
    def __init__(self):
        super().__init__("Workspace Project query owner unavailable")


class ProjectQueryInvalidRequest(ValueError):
    def __init__(self):
        super().__init__("Invalid Workspace Project query request")


class ProjectQueryProjectionMissing(RuntimeError):
    """An accepted configuration/class command lost its required response row."""


class ProjectQueryRejected(Exception):
    def __init__(self, status_code, detail, headers=None):
        self.error = ProjectQueryError(
            status_code=status_code, detail=detail, headers=headers
        )
        super().__init__(detail)


def _decode(model, payload, failure):
    try:
        return TypeAdapter(model).validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise failure() from None


def decode_project_query_request(payload) -> ProjectQueryRequest:
    return _decode(ProjectQueryRequest, payload, ProjectQueryInvalidRequest)


def decode_project_query_policy(payload) -> ProjectQueryPolicy:
    return _decode(ProjectQueryPolicy, payload, ProjectQueryUnavailable)


def project_query_request(
    operation,
    actor_id,
    actor_role,
    *,
    target_id=None,
    filters: Mapping[str, object] | None = None,
    pagination=None,
    projection_evidence=None,
    request_id=None,
):
    try:
        if filters is not None:
            filters = {
                key: str(value) if isinstance(value, uuid.UUID) else value
                for key, value in filters.items()
            }
        if isinstance(pagination, ClosedModel):
            pagination = pagination.model_dump(mode="json")
        if isinstance(projection_evidence, ClosedModel):
            projection_evidence = projection_evidence.model_dump(mode="json")
        elif projection_evidence is not None:
            projection_evidence = {
                key: str(value) if isinstance(value, uuid.UUID) else value
                for key, value in projection_evidence.items()
            }
        opaque = (
            json.dumps(
                filters,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if filters is not None
            else None
        )
        return decode_project_query_request(
            {
                "schema": PROJECT_QUERY_PROTOCOL,
                "request_id": str(request_id or uuid.uuid4()),
                "operation": operation,
                "admitted_actor": {"id": str(actor_id), "role": actor_role},
                "target_id": str(target_id) if target_id is not None else None,
                "filters_ascii": opaque,
                "pagination": pagination,
                "projection_evidence": projection_evidence,
            }
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectQueryInvalidRequest() from None


def decode_project_query_filters(request):
    request = _decode(ProjectQueryRequest, request, ProjectQueryUnavailable)
    model = (
        ProjectSearchFilters
        if request.operation == "search_projects"
        else ProjectQueryFilters
    )
    try:
        value = json.loads(
            request.filters_ascii,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        filters = TypeAdapter(model).validate_json(
            json.dumps(value, ensure_ascii=True, allow_nan=False)
        )
        canonical = json.dumps(
            filters.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != request.filters_ascii:
            raise ValueError("Filters must use canonical closed JSON")
        return filters
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectQueryUnavailable() from None


def decode_project_query_step(request, action, parameters, payload):
    request = _decode(ProjectQueryRequest, request, ProjectQueryUnavailable)
    step = _decode(ProjectQueryStep, payload, ProjectQueryUnavailable)
    expected = (
        parameters.model_dump(mode="json")
        if isinstance(parameters, ClosedModel)
        else parameters
    )
    actual = (
        step.parameters.model_dump(mode="json") if step.parameters is not None else None
    )
    if step.request != request or step.action != action or actual != expected:
        raise ProjectQueryUnavailable()
    return step


def project_query_schema():
    """Publish action-specific fact/argument pairs as standard JSON Schema."""
    schema = TypeAdapter(
        ProjectQueryRequest | ProjectQueryPolicy | ProjectQueryReply | ProjectQueryStep
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PROJECT_QUERY_PROTOCOL
    conditions = []
    for action, (parameter_type, fact_type) in PROJECT_QUERY_STEPS.items():
        parameters = (
            {"type": "null"}
            if parameter_type is type(None)
            else {"$ref": f"#/$defs/{parameter_type.__name__}"}
        )
        conditions.append(
            {
                "if": {
                    "properties": {"action": {"const": action}},
                    "required": ["action"],
                },
                "then": {
                    "properties": {
                        "parameters": parameters,
                        "facts": {"$ref": f"#/$defs/{fact_type.__name__}"},
                    }
                },
            }
        )
    for operation, actions in PROJECT_QUERY_OPERATION_ACTIONS.items():
        conditions.append(
            {
                "if": {
                    "properties": {
                        "request": {
                            "properties": {"operation": {"const": operation}},
                            "required": ["operation"],
                        }
                    },
                    "required": ["request"],
                },
                "then": {"properties": {"action": {"enum": sorted(actions)}}},
            }
        )
    schema["$defs"]["ProjectQueryStep"]["allOf"] = conditions
    schema["x-project-row-columns"] = list(PROJECT_ROW_COLUMNS)
    schema["x-json-text-columns"] = list(PROJECT_JSON_COLUMNS)
    schema["x-project-enrichment-columns"] = list(PROJECT_ENRICHMENT_COLUMNS)
    schema["x-bigint-codec"] = (
        "canonical decimal strings; persisted int4 counters remain signed numbers"
    )
    return schema
