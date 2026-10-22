"""SQL relation factories for Workspace facts, independent of provider runtime."""

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    REAL,
    String,
    Text,
    column,
    func,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from aap_workspace_contract.read_relations import PROJECT_COUNTER_COLUMNS


def project_facts():
    return table(
        "project_facts",
        column("id", UUID(as_uuid=True)),
        column("organization_id", UUID(as_uuid=True)),
        column("display_id", String(20)),
        column("name", String(255)),
        column("type_label", String(50)),
        column("type_key", String(30)),
        column("data_type", String(30)),
        column("owner_id", UUID(as_uuid=True)),
        column("status", String(30)),
        column("ai_enabled", Boolean),
        column("ml_backend_pool_id", UUID(as_uuid=True)),
        column("default_variants", JSONB),
        column("preannotate_pipeline", JSONB),
        column("label_config", JSONB),
        column("tool_bindings", JSONB),
        column("sampling", String(30)),
        column("maximum_annotations", Integer),
        column("show_overlap_first", Boolean),
        column("iou_dedup_threshold", Float),
        column("box_threshold", REAL),
        column("text_threshold", REAL),
        column("ai_interactive_enabled", Boolean),
        column("raster_mask_native_editing_enabled", Boolean),
        column("mask_qc_config", JSONB),
        column("point_cloud_quality_config", JSONB),
        column("rendering_config", JSONB),
        column("annotation_guide", Text),
        column("guide_assets", JSONB),
        column("model_version", String(100)),
        column("task_lock_ttl_seconds", Integer),
        column("video_sampling", JSONB),
        column("video_collaboration", JSONB),
        column("scene_mode", Boolean),
        column("prefer_same_scene_continuation", Boolean),
        column("scene_continuation_window_min", Integer),
        column("due_date", Date),
        column("created_at", DateTime(timezone=True)),
        column("updated_at", DateTime(timezone=True)),
        schema="aap_workspace_v1",
    ).alias("workspace_project_facts")


def project_counter_facts():
    return table(
        "project_catalog_counters",
        column("project_id", UUID(as_uuid=True)),
        column("total_tasks", Integer),
        column("completed_tasks", Integer),
        column("review_tasks", Integer),
        column("in_progress_tasks", Integer),
        column("batch_summary", JSONB),
        schema="aap_labeling_v1",
    ).alias("labeling_project_counter_facts")


def project_rows():
    project = project_facts()
    counters = project_counter_facts()
    counter_columns = frozenset(PROJECT_COUNTER_COLUMNS)
    row = select(
        *(
            counters.c[name] if name in counter_columns else project.c[name]
            for name in PROJECT_ROW_COLUMNS
        )
    ).select_from(project.join(counters, project.c.id == counters.c.project_id))
    return row.subquery("workspace_project_rows")


def project_member_facts():
    return table(
        "project_member_facts",
        column("id", UUID(as_uuid=True)),
        column("project_id", UUID(as_uuid=True)),
        column("user_id", UUID(as_uuid=True)),
        column("role", String(30)),
        column("assigned_by", UUID(as_uuid=True)),
        column("assigned_at", DateTime(timezone=True)),
        column("version", Integer),
        column("updated_at", DateTime(timezone=True)),
        column("weekly_target", Integer),
        schema="aap_workspace_v1",
    ).alias("workspace_project_member_facts")


def actor_project_memberships(actor_id):
    return (
        func.aap_workspace_v1.actor_project_memberships(actor_id)
        .table_valued(
            column("project_id", UUID(as_uuid=True)),
            column("member_role", Text),
        )
        .alias("workspace_membership_scope")
    )


def project_search_scope(actor):
    return (
        func.aap_workspace_project_query_v1.project_search_scope(
            actor.id, getattr(actor.role, "value", actor.role)
        )
        .table_valued(
            column("project_id", UUID(as_uuid=True)),
            column("project_name", Text),
            column("owner_id", UUID(as_uuid=True)),
            column("video_collaboration", JSONB),
            column("member_role", Text),
        )
        .alias("workspace_project_scope")
    )


def project_search_member_ids(actor):
    return (
        func.aap_workspace_project_query_v1.project_search_member_ids(
            actor.id, getattr(actor.role, "value", actor.role)
        )
        .table_valued(column("user_id", UUID(as_uuid=True)))
        .alias("workspace_project_members")
    )
