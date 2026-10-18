ALTER TABLE public.async_jobs
    ADD COLUMN retained_project_id uuid;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retain_async_job_project_scope()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.retained_project_id IS NOT NULL THEN
            RAISE EXCEPTION 'retained async-job project scope is database-managed'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.retained_project_id IS NOT NULL
       AND NEW.retained_project_id IS DISTINCT FROM OLD.retained_project_id THEN
        RAISE EXCEPTION 'retained async-job project scope is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.project_id IS NOT NULL AND NEW.project_id IS NULL THEN
        NEW.retained_project_id := OLD.project_id;
    ELSIF OLD.retained_project_id IS NULL
          AND NEW.retained_project_id IS NOT NULL THEN
        RAISE EXCEPTION 'retained async-job project scope is database-managed'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.retained_project_id IS NOT NULL
       AND NEW.project_id IS NOT NULL
       AND NEW.project_id IS DISTINCT FROM NEW.retained_project_id THEN
        RAISE EXCEPTION 'async-job project scope cannot be rebound'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retain_async_job_project_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER async_jobs_retain_project_scope
BEFORE INSERT OR UPDATE OF project_id, retained_project_id ON public.async_jobs
FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.retain_async_job_project_scope();
