"""Retain immutable accepted revisions behind a mutable source path head."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER source_item_immutable ON dataset_source_items")
    op.create_table(
        "dataset_source_revisions",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "source_id",
            UUID(),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("path_digest", sa.String(64), nullable=False),
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("item_id_at_accept", UUID(), nullable=False),
        sa.Column("asset_id_at_accept", UUID(), nullable=False),
        sa.Column(
            "dataset_item_id",
            UUID(),
            sa.ForeignKey("dataset_items.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "asset_id", UUID(), sa.ForeignKey("dataset_assets.id", ondelete="SET NULL")
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("ownership_mode", sa.String(20), nullable=False),
        sa.Column("owns_item", sa.Boolean(), nullable=False),
        sa.Column(
            "previous_revision_id",
            UUID(),
            sa.ForeignKey("dataset_source_revisions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "registration_id",
            UUID(),
            sa.ForeignKey("dataset_registration_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "accepted_by_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_id",
            "path_digest",
            "revision_number",
            name="uq_source_revision_number",
        ),
        sa.UniqueConstraint(
            "source_id", "path_digest", "run_id", name="uq_source_revision_observation"
        ),
        sa.UniqueConstraint(
            "registration_id", "path_digest", name="uq_source_revision_resolution"
        ),
        sa.CheckConstraint("revision_number > 0", name="ck_source_revision_number"),
        sa.CheckConstraint(
            "ownership_mode IN ('reference','managed_copy')",
            name="ck_source_revision_ownership",
        ),
    )
    for name in ("source_id", "dataset_item_id"):
        op.create_index(
            f"ix_dataset_source_revisions_{name}", "dataset_source_revisions", [name]
        )
    op.add_column("dataset_source_items", sa.Column("current_revision_id", UUID()))
    op.add_column("dataset_source_changes", sa.Column("before_revision_id", UUID()))
    op.execute("""
        INSERT INTO dataset_source_revisions
          (id, source_id, path_digest, run_id, revision_number, item_id_at_accept, asset_id_at_accept,
           dataset_item_id, asset_id, content_sha256, ownership_mode, owns_item)
        SELECT gen_random_uuid(), source_id, path_digest, run_id, 1, dataset_item_id, asset_id,
               dataset_item_id, asset_id, content_sha256, ownership_mode, owns_item
        FROM dataset_source_items
    """)
    op.execute("""
        UPDATE dataset_source_items h SET current_revision_id = r.id
        FROM dataset_source_revisions r
        WHERE r.source_id = h.source_id AND r.path_digest = h.path_digest
    """)
    op.execute("""
        UPDATE dataset_source_changes c SET before_revision_id = r.id
        FROM dataset_source_revisions r, dataset_registration_runs j
        WHERE j.id = c.run_id AND r.source_id = j.source_id
          AND r.path_digest = c.path_digest AND r.run_id = c.baseline_run_id
          AND r.item_id_at_accept = c.before_item_id AND r.content_sha256 = c.before_sha256
    """)
    op.alter_column("dataset_source_items", "current_revision_id", nullable=False)
    op.create_foreign_key(
        "fk_source_head_revision",
        "dataset_source_items",
        "dataset_source_revisions",
        ["current_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_change_before_revision",
        "dataset_source_changes",
        "dataset_source_revisions",
        ["before_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_reference_source_path", "dataset_references", type_="unique")
    op.create_unique_constraint(
        "uq_reference_source_observation",
        "dataset_references",
        ["source_id", "path_digest", "run_id"],
    )

    op.execute("""CREATE FUNCTION protect_source_revision() RETURNS trigger AS $$
    BEGIN
      IF (to_jsonb(NEW) - ARRAY['dataset_item_id','asset_id','accepted_by_id'])
           IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['dataset_item_id','asset_id','accepted_by_id'])
         OR (NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id AND NEW.dataset_item_id IS NOT NULL)
         OR (NEW.asset_id IS DISTINCT FROM OLD.asset_id AND NEW.asset_id IS NOT NULL)
         OR (NEW.accepted_by_id IS DISTINCT FROM OLD.accepted_by_id AND NEW.accepted_by_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Accepted source revisions are immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER source_revision_immutable BEFORE UPDATE ON dataset_source_revisions FOR EACH ROW EXECUTE FUNCTION protect_source_revision()"
    )
    op.execute("""CREATE FUNCTION protect_source_head() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.path_digest IS DISTINCT FROM OLD.path_digest THEN
          RAISE EXCEPTION 'Source head locator is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id AND NOT EXISTS (
          SELECT 1 FROM dataset_source_revisions WHERE id=NEW.current_revision_id
            AND previous_revision_id=OLD.current_revision_id) THEN
          RAISE EXCEPTION 'Source revision predecessor changed' USING ERRCODE='23514';
        END IF;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM dataset_source_revisions r
        WHERE r.id=NEW.current_revision_id AND r.source_id=NEW.source_id
          AND r.path_digest=NEW.path_digest AND r.run_id=NEW.run_id
          AND r.dataset_item_id=NEW.dataset_item_id AND r.asset_id=NEW.asset_id
          AND r.content_sha256=NEW.content_sha256 AND r.ownership_mode=NEW.ownership_mode
          AND r.owns_item=NEW.owns_item) THEN
        RAISE EXCEPTION 'Source head must match its accepted revision' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER source_head_guard BEFORE INSERT OR UPDATE ON dataset_source_items FOR EACH ROW EXECUTE FUNCTION protect_source_head()"
    )
    directory_guards("dataset_source_revisions")


def directory_guards(table):
    op.execute(f"""CREATE OR REPLACE FUNCTION protect_directory_item() RETURNS trigger AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM {table} WHERE dataset_item_id=OLD.id)
         AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_id IS DISTINCT FROM OLD.dataset_id) THEN
        RAISE EXCEPTION 'Directory item binding is immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(f"""CREATE OR REPLACE FUNCTION protect_directory_asset() RETURNS trigger AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM {table} WHERE asset_id=OLD.id)
         AND (NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id
           OR NEW.checksum_algorithm IS DISTINCT FROM OLD.checksum_algorithm
           OR NEW.checksum_value IS DISTINCT FROM OLD.checksum_value
           OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
           OR NEW.verification_state IS DISTINCT FROM OLD.verification_state) THEN
        RAISE EXCEPTION 'Directory asset identity is immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM dataset_source_revisions)")
    ):
        raise RuntimeError("Retain accepted source revisions on application rollback")
    op.execute("DROP TRIGGER source_head_guard ON dataset_source_items")
    op.execute("DROP FUNCTION protect_source_head()")
    op.execute("DROP TRIGGER source_revision_immutable ON dataset_source_revisions")
    op.execute("DROP FUNCTION protect_source_revision()")
    directory_guards("dataset_source_items")
    op.execute(
        "CREATE TRIGGER source_item_immutable BEFORE UPDATE ON dataset_source_items FOR EACH ROW EXECUTE FUNCTION protect_dataset_reference()"
    )
    op.drop_constraint(
        "uq_reference_source_observation", "dataset_references", type_="unique"
    )
    op.create_unique_constraint(
        "uq_reference_source_path", "dataset_references", ["source_id", "path_digest"]
    )
    op.drop_column("dataset_source_changes", "before_revision_id")
    op.drop_column("dataset_source_items", "current_revision_id")
    op.drop_table("dataset_source_revisions")
