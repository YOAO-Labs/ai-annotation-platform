-- Seven leaf functions, one qualified self-caller and no external SQL callers.
-- Admission must already be closed. Do not create roles or terminate sessions here.
DO $cutover$
DECLARE
    capability text;
    source_name text;
    target_name text;
    source_oid oid;
    client_name text;
    client pg_catalog.pg_roles%ROWTYPE;
    entry record;
    installed pg_catalog.pg_proc%ROWTYPE;
    source_body text;
    definition text;
    qualified_caller oid;
    expected_count integer;
BEGIN
    FOREACH capability IN ARRAY ARRAY['identity_briefs', 'labeling_class_query'] LOOP
        source_name := 'aap_' || capability || '_v1';
        target_name := 'yoao_' || capability || '_v1';
        client_name := capability || '_client';
        source_oid := pg_catalog.to_regnamespace(source_name);
        IF source_oid IS NULL OR pg_catalog.to_regnamespace(target_name) IS NOT NULL THEN
            RAISE EXCEPTION 'Missing source or occupied destination: % -> %', source_name, target_name
                USING ERRCODE = '55000';
        END IF;
        SELECT * INTO client FROM pg_catalog.pg_roles WHERE rolname = client_name;
        IF client.rolcanlogin THEN
            RAISE EXCEPTION 'Disable legacy reader % before cutover', client_name USING ERRCODE = '55006';
        END IF;
        IF client.rolsuper OR client.rolcreaterole OR client.rolcreatedb OR client.rolreplication
           OR client.rolbypassrls OR EXISTS (SELECT FROM pg_catalog.pg_auth_members
                                            WHERE client.oid IN (member, roleid)) THEN
            RAISE EXCEPTION 'Legacy reader % is not a closed client', client_name USING ERRCODE = '42501';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.pg_namespace n WHERE n.oid = source_oid AND
                   (n.nspowner = client.oid OR EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(
                       n.nspacl, pg_catalog.acldefault('n', n.nspowner))) a WHERE a.grantee = 0)))
           OR EXISTS (SELECT FROM pg_catalog.pg_depend d
                      WHERE d.refclassid = 'pg_namespace'::regclass AND d.refobjid = source_oid
                        AND d.classid <> 'pg_proc'::regclass) THEN
            RAISE EXCEPTION 'Unexpected object or privilege in %', source_name USING ERRCODE = '42501';
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.pg_stat_activity a WHERE a.datname = current_database()
                   AND a.pid <> pg_backend_pid() AND a.backend_type = 'client backend'
                   AND (pg_catalog.has_schema_privilege(a.usesysid, source_oid, 'USAGE') OR EXISTS (
                       SELECT FROM pg_catalog.pg_proc p WHERE p.pronamespace = source_oid
                         AND pg_catalog.has_function_privilege(a.usesysid, p.oid, 'EXECUTE')))) THEN
            RAISE EXCEPTION 'Drain callers of % before cutover', source_name USING ERRCODE = '55006';
        END IF;

        expected_count := 0;
        qualified_caller := NULL;
        FOR entry IN SELECT * FROM (VALUES
            ('identity_briefs','_failure()','e73a425d02e83c05054ae9debe1c2e3775e72e3db0a638858b0cacd716f8f08c','i','u'),
            ('identity_briefs','_has_exact_keys(jsonb,text[])','911c08e276404c4b90e8555f0f40892fa7ab765f9d6f58477f3a0c555be5cf0a','i','u'),
            ('identity_briefs','briefs_policy()','4806ecdb60c7fd08e970420ab890d914d539bd60799a47716f424407657854d8','s','u'),
            ('identity_briefs','execute_briefs_command(jsonb)','3afc7373b1de06390248db9b5e1257875e4900e6e28abbcaeadaef012200db24','s','u'),
            ('labeling_class_query','attribute_rows(uuid)','5ecbf1385d7053c3f17566a22b9f59e36ddc2469e713f6b927b3f728fb778b60','s','u'),
            ('labeling_class_query','class_counts(uuid)','0617af02831100e0ef59c54c5b99c2b44816e25b4335ef14f9d4551b75907ac6','s','u'),
            ('labeling_class_query','class_query_policy()','bff16eb78deaa916fb6c2d49ef95d065b83f1a60ceaba831085891f9b96c211e','s','s')
        ) AS spec(domain, signature, digest, volatility, parallel) WHERE spec.domain = capability LOOP
            expected_count := expected_count + 1;
            SELECT * INTO installed FROM pg_catalog.pg_proc
            WHERE oid = pg_catalog.to_regprocedure(source_name || '.' || entry.signature);
            IF NOT FOUND OR NOT installed.prosecdef OR installed.prokind <> 'f'
               OR installed.proowner = client.oid OR installed.provolatile::text <> entry.volatility
               OR installed.proparallel::text <> entry.parallel
               OR installed.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(installed.prosrc, 'UTF8')), 'hex') <> entry.digest
               OR installed.proowner IS DISTINCT FROM (
                   SELECT p.proowner FROM pg_catalog.pg_proc p WHERE p.oid = CASE capability
                       WHEN 'identity_briefs' THEN pg_catalog.to_regprocedure('aap_identity_v1.read_actor_facts(uuid)')
                       ELSE pg_catalog.to_regprocedure('aap_labeling_v1.mask_edit_command(jsonb)') END)
               OR EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(installed.proacl,
                             pg_catalog.acldefault('f', installed.proowner))) a WHERE a.grantee = 0) THEN
                RAISE EXCEPTION 'Unexpected reader capability: %.%', source_name, entry.signature USING ERRCODE = '55000';
            END IF;
            IF entry.signature = 'execute_briefs_command(jsonb)' THEN
                qualified_caller := installed.oid;
            END IF;
        END LOOP;
        IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace = source_oid) <> expected_count
           OR EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE p.oid IS DISTINCT FROM qualified_caller
                      AND strpos(p.prosrc, source_name || '.') > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_proc p ON p.oid = d.refobjid
                      WHERE d.refclassid = 'pg_proc'::regclass AND p.pronamespace = source_oid)
           OR EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE strpos(p.proconfig::text, source_name) > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_views v WHERE strpos(v.definition, source_name || '.') > 0)
           OR EXISTS (SELECT FROM pg_catalog.pg_matviews v WHERE strpos(v.definition, source_name || '.') > 0) THEN
            RAISE EXCEPTION 'Unlisted reader capability or caller of %', source_name USING ERRCODE = '55000';
        END IF;

        EXECUTE format('ALTER SCHEMA %I RENAME TO %I', source_name, target_name);
        IF qualified_caller IS NOT NULL THEN
            SELECT * INTO STRICT installed FROM pg_catalog.pg_proc WHERE oid = qualified_caller;
            source_body := replace(installed.prosrc, source_name || '.', target_name || '.');
            definition := pg_catalog.pg_get_functiondef(qualified_caller);
            EXECUTE replace(definition, installed.prosrc, source_body);
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_proc p WHERE p.oid = qualified_caller
                           AND p.prosrc = source_body
                           AND (to_jsonb(p) - 'prosrc') = (to_jsonb(installed) - 'prosrc')) THEN
                RAISE EXCEPTION 'Reader definition or privileges changed during cutover' USING ERRCODE = '55000';
            END IF;
        END IF;
        IF client.oid IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', target_name, client_name);
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', target_name, client_name);
        END IF;
    END LOOP;
END;
$cutover$;
