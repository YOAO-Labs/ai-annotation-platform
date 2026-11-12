ALTER TABLE public.export_outputs ADD COLUMN upload_proof jsonb;
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_export_zip_versions (
    output_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    job_id uuid NOT NULL,
    bundle_id uuid,
    storage_ref jsonb NOT NULL,
    sha256 varchar(64) NOT NULL,
    size_bytes bigint NOT NULL,
    upload_proof jsonb NOT NULL,
    confirmed_absent_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON yoao_fixture_v1.fixture_export_zip_versions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_export_zip_version_proof()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_private boolean;
BEGIN
    SELECT EXISTS (SELECT 1 FROM public.export_media_bundles AS bundle
        WHERE bundle.id = OLD.media_bundle_id AND bundle.fixture_run_id IS NOT NULL)
        OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs AS native
        WHERE native.output_id = OLD.id AND native.job_id = OLD.job_id
          AND native.project_id = OLD.project_id) INTO v_private;
    IF TG_OP = 'DELETE' THEN
        IF v_private AND NOT EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_export_zip_versions AS proof
            WHERE proof.output_id = OLD.id AND proof.job_id = OLD.job_id
              AND proof.project_id = OLD.project_id AND proof.storage_ref = OLD.storage_ref
              AND proof.sha256 = OLD.sha256 AND proof.size_bytes = OLD.size_bytes
              AND proof.upload_proof = OLD.upload_proof AND proof.confirmed_absent_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture ZIP absence is unproven';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.upload_proof IS DISTINCT FROM OLD.upload_proof THEN
        IF OLD.upload_proof IS NOT NULL OR OLD.state <> 'pending' OR NEW.state <> 'pending'
           OR NOT COALESCE((jsonb_typeof(NEW.upload_proof) = 'object'
             AND NEW.upload_proof ?& ARRAY['version_id', 'etag']
             AND NEW.upload_proof - ARRAY['version_id', 'etag']::text[] = '{}'::jsonb
             AND (NEW.upload_proof->'version_id' = 'null'::jsonb OR (
               jsonb_typeof(NEW.upload_proof->'version_id') = 'string'
               AND length(NEW.upload_proof->>'version_id') BETWEEN 1 AND 1024
               AND NEW.upload_proof->>'version_id' <> 'null'
               AND NEW.upload_proof->>'version_id' !~ '[[:cntrl:]]'))
             AND jsonb_typeof(NEW.upload_proof->'etag') = 'string'
             AND length(NEW.upload_proof->>'etag') BETWEEN 1 AND 1024
             AND NEW.upload_proof->>'etag' !~ '[[:cntrl:]]'), false) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid or changed ZIP upload proof';
        END IF;
    END IF;
    IF v_private AND NEW.state = 'ready' AND (NEW.upload_proof IS NULL OR NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_export_zip_versions AS proof
        WHERE proof.output_id = NEW.id AND proof.project_id = NEW.project_id
          AND proof.job_id = NEW.job_id AND proof.bundle_id IS NOT DISTINCT FROM NEW.media_bundle_id
          AND proof.storage_ref = NEW.storage_ref AND proof.sha256 = NEW.sha256
          AND proof.size_bytes = NEW.size_bytes AND proof.upload_proof = NEW.upload_proof
          AND proof.confirmed_absent_at IS NULL
    )) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture ZIP upload is unproven';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_export_zip_version_proof() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_export_zip_version_proof
