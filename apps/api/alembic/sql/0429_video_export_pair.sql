CREATE OR REPLACE FUNCTION public.check_export_media_pair()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
        DECLARE
            bundle public.export_media_bundles%ROWTYPE;
            output public.export_outputs%ROWTYPE;
            first_ready boolean := false;
        BEGIN
            IF TG_TABLE_NAME='export_outputs' THEN
                SELECT * INTO output FROM public.export_outputs WHERE id=NEW.id;
                IF NOT FOUND OR output.media_bundle_id IS NULL THEN RETURN NULL; END IF;
                SELECT * INTO bundle FROM public.export_media_bundles WHERE id=output.media_bundle_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Export ZIP has no media owner'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
                END IF;
                IF TG_OP='INSERT' THEN first_ready := NEW.state='ready';
                ELSE first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
            ELSE
                SELECT * INTO bundle FROM public.export_media_bundles WHERE id=NEW.id;
                IF NOT FOUND THEN RETURN NULL; END IF;
                IF TG_OP='UPDATE' THEN first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
                SELECT * INTO output FROM public.export_outputs WHERE media_bundle_id=bundle.id;
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
                OR output.media IS DISTINCT FROM (CASE WHEN bundle.input_snapshot->'schema' = '4'::jsonb
                    THEN 'video' ELSE 'image' END)
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
        END
$function$;
