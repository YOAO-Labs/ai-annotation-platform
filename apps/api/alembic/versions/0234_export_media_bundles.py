"""Retain canonical LiDAR delivery media independently of jobs and ZIPs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0234"
down_revision = "0233"
branch_labels = None
depends_on = None

# Freeze these checks rather than importing changing application models.
_REF_CHECK = r"""
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'export'
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
    AND storage_ref->>'key_prefix' = 'lidar-export-media/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

_DOCUMENTS_CHECK = """
COALESCE((jsonb_typeof(input_snapshot) = 'object'
    AND jsonb_typeof(logical_paths) = 'object'
    AND logical_paths ?& ARRAY['schema','targets']
    AND logical_paths - ARRAY['schema','targets']::text[] = '{}'::jsonb
    AND logical_paths->'schema' = '1'::jsonb AND logical_paths->>'schema' = '1'
    AND jsonb_typeof(logical_paths->'targets') = 'object'
    AND jsonb_typeof(inventory) = 'object'
    AND inventory ?& ARRAY['schema','objects']
    AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb
    AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1'
    AND jsonb_typeof(inventory->'objects') = 'object'
    AND octet_length(input_snapshot::text) + octet_length(logical_paths::text)
        + octet_length(inventory::text) <= 268435456), false)
"""


