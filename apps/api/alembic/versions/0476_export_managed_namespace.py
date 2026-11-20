"""Accept both exact export roots without rewriting any saved locator."""

from alembic import op

revision = "bo0476"
down_revision = "bo0475"
branch_labels = depends_on = None


def upgrade() -> None:
    # The predecessor contains the complete immutable storage-ref predicates.
    # Use those, not today's ORM or an expression read from a modified database.
    frozen = op.get_context().script.get_revision("0248").module
    for table, constraint, predicate in (
        ("export_outputs", "ck_export_output_storage_ref", frozen._NEW_0),
        ("export_media_bundles", "ck_export_media_storage_ref", frozen._NEW_1),
    ):
        old = "^([^/]+/)*_aap_managed/export/$"
        if predicate.count(old) != 1:
            raise RuntimeError("Unexpected frozen export owner predicate")
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            predicate.replace(old, "^([^/]+/)*_(aap|yoao)_managed/export/$"),
        )


def downgrade() -> None:
    raise NotImplementedError("Export allocation cutover is forward-only")
