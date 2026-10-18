"""Strict language-neutral contract for Assets source deletion."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from aap_audit_contract import ProjectDeletionAuditContext
from aap_assets_cleanup.contract import CleanupBucket

Count = Annotated[int, Field(strict=True, ge=0)]


class DatasetSourceDeletionFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetSourceDeletionCommand(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID | None = None
    cleanup_bucket: CleanupBucket
    audit_context: ProjectDeletionAuditContext | None = None


class DatasetSourceDeletionReceipt(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID | None
    cleanup_bucket: CleanupBucket
    audit_id: Annotated[int, Field(strict=True, gt=0)]
    deleted_datasets: Annotated[int, Field(strict=True, ge=0, le=1)]
    deleted_items: Count
    deleted_scenes: Count
    deleted_associations: Count
    discarded_uploads: Count
    queued_storage_deletions: Count
    cancelled_creation_jobs: Count

    @model_validator(mode="after")
    def check_scope(self):
        if self.deleted_datasets != int(self.item_id is None):
            raise ValueError("Source deletion receipt has the wrong scope")
        if self.item_id is not None and (
            self.deleted_items > 1
            or self.deleted_scenes
            or self.deleted_associations
            or self.cancelled_creation_jobs
            or self.discarded_uploads > 1
            or self.deleted_items + self.discarded_uploads != 1
        ):
            raise ValueError("Item deletion receipt has inconsistent counts")
        return self


class _Failure(_Value):
    code: Literal[
        "invalid_request",
        "account_not_active",
        "account_busy",
        "invalid_account_role",
        "catalog_manager_required",
        "dataset_missing",
        "item_missing",
        "source_busy",
        "source_in_use",
        "active_learning_reference",
        "creation_in_progress",
        "operation_conflict",
    ]


class _Reply(_Value):
    schema_version: Literal["aap-dataset-source-delete/v1"] = Field(alias="schema")
    ok: StrictBool
    data: DatasetSourceDeletionReceipt | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful deletion must contain only its receipt")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed deletion must contain only its stable error")
        return self


def decode_source_deletion_reply(payload: object) -> DatasetSourceDeletionReceipt:
    reply = _Reply.model_validate(payload)
    if not reply.ok:
        assert reply.error is not None
        raise DatasetSourceDeletionFailure(reply.error.code)
    assert reply.data is not None
    return reply.data
