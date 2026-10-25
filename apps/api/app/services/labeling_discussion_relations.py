"""Typed application mappings for Labeling's public discussion projections."""

from sqlalchemy import Boolean, DateTime, Integer, String, column, table
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import aliased

from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback

_comment_facts = table(
    "annotation_comment_facts",
    column("id", UUID(as_uuid=True)),
    column("annotation_id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    column("author_id", UUID(as_uuid=True)),
    column("body", String),
    column("is_resolved", Boolean),
    column("is_active", Boolean),
    column("mentions", JSONB),
    column("attachments", JSONB),
    column("canvas_drawing", JSONB),
    column("anchor", JSONB),
    column("created_at", DateTime(timezone=True)),
    column("updated_at", DateTime(timezone=True)),
    schema="aap_labeling_v1",
)

_feedback_facts = table(
    "annotation_feedback_facts",
    column("id", UUID(as_uuid=True)),
    column("kind", String),
    column("anchor_type", String),
    column("project_id", UUID(as_uuid=True)),
    column("task_id", UUID(as_uuid=True)),
    column("annotation_id", UUID(as_uuid=True)),
    column("anchor_position", JSONB),
    column("status", String),
    column("severity", String),
    column("title", String),
    column("body", String),
    column("author_id", UUID(as_uuid=True)),
    column("attachments", JSONB),
    column("mentions", JSONB),
    column("canvas_drawing", JSONB),
    column("thread_parent_id", UUID(as_uuid=True)),
    column("is_active", Boolean),
    column("resolved_at", DateTime(timezone=True)),
    column("resolved_by_id", UUID(as_uuid=True)),
    column("created_at", DateTime(timezone=True)),
    column("updated_at", DateTime(timezone=True)),
    schema="aap_labeling_v1",
)

annotation_comment_facts = aliased(
    AnnotationComment, _comment_facts, adapt_on_names=True
)
annotation_feedback_facts = aliased(
    AnnotationFeedback, _feedback_facts, adapt_on_names=True
)


def feedback_facts_alias(name: str | None = None):
    return aliased(
        AnnotationFeedback,
        _feedback_facts.alias(name),
        adapt_on_names=True,
    )


task_event_facts = table(
    "task_event_facts",
    column("id", UUID(as_uuid=True)),
    column("task_id", UUID(as_uuid=True)),
    column("user_id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    column("kind", String),
    column("started_at", DateTime(timezone=True)),
    column("ended_at", DateTime(timezone=True)),
    column("duration_ms", Integer),
    column("annotation_count", Integer),
    column("was_rejected", Boolean),
    column("collector_version", String),
    column("collection_source", String),
    column("collection_coverage", String),
    column("created_at", DateTime(timezone=True)),
    column("reject_reason_type", String),
    schema="aap_labeling_v1",
)
