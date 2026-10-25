"""Fixed Jobs queries for current cross-frame and preannotation consumers."""

from datetime import datetime
from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter

from .lifecycle_contract import (
    ASYNC_JOB_FUNCTION_ROW_TYPES,
    ASYNC_JOB_ROW_COLUMNS,
    AsyncJobLifecycleRow,
)

JOBS_DOMAIN_QUERY_PROTOCOL = "aap-jobs-domain-queries/v1"
JOBS_DOMAIN_QUERY_FUNCTIONS = {
    "lock_cross_frame_singleflight": "aap_jobs_v1.lock_cross_frame_singleflight",
    "active_cross_frame_job": "aap_jobs_v1.active_cross_frame_job",
    "cross_frame_page": "aap_jobs_v1.cross_frame_job_page",
    "cross_frame_count": "aap_jobs_v1.cross_frame_job_count",
    "latest_batch_prediction_times": "aap_jobs_v1.latest_batch_prediction_times",
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CrossFrameKeyRequest(_ClosedModel):
    actor_id: UUID
    singleflight_key: StrictStr


class CrossFrameSceneRequest(_ClosedModel):
    actor_id: UUID
    scene_id: StrictStr


class CrossFramePageRequest(CrossFrameSceneRequest):
    limit: StrictInt = Field(ge=1, le=50)


class LatestPredictionTimesRequest(_ClosedModel):
    project_ids: list[UUID]


class LatestPredictionTime(_ClosedModel):
    project_id: UUID
    latest_created_at: datetime


class JobCount(_ClosedModel):
    count: StrictInt = Field(ge=0, le=2**63 - 1)


def jobs_domain_query_schema():
    row_return = (
        "TABLE("
        + ", ".join(
            f"{name} {ASYNC_JOB_FUNCTION_ROW_TYPES[name]}"
            for name in ASYNC_JOB_ROW_COLUMNS
        )
        + ")"
    )
    functions = {
        "lock_cross_frame_singleflight": ("uuid, text", "void", "volatile"),
        "active_cross_frame_job": ("uuid, text", row_return, "stable"),
        "cross_frame_page": ("uuid, text, integer", row_return, "stable"),
        "cross_frame_count": ("uuid, text", "bigint", "stable"),
        "latest_batch_prediction_times": (
            "uuid[]",
            "TABLE(project_id uuid, latest_created_at timestamp with time zone)",
            "stable",
        ),
    }
    models = (
        CrossFrameKeyRequest,
        CrossFrameSceneRequest,
        CrossFramePageRequest,
        LatestPredictionTimesRequest,
        LatestPredictionTime,
        JobCount,
        AsyncJobLifecycleRow,
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": JOBS_DOMAIN_QUERY_PROTOCOL,
        "$defs": {model.__name__: TypeAdapter(model).json_schema() for model in models},
        "x-postgresql-functions": {
            JOBS_DOMAIN_QUERY_FUNCTIONS[name]: {
                "arguments": arguments,
                "returns": returns,
                "volatility": volatility,
            }
            for name, (arguments, returns, volatility) in functions.items()
        },
        "x-row-columns": list(ASYNC_JOB_ROW_COLUMNS),
        "x-row-types": dict(ASYNC_JOB_FUNCTION_ROW_TYPES),
        "x-query-semantics": {
            "cross_frame_kind": "point_cloud_cross_frame",
            "active_statuses": ["pending", "running"],
            "json_filters": "native JSONB ->> text comparison; no UUID or JSON normalization",
            "order": "created_at DESC; no added tie breaker",
            "page_and_count": "separate statements; count ignores the page limit",
            "latest_prediction": "batch_predict; current project_id only; max(created_at)",
            "authorization": "caller retains current actor/task/project visibility checks",
            "singleflight": "lock before the active lookup; original advisory namespace and transaction lifetime",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "lock_autoflush": False,
            "read_autoflush": "original caller setting",
            "hydration": "original ORM identity and default loaded-field cache behavior",
        },
        "x-security": "fixed Jobs definer functions; pg_catalog,pg_temp; PUBLIC revoked",
    }


def jobs_domain_query_sql():
    return files("aap_jobs").joinpath("sql/0329_jobs_domain_queries.sql").read_text()
