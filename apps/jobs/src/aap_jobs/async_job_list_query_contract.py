"""Portable contract for the Jobs-owned async-job list query."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter

ASYNC_JOB_LIST_QUERY_PROTOCOL = "aap-jobs-async-job-list-query/v1"
ASYNC_JOB_LIST_PAGE_FUNCTION = "aap_jobs_v1.async_job_list_page"
ASYNC_JOB_LIST_TOTAL_FUNCTION = "aap_jobs_v1.async_job_list_total"
ASYNC_JOB_LIST_HELPER_FUNCTION = "aap_jobs_v1._async_job_list_rows"

ASYNC_JOB_STATUSES = Literal["pending", "running", "completed", "failed", "cancelled"]
ASYNC_JOB_LIST_ROW_COLUMNS = (
    "id",
    "kind",
    "project_id",
    "retained_project_id",
    "user_id",
    "status",
    "progress_pct",
    "payload",
    "result",
    "error_message",
    "celery_task_id",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
)
ASYNC_JOB_LIST_ROW_TYPES = {
    "id": "uuid",
    "kind": "character varying(40)",
    "project_id": "uuid",
    "retained_project_id": "uuid",
    "user_id": "uuid",
    "status": "character varying(20)",
    "progress_pct": "integer",
    "payload": "jsonb",
    "result": "jsonb",
    "error_message": "text",
    "celery_task_id": "character varying(64)",
    "started_at": "timestamp with time zone",
    "completed_at": "timestamp with time zone",
    "created_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
}
ASYNC_JOB_LIST_FUNCTION_ROW_TYPES = {
    **ASYNC_JOB_LIST_ROW_TYPES,
    "kind": "character varying",
    "status": "character varying",
    "celery_task_id": "character varying",
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AsyncJobListQuery(_ClosedModel):
    """One page/count input, with caller-normalized filters and Identity snapshot."""

    actor_id: UUID
    platform_role: StrictStr
    statuses: list[ASYNC_JOB_STATUSES] = Field(default_factory=list)
    kinds: list[StrictStr] = Field(default_factory=list)
    project_id: UUID | None = None
    search_pattern: StrictStr | None = Field(default=None, max_length=202)
    limit: StrictInt = Field(ge=1, le=200)
    offset: StrictInt = Field(ge=0)


class AsyncJobListRow(_ClosedModel):
    """Fixed row returned to hydrate the existing API AsyncJob ORM mapper."""

    id: UUID
    kind: StrictStr = Field(max_length=40)
    project_id: UUID | None
    retained_project_id: UUID | None
    user_id: UUID | None
    status: StrictStr = Field(max_length=20)
    progress_pct: StrictInt
    payload: Any
    result: Any
    error_message: StrictStr | None
    celery_task_id: StrictStr | None = Field(max_length=64)
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def async_job_list_query_schema() -> dict[str, Any]:
    schema = TypeAdapter(AsyncJobListQuery).json_schema()
    schema.setdefault("$defs", {})["AsyncJobListRow"] = TypeAdapter(
        AsyncJobListRow
    ).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": ASYNC_JOB_LIST_QUERY_PROTOCOL,
            "title": "AAP Jobs async-job list query",
            "$comment": (
                "The API reads Identity actor status once before calling Jobs and "
                "passes the trusted role snapshot. Each page and total statement "
                "also reads current Identity facts only for membership validation. "
                "They remain separate ordered statements in the caller's transaction; "
                "ORM autoflush and identity refresh are preserved."
            ),
            "x-postgresql-functions": {
                ASYNC_JOB_LIST_PAGE_FUNCTION: {
                    "arguments": "uuid, text, text[], text[], uuid, text, integer, integer",
                    "returns": "TABLE("
                    + ", ".join(
                        f"{name} {ASYNC_JOB_LIST_FUNCTION_ROW_TYPES[name]}"
                        for name in ASYNC_JOB_LIST_ROW_COLUMNS
                    )
                    + ")",
                },
                ASYNC_JOB_LIST_TOTAL_FUNCTION: {
                    "arguments": "uuid, text, text[], text[], uuid, text",
                    "returns": "bigint",
                },
                ASYNC_JOB_LIST_HELPER_FUNCTION: {
                    "arguments": "uuid, text, text[], text[], uuid, text",
                    "returns": "SETOF public.async_jobs (private helper)",
                },
            },
            "x-row-columns": list(ASYNC_JOB_LIST_ROW_COLUMNS),
            "x-row-types": dict(ASYNC_JOB_LIST_ROW_TYPES),
            "x-row-key": ["id"],
            "x-result": {"row_schema": "#/$defs/AsyncJobListRow"},
            "x-identity-snapshot": {
                "source": "IdentityAccountLockData.read_actor_status",
                "preflight_reads_per_list_request": 1,
                "passed_fields": ["actor_id", "platform_role"],
                "inactive_actor": "return empty items and total before Jobs call",
                "same_statement_membership_facts": (
                    "Each page/total statement reads current actor active/role facts "
                    "through aap_identity_v1.read_actor_facts(uuid), matching the "
                    "old membership predicate without replacing the preflight snapshot."
                ),
            },
            "x-policy": {
                "owner": "non-super-admin rows require matching user_id",
                "super_admin": "all existing project scopes; global jobs retain kind policy",
                "global_unscoped_kinds": ["audit_archive", "dataset_import"],
                "project_scope": "coalesce(project_id, retained_project_id)",
                "export": "reviewer membership or manager-owned project; super-admin remains global",
                "membership_identity_facts": (
                    "Current Identity active/known-role/viewer-pairing facts gate only "
                    "the membership arm. The preflight role snapshot still selects the "
                    "super-admin and manager-owned branches."
                ),
                "workspace_facts": [
                    "aap_workspace_v1.actor_project_memberships(uuid)",
                    "aap_workspace_project_query_v1.project_search_scope(uuid,text)",
                ],
                "unknown_kinds": "preserved as open strings and only visible under normal scope rules",
            },
            "x-owner-capabilities": {
                "aap_identity_v1.read_actor_facts(uuid)": {
                    "grant_to": "jobs_owner",
                    "use": "membership predicate only; no direct Identity table access",
                }
            },
            "x-filter-semantics": {
                "empty_statuses_or_kinds": "no filter",
                "project_id": "matches coalesced current or retained project scope",
                "search": {
                    "normalization": "Python str.strip(); no filter when empty",
                    "pattern": "one unescaped %text% ILIKE pattern",
                    "jsonb_text_keys": [
                        "prompt",
                        "batch_display_id",
                        "task_display_id",
                        "model_key",
                        "ml_backend_name",
                        "error_type",
                    ],
                    "non_object_payload": "each ->> lookup yields no match, as in the ORM query",
                },
            },
            "x-page-semantics": {
                "order": ["created_at DESC", "id DESC"],
                "page_then_total": True,
                "separate_statements": True,
                "empty_page_still_counts": True,
                "limit": {"minimum": 1, "maximum": 200},
                "offset": {"minimum": 0},
            },
            "x-borrowed-transaction": {
                "commit": False,
                "rollback": False,
                "autoflush": "preserve the caller setting at both ORM query boundaries",
                "page_identity_map": "populate_existing=True",
                "workspace_project_enrichment": "API-owned after page and total",
            },
            "x-security": {
                "functions": "stable security_definer",
                "search_path": ["pg_catalog", "pg_temp"],
                "public_execute": False,
                "api_execute": [
                    ASYNC_JOB_LIST_PAGE_FUNCTION,
                    ASYNC_JOB_LIST_TOTAL_FUNCTION,
                ],
                "private_helper_execute": False,
                "api_async_jobs_revoke": False,
            },
        }
    )
    return schema


def async_job_list_query_sql() -> str:
    from importlib.resources import files

    return (
        files("aap_jobs")
        .joinpath("sql/0326_jobs_list_query.sql")
        .read_text(encoding="utf-8")
    )
