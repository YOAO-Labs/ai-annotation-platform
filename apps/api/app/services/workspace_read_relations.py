"""SQL relation factories for Workspace facts, independent of provider runtime."""

from sqlalchemy import Text, column, func
from sqlalchemy.dialects.postgresql import JSONB, UUID


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
