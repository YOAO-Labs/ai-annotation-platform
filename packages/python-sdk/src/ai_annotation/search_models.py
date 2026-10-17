"""Typed global-search query and response models."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from ai_annotation.models import _AAPModel


class SearchQuery(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=5, ge=1, le=20)


class SearchProjectItem(_AAPModel):
    id: str
    display_id: str
    name: str
    type_key: str
    data_type: str
    type_label: str


class SearchTaskItem(_AAPModel):
    id: str
    display_id: str
    file_name: str
    project_id: str
    project_name: str


class SearchDatasetItem(_AAPModel):
    id: str
    name: str
    data_type: str


class SearchMemberItem(_AAPModel):
    id: str
    name: str
    email: str
    role: str


class SearchResponse(_AAPModel):
    projects: list[SearchProjectItem]
    tasks: list[SearchTaskItem]
    datasets: list[SearchDatasetItem]
    members: list[SearchMemberItem]
