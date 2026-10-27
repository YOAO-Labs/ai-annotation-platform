"""Retain immutable sealed read publications and exact source/target versions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None

PUBLICATIONS = "storage_migration_read_publications"
ROUTES = "storage_migration_read_routes"


def upgrade():
    columns = [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_migration_finalizations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
    ]
    for name, table in (
        ("window_id", "storage_migration_windows"),
        ("copy_id", "storage_migration_copies"),
        ("target_location_id", "storage_locations"),
    ):
        columns.append(
            sa.Column(
                name,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
                nullable=False,
            )
        )
    columns.extend(
        sa.Column(name, postgresql.UUID(as_uuid=True), nullable=False)
        for name in ("source_location_id", "created_by")
    )
    columns.extend(
        sa.Column(name, sa.Integer(), nullable=False)
        for name in (
            "revision",
            "finalization_revision",
            "window_revision",
            "route_count",
        )
    )
    columns.extend(
        sa.Column(name, postgresql.JSONB(), nullable=False)
        for name in (
            "source",
            "target",
            "target_revisions",
            "token",
            "evidence",
            "native",
        )
    )
    columns.extend(
        [
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("mapping_sha256", sa.String(64), nullable=False),
        ]
    )
    columns.extend(
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=nullable,
            server_default=sa.func.now() if not nullable else None,
        )
        for name, nullable in (
            ("created_at", False),
            ("updated_at", False),
            ("withdrawing_at", True),
            ("withdrawn_at", True),
        )
    )
    op.create_table(
        PUBLICATIONS,
        *columns,
        sa.CheckConstraint(
            "state IN ('active','withdrawing','withdrawn')",
            name="ck_migration_read_publication_state",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND finalization_revision >= 1 AND window_revision >= 1",
            name="ck_migration_read_publication_revision",
        ),
        sa.CheckConstraint(
            "mapping_sha256 ~ '^[a-f0-9]{64}$' AND route_count BETWEEN 0 AND 100000",
            name="ck_migration_read_publication_mapping",
        ),
        sa.CheckConstraint(
            "octet_length(source::text) <= 65536 AND octet_length(target::text) <= 65536 AND octet_length(token::text) <= 65536 AND octet_length(evidence::text) <= 65536 AND octet_length(native::text) <= 65536",
            name="ck_migration_read_publication_budget",
        ),
        sa.CheckConstraint(
            "(state = 'withdrawn') = (withdrawn_at IS NOT NULL) AND (state <> 'active') = (withdrawing_at IS NOT NULL)",
            name="ck_migration_read_publication_withdrawn",
        ),
    )
    op.create_index(
        "uq_migration_read_publication_active_source",
        PUBLICATIONS,
        ["source_location_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'withdrawn'"),
    )
    op.create_index("ix_migration_read_publication_copy", PUBLICATIONS, ["copy_id"])
    op.create_table(
        ROUTES,
        sa.Column(
            "publication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{PUBLICATIONS}.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("source_object_id", sa.String(64), primary_key=True),
        *[
            sa.Column(name, sa.Text(), nullable=False)
            for name in (
                "endpoint_url",
                "bucket",
                "object_key",
                "source_etag",
                "target_key",
                "target_version_id",
                "target_etag",
                "nonce",
                "creation_plan_uuid",
            )
        ],
        sa.Column("source_version_id", sa.Text()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("target_content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "source_object_id ~ '^[a-f0-9]{64}$' AND sha256 ~ '^[a-f0-9]{64}$' AND target_content_sha256 ~ '^[a-f0-9]{64}$' AND size_bytes >= 0",
            name="ck_migration_read_route_content",
        ),
        sa.CheckConstraint(
            "target_version_id NOT IN ('','null') AND target_etag <> '' AND source_etag <> '' AND nonce <> '' AND creation_plan_uuid <> ''",
            name="ck_migration_read_route_receipt",
        ),
        sa.CheckConstraint(
            "octet_length(endpoint_url) <= 2048 AND octet_length(bucket) BETWEEN 1 AND 255 AND octet_length(object_key) BETWEEN 1 AND 1024 AND octet_length(target_key) BETWEEN 1 AND 1024 AND octet_length(source_etag) <= 1024 AND octet_length(target_etag) <= 1024 AND octet_length(target_version_id) <= 1024 AND (source_version_id IS NULL OR octet_length(source_version_id) <= 1024)",
            name="ck_migration_read_route_budget",
        ),
    )
    op.create_index(
        "ix_migration_read_route_lookup",
        ROUTES,
        ["publication_id", "sha256", "size_bytes"],
    )
    op.execute("""
        CREATE FUNCTION guard_storage_migration_read_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Read publication responsibility and history must be retained' USING ERRCODE='23514';
            END IF;
            IF ROW(NEW.id, NEW.window_id, NEW.copy_id, NEW.source_location_id, NEW.target_location_id,
                   NEW.finalization_revision, NEW.window_revision, NEW.source, NEW.target, NEW.target_revisions,
                   NEW.token, NEW.evidence, NEW.native, NEW.mapping_sha256, NEW.route_count, NEW.created_by, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.window_id, OLD.copy_id, OLD.source_location_id, OLD.target_location_id,
                   OLD.finalization_revision, OLD.window_revision, OLD.source, OLD.target, OLD.target_revisions,
                   OLD.token, OLD.evidence, OLD.native, OLD.mapping_sha256, OLD.route_count, OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'Read publication identity and evidence are immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.state = 'withdrawn' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Withdrawn read publication is historical' USING ERRCODE='23514';
            END IF;
            IF (OLD.withdrawing_at IS NOT NULL AND NEW.withdrawing_at IS DISTINCT FROM OLD.withdrawing_at)
               OR (OLD.withdrawn_at IS NOT NULL AND NEW.withdrawn_at IS DISTINCT FROM OLD.withdrawn_at) THEN
                RAISE EXCEPTION 'Read withdrawal timestamps are immutable' USING ERRCODE='23514';
            END IF;
            IF NEW.state <> OLD.state AND NOT (
                (OLD.state = 'active' AND NEW.state = 'withdrawing') OR
                (OLD.state = 'withdrawing' AND NEW.state = 'withdrawn')) THEN
                RAISE EXCEPTION 'Invalid read publication transition' USING ERRCODE='23514';
            END IF;
            IF NEW IS DISTINCT FROM OLD AND NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'Read publication revision must advance once' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER storage_migration_read_publication_facts BEFORE UPDATE OR DELETE ON storage_migration_read_publications
        FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_read_publication();
    """)
    op.execute("""
        CREATE FUNCTION guard_storage_migration_read_route() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Exact read routes and history are immutable' USING ERRCODE='23514';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER storage_migration_read_route_facts BEFORE UPDATE OR DELETE ON storage_migration_read_routes
        FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_read_route();
    """)


def downgrade():
    op.execute(f"LOCK TABLE {PUBLICATIONS}, {ROUTES} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            f"SELECT EXISTS(SELECT 1 FROM {PUBLICATIONS}) OR EXISTS(SELECT 1 FROM {ROUTES})"
        )
    ):
        raise RuntimeError(
            "Read publications retain migration responsibility; keep schema for application rollback"
        )
    op.execute(f"DROP TRIGGER storage_migration_read_route_facts ON {ROUTES}")
    op.execute("DROP FUNCTION guard_storage_migration_read_route()")
    op.execute(
        f"DROP TRIGGER storage_migration_read_publication_facts ON {PUBLICATIONS}"
    )
    op.execute("DROP FUNCTION guard_storage_migration_read_publication()")
    op.drop_index("ix_migration_read_route_lookup", ROUTES)
    op.drop_table(ROUTES)
    op.drop_index("ix_migration_read_publication_copy", PUBLICATIONS)
    op.drop_index("uq_migration_read_publication_active_source", PUBLICATIONS)
    op.drop_table(PUBLICATIONS)
