-- Drain existing admission writers before copying their final activity timestamps.
LOCK TABLE public.api_keys IN ACCESS EXCLUSIVE MODE;
-- aap-statement-break
CREATE TABLE public.identity_api_key_usage (
    api_key_id uuid PRIMARY KEY REFERENCES public.api_keys(id) ON DELETE CASCADE,
    last_used_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON public.identity_api_key_usage FROM PUBLIC;
-- aap-statement-break
INSERT INTO public.identity_api_key_usage (api_key_id, last_used_at)
SELECT id, last_used_at FROM public.api_keys;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1._initialize_api_key_usage()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.identity_api_key_usage (api_key_id) VALUES (NEW.id);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_identity_v1._initialize_api_key_usage() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER initialize_api_key_usage AFTER INSERT ON public.api_keys
FOR EACH ROW EXECUTE FUNCTION yoao_identity_v1._initialize_api_key_usage();
-- aap-statement-break
DO $owner$
DECLARE
    owner_name name;
    had_create boolean;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT owner_name
    FROM pg_catalog.pg_proc
    WHERE oid = 'yoao_identity_v1.confirm_api_key_principal(uuid,text)'::regprocedure;
    EXECUTE pg_catalog.format(
        'GRANT SELECT, INSERT, UPDATE ON public.identity_api_key_usage TO %I', owner_name
    );
    had_create := pg_catalog.has_schema_privilege(owner_name, 'yoao_identity_v1', 'CREATE');
    IF NOT had_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA yoao_identity_v1 TO %I', owner_name);
    END IF;
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION yoao_identity_v1._initialize_api_key_usage() OWNER TO %I', owner_name
    );
    IF NOT had_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM %I', owner_name);
    END IF;
END;
$owner$;
-- aap-statement-break
-- Change only admission's lock mode, its activity write, and the two consumers of that activity.
DO $migration$
DECLARE
    confirm_key constant regprocedure := 'yoao_identity_v1.confirm_api_key_principal(uuid,text)';
    manage_keys constant regprocedure := 'yoao_identity_keys_v1.execute_key_command(jsonb)';
    offboarding constant regprocedure := 'yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)';
    projection_before constant text := $projection_before$api_key.last_used_at$projection_before$;
    projection_after constant text := $projection_after$(SELECT usage.last_used_at FROM public.identity_api_key_usage AS usage WHERE usage.api_key_id = api_key.id)$projection_after$;
    patch record;
    definition text;
BEGIN
    FOR patch IN SELECT * FROM (VALUES
        (confirm_key, 1, $lock_before$FOR UPDATE NOWAIT;$lock_before$, $lock_after$FOR SHARE NOWAIT;$lock_after$),
        (confirm_key, 1, $write_before$    UPDATE public.api_keys AS key
    SET last_used_at = pg_catalog.clock_timestamp()
    WHERE key.id = p_key_id;$write_before$, $write_after$    BEGIN
        UPDATE public.identity_api_key_usage AS usage
        SET last_used_at = pg_catalog.clock_timestamp()
        WHERE usage.api_key_id = (
            SELECT activity.api_key_id FROM public.identity_api_key_usage AS activity
            WHERE activity.api_key_id = p_key_id FOR UPDATE NOWAIT
        );
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing activity row for existing API key' USING ERRCODE = '23503';
        END IF;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN yoao_identity_v1._admission_failure('credential_busy');
    END;$write_after$),
        (manage_keys, 4, projection_before, projection_after),
        (manage_keys, 1, $rotate_before$                key_hash = v_key_hash,
                last_used_at = NULL
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;$rotate_before$, $rotate_after$                key_hash = v_key_hash
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            UPDATE public.identity_api_key_usage SET last_used_at = NULL WHERE api_key_id = v_key_id;$rotate_after$),
        (offboarding, 1, projection_before, projection_after)
    ) AS changes(target, occurrences, before_sql, after_sql) LOOP
        definition := pg_catalog.pg_get_functiondef(patch.target);
        IF pg_catalog.length(definition) - pg_catalog.length(pg_catalog.replace(definition, patch.before_sql, ''))
           <> patch.occurrences * pg_catalog.length(patch.before_sql) THEN
            RAISE EXCEPTION 'Unexpected API key usage projection in %', patch.target;
        END IF;
        EXECUTE pg_catalog.replace(definition, patch.before_sql, patch.after_sql);
    END LOOP;
END;
$migration$;
-- aap-statement-break
ALTER TABLE public.api_keys DROP COLUMN last_used_at;
