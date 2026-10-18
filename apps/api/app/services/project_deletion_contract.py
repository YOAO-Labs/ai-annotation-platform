"""Language-neutral project deletion command and immutable receipt bindings."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class ProjectDeletionFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectDeletionReceipt(_Value):
    operation_id: UUID
    actor_id: UUID
    project_id: UUID
    deleted_tasks: Annotated[int, Field(strict=True, ge=0)]
    deleted_annotations: Annotated[int, Field(strict=True, ge=0)]
    audit_id: Annotated[int, Field(strict=True, gt=0)]


class _Failure(_Value):
    code: str


class _Reply(_Value):
    schema_version: Literal["aap-project-delete/v1"] = Field(alias="schema")
    ok: StrictBool
    data: ProjectDeletionReceipt | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful deletion must have only its receipt")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed deletion must have only its stable error")
        return self


def decode_project_deletion_reply(payload: object) -> ProjectDeletionReceipt:
    reply = _Reply.model_validate(payload)
    if not reply.ok:
        assert reply.error is not None
        raise ProjectDeletionFailure(reply.error.code)
    assert reply.data is not None
    return reply.data
