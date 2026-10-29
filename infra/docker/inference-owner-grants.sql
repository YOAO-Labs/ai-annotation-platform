-- Apply after module-data-grants.sql and lan-production-grants.sql as migration owner.
-- One LOGIN per closed profile; ordinary API/worker credentials must not use this script.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v inference_owner=inference_code -v client_role=inference_execution_client \
--   -v profile=execution -f infra/docker/inference-owner-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.inference_client', :'client_role', true);
SELECT set_config('aap.inference_profile', :'profile', true);

DO $profile$
DECLARE
    v_role name := current_setting('aap.inference_client');
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_role);
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.inference_owner'));
    v_profile text := current_setting('aap.inference_profile');
    v_schema text := 'aap_inference_backend_endpoint_v1';
    v_signatures text[];
    v_allowed oid[];
    v_signature text;
    v_function oid;
    v_namespace record;
    v_relation record;
BEGIN
    IF current_database()<>current_setting('aap.expected_database')
       OR current_user<>current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected Inference database or migration role';
    END IF;
    IF v_owner IS NULL OR v_client IS NULL OR v_owner=v_client
       OR current_user::pg_catalog.regrole=v_client
       OR NOT EXISTS (
           SELECT FROM pg_catalog.pg_roles WHERE oid=v_owner
             AND NOT rolcanlogin AND NOT rolinherit
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR NOT EXISTS (
           SELECT FROM pg_catalog.pg_roles WHERE oid=v_client
             AND rolcanlogin AND NOT rolinherit
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR EXISTS (
           SELECT FROM pg_catalog.pg_auth_members
           WHERE member=v_client OR roleid=v_client OR member=v_owner
              OR (roleid=v_owner AND member<>current_user::pg_catalog.regrole)
       ) THEN
        RAISE EXCEPTION 'Unsafe Inference owner/client role profile';
    END IF;
    IF v_profile IN ('setup','health_observation','catalog_fallback','stats',
                     'backend_read','lifecycle','execution') THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.open_'||v_profile||'_endpoint(text,uuid,uuid,text)'
        ];
    ELSIF v_profile IN ('gpu_health_observation','gpu_control') THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.capture_backend_endpoint_subject(uuid,uuid,text,text)',
            v_schema||'.open_'||CASE v_profile WHEN 'gpu_health_observation'
                THEN 'health_observation' ELSE 'gpu_control' END||'_endpoint(text,uuid,uuid,text)'
        ];
        IF v_profile='gpu_control' THEN
            v_signatures := pg_catalog.array_append(v_signatures,
                v_schema||'.open_installation_terminal_endpoint(uuid,uuid,text,text,text)');
        END IF;
    ELSIF v_profile='backend_stats' THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.capture_backend_endpoint_subject(uuid,uuid,text,text)',
            v_schema||'.stats_endpoint_rows()',
            v_schema||'.open_stats_endpoint(text,uuid,uuid,text)'
        ];
    ELSIF v_profile='gpu_authority' THEN
        v_schema := 'aap_inference_gpu_v1';
        v_signatures := ARRAY[v_schema||'.gpu_authority_policy()',
                              v_schema||'.gpu_authority_step(text,text)'];
    ELSIF v_profile='gpu_collector' THEN
        v_schema := 'aap_inference_gpu_collector_v1';
        v_signatures := ARRAY[v_schema||'.gpu_collector_policy()',
                              v_schema||'.gpu_collector_step(text,text)'];
    ELSE
        RAISE EXCEPTION 'Unknown closed Inference owner profile';
    END IF;
    FOREACH v_signature IN ARRAY v_signatures LOOP
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL OR NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc WHERE oid=v_function AND proowner=v_owner
              AND prosecdef AND prokind='f'
              AND proconfig @> ARRAY['search_path=pg_catalog, pg_temp','TimeZone=UTC']::text[]
        ) THEN
            RAISE EXCEPTION 'Missing or changed Inference owner capability: %',v_signature;
        END IF;
        v_allowed := pg_catalog.array_append(v_allowed,v_function);
    END LOOP;
    EXECUTE pg_catalog.format('REVOKE ALL ON DATABASE %I FROM %I',current_database(),v_role);
    EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE %I TO %I',current_database(),v_role);
    FOR v_namespace IN
        SELECT nspname FROM pg_catalog.pg_namespace
        WHERE nspname NOT IN ('pg_catalog','information_schema')
          AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
    END LOOP;
    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA %I TO %I',v_schema,v_role);
    FOREACH v_function IN ARRAY v_allowed LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',v_function::pg_catalog.regprocedure);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO %I',v_function::pg_catalog.regprocedure,v_role);
    END LOOP;
    IF pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR EXISTS (
           SELECT FROM pg_catalog.pg_namespace
           WHERE nspname NOT IN ('pg_catalog','information_schema')
             AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
             AND (pg_catalog.has_schema_privilege(v_client,oid,'CREATE')
                  OR (nspname NOT IN ('public',v_schema)
                      AND pg_catalog.has_schema_privilege(v_client,oid,'USAGE')))
       ) OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Inference owner LOGIN has unsafe database/schema privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_type t ON t.oid=p.prorettype
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND p.prokind IN ('f','p','a','w')
          AND NOT (t.typnamespace='pg_catalog'::pg_catalog.regnamespace
                   AND t.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend d
              WHERE d.classid='pg_catalog.pg_proc'::pg_catalog.regclass AND d.objid=p.oid
                AND d.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass AND d.deptype='e'
          ) AND (pg_catalog.left(n.nspname,4)='aap_' OR p.prosecdef OR EXISTS (
              SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) a
              WHERE a.privilege_type='EXECUTE' AND a.grantee<>0
                AND pg_catalog.pg_has_role(v_client,a.grantee,'USAGE')
          )) AND NOT (p.oid=ANY(v_allowed))
          AND pg_catalog.has_function_privilege(v_client,p.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Inference owner LOGIN can execute an unrelated function';
    END IF;
    FOR v_relation IN
        SELECT c.oid,c.relkind FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relkind IN ('r','p','v','m','f','S')
    LOOP
        IF (v_relation.relkind<>'S' AND (
                pg_catalog.has_table_privilege(v_client,v_relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind='S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Inference owner LOGIN has direct relation privileges';
        END IF;
    END LOOP;
    IF EXISTS (SELECT FROM pg_catalog.pg_class WHERE relowner=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_namespace WHERE nspowner=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_database WHERE datdba=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_proc WHERE proowner=v_client) THEN
        RAISE EXCEPTION 'Inference owner LOGIN owns database objects';
    END IF;
END;
$profile$;
COMMIT;