def upgrade():
    op.create_table(
        "export_media_bundles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("zip_output_id", UUID(as_uuid=True), nullable=False),
        sa.Column("input_snapshot", JSONB(none_as_null=True), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("logical_paths", JSONB(none_as_null=True), nullable=False),
        sa.Column("inventory", JSONB(none_as_null=True), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="building"),
        sa.Column("lease_token", UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("zip_output_id", name="uq_export_media_bundle_zip_output"),
        sa.CheckConstraint(
            "input_digest ~ '^[0-9a-f]{64}$'", name="ck_export_media_input_digest"
        ),
        sa.CheckConstraint(_DOCUMENTS_CHECK, name="ck_export_media_documents"),
        sa.CheckConstraint(
            "object_count BETWEEN 0 AND 21000 AND size_bytes BETWEEN 0 AND 25769803776",
            name="ck_export_media_budget",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_export_media_storage_ref"),
        sa.CheckConstraint(
            "state IN ('building','ready','retiring','deleted')",
            name="ck_export_media_state",
        ),
        sa.CheckConstraint(
            "expires_at = created_at + INTERVAL '168 hours'",
            name="ck_export_media_expiry",
        ),
        sa.CheckConstraint(
            "((lease_token IS NULL) = (lease_expires_at IS NULL)) "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= expires_at) "
            "AND (state != 'ready' OR lease_token IS NOT NULL)",
            name="ck_export_media_lease",
        ),
        sa.CheckConstraint(
            "(state != 'building' OR published_at IS NULL) "
            "AND (state != 'ready' OR published_at IS NOT NULL) "
            "AND (published_at IS NULL OR "
            "(published_at >= created_at AND published_at < expires_at))",
            name="ck_export_media_publication",
        ),
        sa.CheckConstraint(
            "((state IN ('retiring','deleted')) = (retired_at IS NOT NULL)) "
            "AND (retired_at IS NULL OR retired_at >= created_at)",
            name="ck_export_media_retirement",
        ),
    )
    op.create_index(
        "ix_export_media_bundle_cleanup",
        "export_media_bundles",
        ["state", "last_attempted_at", "id"],
    )
    op.create_index("ix_export_media_bundle_job", "export_media_bundles", ["job_id"])
    op.add_column("export_outputs", sa.Column("media_bundle_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_export_output_media_bundle",
        "export_outputs",
        "export_media_bundles",
        ["media_bundle_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_export_output_media_bundle", "export_outputs", ["media_bundle_id"]
    )
    op.execute(r"""
        CREATE FUNCTION guard_export_media_bundle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            member record;
            counted integer := 0;
            total bigint := 0;
            member_size numeric;
        BEGIN
            IF TG_OP='DELETE' THEN
                IF OLD.state <> 'deleted' THEN
                    RAISE EXCEPTION 'Unsettled export media owners must be retained'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_delete_guard';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.state <> 'building' OR NEW.lease_token IS NOT NULL
                    OR NEW.lease_expires_at IS NOT NULL OR NEW.published_at IS NOT NULL
                    OR NEW.retired_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'New export media must begin unclaimed and building'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_initial';
                END IF;
                IF jsonb_typeof(NEW.inventory->'objects') IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION 'Expected export media inventory is required'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                END IF;
                FOR member IN SELECT key, value FROM jsonb_each(NEW.inventory->'objects') LOOP
                    counted := counted + 1;
                    IF jsonb_typeof(member.value) IS DISTINCT FROM 'object' THEN
                        RAISE EXCEPTION 'Expected export media receipt must be an object'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                    END IF;
                    IF counted > 21000 OR NOT COALESCE(
                        member.key ~ ('^' || (NEW.storage_ref->>'key_prefix')
                            || '[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\.bin$')
                        AND jsonb_typeof(member.value) = 'object'
                        AND member.value ?& ARRAY['sha256','size_bytes']
                        AND member.value - ARRAY['sha256','size_bytes']::text[] = '{}'::jsonb
                        AND jsonb_typeof(member.value->'sha256') = 'string'
                        AND member.value->>'sha256' ~ '^[0-9a-f]{64}$'
                        AND jsonb_typeof(member.value->'size_bytes') = 'number'
                        AND member.value->>'size_bytes' ~ '^[0-9]+$', false)
                    THEN
                        RAISE EXCEPTION 'Expected export media member is invalid'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                    END IF;
                    member_size := (member.value->>'size_bytes')::numeric;
                    IF member_size <= 0 OR member_size > 268435456 THEN
                        RAISE EXCEPTION 'Expected export media member exceeds its budget'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                    END IF;
                    total := total + member_size::bigint;
                    IF total > 25769803776 THEN
                        RAISE EXCEPTION 'Expected export media exceeds its total budget'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                    END IF;
                END LOOP;
                IF counted IS DISTINCT FROM NEW.object_count OR total IS DISTINCT FROM NEW.size_bytes THEN
                    RAISE EXCEPTION 'Expected export media accounting must be exact'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_inventory';
                END IF;
                RETURN NEW;
            END IF;
            -- The separate storage_ref trigger owns the narrow offline restore exception.
            IF OLD.state='deleted' AND (to_jsonb(NEW) - 'storage_ref') IS DISTINCT FROM (to_jsonb(OLD) - 'storage_ref') THEN
                RAISE EXCEPTION 'Deleted export media owners are terminal'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_terminal';
            END IF;
            IF ROW(NEW.id,NEW.project_id,NEW.job_id,NEW.zip_output_id,
                    NEW.input_snapshot,NEW.input_digest,NEW.logical_paths,NEW.inventory,
                    NEW.object_count,NEW.size_bytes,NEW.created_at,NEW.expires_at)
                IS DISTINCT FROM
                ROW(OLD.id,OLD.project_id,OLD.job_id,OLD.zip_output_id,
                    OLD.input_snapshot,OLD.input_digest,OLD.logical_paths,OLD.inventory,
                    OLD.object_count,OLD.size_bytes,OLD.created_at,OLD.expires_at)
            THEN
                RAISE EXCEPTION 'Export media inputs, identity and deadline are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_facts_immutable';
            END IF;
            IF NOT (NEW.state=OLD.state
                OR (OLD.state='building' AND NEW.state IN ('ready','retiring'))
                OR (OLD.state='ready' AND NEW.state='retiring')
                OR (OLD.state='retiring' AND NEW.state='deleted'))
            THEN
                RAISE EXCEPTION 'Export media state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_state_forward';
            END IF;
            IF ROW(NEW.lease_token,NEW.lease_expires_at) IS DISTINCT FROM ROW(OLD.lease_token,OLD.lease_expires_at) THEN
                IF NOT (OLD.state='building' AND NEW.state='building'
                    AND OLD.lease_token IS NULL AND OLD.lease_expires_at IS NULL
                    AND NEW.lease_token IS NOT NULL AND NEW.lease_expires_at IS NOT NULL
                    AND NEW.lease_expires_at > clock_timestamp()
                    AND NEW.lease_expires_at <= clock_timestamp() + INTERVAL '3600 seconds')
                THEN
                    RAISE EXCEPTION 'Export media lease may be claimed exactly once within its budget'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_lease_once';
                END IF;
            END IF;
            IF NEW.published_at IS DISTINCT FROM OLD.published_at
                AND NOT (OLD.state='building' AND NEW.state='ready'
                    AND OLD.published_at IS NULL AND NEW.published_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Export media publication is permanent'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_publication_once';
            END IF;
            IF OLD.state='building' AND NEW.state='ready'
                AND (NEW.lease_token IS NULL OR NEW.lease_expires_at <= clock_timestamp()
                    OR NEW.expires_at <= clock_timestamp())
            THEN
                RAISE EXCEPTION 'Expired export media cannot publish'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_publication_expired';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
                RAISE EXCEPTION 'Export media retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_retirement_immutable';
            END IF;
            IF OLD.state='retiring' AND NEW.state='deleted'
                AND NEW.published_at IS NOT NULL AND NEW.expires_at > clock_timestamp()
            THEN
                RAISE EXCEPTION 'Published export media must survive its delivery deadline'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_delivery_retention';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_export_media_bundle BEFORE INSERT OR UPDATE OR DELETE ON export_media_bundles FOR EACH ROW EXECUTE FUNCTION guard_export_media_bundle()"
    )
    op.execute(
        "CREATE TRIGGER trg_export_media_storage_ref BEFORE UPDATE ON export_media_bundles FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )
    op.execute("""
        CREATE FUNCTION guard_export_output_bundle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.media_bundle_id IS DISTINCT FROM OLD.media_bundle_id THEN
                RAISE EXCEPTION 'Export ZIP media association is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_output_media_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_export_output_bundle BEFORE UPDATE ON export_outputs FOR EACH ROW EXECUTE FUNCTION guard_export_output_bundle()"
    )
    op.execute("""
        CREATE FUNCTION check_export_media_pair() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            bundle export_media_bundles%ROWTYPE;
            output export_outputs%ROWTYPE;
            first_ready boolean := false;
        BEGIN
            IF TG_TABLE_NAME='export_outputs' THEN
                SELECT * INTO output FROM export_outputs WHERE id=NEW.id;
                IF NOT FOUND OR output.media_bundle_id IS NULL THEN RETURN NULL; END IF;
                SELECT * INTO bundle FROM export_media_bundles WHERE id=output.media_bundle_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Export ZIP has no media owner'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
                END IF;
                IF TG_OP='INSERT' THEN first_ready := NEW.state='ready';
                ELSE first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
            ELSE
                SELECT * INTO bundle FROM export_media_bundles WHERE id=NEW.id;
                IF NOT FOUND THEN RETURN NULL; END IF;
                IF TG_OP='UPDATE' THEN first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
                SELECT * INTO output FROM export_outputs WHERE media_bundle_id=bundle.id;
                IF NOT FOUND THEN
                    IF NOT first_ready THEN RETURN NULL; END IF;
                    RAISE EXCEPTION 'Published export media has no matching ZIP'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
                END IF;
            END IF;
            IF output.id IS DISTINCT FROM bundle.zip_output_id
                OR output.project_id IS DISTINCT FROM bundle.project_id
                OR output.job_id IS DISTINCT FROM bundle.job_id
                OR output.expires_at IS DISTINCT FROM bundle.expires_at
                OR output.media IS DISTINCT FROM 'image'
                OR (first_ready AND (output.state<>'ready' OR bundle.state<>'ready' OR bundle.published_at IS NULL))
            THEN
                RAISE EXCEPTION 'Export ZIP and media must describe the same delivery'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
            END IF;
            IF first_ready AND (bundle.lease_expires_at <= clock_timestamp()
                OR bundle.expires_at <= clock_timestamp())
            THEN
                RAISE EXCEPTION 'Expired export media cannot publish'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_publication_expired';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER ct_export_output_media_pair AFTER INSERT OR UPDATE ON export_outputs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_export_media_pair()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER ct_export_media_output_pair AFTER INSERT OR UPDATE ON export_media_bundles DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_export_media_pair()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE export_media_bundles, export_outputs IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM export_media_bundles) OR EXISTS "
            "(SELECT 1 FROM export_outputs WHERE media_bundle_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("Cannot discard saved export media owners or associations")
    op.execute("DROP TRIGGER ct_export_media_output_pair ON export_media_bundles")
    op.execute("DROP TRIGGER ct_export_output_media_pair ON export_outputs")
    op.execute("DROP FUNCTION check_export_media_pair()")
    op.execute("DROP TRIGGER trg_export_output_bundle ON export_outputs")
    op.execute("DROP FUNCTION guard_export_output_bundle()")
    op.drop_constraint(
        "uq_export_output_media_bundle", "export_outputs", type_="unique"
    )
    op.drop_constraint(
        "fk_export_output_media_bundle", "export_outputs", type_="foreignkey"
    )
    op.drop_column("export_outputs", "media_bundle_id")
    op.execute("DROP TRIGGER trg_export_media_storage_ref ON export_media_bundles")
    op.execute("DROP TRIGGER trg_export_media_bundle ON export_media_bundles")
    op.execute("DROP FUNCTION guard_export_media_bundle()")
    op.drop_index("ix_export_media_bundle_job", table_name="export_media_bundles")
    op.drop_index("ix_export_media_bundle_cleanup", table_name="export_media_bundles")
    op.drop_table("export_media_bundles")
