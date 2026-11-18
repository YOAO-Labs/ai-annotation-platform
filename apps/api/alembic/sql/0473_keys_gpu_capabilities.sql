-- Persisted key material, operation receipts, GPU fences and signed domains do not move.
DO $cutover$
DECLARE
    scope record;
    capability record;
    source_oid oid;
    owner_oid oid;
    client pg_catalog.pg_roles%ROWTYPE;
    installed pg_catalog.pg_proc%ROWTYPE;
    policy_oid oid;
    body text;
    secure_path constant text[] := ARRAY['search_path=pg_catalog, pg_temp'];
    gpu_settings constant text[] := secure_path || ARRAY['TimeZone=UTC','DateStyle=ISO, YMD','extra_float_digits=3'];
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_prepared_xacts WHERE database = current_database()) THEN
        RAISE EXCEPTION 'Settle prepared transactions before the capability cutover' USING ERRCODE = '55006';
    END IF;
    FOR scope IN SELECT * FROM (VALUES
        ('aap_identity_keys_v1', 'yoao_identity_keys_v1', 'identity_key_client', 'aap_identity_v1.read_actor_facts(uuid)'),
        ('aap_inference_gpu_caller_v1', 'yoao_inference_gpu_caller_v1', NULL, 'aap_inference_gpu_v1.gpu_authority_step(text,text)'),
        ('aap_inference_gpu_collector_v1', 'yoao_inference_gpu_collector_v1', 'inference_gpu_collector_client', 'aap_inference_gpu_v1.gpu_authority_step(text,text)')
    ) AS names(source, target, dedicated, owner_anchor) LOOP
        source_oid := pg_catalog.to_regnamespace(scope.source);
        SELECT proowner INTO owner_oid FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure(scope.owner_anchor);
        IF source_oid IS NULL OR owner_oid IS NULL OR pg_catalog.to_regnamespace(scope.target) IS NOT NULL THEN
            RAISE EXCEPTION 'Missing source or occupied destination: % -> %', scope.source, scope.target USING ERRCODE = '55000';
        END IF;
        SELECT * INTO client FROM pg_catalog.pg_roles WHERE rolname = scope.dedicated;
        IF client.rolcanlogin THEN
            RAISE EXCEPTION 'Disable old dedicated LOGIN % before cutover', scope.dedicated USING ERRCODE = '55006';
        END IF;
        IF client.rolsuper OR client.rolcreaterole OR client.rolcreatedb OR client.rolreplication OR client.rolbypassrls
           OR client.oid = owner_oid OR EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE client.oid IN (member, roleid)) THEN
            RAISE EXCEPTION 'Unexpected old client ownership or membership: %', scope.dedicated USING ERRCODE = '42501';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.pg_namespace n WHERE n.oid = source_oid AND
                   (n.nspowner = client.oid OR EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(
                       n.nspacl, pg_catalog.acldefault('n', n.nspowner))) a WHERE a.grantee = 0)))
           OR EXISTS (SELECT FROM pg_catalog.pg_depend WHERE refclassid = 'pg_namespace'::regclass
                      AND refobjid = source_oid AND classid <> 'pg_proc'::regclass) THEN
            RAISE EXCEPTION 'Unexpected object or privilege in %', scope.source USING ERRCODE = '42501';
        END IF;
        -- The embedded caller is a shared application LOGIN, not a role to rename or revoke.
        -- Disallow new ordinary connections while a live shared LOGIN still has this capability.
        IF (SELECT datconnlimit FROM pg_catalog.pg_database WHERE datname = current_database()) <> 0 AND EXISTS (
            SELECT FROM pg_catalog.pg_roles r WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname <> current_user
              AND pg_catalog.has_schema_privilege(r.oid, source_oid, 'USAGE') AND EXISTS (
                SELECT FROM pg_catalog.pg_proc p WHERE p.pronamespace = source_oid
                  AND pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE'))) THEN
            RAISE EXCEPTION 'Close caller admission to % before cutover', scope.source USING ERRCODE = '55006';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.pg_stat_activity a WHERE a.datname = current_database()
                   AND a.pid <> pg_backend_pid() AND a.backend_type = 'client backend' AND
                   (pg_catalog.has_schema_privilege(a.usesysid, source_oid, 'USAGE') OR EXISTS (
                       SELECT FROM pg_catalog.pg_proc p WHERE p.pronamespace = source_oid
                         AND pg_catalog.has_function_privilege(a.usesysid, p.oid, 'EXECUTE')))) THEN
            RAISE EXCEPTION 'Drain callers of % before cutover', scope.source USING ERRCODE = '55006';
        END IF;
        policy_oid := NULL;
        FOR capability IN SELECT * FROM (VALUES
            ('aap_identity_keys_v1','key_policy()','5b66b2a4a3debc5718f04056ec8ac35e2e28ad08675c7db2d452e335905ae0c8','s','s',secure_path,false),
            ('aap_identity_keys_v1','execute_key_command(jsonb)','fcb8ed972b99c814c2f661c00cb32fd7a75426026e422ad82cdcfd9b5198c96b','v','u',secure_path || ARRAY['TimeZone=UTC'],false),
            ('aap_inference_gpu_caller_v1','gpu_caller_policy()','4ffbfd80e7f67d338e5d1a3d50b9879d5840b31163be98b0d08522259ff4458a','i','s',gpu_settings,true),
            ('aap_inference_gpu_caller_v1','gpu_caller_step(text,text)','f97ba47fead77c8c7a4e776fab143bac53a1f70533baab88d5739e9dcc5b57e0','v','u',gpu_settings,false),
            ('aap_inference_gpu_collector_v1','gpu_collector_policy()','ffa6276dabc5197cf784aa1ba2247cdfbda35c475197aa331520e553a80530c9','i','s',gpu_settings,true),
            ('aap_inference_gpu_collector_v1','gpu_collector_step(text,text)','3aec873d5655ee4ba55cc4d455b0fd6b5876e4039c5d6582439e1f9ccbe805db','v','u',gpu_settings,false)
        ) AS manifest(source, signature, digest, volatility, parallel, settings, advertises_name) WHERE source = scope.source LOOP
            SELECT * INTO installed FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure(scope.source || '.' || capability.signature);
            IF NOT FOUND OR NOT installed.prosecdef OR installed.prokind <> 'f' OR installed.proowner <> owner_oid
               OR installed.proretset OR installed.prorettype <> 'pg_catalog.jsonb'::regtype
               OR installed.provolatile::text <> capability.volatility OR installed.proparallel::text <> capability.parallel
               OR installed.proconfig IS DISTINCT FROM capability.settings OR pg_catalog.encode(pg_catalog.sha256(
                   pg_catalog.convert_to(installed.prosrc,'UTF8')),'hex') <> capability.digest
               OR EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(installed.proacl,
                           pg_catalog.acldefault('f', installed.proowner))) a WHERE a.grantee = 0) THEN
                RAISE EXCEPTION 'Unexpected capability: %.%', scope.source, capability.signature USING ERRCODE = '55000';
            END IF;
            IF capability.advertises_name THEN policy_oid := installed.oid; END IF;
        END LOOP;
        IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace = source_oid) <> 2
           OR EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE p.oid IS DISTINCT FROM policy_oid AND strpos(p.prosrc, scope.source) > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_proc p ON p.oid = d.refobjid
                      WHERE d.refclassid = 'pg_proc'::regclass AND p.pronamespace = source_oid)
           OR EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE strpos(p.proconfig::text, scope.source) > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_views WHERE strpos(definition, scope.source) > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_matviews WHERE strpos(definition, scope.source) > 0) THEN
            RAISE EXCEPTION 'Unlisted capability or caller of %', scope.source USING ERRCODE = '55000';
        END IF;
        EXECUTE format('ALTER SCHEMA %I RENAME TO %I', scope.source, scope.target);
        IF policy_oid IS NOT NULL THEN
            SELECT * INTO STRICT installed FROM pg_catalog.pg_proc WHERE oid = policy_oid;
            body := replace(installed.prosrc, scope.source, scope.target);
            EXECUTE replace(pg_catalog.pg_get_functiondef(policy_oid), installed.prosrc, body);
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE p.oid = policy_oid AND p.prosrc = body
                           AND (to_jsonb(p) - 'prosrc') = (to_jsonb(installed) - 'prosrc')) THEN
                RAISE EXCEPTION 'GPU policy metadata changed: %', scope.source USING ERRCODE = '55000';
            END IF;
        END IF;
        IF client.oid IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', scope.target, scope.dedicated);
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', scope.target, scope.dedicated);
        END IF;
    END LOOP;
END;
$cutover$;
