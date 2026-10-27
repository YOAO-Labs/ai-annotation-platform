"""Retain immutable physical ownership and receipts for normalized avatars."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None

# Freeze the shape here; migrations must not import changing application models.
_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'avatars'
    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'
    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND storage_ref->>'object_key' = token || '.webp'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""


def upgrade():
    op.create_table(
        "avatar_uploads",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("token ~ '^[0-9a-f]{32}$'", name="ck_avatar_upload_token"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_avatar_upload_sha256"),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 2097152", name="ck_avatar_upload_size"
        ),
        sa.CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_avatar_upload_state"
        ),
        sa.CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL) OR "
            "(state<>'retired' AND retired_at IS NULL)",
            name="ck_avatar_upload_retirement",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_avatar_upload_storage_ref"),
    )
    op.create_index(
        "ix_avatar_upload_cleanup",
        "avatar_uploads",
        ["state", "last_attempted_at", "token"],
    )
    op.execute("""
        CREATE FUNCTION guard_avatar_upload_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.token IS DISTINCT FROM OLD.token
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'Avatar upload identity and content receipt are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_avatar_upload_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'Avatar upload state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_avatar_upload_state_forward';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'Avatar upload retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_avatar_upload_retirement_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_avatar_upload_facts BEFORE UPDATE ON avatar_uploads "
        "FOR EACH ROW EXECUTE FUNCTION guard_avatar_upload_facts()"
    )
    # Reuse the existing owner-role/transaction-marker whitelist unchanged.
    op.execute(
        "CREATE TRIGGER trg_avatar_upload_storage_ref BEFORE UPDATE ON avatar_uploads "
        "FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )


def downgrade():
    op.execute("LOCK TABLE avatar_uploads IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM avatar_uploads)")):
        raise RuntimeError(
            "Avatar upload ownership facts prevent destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_avatar_upload_storage_ref ON avatar_uploads")
    op.execute("DROP TRIGGER trg_avatar_upload_facts ON avatar_uploads")
    op.execute("DROP FUNCTION guard_avatar_upload_facts()")
    op.drop_index("ix_avatar_upload_cleanup", table_name="avatar_uploads")
    op.drop_table("avatar_uploads")
