"""Immutable request facts safe to pass across module boundaries."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectDeletionAuditContext(_Value):
    method: Annotated[str, Field(max_length=8)] | None = None
    path: Annotated[str, Field(max_length=256)] | None = None
    ip: Annotated[str, Field(max_length=64)] | None = None
    request_id: Annotated[str, Field(max_length=36)] | None = None
