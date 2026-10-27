"""Retain verified zero-byte raw LiDAR delivery objects."""

from alembic import op
import sqlalchemy as sa

revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None

OLD_BOUND = "member_size <= 0 OR member_size > 268435456"
NEW_BOUND = "member_size < 0 OR member_size > 268435456"


def _definition(value, *, upgrade):
    """Alter exactly the lower bound, preserving every ownership/lifecycle guard."""
    before, after = (OLD_BOUND, NEW_BOUND) if upgrade else (NEW_BOUND, OLD_BOUND)
    if not isinstance(value, str) or value.count(before) != 1 or after in value:
        raise RuntimeError("Unexpected export media inventory guard definition")
    return value.replace(before, after)


def _replace(*, upgrade):
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "SELECT pg_get_functiondef('guard_export_media_bundle()'::regprocedure)"
        )
    )
    op.execute(_definition(definition, upgrade=upgrade))


def upgrade():
    _replace(upgrade=True)


def downgrade():
    # Fence inserts until the guard replacement commits, including empty owners.
    op.execute("LOCK TABLE export_media_bundles IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("""
            SELECT EXISTS (
                SELECT 1 FROM export_media_bundles AS bundle
                CROSS JOIN LATERAL jsonb_each(bundle.inventory->'objects') AS member
                WHERE member.value->>'size_bytes' = '0'
            )
        """)
    ):
        raise RuntimeError(
            "Cannot downgrade while zero-byte export media responsibility exists"
        )
    _replace(upgrade=False)
