"""SQL relation factories for Workspace facts, independent of provider runtime."""

from sqlalchemy import DateTime, Integer, String, Text, column, func, table
from sqlalchemy.dialects.postgresql import JSONB, UUID


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
