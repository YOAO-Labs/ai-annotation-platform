"""Pin reference items to immutable verified scan entries."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_references",
        sa.Column(
            "dataset_item_id",
            UUID(),
            sa.ForeignKey("dataset_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_id",
            UUID(),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column("path_digest", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("location_revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_id", "path_digest", name="uq_reference_source_path"
        ),
    )
    op.create_index(
        "ix_dataset_references_source_id", "dataset_references", ["source_id"]
    )
    op.execute("""CREATE FUNCTION protect_dataset_reference() RETURNS trigger AS $$
    BEGIN
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'Dataset references are immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER dataset_reference_immutable BEFORE UPDATE ON dataset_references FOR EACH ROW EXECUTE FUNCTION protect_dataset_reference()"
    )
    op.execute("""CREATE FUNCTION protect_reference_item() RETURNS trigger AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_references WHERE dataset_item_id = OLD.id)
           AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_id IS DISTINCT FROM OLD.dataset_id) THEN
            RAISE EXCEPTION 'Reference item binding is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER reference_item_immutable BEFORE UPDATE ON dataset_items FOR EACH ROW EXECUTE FUNCTION protect_reference_item()"
    )
    op.execute("""CREATE FUNCTION protect_reference_task() RETURNS trigger AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_references WHERE dataset_item_id = OLD.dataset_item_id)
           AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id OR NEW.project_id IS DISTINCT FROM OLD.project_id) THEN
            RAISE EXCEPTION 'Reference task binding is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER reference_task_immutable BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION protect_reference_task()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM dataset_references)")
    ):
        raise RuntimeError(
            "Retain registered reference identities on application rollback"
        )
    for table, trigger, function in (
        ("tasks", "reference_task_immutable", "protect_reference_task"),
        ("dataset_items", "reference_item_immutable", "protect_reference_item"),
        (
            "dataset_references",
            "dataset_reference_immutable",
            "protect_dataset_reference",
        ),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_table("dataset_references")
