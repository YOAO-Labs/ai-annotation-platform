"""Closed command and reply contract for Audit partition maintenance."""

from __future__ import annotations

from datetime import date, datetime
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .append_contract import JSONValue

MAINTENANCE_PROTOCOL = "aap-audit-maintenance/v1"
MAINTENANCE_FUNCTION = "aap_audit_v1.maintenance_command"
_PARTITION_NAME = re.compile(r"^audit_logs_y([0-9]{4})m(0[1-9]|1[0-2])$")
_MAX_OID = (1 << 32) - 1
_MAX_POSTGRES_BIGINT = (1 << 63) - 1


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )


def _calendar_date(value: object, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO calendar date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO calendar date") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical ISO calendar date")
    return parsed


def _next_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


class AuditPartition(_ClosedModel):
    """Frozen identity and UTC date range for one monthly audit partition."""

    name: StrictStr = Field(pattern=_PARTITION_NAME.pattern)
    range_from: date = Field(alias="from")
    range_to: date = Field(alias="to")
    relation_oid: StrictInt = Field(alias="oid", gt=0, le=_MAX_OID)

    @field_validator("range_from", "range_to", mode="before")
    @classmethod
    def canonical_date(cls, value: object, info) -> date:
        return _calendar_date(value, info.field_name)

    @model_validator(mode="after")
    def monthly_identity(self) -> AuditPartition:
        match = _PARTITION_NAME.fullmatch(self.name)
        if match is None:
            raise ValueError("partition name is not a monthly audit partition")
        year, month = (int(part) for part in match.groups())
        try:
            range_to = _next_month(self.range_from)
        except OverflowError:
            raise ValueError(
                "partition range exceeds supported calendar dates"
            ) from None
        if (
            self.range_from.day != 1
            or self.range_from != date(year, month, 1)
            or self.range_to != range_to
        ):
            raise ValueError("partition name and monthly date range do not match")
        return self

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "from": self.range_from.isoformat(),
            "to": self.range_to.isoformat(),
            "oid": self.relation_oid,
        }


class _Command(_ClosedModel):
    protocol: Literal[MAINTENANCE_PROTOCOL] = Field(alias="schema")


class _ListPartitionsRequest(_Command):
    operation: Literal["partitions"]


class _EnsurePartitionRequest(_Command):
    operation: Literal["ensure"]
    month: date

    @field_validator("month", mode="before")
    @classmethod
    def first_day(cls, value: object) -> date:
        parsed = _calendar_date(value, "month")
        if parsed.day != 1:
            raise ValueError("month must be the first day of a month")
        return parsed


class _SnapshotPartitionRequest(_Command):
    operation: Literal["snapshot"]
    partition: AuditPartition


class _DropPartitionRequest(_Command):
    operation: Literal["drop"]
    partition: AuditPartition


class _ArchiveEventRequest(_Command):
    operation: Literal["archive_event"]
    partition: AuditPartition
    row_count: StrictInt = Field(ge=0, le=_MAX_POSTGRES_BIGINT)
    object_key: StrictStr = Field(pattern=r"^[0-9]{4}/(0[1-9]|1[0-2])\.jsonl\.gz$")

    @model_validator(mode="after")
    def archive_identity(self) -> _ArchiveEventRequest:
        expected = (
            f"{self.partition.range_from.year}/"
            f"{self.partition.range_from.month:02d}.jsonl.gz"
        )
        if self.object_key != expected:
            raise ValueError("archive key does not match the partition month")
        return self


class _RefreshDailyRequest(_Command):
    operation: Literal["refresh_daily"]


_MaintenanceRequest = Annotated[
    Union[
        _ListPartitionsRequest,
        _EnsurePartitionRequest,
        _SnapshotPartitionRequest,
        _DropPartitionRequest,
        _ArchiveEventRequest,
        _RefreshDailyRequest,
    ],
    Field(discriminator="operation"),
]


class _PartitionsData(_ClosedModel):
    partitions: list[AuditPartition]

    @field_validator("partitions", mode="before")
    @classmethod
    def exact_partition_wire(cls, value: object) -> object:
        if not isinstance(value, list) or any(
            not isinstance(partition, Mapping)
            or set(partition) != {"name", "from", "to", "oid"}
            for partition in value
        ):
            raise ValueError("partition wire must contain exactly name/from/to/oid")
        return value

    @model_validator(mode="after")
    def unique_partitions(self) -> _PartitionsData:
        names = [partition.name for partition in self.partitions]
        oids = [partition.relation_oid for partition in self.partitions]
        if len(names) != len(set(names)) or len(oids) != len(set(oids)):
            raise ValueError("partition reply contains duplicate identities")
        return self


class _EnsureData(_ClosedModel):
    created: StrictBool


class _SnapshotData(_ClosedModel):
    rows: list[dict[str, JSONValue]]


class _EmptyData(_ClosedModel):
    pass


class _MaintenanceReply(_ClosedModel):
    protocol: Literal[MAINTENANCE_PROTOCOL] = Field(alias="schema")
    ok: Literal[True]
    data: Union[_PartitionsData, _EnsureData, _SnapshotData, _EmptyData]


class AuditMaintenanceUnavailable(RuntimeError):
    """The Audit owner returned a malformed or foreign maintenance reply."""


def decode_maintenance_reply(value: Any) -> _MaintenanceReply:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            raw = json.loads(value)
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise ValueError("reply must be a JSON object")
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("reply must contain exactly schema/ok/data")
        return _MaintenanceReply.model_validate(raw)
    except (json.JSONDecodeError, UnicodeError, ValidationError, ValueError, TypeError):
        raise AuditMaintenanceUnavailable() from None


def audit_maintenance_schema() -> dict[str, Any]:
    """Return the exact closed JSON request/reply and owner capability schema."""
    request_schema = TypeAdapter(_MaintenanceRequest).json_schema()
    reply_schema = TypeAdapter(_MaintenanceReply).json_schema()
    definitions = dict(request_schema.pop("$defs", {}))
    definitions.update(reply_schema.pop("$defs", {}))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": MAINTENANCE_PROTOCOL,
        "title": "Audit partition maintenance commands",
        "$comment": (
            "Partition names encode their first-day UTC month and next-month upper "
            "bound. Ensure accepts only first-day dates; archive keys must match "
            "the partition month. Commands remain in the caller transaction. "
            "Snapshot locks the identified partition ACCESS EXCLUSIVE; drop "
            "requires that lock in the same transaction. The archive object is "
            "written before drop."
        ),
        "$defs": definitions,
        "oneOf": request_schema["oneOf"],
        "x-aap-replies": reply_schema,
        "x-postgresql-functions": {
            f"{MAINTENANCE_FUNCTION}(jsonb)": {
                "argument_types": ["jsonb"],
                "returns": "jsonb",
                "owner": "audit",
                "security_definer": True,
                "fixed_targets": [
                    "public.audit_logs monthly partitions",
                    "public.mv_audit_bi_daily",
                ],
                "caller_transaction_required": True,
                "snapshot_lock": "ACCESS EXCLUSIVE",
                "drop_requires_same_transaction_lock": True,
            }
        },
    }
