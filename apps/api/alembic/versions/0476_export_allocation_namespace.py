"""Allow current and retained registered-S3 export addresses without rewriting rows."""

from alembic import op
import sqlalchemy as sa

revision = "bo0476"
down_revision = "bo0475"
branch_labels = depends_on = None


def upgrade() -> None:
    previous = "'^([^/]+/)*_aap_managed/export/$'"
    accepted = "'^([^/]+/)*_(aap|yoao)_managed/export/$'"
    db = op.get_bind()
    db.exec_driver_sql(
        "LOCK TABLE public.export_outputs, public.export_media_bundles IN ACCESS EXCLUSIVE MODE"
    )
    for table, constraint in (
        ("export_outputs", "ck_export_output_storage_ref"),
        ("export_media_bundles", "ck_export_media_storage_ref"),
    ):
        predicate = db.scalar(
            sa.text(
                "SELECT pg_get_expr(conbin, conrelid) FROM pg_catalog.pg_constraint "
                "WHERE conrelid = to_regclass(:relation) AND conname = :name "
                "AND contype = 'c' AND convalidated"
            ),
            {"relation": "public." + table, "name": constraint},
        )
        if predicate is None or predicate.count(previous) != 1:
            raise RuntimeError(f"Unexpected saved export constraint: {constraint}")
        # Only the terminal namespace component changes. Preserve the installed
        # location, row identity, logical suffix, byte limit and path checks.
        db.exec_driver_sql(
            f"ALTER TABLE public.{table} DROP CONSTRAINT {constraint}, "
            f"ADD CONSTRAINT {constraint} CHECK ({predicate.replace(previous, accepted)})"
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Restore a coordinated backup; existing YOAO objects keep their addresses"
    )
