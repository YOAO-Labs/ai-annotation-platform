"""Pin standalone Task originals without cascading away physical responsibility."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0231"
down_revision = "0230"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_originals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "creation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_original_creations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("creation_id", name="uq_task_original_creation"),
        sa.CheckConstraint("id = creation_id", name="ck_task_original_creation_id"),
        sa.CheckConstraint(
            "location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid",
            name="ck_task_original_location",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' AND storage_identity ~ '^[0-9a-f]{64}$'",
            name="ck_task_original_sha",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_task_original_size"),
        sa.CheckConstraint(
            "object_key = '__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload'",
            name="ck_task_original_key",
        ),
        sa.CheckConstraint(
            "length(bucket) BETWEEN 1 AND 255 AND bucket !~ '[[:space:][:cntrl:]/?#@]' AND position(chr(92) in bucket) = 0",
            name="ck_task_original_bucket",
        ),
        sa.CheckConstraint(
            "length(etag) BETWEEN 1 AND 1024 AND etag !~ '[[:cntrl:]]'",
            name="ck_task_original_etag",
        ),
        sa.CheckConstraint(
            "version_id IS NULL OR (length(version_id) BETWEEN 1 AND 1024 AND version_id <> 'null' AND version_id !~ '[[:cntrl:]]')",
            name="ck_task_original_version",
        ),
    )
    op.add_column("tasks", sa.Column("original_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_tasks_original_id",
        "tasks",
        "task_originals",
        ["original_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute("""
        CREATE FUNCTION guard_task_original() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE relation_owner oid;
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'Task original responsibility cannot be deleted'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_immutable';
            END IF;
            IF TG_OP='INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM managed_original_creations c
                    WHERE c.id=NEW.creation_id AND c.id=NEW.id
                      AND c.owner_kind='task' AND c.dataset_id IS NULL
                      AND c.task_id=NEW.task_id AND c.project_id=NEW.project_id
                      AND c.status='verified' AND c.origin='direct'
                      AND c.location_id=NEW.location_id
                      AND c.storage_identity=NEW.storage_identity AND c.bucket=NEW.bucket
                      AND c.object_key=NEW.object_key AND c.sha256=NEW.sha256
                      AND c.size_bytes=NEW.size_bytes AND c.etag=NEW.etag
                      AND c.version_id IS NOT DISTINCT FROM NEW.version_id
                      AND c.completed_at=NEW.created_at
                ) THEN
                    RAISE EXCEPTION 'Task original requires matching verified creation'
                        USING ERRCODE='23514', CONSTRAINT='ck_task_original_creation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id,NEW.creation_id,NEW.task_id,NEW.project_id,NEW.location_id,
                   NEW.object_key,NEW.sha256,NEW.size_bytes,NEW.etag,NEW.version_id,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.creation_id,OLD.task_id,OLD.project_id,OLD.location_id,
                   OLD.object_key,OLD.sha256,OLD.size_bytes,OLD.etag,OLD.version_id,OLD.created_at)
            THEN
                RAISE EXCEPTION 'Task original facts are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_immutable';
            END IF;
            IF ROW(NEW.bucket,NEW.storage_identity) IS DISTINCT FROM ROW(OLD.bucket,OLD.storage_identity) THEN
                SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
                IF current_setting('aap.restore_training_storage',true)='on'
                   AND pg_has_role(current_user,relation_owner,'MEMBER')
                   AND NEW.bucket IS DISTINCT FROM OLD.bucket
                   AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
                THEN RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Task original location is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_location_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_original BEFORE INSERT OR UPDATE OR DELETE ON task_originals
        FOR EACH ROW EXECUTE FUNCTION guard_task_original()
    """)
    op.execute("""
        CREATE FUNCTION guard_task_original_attachment() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND OLD.original_id IS NOT NULL
               AND NEW.original_id IS DISTINCT FROM OLD.original_id THEN
                RAISE EXCEPTION 'Task original link cannot be rebound'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_attachment';
            END IF;
            IF NEW.original_id IS NOT NULL AND (
                NEW.dataset_item_id IS NOT NULL
                OR EXISTS (SELECT 1 FROM task_dataset_item_links WHERE task_id=NEW.id)
                OR NOT EXISTS (SELECT 1 FROM task_originals o
                    WHERE o.id=NEW.original_id AND o.task_id=NEW.id
                      AND o.project_id=NEW.project_id AND o.object_key=NEW.file_path)
            ) THEN
                RAISE EXCEPTION 'Task original attachment does not match standalone task'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_attachment';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_original_attachment BEFORE INSERT OR UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION guard_task_original_attachment()
    """)
    op.execute("""
        CREATE FUNCTION guard_task_original_dataset_link() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE current_original uuid;
        BEGIN
            -- Serialize both directions of the standalone/multifile boundary.
            -- Never wait behind a caller that may already own a link row.
            SELECT original_id INTO current_original FROM tasks
                WHERE id=NEW.task_id FOR UPDATE NOWAIT;
            IF current_original IS NOT NULL THEN
                RAISE EXCEPTION 'Standalone original cannot acquire dataset links'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_dataset_link';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_task_original_dataset_link BEFORE INSERT OR UPDATE ON task_dataset_item_links
        FOR EACH ROW EXECUTE FUNCTION guard_task_original_dataset_link()
    """)


def downgrade():
    op.execute(
        "LOCK TABLE task_originals, tasks, task_dataset_item_links IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM task_originals) OR EXISTS (SELECT 1 FROM tasks WHERE original_id IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Task original responsibility prevents destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_task_original_dataset_link ON task_dataset_item_links")
    op.execute("DROP FUNCTION guard_task_original_dataset_link()")
    op.execute("DROP TRIGGER trg_task_original_attachment ON tasks")
    op.execute("DROP FUNCTION guard_task_original_attachment()")
    op.drop_constraint("fk_tasks_original_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "original_id")
    op.execute("DROP TRIGGER trg_task_original ON task_originals")
    op.execute("DROP FUNCTION guard_task_original()")
    op.drop_table("task_originals")
