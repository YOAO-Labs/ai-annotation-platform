"""Scope current model selections and release generations by canonical task.

Revision ID: 0192
Revises: 0191
"""

from alembic import op
import sqlalchemy as sa

revision = "0192"
down_revision = "0191"
branch_labels = depends_on = None

DETECTION_ALIASES = {"detect", "detection", "image-detection"}


def _validate_legacy_releases(connection):
    versions = connection.execute(
        sa.text("""
        SELECT v.id, v.provenance, r.config
        FROM trained_model_versions v
        JOIN training_runs r ON r.id = v.run_id
    """)
    ).mappings()
    for row in versions:
        provenance, config = row["provenance"], row["config"]
        values = (
            provenance.get("task"),
            config.get("task"),
            (config.get("configuration") or {}).get("identity", {}).get("task"),
        )
        if any(
            value is not None and value not in DETECTION_ALIASES for value in values
        ):
            raise RuntimeError(
                f"Cannot classify historical trained model {row['id']} as detect"
            )
        if str((config.get("base_model") or {}).get("id", "")).lower().endswith("-seg"):
            raise RuntimeError(
                f"Historical segmentation model {row['id']} requires explicit reconciliation"
            )
    for row in connection.execute(
        sa.text("""
        SELECT id, snapshot FROM project_model_releases
    """)
    ).mappings():
        if row["snapshot"].get("task", "detect") not in DETECTION_ALIASES:
            raise RuntimeError(
                f"Cannot classify historical model release {row['id']} as detect"
            )
    invalid = connection.scalar(
        sa.text("""
        SELECT count(*) FROM project_model_selections s
        LEFT JOIN project_model_releases r ON r.id = s.release_id
        WHERE (s.release_id IS NOT NULL AND
          (r.id IS NULL OR r.project_id <> s.project_id OR r.generation <> s.generation))
          OR (s.release_id IS NULL AND s.generation <> 0)
    """)
    )
    if invalid:
        raise RuntimeError(
            "Historical current model selections require explicit reconciliation"
        )
    invalid_links = connection.scalar(
        sa.text("""
        SELECT count(*) FROM project_model_releases r
        LEFT JOIN project_model_releases p ON p.id = r.previous_release_id
        LEFT JOIN project_model_releases b ON b.id = r.rollback_from_id
        LEFT JOIN trained_model_versions v ON v.id = r.model_version_id
        WHERE (p.id IS NOT NULL AND p.project_id <> r.project_id)
           OR (b.id IS NOT NULL AND b.project_id <> r.project_id)
           OR (v.id IS NOT NULL AND v.project_id <> r.project_id)
    """)
    )
    if invalid_links:
        raise RuntimeError("Historical model release links cross project boundaries")


def upgrade():
    connection = op.get_bind()
    _validate_legacy_releases(connection)
    for table in ("project_model_releases", "project_model_selections"):
        op.add_column(
            table,
            sa.Column("task", sa.String(20), nullable=False, server_default="detect"),
        )
    op.drop_constraint(
        "uq_model_release_generation", "project_model_releases", type_="unique"
    )
    op.create_unique_constraint(
        "uq_model_release_generation",
        "project_model_releases",
        ["project_id", "task", "generation"],
    )
    op.drop_constraint(
        "project_model_selections_pkey", "project_model_selections", type_="primary"
    )
    op.create_primary_key(
        "project_model_selections_pkey",
        "project_model_selections",
        ["project_id", "task"],
    )
    op.create_check_constraint(
        "ck_model_release_task",
        "project_model_releases",
        "task IN ('detect','segment')",
    )
    op.create_check_constraint(
        "ck_model_selection_task",
        "project_model_selections",
        "task IN ('detect','segment')",
    )


def downgrade():
    connection = op.get_bind()
    if any(
        connection.scalar(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE task <> 'detect')")
        )
        for table in ("project_model_releases", "project_model_selections")
    ):
        raise RuntimeError(
            "Retained task-scoped model releases require a compatible application; refusing lossy downgrade"
        )
    op.drop_constraint(
        "ck_model_selection_task", "project_model_selections", type_="check"
    )
    op.drop_constraint("ck_model_release_task", "project_model_releases", type_="check")
    op.drop_constraint(
        "project_model_selections_pkey", "project_model_selections", type_="primary"
    )
    op.create_primary_key(
        "project_model_selections_pkey", "project_model_selections", ["project_id"]
    )
    op.drop_constraint(
        "uq_model_release_generation", "project_model_releases", type_="unique"
    )
    op.create_unique_constraint(
        "uq_model_release_generation",
        "project_model_releases",
        ["project_id", "generation"],
    )
    op.drop_column("project_model_selections", "task")
    op.drop_column("project_model_releases", "task")