BEFORE UPDATE OF upload_proof, state OR DELETE ON public.export_outputs
FOR EACH ROW EXECUTE FUNCTION public.guard_export_zip_version_proof();
-- aap-statement-break
CREATE FUNCTION public.capture_fixture_export_zip_version()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run uuid;
BEGIN
    IF OLD.upload_proof IS NOT NULL OR NEW.upload_proof IS NULL THEN RETURN NEW; END IF;
    IF NEW.media_bundle_id IS NOT NULL THEN
        SELECT bundle.fixture_run_id INTO v_run FROM public.export_media_bundles AS bundle
        WHERE bundle.id = NEW.media_bundle_id AND bundle.project_id = NEW.project_id
          AND bundle.job_id = NEW.job_id AND bundle.zip_output_id = NEW.id;
    ELSE
        SELECT native.run_id INTO v_run FROM yoao_fixture_v1.fixture_native_export_outputs AS native
        WHERE native.output_id = NEW.id AND native.project_id = NEW.project_id AND native.job_id = NEW.job_id;
    END IF;
    IF v_run IS NOT NULL THEN
        INSERT INTO yoao_fixture_v1.fixture_export_zip_versions
            (output_id, run_id, project_id, job_id, bundle_id, storage_ref, sha256, size_bytes, upload_proof)
        VALUES (NEW.id, v_run, NEW.project_id, NEW.job_id, NEW.media_bundle_id,
                NEW.storage_ref, NEW.sha256, NEW.size_bytes, NEW.upload_proof);
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.capture_fixture_export_zip_version() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_capture_fixture_export_zip_version
AFTER UPDATE OF upload_proof ON public.export_outputs
FOR EACH ROW EXECUTE FUNCTION public.capture_fixture_export_zip_version();
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_zip_version()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.confirmed_absent_at IS NOT NULL OR NEW.confirmed_absent_at IS NULL
       OR (to_jsonb(NEW) - 'confirmed_absent_at') IS DISTINCT FROM (to_jsonb(OLD) - 'confirmed_absent_at')
       OR NOT public.fixture_export_owner_session() THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture ZIP version evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_export_zip_version() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_zip_version_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_export_zip_versions
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_zip_version();
-- aap-statement-break
CREATE FUNCTION public.confirm_fixture_export_zip_absent(p_output_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT public.fixture_export_owner_session() THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'fixture export retirement requires the private owner session';
    END IF;
    UPDATE yoao_fixture_v1.fixture_export_zip_versions AS proof
    SET confirmed_absent_at = pg_catalog.clock_timestamp()
    FROM public.export_outputs AS output
    WHERE proof.output_id = p_output_id AND output.id = proof.output_id
      AND output.state = 'retired' AND proof.confirmed_absent_at IS NULL
      AND proof.project_id = output.project_id AND proof.job_id = output.job_id
      AND proof.bundle_id IS NOT DISTINCT FROM output.media_bundle_id
      AND proof.storage_ref = output.storage_ref AND proof.sha256 = output.sha256
      AND proof.size_bytes = output.size_bytes AND proof.upload_proof = output.upload_proof
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS operation WHERE operation.purpose IN (
          'export_write_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_read_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_delete_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_media_' || pg_catalog.replace(output.media_bundle_id::text, '-', '')
      ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.confirm_fixture_export_zip_absent(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_zip_retirement_proof()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'fixture_export_zip_retirements' THEN
        IF NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_export_zip_versions AS proof
          WHERE proof.output_id = NEW.output_id AND proof.run_id = NEW.fixture_run_id
            AND proof.project_id = NEW.project_id AND proof.job_id = NEW.job_id
            AND proof.bundle_id = NEW.bundle_id AND proof.sha256 = NEW.sha256
            AND proof.size_bytes = NEW.size_bytes AND proof.confirmed_absent_at IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture ZIP retirement lacks version evidence';
        END IF;
    ELSIF NEW.retired_at IS NOT NULL AND OLD.retired_at IS NULL AND NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_export_zip_versions AS proof
        WHERE proof.output_id = NEW.output_id AND proof.run_id = NEW.run_id
          AND proof.project_id = NEW.project_id AND proof.job_id = NEW.job_id
          AND proof.bundle_id IS NULL AND proof.sha256 = NEW.sha256
          AND proof.size_bytes = NEW.size_bytes AND proof.confirmed_absent_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture native ZIP retirement lacks version evidence';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_zip_retirement_proof() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_zip_retirement_proof
BEFORE INSERT ON public.fixture_export_zip_retirements
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_zip_retirement_proof();
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_zip_retirement_proof
BEFORE UPDATE OF retired_at ON yoao_fixture_v1.fixture_native_export_outputs
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_zip_retirement_proof();
