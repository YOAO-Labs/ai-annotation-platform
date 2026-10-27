"""Order accepted locators independently of path-local revision numbers."""

from alembic import op
import sqlalchemy as sa

revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SEQUENCE dataset_source_acceptance_order_seq")
    op.add_column(
        "dataset_source_revisions",
        sa.Column("acceptance_order", sa.BigInteger(), nullable=True),
    )
    op.execute("DROP TRIGGER source_revision_immutable ON dataset_source_revisions")
    op.execute("""WITH ordered AS (
      SELECT id, row_number() OVER (ORDER BY source_id,path_digest,revision_number) AS position
      FROM dataset_source_revisions)
      UPDATE dataset_source_revisions r SET acceptance_order=o.position FROM ordered o WHERE r.id=o.id""")
    op.execute(
        "SELECT setval('dataset_source_acceptance_order_seq', COALESCE((SELECT max(acceptance_order) FROM dataset_source_revisions),0)+1, false)"
    )
    op.alter_column(
        "dataset_source_revisions",
        "acceptance_order",
        nullable=False,
        server_default=sa.text("nextval('dataset_source_acceptance_order_seq')"),
    )
    op.execute(
        "ALTER SEQUENCE dataset_source_acceptance_order_seq OWNED BY dataset_source_revisions.acceptance_order"
    )
    op.create_unique_constraint(
        "uq_source_acceptance_order", "dataset_source_revisions", ["acceptance_order"]
    )
    op.create_index(
        "ix_source_revision_previous",
        "dataset_source_revisions",
        ["previous_revision_id"],
    )
    op.execute(
        "CREATE TRIGGER source_revision_immutable BEFORE UPDATE ON dataset_source_revisions FOR EACH ROW EXECUTE FUNCTION protect_source_revision()"
    )
    op.execute("""CREATE FUNCTION protect_relocated_revision() RETURNS trigger AS $$
    DECLARE p dataset_source_revisions;
    BEGIN
      IF NEW.previous_revision_id IS NOT NULL THEN
        SELECT * INTO p FROM dataset_source_revisions WHERE id=NEW.previous_revision_id;
        IF p.source_id IS DISTINCT FROM NEW.source_id THEN
          RAISE EXCEPTION 'Revision predecessor source differs' USING ERRCODE='23514';
        END IF;
        IF p.path_digest IS DISTINCT FROM NEW.path_digest AND
          (p.item_id_at_accept,p.asset_id_at_accept,p.content_sha256,p.ownership_mode,p.owns_item)
          IS DISTINCT FROM
          (NEW.item_id_at_accept,NEW.asset_id_at_accept,NEW.content_sha256,NEW.ownership_mode,NEW.owns_item) THEN
          RAISE EXCEPTION 'Relocation must preserve business identity' USING ERRCODE='23514';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER source_relocation_guard BEFORE INSERT ON dataset_source_revisions FOR EACH ROW EXECUTE FUNCTION protect_relocated_revision()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT EXISTS (SELECT 1 FROM dataset_source_revisions r
      JOIN dataset_source_revisions p ON p.id=r.previous_revision_id
      WHERE r.path_digest<>p.path_digest)""")
    ):
        raise RuntimeError("Relocated source history requires the current reader")
    op.execute("DROP TRIGGER source_relocation_guard ON dataset_source_revisions")
    op.execute("DROP FUNCTION protect_relocated_revision()")
    op.drop_index("ix_source_revision_previous", table_name="dataset_source_revisions")
    op.drop_constraint(
        "uq_source_acceptance_order", "dataset_source_revisions", type_="unique"
    )
    op.drop_column("dataset_source_revisions", "acceptance_order")
