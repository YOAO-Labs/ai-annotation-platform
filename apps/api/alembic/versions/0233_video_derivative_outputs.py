"""Retain video-derived attempts and physical responsibility independently."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0233"
down_revision = "0232"
branch_labels = None
depends_on = None

VIDEO_DERIVATIVE_STORAGE_REF_CHECK = r"""
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'media-cache'
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
    AND storage_ref->>'prefix' = 'video-derivatives/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

VIDEO_DERIVATIVE_INPUT_SHAPE_CHECK = r"""
COALESCE(jsonb_typeof(spec) = 'object'
    AND jsonb_typeof(original_ref) = 'object'
    AND jsonb_typeof(auth_ref) = 'object', false)
"""

VIDEO_DERIVATIVE_PUBLICATION_CHECK = r"""
COALESCE((state != 'ready' OR (inventory IS NOT NULL AND result IS NOT NULL))
    AND (state != 'building' OR (inventory IS NULL AND result IS NULL))
    AND ((inventory IS NULL AND result IS NULL)
        OR (jsonb_typeof(inventory) = 'object'
            AND inventory ?& ARRAY['schema','objects']
            AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb
            AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1'
            AND jsonb_typeof(inventory->'objects') = 'object'
            AND jsonb_typeof(result) = 'object')), false)
"""

VIDEO_DERIVATIVE_LEASE_CHECK = r"""
((lease_token IS NULL) = (lease_expires_at IS NULL))
    AND (state != 'ready' OR lease_token IS NOT NULL)
"""


def upgrade():
    op.create_table(
        "video_derivative_outputs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("spec", JSONB(none_as_null=True), nullable=False),
        sa.Column("original_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("auth_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("metadata_output_id", UUID(as_uuid=True)),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="building"),
        sa.Column("lease_token", UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("inventory", JSONB(none_as_null=True)),
        sa.Column("result", JSONB(none_as_null=True)),
        sa.Column("error_code", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('metadata','frame','chunk')", name="ck_video_derivative_kind"
        ),
        sa.CheckConstraint(
            "state IN ('building','ready','retiring','deleted')",
            name="ck_video_derivative_state",
        ),
        sa.CheckConstraint(
            "(kind = 'metadata') = (metadata_output_id IS NULL)",
            name="ck_video_derivative_metadata",
        ),
        sa.CheckConstraint(
            VIDEO_DERIVATIVE_INPUT_SHAPE_CHECK, name="ck_video_derivative_input_shape"
        ),
        sa.CheckConstraint(
            VIDEO_DERIVATIVE_LEASE_CHECK, name="ck_video_derivative_lease"
        ),
        sa.CheckConstraint(
            VIDEO_DERIVATIVE_STORAGE_REF_CHECK, name="ck_video_derivative_storage_ref"
        ),
        sa.CheckConstraint(
            VIDEO_DERIVATIVE_PUBLICATION_CHECK, name="ck_video_derivative_publication"
        ),
    )
    op.create_index(
        "ix_video_derivative_outputs_cleanup",
        "video_derivative_outputs",
        ["state", "updated_at", "id"],
    )
    op.create_index(
        "ix_video_derivative_outputs_item_kind",
        "video_derivative_outputs",
        ["dataset_item_id", "kind", "id"],
    )
    for table, column, constraint in (
        ("dataset_items", "video_output_id", "fk_dataset_item_video_output"),
        ("video_frame_cache", "output_id", "fk_video_frame_cache_output"),
        ("video_chunks", "output_id", "fk_video_chunk_output"),
    ):
        op.add_column(table, sa.Column(column, UUID(as_uuid=True)))
        op.create_foreign_key(
            constraint,
            table,
            "video_derivative_outputs",
            [column],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_{column}", table, [column])
    op.execute("""
        CREATE FUNCTION guard_video_derivative_output() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                IF OLD.state <> 'deleted' THEN
                    RAISE EXCEPTION 'Unsettled video output owners must be retained'
                        USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_delete_guard';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.state <> 'building' OR NEW.inventory IS NOT NULL
                    OR NEW.result IS NOT NULL OR NEW.lease_token IS NOT NULL
                    OR NEW.lease_expires_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'New video outputs must begin unclaimed and building'
                        USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_initial';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state='deleted' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Deleted video outputs are terminal'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_terminal';
            END IF;
            IF ROW(NEW.id,NEW.dataset_item_id,NEW.kind,NEW.spec,NEW.original_ref,
                    NEW.auth_ref,NEW.metadata_output_id,NEW.storage_ref,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.dataset_item_id,OLD.kind,OLD.spec,OLD.original_ref,
                    OLD.auth_ref,OLD.metadata_output_id,OLD.storage_ref,OLD.created_at)
            THEN
                RAISE EXCEPTION 'Video output identity and inputs are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_facts_immutable';
            END IF;
            IF NOT (NEW.state=OLD.state
                OR (OLD.state='building' AND NEW.state IN ('ready','retiring'))
                OR (OLD.state='ready' AND NEW.state='retiring')
                OR (OLD.state='retiring' AND NEW.state='deleted'))
            THEN
                RAISE EXCEPTION 'Video output state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_state_forward';
            END IF;
            IF ROW(NEW.lease_token,NEW.lease_expires_at)
                IS DISTINCT FROM ROW(OLD.lease_token,OLD.lease_expires_at)
                AND NOT (OLD.state='building' AND NEW.state='building'
                    AND OLD.lease_token IS NULL AND OLD.lease_expires_at IS NULL
                    AND NEW.lease_token IS NOT NULL AND NEW.lease_expires_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Video output lease may be claimed exactly once'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_lease_once';
            END IF;
            IF ROW(NEW.inventory,NEW.result) IS DISTINCT FROM ROW(OLD.inventory,OLD.result)
                AND NOT (OLD.state='building' AND NEW.state='ready'
                    AND OLD.inventory IS NULL AND OLD.result IS NULL
                    AND NEW.inventory IS NOT NULL AND NEW.result IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Video result and inventory are published exactly once'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_publication_immutable';
            END IF;
            IF NEW.error_code IS DISTINCT FROM OLD.error_code
                AND NEW.state NOT IN ('building','retiring')
            THEN
                RAISE EXCEPTION 'Video output errors may change only before publication or while retiring'
                    USING ERRCODE='23514', CONSTRAINT='ck_video_derivative_error_state';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_video_derivative_output BEFORE INSERT OR UPDATE OR DELETE ON video_derivative_outputs FOR EACH ROW EXECUTE FUNCTION guard_video_derivative_output()"
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM video_derivative_outputs) "
            "OR EXISTS (SELECT 1 FROM dataset_items WHERE video_output_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM video_frame_cache WHERE output_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM video_chunks WHERE output_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("Cannot discard saved video output ownership or references")
    for table, column, constraint in (
        ("video_chunks", "output_id", "fk_video_chunk_output"),
        ("video_frame_cache", "output_id", "fk_video_frame_cache_output"),
        ("dataset_items", "video_output_id", "fk_dataset_item_video_output"),
    ):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.drop_column(table, column)
    op.execute("DROP TRIGGER trg_video_derivative_output ON video_derivative_outputs")
    op.execute("DROP FUNCTION guard_video_derivative_output()")
    op.drop_index(
        "ix_video_derivative_outputs_item_kind", table_name="video_derivative_outputs"
    )
    op.drop_index(
        "ix_video_derivative_outputs_cleanup", table_name="video_derivative_outputs"
    )
    op.drop_table("video_derivative_outputs")
