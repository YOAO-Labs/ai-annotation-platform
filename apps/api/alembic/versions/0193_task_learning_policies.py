"""Isolate policy baselines and candidate deduplication by visual task.

Revision ID: 0193
Revises: 0192
"""

from alembic import op
import sqlalchemy as sa

revision = "0193"
down_revision = "0192"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    invalid = bind.scalar(
        sa.text("""
        SELECT count(*) FROM learning_policies p
        LEFT JOIN training_dataset_versions d ON d.id::text = p.config->>'parent_id'
        LEFT JOIN training_dataset_versions b ON b.id = p.baseline_dataset_id
        LEFT JOIN trained_model_versions m ON m.id = p.initialization_model_id
        WHERE d.id IS NULL OR d.project_id != p.project_id
          OR coalesce(d.snapshot->>'task', 'image-detection') NOT IN ('detect','detection','image-detection')
          OR coalesce(p.config->'template'->>'task', 'detect') NOT IN ('detect','detection','image-detection')
          OR coalesce(p.config->'template'->'base_model'->>'id','') LIKE '%-seg'
          OR (b.id IS NOT NULL AND (b.project_id != p.project_id OR
              coalesce(b.snapshot->>'task','image-detection') NOT IN ('detect','detection','image-detection')))
          OR (m.id IS NOT NULL AND (m.project_id != p.project_id OR
              coalesce(m.provenance->>'task','detect') NOT IN ('detect','detection','image-detection')))
    """)
    )
    invalid_triggers = bind.scalar(
        sa.text("""
        SELECT count(*) FROM learning_triggers t
        JOIN learning_policies p ON p.id=t.policy_id
        JOIN training_dataset_versions d ON d.id=t.dataset_id
        WHERE p.project_id != t.project_id OR d.project_id != t.project_id
           OR coalesce(d.snapshot->>'task','image-detection') NOT IN ('detect','detection','image-detection')
    """)
    )
    if invalid or invalid_triggers:
        raise RuntimeError(
            "Historical learning policy task identity is missing or conflicting"
        )
    op.add_column(
        "learning_policies",
        sa.Column("task", sa.String(20), nullable=False, server_default="detect"),
    )
    uniques = sa.inspect(bind).get_unique_constraints("learning_policies")
    for constraint in uniques:
        if constraint["column_names"] == ["project_id"]:
            op.drop_constraint(constraint["name"], "learning_policies", type_="unique")
    op.create_unique_constraint(
        "uq_learning_policy_task", "learning_policies", ["project_id", "task"]
    )
    op.create_check_constraint(
        "ck_learning_policy_task", "learning_policies", "task IN ('detect', 'segment')"
    )
    op.drop_constraint("uq_learning_candidate", "learning_triggers", type_="unique")
    op.create_unique_constraint(
        "uq_learning_candidate",
        "learning_triggers",
        ["policy_id", "policy_revision", "candidate_digest"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM learning_policies WHERE task != 'detect'")
    ):
        raise RuntimeError("Retained segmentation policies prevent a lossy downgrade")
    op.drop_constraint("uq_learning_candidate", "learning_triggers", type_="unique")
    op.create_unique_constraint(
        "uq_learning_candidate",
        "learning_triggers",
        ["project_id", "policy_revision", "candidate_digest"],
    )
    op.drop_constraint("ck_learning_policy_task", "learning_policies", type_="check")
    op.drop_constraint("uq_learning_policy_task", "learning_policies", type_="unique")
    op.create_unique_constraint(
        "learning_policies_project_id_key", "learning_policies", ["project_id"]
    )
    op.drop_column("learning_policies", "task")
