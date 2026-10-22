"""Portable descriptors for fixed Workspace-owned read relations."""

from __future__ import annotations

from dataclasses import fields
import json
from importlib.resources import files
from typing import Any

from .member_values import PROJECT_MEMBER_COLUMNS, ProjectMemberRow

READ_RELATIONS_SCHEMA = "aap-workspace-read-relations/v1"
PROJECT_MEMBER_FACTS_RELATION = "aap_workspace_v1.project_member_facts"


def read_relations_schema() -> dict[str, Any]:
    """Load and verify the packaged fixed-row relation descriptor."""

    row_fields = tuple(field.name for field in fields(ProjectMemberRow))
    if row_fields != PROJECT_MEMBER_COLUMNS:
        raise RuntimeError("ProjectMemberRow fields differ from PROJECT_MEMBER_COLUMNS")

    schema = json.loads(
        files("aap_workspace_contract")
        .joinpath("read_relations.schema.json")
        .read_text(encoding="utf-8")
    )
    if schema.get("$id") != READ_RELATIONS_SCHEMA:
        raise RuntimeError("Workspace read relation schema version is invalid")

    expected_columns = list(PROJECT_MEMBER_COLUMNS)
    row_schema = schema.get("$defs", {}).get("ProjectMemberRow", {})
    relation = schema.get("x-postgresql-relations", {}).get(
        PROJECT_MEMBER_FACTS_RELATION, {}
    )
    if (
        list(row_schema.get("properties", {})) != expected_columns
        or row_schema.get("required") != expected_columns
        or schema.get("x-project-member-columns") != expected_columns
        or relation.get("columns") != expected_columns
        or relation.get("row") != {"$ref": "#/$defs/ProjectMemberRow"}
    ):
        raise RuntimeError("Workspace ProjectMember relation descriptor is invalid")
    return schema
