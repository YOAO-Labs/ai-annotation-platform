"""Add explicit Task creation scope without rewriting historical evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0230"
down_revision = "0229"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "managed_original_creations",
        sa.Column(
            "owner_kind", sa.String(20), nullable=False, server_default="dataset"
        ),
    )
    op.add_column(
        "managed_original_creations", sa.Column("project_id", UUID(as_uuid=True))
    )
    op.add_column(
        "managed_original_creations", sa.Column("task_id", UUID(as_uuid=True))
    )
    op.alter_column(
        "managed_original_creations",
        "dataset_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_managed_creation_scope",
        "managed_original_creations",
        "(owner_kind='dataset' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL) OR (owner_kind='task' AND dataset_id IS NULL AND project_id IS NOT NULL AND task_id IS NOT NULL AND origin='direct' AND object_key='__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload')",
    )
    op.create_index(
        "ix_managed_creation_task",
        "managed_original_creations",
        ["task_id", "allocated_at", "id"],
    )
    # Keep 0219's history and attachment guards intact. The new guard extends
    # immutability to scope even while an allocation is still nonterminal.
    op.execute("""
        CREATE FUNCTION guard_managed_original_creation_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.owner_kind,NEW.project_id,NEW.task_id)
                IS DISTINCT FROM ROW(OLD.owner_kind,OLD.project_id,OLD.task_id) THEN
                RAISE EXCEPTION 'Managed original creation scope is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_scope_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_creation_scope
        BEFORE UPDATE ON managed_original_creations
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_creation_scope()
    """)


def downgrade():
    op.execute("LOCK TABLE managed_original_creations IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM managed_original_creations WHERE owner_kind='task')"
        )
    ):
        raise RuntimeError("Task creation facts prevent destructive downgrade")
    op.execute(
        "DROP TRIGGER trg_managed_original_creation_scope ON managed_original_creations"
    )
    op.execute("DROP FUNCTION guard_managed_original_creation_scope()")
    op.drop_index("ix_managed_creation_task", table_name="managed_original_creations")
    op.drop_constraint(
        "ck_managed_creation_scope", "managed_original_creations", type_="check"
    )
    op.alter_column(
        "managed_original_creations",
        "dataset_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
    for column in ("task_id", "project_id", "owner_kind"):
        op.drop_column("managed_original_creations", column)
