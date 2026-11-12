-- The receipt-table owner is the verified private fixture operator.  A caller
-- cannot acquire this authority by invoking a SECURITY DEFINER function or by
-- changing current_role: it is bound to the authenticated session identity.
CREATE FUNCTION public.fixture_export_owner_session()
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER, pg_catalog.pg_get_userbyid(c.relowner), 'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid = pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ), false);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_export_owner_session() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_private_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_private boolean := false;
BEGIN
    IF TG_TABLE_NAME = 'fixture_export_zip_retirements' THEN
        v_private := true;
    ELSIF TG_TABLE_NAME = 'fixture_native_export_outputs' THEN
        IF TG_OP = 'INSERT' THEN
            v_private := NEW.retired_at IS NOT NULL;
        ELSE
            v_private := NEW.retired_at IS DISTINCT FROM OLD.retired_at;
        END IF;
    ELSIF TG_TABLE_NAME = 'export_media_bundles' THEN
        v_private := OLD.fixture_run_id IS NOT NULL
          AND NEW.state IS DISTINCT FROM OLD.state AND NEW.state IN ('retiring', 'deleted');
    ELSIF TG_TABLE_NAME = 'export_outputs' THEN
        IF TG_OP = 'UPDATE' AND (NEW.state IS NOT DISTINCT FROM OLD.state OR NEW.state <> 'retired') THEN
            RETURN NEW;
        END IF;
        v_private := EXISTS (
            SELECT 1 FROM public.export_media_bundles AS bundle
            WHERE bundle.id = OLD.media_bundle_id AND bundle.fixture_run_id IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs AS native
            WHERE native.output_id = OLD.id AND native.job_id = OLD.job_id
              AND native.project_id = OLD.project_id
        );
    END IF;
    IF v_private AND NOT public.fixture_export_owner_session() THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'fixture export retirement requires the private owner session';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_export_private_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_zip_receipt_owner
BEFORE INSERT ON public.fixture_export_zip_retirements
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_private_retirement();
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_media_retirement_owner
BEFORE UPDATE OF state ON public.export_media_bundles
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_private_retirement();
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_output_retirement_owner
BEFORE UPDATE OF state OR DELETE ON public.export_outputs
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_private_retirement();
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_export_receipt_owner
BEFORE INSERT OR UPDATE OF retired_at ON yoao_fixture_v1.fixture_native_export_outputs
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_private_retirement();
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.retire_fixture_native_export_output(p_output_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT public.fixture_export_owner_session() THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'fixture export retirement requires the private owner session';
    END IF;
    IF NOT public.fixture_native_export_cleanup_eligible(p_output_id) THEN RETURN false; END IF;
    UPDATE yoao_fixture_v1.fixture_native_export_outputs AS receipt SET retired_at = pg_catalog.clock_timestamp()
    FROM public.export_outputs AS output
    WHERE receipt.output_id = p_output_id AND receipt.output_id = output.id
      AND receipt.job_id = output.job_id AND receipt.project_id = output.project_id
      AND receipt.sha256 = output.sha256 AND receipt.size_bytes = output.size_bytes
      AND output.media_bundle_id IS NULL AND output.state = 'retired'
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS operation WHERE operation.purpose IN (
          'export_write_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_read_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_delete_' || pg_catalog.replace(output.id::text, '-', '')
      ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.retire_fixture_native_export_output(uuid) FROM PUBLIC;
