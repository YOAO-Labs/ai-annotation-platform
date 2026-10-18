"""Strict language-neutral bindings for transactional Dataset unlink."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from aap_audit_contract import ProjectDeletionAuditContext
from aap_assets_cleanup.contract import CleanupBucket

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


def decode_unlink_reply(
    payload: object, *, preview: bool = False
) -> DatasetUnlinkReceipt | DatasetUnlinkPreview:
    reply = _Reply.model_validate(payload)
    expected = "aap-dataset-unlink-preview/v1" if preview else "aap-dataset-unlink/v1"
    if reply.schema_version != expected:
        raise ValueError("Unexpected Dataset unlink contract")
    if not reply.ok:
        assert reply.error is not None
        raise DatasetUnlinkFailure(reply.error.code)
    model = DatasetUnlinkPreview if preview else DatasetUnlinkReceipt
    return model.model_validate(reply.data)
