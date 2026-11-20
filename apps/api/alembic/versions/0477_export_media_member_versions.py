"""Keep external export member PUT receipts separate from the frozen inventory."""

from alembic import op

revision = "bo0477"
down_revision = "bo0476"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.export_media_member_versions (
            bundle_id uuid NOT NULL REFERENCES public.export_media_bundles(id) ON DELETE CASCADE,
            member_id uuid NOT NULL,
            version_id varchar(1024) NOT NULL CHECK (version_id <> '' AND version_id <> 'null'),
            PRIMARY KEY (bundle_id, member_id)
        )
    """)
    op.execute("""
        CREATE FUNCTION public.check_export_media_member_version() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM public.export_media_bundles WHERE id = OLD.bundle_id
            ) THEN RETURN OLD; END IF;
            IF TG_OP = 'INSERT' AND EXISTS (
                SELECT 1 FROM public.export_media_bundles b
                WHERE b.id = NEW.bundle_id AND b.state = 'building'
                    AND b.lease_token IS NOT NULL AND b.lease_expires_at > statement_timestamp()
                    AND b.inventory->'objects' ? (
                        (b.storage_ref->>'key_prefix') || NEW.member_id::text || '.bin'
                    )
            ) THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'export_media_member_version_not_admitted' USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER export_media_member_version_write_once
        BEFORE INSERT OR UPDATE OR DELETE ON public.export_media_member_versions
        FOR EACH ROW EXECUTE FUNCTION public.check_export_media_member_version()
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore the registry and versioned objects together")
