"""Portable transactional Dataset unlink command and receipt contract."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    TypeAdapter,
    model_validator,
)

from aap_assets_cleanup.contract import CleanupBucket
from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace_contract.wire import _json_bytes

DATASET_UNLINK_PROTOCOL = "aap-dataset-unlink/v1"
DATASET_UNLINK_PREVIEW_PROTOCOL = "aap-dataset-unlink-preview/v1"
DATASET_UNLINK_SCHEMA = "urn:aap:contract:dataset-unlink:v1"
NATIVE_SCHEMA = "aap_workspace_v1"

Count = Annotated[int, Field(strict=True, ge=0)]


class DatasetUnlinkFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetUnlinkCommand(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    project_id: UUID
    cleanup_bucket: CleanupBucket
    audit_context: ProjectDeletionAuditContext | None = None


class DatasetUnlinkPreviewCommand(_Value):
    actor_id: UUID
    dataset_id: UUID
    project_id: UUID


class DatasetUnlinkPreview(_Value):
    will_delete_tasks: Count
    will_delete_annotations: Count
    will_delete_batches: Count


class DatasetUnlinkReceipt(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    project_id: UUID
    association_id: UUID
    cleanup_bucket: CleanupBucket
    audit_id: Annotated[int, Field(strict=True, gt=0)]
    deleted_tasks: Count
    deleted_annotations: Count
    deleted_batches: Count
    deleted_batch_ids: tuple[UUID, ...]
    soft: Annotated[StrictBool, Field(json_schema_extra={"const": False})]

    @model_validator(mode="after")
    def check_batch_count(self):
        if (
            self.soft is not False
            or self.deleted_batches != len(set(self.deleted_batch_ids))
            or self.deleted_batches != len(self.deleted_batch_ids)
        ):
            raise ValueError("Unlink batch count does not match its frozen identities")
        return self

    def public_result(self) -> dict:
        return self.model_dump(
            mode="json",
            include={
                "deleted_tasks",
                "deleted_annotations",
                "deleted_batches",
                "deleted_batch_ids",
                "soft",
            },
        )


class _Failure(_Value):
    code: str


class _Reply(_Value):
    schema_version: Literal[
        "aap-dataset-unlink/v1", "aap-dataset-unlink-preview/v1"
    ] = Field(alias="schema")
    ok: StrictBool
    data: dict | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful unlink must contain only its data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed unlink must contain only its stable error")
        return self


class _UnlinkSuccess(_Value):
    schema_version: Literal["aap-dataset-unlink/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: DatasetUnlinkReceipt


class _PreviewSuccess(_Value):
    schema_version: Literal["aap-dataset-unlink-preview/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: DatasetUnlinkPreview


class _FailureReply(_Value):
    schema_version: Literal[
        "aap-dataset-unlink/v1", "aap-dataset-unlink-preview/v1"
    ] = Field(alias="schema")
    ok: Literal[False]
    error: _Failure


def _payload_value(payload: object) -> object:
    if isinstance(payload, (bytes, str)):
        return json.loads(_json_bytes(payload))
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json", by_alias=True)
    if isinstance(payload, Mapping):
        return dict(payload)
    return payload


def decode_unlink_command(
    payload: DatasetUnlinkCommand | Mapping[str, object] | BaseModel | bytes | str,
) -> DatasetUnlinkCommand:
    return DatasetUnlinkCommand.model_validate(_payload_value(payload))


def decode_unlink_preview_command(
    payload: DatasetUnlinkPreviewCommand
    | Mapping[str, object]
    | BaseModel
    | bytes
    | str,
) -> DatasetUnlinkPreviewCommand:
    return DatasetUnlinkPreviewCommand.model_validate(_payload_value(payload))


def decode_unlink_reply(
    payload: object, *, preview: bool = False
) -> DatasetUnlinkReceipt | DatasetUnlinkPreview:
    reply = _Reply.model_validate(_payload_value(payload))
    expected = DATASET_UNLINK_PREVIEW_PROTOCOL if preview else DATASET_UNLINK_PROTOCOL
    if reply.schema_version != expected:
        raise ValueError("Unexpected Dataset unlink contract")
    if not reply.ok:
        assert reply.error is not None
        raise DatasetUnlinkFailure(reply.error.code)
    assert reply.data is not None
    model = DatasetUnlinkPreview if preview else DatasetUnlinkReceipt
    return model.model_validate(reply.data)


def dataset_unlink_schema() -> dict[str, Any]:
    """Return the portable unlink/preview descriptor for artifact generation."""
    schema = TypeAdapter(
        DatasetUnlinkCommand
        | DatasetUnlinkPreviewCommand
        | _UnlinkSuccess
        | _PreviewSuccess
        | _FailureReply
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = DATASET_UNLINK_SCHEMA
    schema["title"] = "Workspace transactional dataset unlink v1"
    schema["oneOf"] = schema.pop("anyOf")
    unlink_signature = (
        f"{NATIVE_SCHEMA}.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)"
    )
    preview_signature = f"{NATIVE_SCHEMA}.preview_dataset_unlink(uuid, uuid, uuid)"
    schema["x-postgresql-functions"] = [unlink_signature, preview_signature]
    schema["x-native-capabilities"] = {
        "protocols": [DATASET_UNLINK_PROTOCOL, DATASET_UNLINK_PREVIEW_PROTOCOL],
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": "unlink_dataset",
                "signature": unlink_signature,
                "arguments": [
                    {"name": "operation_id", "postgres_type": "uuid"},
                    {"name": "actor_id", "postgres_type": "uuid"},
                    {"name": "dataset_id", "postgres_type": "uuid"},
                    {"name": "project_id", "postgres_type": "uuid"},
                    {"name": "cleanup_bucket", "postgres_type": "text"},
                    {
                        "name": "audit_context",
                        "postgres_type": "jsonb",
                        "nullable": True,
                    },
                ],
                "result_postgres_type": "jsonb",
                "volatility": "VOLATILE",
                "transaction_semantics": (
                    "Execute on the caller's existing active transaction; native unlink "
                    "retains its current locks, deletion, audit, cleanup preparation, "
                    "and receipt behavior. The caller owns commit and rollback."
                ),
            },
            {
                "name": "preview_dataset_unlink",
                "signature": preview_signature,
                "arguments": [
                    {"name": "actor_id", "postgres_type": "uuid"},
                    {"name": "dataset_id", "postgres_type": "uuid"},
                    {"name": "project_id", "postgres_type": "uuid"},
                ],
                "result_postgres_type": "jsonb",
                "volatility": "VOLATILE",
                "transaction_semantics": (
                    "Execute on the caller's existing active transaction. Preview locks "
                    "the current project authority, so it must not use a read-only "
                    "transaction. The caller owns transaction completion."
                ),
            },
        ],
        "authorization_boundary": (
            "The existing Native functions retain account and project authority checks; "
            "this portable binding adds no authorization policy."
        ),
        "transaction_boundary": "The caller owns the active transaction, commit, and rollback.",
    }
    return schema


__all__ = [
    "DATASET_UNLINK_PREVIEW_PROTOCOL",
    "DATASET_UNLINK_PROTOCOL",
    "DATASET_UNLINK_SCHEMA",
    "NATIVE_SCHEMA",
    "DatasetUnlinkCommand",
    "DatasetUnlinkFailure",
    "DatasetUnlinkPreview",
    "DatasetUnlinkPreviewCommand",
    "DatasetUnlinkReceipt",
    "dataset_unlink_schema",
    "decode_unlink_command",
    "decode_unlink_preview_command",
    "decode_unlink_reply",
]
