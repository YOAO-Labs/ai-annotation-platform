"""Keep independent RLE candidates and durable business-publication guards."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None

# Freeze checks in the migration rather than importing mutable application code.
_CHECKS = [
    ("ck_raster_object_canonical_sha", "canonical_sha256 ~ '^[0-9a-f]{64}$'"),
    ("ck_raster_object_stored_sha", "stored_sha256 ~ '^[0-9a-f]{64}$'"),
    ("ck_raster_object_canonical_size", "canonical_size_bytes BETWEEN 1 AND 4194304"),
    ("ck_raster_object_stored_size", "stored_size_bytes BETWEEN 1 AND 8388608"),
    (
        "ck_raster_object_dimensions",
        "height BETWEEN 1 AND 8192 AND width BETWEEN 1 AND 8192 AND height::bigint * width <= 67108864",
    ),
    ("ck_raster_object_runs", "runs BETWEEN 1 AND 1000000"),
    ("ck_raster_object_encoding", "storage_encoding IN ('identity','gzip')"),
    (
        "ck_raster_object_logical_key",
        "\nlogical_key = 'raster-masks/sha256/' || substr(canonical_sha256,1,2) || '/'\n    || substr(canonical_sha256,3,2) || '/' || canonical_sha256\n    || CASE WHEN storage_encoding='gzip' THEN '.json.gz' ELSE '.json' END\n",
    ),
    (
        "ck_raster_object_identity_bytes",
        "\n(storage_encoding='identity' AND stored_sha256=canonical_sha256\n    AND stored_size_bytes=canonical_size_bytes)\nOR (storage_encoding='gzip' AND canonical_size_bytes <= stored_size_bytes::bigint * 20)\n",
    ),
    (
        "ck_raster_object_storage_ref",
        "\nCOALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'\n    AND storage_ref->>'purpose' = 'annotations'\n    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'\n    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND storage_ref->>'object_key' = 'raster-masks/objects/' || id::text\n        || CASE WHEN storage_encoding='gzip' THEN '.json.gz' ELSE '.json' END\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false)\n",
    ),
    ("ck_raster_object_state", "state IN ('pending','ready','retired')"),
    (
        "ck_raster_object_publication",
        "\n(state='pending' AND published_at IS NULL)\nOR (state='ready' AND published_at IS NOT NULL AND published_at >= created_at)\nOR (state='retired' AND (published_at IS NULL OR published_at >= created_at))\n",
    ),
    (
        "ck_raster_object_last_used",
        "\n(published_at IS NULL AND last_used_at IS NULL)\nOR (state <> 'pending' AND published_at IS NOT NULL AND last_used_at IS NOT NULL\n    AND last_used_at >= published_at)\n",
    ),
    (
        "ck_raster_object_retirement",
        "\n(state='retired' AND retired_at IS NOT NULL\n    AND retired_at >= COALESCE(last_used_at,published_at,created_at))\nOR (state<>'retired' AND retired_at IS NULL)\n",
    ),
]


def upgrade():
    op.create_table(
        "raster_mask_objects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("logical_key", sa.String(255), nullable=False),
        sa.Column("canonical_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_size_bytes", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column("storage_encoding", sa.String(8), nullable=False),
        sa.Column("stored_sha256", sa.String(64), nullable=False),
        sa.Column("stored_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        *(sa.CheckConstraint(sql, name=name) for name, sql in _CHECKS),
    )
    op.create_index(
        "uq_raster_object_ready_logical_key",
        "raster_mask_objects",
        ["logical_key"],
        unique=True,
        postgresql_where=sa.text("state='ready'"),
    )
    op.create_index(
        "ix_raster_object_cleanup",
        "raster_mask_objects",
        ["state", "last_attempted_at", "id"],
    )
    op.create_table(
        "raster_mask_publications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "object_id",
            UUID(as_uuid=True),
            sa.ForeignKey("raster_mask_objects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("gate_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "gate_revision > 0", name="ck_raster_publication_gate_revision"
        ),
    )
    op.create_index(
        "ix_raster_publication_object", "raster_mask_publications", ["object_id"]
    )
    op.execute("""
        CREATE FUNCTION guard_raster_object_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.id,NEW.logical_key,NEW.canonical_sha256,NEW.canonical_size_bytes,
                   NEW.height,NEW.width,NEW.runs,NEW.storage_encoding,
                   NEW.stored_sha256,NEW.stored_size_bytes,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.logical_key,OLD.canonical_sha256,OLD.canonical_size_bytes,
                   OLD.height,OLD.width,OLD.runs,OLD.storage_encoding,
                   OLD.stored_sha256,OLD.stored_size_bytes,OLD.created_at)
            THEN
                RAISE EXCEPTION 'RLE object identity and content are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_facts_immutable';
            END IF;
            IF NOT (NEW.state=OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'RLE object state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_state_forward';
            END IF;
            IF NEW.published_at IS DISTINCT FROM OLD.published_at
               AND NOT (OLD.state='pending' AND NEW.state='ready'
                        AND OLD.published_at IS NULL AND NEW.published_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'RLE object publication is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_publication_immutable';
            END IF;
            IF NEW.last_used_at IS DISTINCT FROM OLD.last_used_at
               AND (NEW.state <> 'ready' OR NEW.last_used_at IS NULL
                    OR (OLD.last_used_at IS NOT NULL AND NEW.last_used_at < OLD.last_used_at))
            THEN
                RAISE EXCEPTION 'RLE object use clock must advance on ready objects'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_last_used_monotonic';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'RLE object retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_retirement_immutable';
            END IF;
            IF NEW.state='retired' AND OLD.state<>'retired'
               AND EXISTS (SELECT 1 FROM raster_mask_publications WHERE object_id=OLD.id)
            THEN
                RAISE EXCEPTION 'Guarded RLE objects cannot retire'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_publication_guard';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_raster_object_facts BEFORE UPDATE ON raster_mask_objects FOR EACH ROW EXECUTE FUNCTION guard_raster_object_facts()"
    )
    op.execute(
        "CREATE TRIGGER trg_raster_object_storage_ref BEFORE UPDATE ON raster_mask_objects FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )
    op.execute("""
        CREATE FUNCTION guard_raster_publication() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_state text;
        BEGIN
            IF TG_OP='UPDATE' THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'RLE publication facts are immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_raster_publication_immutable';
                END IF;
                RETURN NEW;
            END IF;
            SELECT state INTO owner_state FROM raster_mask_objects
                WHERE id=NEW.object_id FOR UPDATE NOWAIT;
            IF owner_state IS NULL OR owner_state NOT IN ('pending','ready') THEN
                RAISE EXCEPTION 'RLE publications require pending or ready owner'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_publication_owner';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_raster_publication BEFORE INSERT OR UPDATE ON raster_mask_publications FOR EACH ROW EXECUTE FUNCTION guard_raster_publication()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE raster_mask_objects, raster_mask_publications IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM raster_mask_objects) OR EXISTS (SELECT 1 FROM raster_mask_publications)"
        )
    ):
        raise RuntimeError(
            "RLE ownership or publication guards prevent destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_raster_publication ON raster_mask_publications")
    op.execute("DROP FUNCTION guard_raster_publication()")
    op.drop_index("ix_raster_publication_object", table_name="raster_mask_publications")
    op.drop_table("raster_mask_publications")
    op.execute("DROP TRIGGER trg_raster_object_storage_ref ON raster_mask_objects")
    op.execute("DROP TRIGGER trg_raster_object_facts ON raster_mask_objects")
    op.execute("DROP FUNCTION guard_raster_object_facts()")
    op.drop_index(
        "uq_raster_object_ready_logical_key", table_name="raster_mask_objects"
    )
    op.drop_index("ix_raster_object_cleanup", table_name="raster_mask_objects")
    op.drop_table("raster_mask_objects")
