ALTER TABLE public.export_media_bundles ADD COLUMN fixture_zip_created boolean NOT NULL DEFAULT false;
-- aap-statement-break
CREATE TABLE public.fixture_export_zip_retirements (
    bundle_id uuid PRIMARY KEY,
    output_id uuid NOT NULL UNIQUE,
    fixture_run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    job_id uuid NOT NULL,
    sha256 varchar(64) NOT NULL,
    size_bytes bigint NOT NULL,
    retired_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON public.fixture_export_zip_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_zip_state()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF OLD.fixture_zip_created AND NOT NEW.fixture_zip_created THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'export ZIP creation is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE TRIGGER trg_export_media_fixture_zip_guard
BEFORE UPDATE OF fixture_zip_created ON public.export_media_bundles
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_zip_state();
-- aap-statement-break
CREATE FUNCTION public.record_fixture_export_zip_creation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.media_bundle_id IS NOT NULL THEN
        UPDATE public.export_media_bundles SET fixture_zip_created = true
        WHERE id = NEW.media_bundle_id AND fixture_run_id IS NOT NULL;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE TRIGGER trg_export_output_fixture_creation
AFTER INSERT ON public.export_outputs
FOR EACH ROW EXECUTE FUNCTION public.record_fixture_export_zip_creation();
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_zip_receipt()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'export ZIP retirement receipt is immutable';
END;
$function$;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_zip_receipt_immutable
BEFORE UPDATE OR DELETE ON public.fixture_export_zip_retirements
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_zip_receipt();
