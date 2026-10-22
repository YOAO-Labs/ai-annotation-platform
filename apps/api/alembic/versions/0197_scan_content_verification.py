"""Retain immutable content verification separately from stat observations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dataset_scan_entries", sa.Column("verified_content", JSONB(), nullable=True)
    )
    op.execute("""CREATE FUNCTION protect_scan_content_receipt() RETURNS trigger AS $$
    BEGIN
        IF OLD.verified_content IS NOT NULL AND NEW.verified_content IS DISTINCT FROM OLD.verified_content THEN
            RAISE EXCEPTION 'Verified scan content receipt is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER scan_content_receipt_immutable BEFORE UPDATE OF verified_content ON dataset_scan_entries FOR EACH ROW EXECUTE FUNCTION protect_scan_content_receipt()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dataset_scan_entries WHERE verified_content IS NOT NULL)"
        )
    ):
        raise RuntimeError("Retain verified content identities on application rollback")
    op.execute("DROP TRIGGER scan_content_receipt_immutable ON dataset_scan_entries")
    op.execute("DROP FUNCTION protect_scan_content_receipt()")
    op.drop_column("dataset_scan_entries", "verified_content")
