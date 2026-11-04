"""Portable contract for Audit's published daily aggregate relation."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
)

DAILY_PROJECTION_PROTOCOL = "aap-audit-daily-projection/v1"
AUDIT_DAILY_FACTS_RELATION = "aap_audit_v1.audit_daily_facts"
AUDIT_DAILY_FACTS_ROW_KEY = (
    "day",
    "action",
    "target_type",
    "actor_role",
    "status_family",
)
AUDIT_DAILY_FACTS_COLUMNS = (
    "day",
    "action",
    "target_type",
    "actor_role",
    "status_family",
    "event_count",
)
AUDIT_DAILY_FACTS_COLUMN_TYPES: dict[str, str] = {
    "day": "date",
    "action": "character varying(64)",
    "target_type": "character varying",
    "actor_role": "character varying",
    "status_family": "smallint",
    "event_count": "bigint",
}
AUDIT_DAILY_FACTS_NULLABLE_COLUMNS: tuple[str, ...] = ()
AUDIT_DAILY_REFRESH_FUNCTION = "aap_audit_v1.refresh_audit_bi_mv"

_MAX_POSTGRES_BIGINT = (1 << 63) - 1
_CANONICAL_BIGINT_PATTERN = r"^(?:0|[1-9][0-9]*)$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class AuditDailyFact(_ClosedModel):
    """Lossless JSON representation of a row in ``audit_daily_facts``."""

    day: StrictStr = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        json_schema_extra={"format": "date"},
    )
    action: StrictStr = Field(max_length=64)
    target_type: StrictStr = Field(max_length=32)
    actor_role: StrictStr = Field(max_length=32)
    status_family: StrictInt = Field(ge=-32768, le=32767)
    event_count: StrictStr = Field(
        pattern=_CANONICAL_BIGINT_PATTERN,
        max_length=19,
        json_schema_extra={"format": "int64-decimal"},
    )

    @field_validator("day", mode="before")
    @classmethod
    def iso_day(cls, value: object) -> str:
        if isinstance(value, datetime):
            raise ValueError("day must be a UTC calendar date")
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str):
            raise ValueError("day must be an ISO calendar date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("day must be an ISO calendar date") from None
        if parsed.isoformat() != value:
            raise ValueError("day must be a canonical ISO calendar date")
        return value

    @field_validator("event_count", mode="before")
    @classmethod
    def postgres_bigint_count(cls, value: object) -> str:
        if isinstance(value, bool):
            raise ValueError("event_count must be a PostgreSQL bigint")
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) or not re.fullmatch(
            _CANONICAL_BIGINT_PATTERN, value
        ):
            raise ValueError("event_count must be a canonical decimal string")
        parsed = int(value)
        if not 0 <= parsed <= _MAX_POSTGRES_BIGINT:
            raise ValueError("event_count exceeds PostgreSQL bigint range")
        return value


def audit_daily_projection_schema() -> dict[str, Any]:
    """Return the fixed daily relation and refresh-capability descriptor."""

    row_schema = TypeAdapter(AuditDailyFact).json_schema()
    definitions = dict(row_schema.pop("$defs", {}))
    definitions["AuditDailyFact"] = row_schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": DAILY_PROJECTION_PROTOCOL,
        "title": "Audit daily aggregate projection",
        "$comment": (
            "Read-only daily aggregate of completed UTC days. The caller retains "
            "its existing authorization, month boundaries, raw-tail mixing and "
            "transaction. Event counts use canonical decimal strings in JSON."
        ),
        "$defs": definitions,
        "anyOf": [{"$ref": "#/$defs/AuditDailyFact"}],
        "x-postgresql-relations": {
            AUDIT_DAILY_FACTS_RELATION: {
                "columns": list(AUDIT_DAILY_FACTS_COLUMNS),
                "column_types": AUDIT_DAILY_FACTS_COLUMN_TYPES,
                "nullable_columns": list(AUDIT_DAILY_FACTS_NULLABLE_COLUMNS),
                "row": {"$ref": "#/$defs/AuditDailyFact"},
                "row_key": list(AUDIT_DAILY_FACTS_ROW_KEY),
                "owner": "audit",
                "read_only": True,
                "security_barrier": True,
            }
        },
        "x-postgresql-functions": {
            f"{AUDIT_DAILY_REFRESH_FUNCTION}()": {
                "argument_types": [],
                "returns": "void",
                "owner": "audit",
                "required_owner_database_privileges": ["TEMPORARY"],
                "security_definer": True,
                "fixed_target": "public.mv_audit_bi_daily",
                "concurrent": True,
                "caller_commit_required": True,
            }
        },
    }
