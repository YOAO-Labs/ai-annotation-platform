"""Portable descriptors for fixed owner-separated read relations."""

from __future__ import annotations

from dataclasses import fields
import json
from importlib.resources import files
from typing import Any

from .member_values import PROJECT_MEMBER_COLUMNS, ProjectMemberRow
from .project_query_contract import PROJECT_ROW_COLUMNS

READ_RELATIONS_SCHEMA = "aap-workspace-read-relations/v1"
PROJECT_MEMBER_FACTS_RELATION = "aap_workspace_v1.project_member_facts"
PROJECT_FACTS_RELATION = "aap_workspace_v1.project_facts"
PROJECT_COUNTER_FACTS_RELATION = "aap_labeling_v1.project_catalog_counters"

PROJECT_COUNTER_COLUMNS = (
    "total_tasks",
    "completed_tasks",
    "review_tasks",
    "in_progress_tasks",
    "batch_summary",
)
PROJECT_FACT_COLUMNS = tuple(
    name for name in PROJECT_ROW_COLUMNS if name not in PROJECT_COUNTER_COLUMNS
)
PROJECT_COUNTER_FACT_COLUMNS = ("project_id", *PROJECT_COUNTER_COLUMNS)

PROJECT_FACT_COLUMN_TYPES: dict[str, dict[str, str | bool]] = {
    "id": {"type": "uuid", "nullable": False},
    "organization_id": {"type": "uuid", "nullable": True},
    "display_id": {"type": "character varying(20)", "nullable": False},
    "name": {"type": "character varying(255)", "nullable": False},
    "type_label": {"type": "character varying(50)", "nullable": False},
    "type_key": {"type": "character varying(30)", "nullable": False},
    "data_type": {"type": "character varying(30)", "nullable": False},
    "owner_id": {"type": "uuid", "nullable": False},
    "status": {"type": "character varying(30)", "nullable": False},
    "ai_enabled": {"type": "boolean", "nullable": False},
    "ml_backend_pool_id": {"type": "uuid", "nullable": True},
    "default_variants": {"type": "jsonb", "nullable": False},
    "preannotate_pipeline": {"type": "jsonb", "nullable": True},
    "label_config": {"type": "jsonb", "nullable": False},
    "tool_bindings": {"type": "jsonb", "nullable": False},
    "sampling": {"type": "character varying(30)", "nullable": False},
    "maximum_annotations": {"type": "integer", "nullable": False},
    "show_overlap_first": {"type": "boolean", "nullable": False},
    "iou_dedup_threshold": {"type": "double precision", "nullable": False},
    "box_threshold": {"type": "real", "nullable": False},
    "text_threshold": {"type": "real", "nullable": False},
    "ai_interactive_enabled": {"type": "boolean", "nullable": False},
    "raster_mask_native_editing_enabled": {"type": "boolean", "nullable": False},
    "mask_qc_config": {"type": "jsonb", "nullable": False},
    "point_cloud_quality_config": {"type": "jsonb", "nullable": False},
    "rendering_config": {"type": "jsonb", "nullable": False},
    "annotation_guide": {"type": "text", "nullable": True},
    "guide_assets": {"type": "jsonb", "nullable": False},
    "model_version": {"type": "character varying(100)", "nullable": True},
    "task_lock_ttl_seconds": {"type": "integer", "nullable": False},
    "video_sampling": {"type": "jsonb", "nullable": False},
    "video_collaboration": {"type": "jsonb", "nullable": False},
    "scene_mode": {"type": "boolean", "nullable": False},
    "prefer_same_scene_continuation": {"type": "boolean", "nullable": False},
    "scene_continuation_window_min": {"type": "integer", "nullable": False},
    "due_date": {"type": "date", "nullable": True},
    "created_at": {"type": "timestamp with time zone", "nullable": False},
    "updated_at": {"type": "timestamp with time zone", "nullable": False},
}
PROJECT_COUNTER_FACT_COLUMN_TYPES: dict[str, dict[str, str | bool]] = {
    "project_id": {"type": "uuid", "nullable": False},
    "total_tasks": {"type": "integer", "nullable": False},
    "completed_tasks": {"type": "integer", "nullable": False},
    "review_tasks": {"type": "integer", "nullable": False},
    "in_progress_tasks": {"type": "integer", "nullable": False},
    "batch_summary": {"type": "jsonb", "nullable": False},
}


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

    project_columns = list(PROJECT_FACT_COLUMNS)
    counter_columns = list(PROJECT_COUNTER_FACT_COLUMNS)
    project_facts = schema.get("x-postgresql-relations", {}).get(
        PROJECT_FACTS_RELATION, {}
    )
    counter_facts = schema.get("x-postgresql-relations", {}).get(
        PROJECT_COUNTER_FACTS_RELATION, {}
    )
    if (
        schema.get("x-project-columns") != list(PROJECT_ROW_COLUMNS)
        or schema.get("x-project-counter-columns") != list(PROJECT_COUNTER_COLUMNS)
        or project_facts.get("columns") != project_columns
        or project_facts.get("column_types") != PROJECT_FACT_COLUMN_TYPES
        or project_facts.get("owner") != "workspace"
        or project_facts.get("row_key") != ["id"]
        or project_facts.get("read_only") is not True
        or project_facts.get("security_barrier") is not True
        or counter_facts.get("columns") != counter_columns
        or counter_facts.get("column_types") != PROJECT_COUNTER_FACT_COLUMN_TYPES
        or counter_facts.get("owner") != "labeling"
        or counter_facts.get("row_key") != ["project_id"]
        or counter_facts.get("read_only") is not True
        or counter_facts.get("security_barrier") is not True
    ):
        raise RuntimeError("Workspace Project read relation descriptor is invalid")
    return schema
