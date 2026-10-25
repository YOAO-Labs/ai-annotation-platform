"""Borrow the API request transaction for fixed Inference pipeline steps."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_inference.pipeline_contract import PipelineRejected

PIPELINE_SCHEMA = "aap_inference_pipelines_v1"

_STEP = text(
    f"SELECT {PIPELINE_SCHEMA}.pipeline_step("
    "CAST(:request AS jsonb), CAST(:action AS text), CAST(:args AS jsonb), "
    "CAST(:columns AS text[]), CAST(:values AS text[]))"
)

_AUTOFLUSH_ACTIONS = frozenset(
    {
        "organization_ids",
        "pipeline_get",
        "pipeline_list",
        "backend_get",
        "backend_pool",
        "backend_enabled",
        "project_query",
        "default_clear",
        "source_usage_increment",
    }
)


def _json_text(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        from aap_inference.pipeline_contract import PipelineInvalidRequest

        raise PipelineInvalidRequest() from None


class InferencePipelinesData:
    """Expose the fixed SQL capability without taking transaction ownership."""

    def __init__(self, db):
        self.db = db

    @property
    def autoflush(self) -> bool:
        return bool(self.db.autoflush)

    @property
    def owns_commit(self) -> bool:
        return False

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False):
        # The API request owns this SQLAlchemy transaction. The flag is part of
        # the portable data protocol; PostgreSQL cannot impose a nested mode on
        # an already active request transaction.
        del readonly
        yield self

    async def step(
        self,
        request,
        action: str,
        args=None,
        columns=(),
        values=(),
    ):
        if action in _AUTOFLUSH_ACTIONS and self.db.autoflush:
            await self.db.flush()
        elif action in {"pipeline_insert", "pipeline_update"}:
            # These mirror the explicit db.flush() at the end of the legacy
            # create/update/apply paths, including when autoflush is disabled.
            await self.db.flush()

        parameters = {
            "request": _json_text(request),
            "action": action,
            "args": _json_text({} if args is None else args),
            "columns": list(columns),
            "values": list(values),
        }
        with self.db.no_autoflush:
            try:
                return await self.db.scalar(_STEP, parameters)
            except DBAPIError as error:
                sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
                    error.orig, "pgcode", None
                )
                phase = (args or {}).get("phase")
                reason = {
                    "lock_actor_share": "account_busy",
                    "lock_project_key_share": "project_busy",
                    "lock_member_share": "membership_busy",
                }.get(phase)
                if (
                    action == "project_access"
                    and reason is not None
                    and sqlstate in {"55P03", "40001", "40P01"}
                ):
                    raise PipelineRejected(
                        409, {"reason": reason}, rollback=True
                    ) from None
                raise
