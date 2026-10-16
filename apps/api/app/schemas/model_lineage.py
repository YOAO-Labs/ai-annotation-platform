"""Small, typed projections for project provenance and candidate decisions."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeKind = Literal[
    "dataset",
    "run",
    "attempt",
    "model",
    "deployment",
    "release",
    "recipe",
    "recipe_revision",
    "evaluation",
]
Disposition = Literal["active", "research", "declined", "archived"]
ArchiveFilter = Literal["exclude", "include", "only"]


class LineageNode(BaseModel):
    kind: NodeKind
    id: uuid.UUID
    name: str
    status: str | None = None
    created_at: datetime
    facts: dict[str, str] = Field(default_factory=dict)


class LineageLink(BaseModel):
    relation: str
    target: LineageNode | None = None
    gap: str | None = None


class LineageUsage(BaseModel):
    retained_count: int | None
    unavailable_reason: str | None = None
    truncated: bool
    limit: int = 1000
    scope: str = "retained_attributable_predictions"


class LineageOut(BaseModel):
    node: LineageNode
    inputs: list[LineageLink]
    sections: list[str]
    relation: str | None = None
    items: list[LineageNode] = Field(default_factory=list)
    next_cursor: str | None = None
    usage: LineageUsage | None = None


class ModelDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: uuid.UUID
    disposition: Disposition
    note: str = Field(min_length=1, max_length=2000)


class ModelDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    model_version_id: uuid.UUID
    sequence: int
    creator_id: uuid.UUID | None
    creator_label: str
    disposition: Disposition
    note: str
    created_at: datetime


class ModelDecisionPage(BaseModel):
    items: list[ModelDecisionOut]
    next_before: int | None = None
