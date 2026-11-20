"""Keep fixture proxy addresses exclusive only while their reader is active."""

from alembic import op

revision = "bo0478"
down_revision = "bo0477"
branch_labels = depends_on = None


def upgrade() -> None:
    # The ALTER lock spans both statements; registration never sees a gap.
    op.execute("""
        ALTER TABLE yoao_assets_v1.fixture_native_read_principals
        DROP CONSTRAINT fixture_native_read_principals_proxy_origin_key
    """)
    op.execute("""
        CREATE UNIQUE INDEX fixture_native_reader_active_proxy_origin
        ON yoao_assets_v1.fixture_native_read_principals (proxy_origin)
        WHERE state = 'active'
    """)


def downgrade() -> None:
    raise NotImplementedError("Retired readers may share a retained proxy address")
