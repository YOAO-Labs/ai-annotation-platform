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
    frozen = op.get_context().script.get_revision("0248").module
    for table, constraint, original in (
        ("export_outputs", "ck_export_output_storage_ref", frozen._NEW_0),
        ("export_media_bundles", "ck_export_media_storage_ref", frozen._NEW_1),
    ):
        predicate = db.scalar(
            sa.text(
                "SELECT pg_get_expr(conbin, conrelid) FROM pg_catalog.pg_constraint "
                "WHERE conrelid = to_regclass(:relation) AND conname = :name "
                "AND contype = 'c' AND convalidated"
            ),
            {"relation": "public." + table, "name": constraint},
        )
        if (
            predicate is None
            or predicate.count(previous) != 1
            or original.count(previous) != 1
        ):
            raise RuntimeError(f"Unexpected saved export constraint: {constraint}")
        # Rebuild from the frozen predecessor, not today's ORM or a live
        # predicate that may have drifted. Saved object addresses stay unchanged.
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint, table, original.replace(previous, accepted)
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Restore a coordinated backup; existing YOAO objects keep their addresses"
    )
