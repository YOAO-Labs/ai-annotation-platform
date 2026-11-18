-- Move Diagnostics and Insights owner capabilities to their YOAO namespaces.
-- One atomic DO: exact identities, bodies, settings and ACLs are checked before
-- and after two ALTER SCHEMA operations and two allowlisted caller replacements.
DO $cutover$
DECLARE
    v_diag oid := pg_catalog.to_regnamespace('aap_diagnostics_v1');
    v_ins oid := pg_catalog.to_regnamespace('aap_insights_v1');
    v_new_diag oid; v_new_ins oid;
    v_caps text[] := ARRAY['aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)', 'aap_diagnostics_v1.detach_project_references(uuid)', 'aap_insights_v1.clear_project_task_views(uuid)'];
    v_calls text[] := ARRAY['aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)', 'aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)'];
    v_sigs text[];
    v_digests text[] := ARRAY['5cea043d2fdcc088ee1d1842bb94a21044075271b01249082f9fca8db0068e22', '31aa9c50715a42faed14a878409e2cbed17694177992a8216b5b22985c9d5abf', 'bf382b1f3653dfeffd04f09a8787f3a9861dc116fd63fe23f1a3d92f17eb20f7', '85794d8f70ed17bd40e7520ce1f93af89ed44ea912d9faf3b4cdaa2afdbbd4ad', '9f9a140bf2ec88b784d36273c7ba2f31f8b4b035db5a4ebac3ed21cc544798f2'];
    v_post_digests text[];
    v_oids oid[]; v_fps jsonb[]; v_ns_oids oid[]; v_ns_fps jsonb[];
    v_sig text; v_f oid;
    v_h text; v_def text; v_bad integer; v_changed integer := 0; v_unchanged integer := 0; i integer;
BEGIN
    v_sigs := v_caps || v_calls; v_post_digests := v_digests;
    v_post_digests[4] := 'bbe50a16726ebb954b9594d4c923b72a995d61158a1bb8613d0203a4df51bc82'; v_post_digests[5] := 'ffe8c2a35e60d832c4f0907d8cef9a74c69dc0e96c488bb7169cb267e38a97c4';

    IF v_diag IS NULL OR v_ins IS NULL THEN RAISE EXCEPTION USING ERRCODE = '42704', MESSAGE = 'Diagnostics or Insights source namespace is missing'; END IF;
    IF pg_catalog.to_regnamespace('yoao_diagnostics_v1') IS NOT NULL OR pg_catalog.to_regnamespace('yoao_insights_v1') IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE = '42P06', MESSAGE = 'Diagnostics or Insights destination namespace already exists'; END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_class WHERE relnamespace IN (v_diag, v_ins))
       OR EXISTS (SELECT FROM pg_catalog.pg_type WHERE typnamespace IN (v_diag, v_ins))
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS p,
                pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS e
           WHERE p.pronamespace IN (v_diag, v_ins) AND e.grantee = 0) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Diagnostics or Insights namespace has an extra object or PUBLIC function grant';
    END IF;

    SELECT pg_catalog.array_agg(q.oid ORDER BY q.i), pg_catalog.array_agg(q.fp ORDER BY q.i), pg_catalog.count(*) FILTER (WHERE q.oid IS NULL OR q.public_grant)
    INTO v_ns_oids, v_ns_fps, v_bad
    FROM (
        SELECT s.i, n.oid, pg_catalog.to_jsonb(n) - 'nspname' AS fp,
               CASE WHEN n.oid IS NULL THEN false ELSE EXISTS (
                   SELECT 1 FROM pg_catalog.aclexplode(COALESCE(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) AS e WHERE e.grantee = 0)
               END AS public_grant
        FROM pg_catalog.unnest(ARRAY['aap_diagnostics_v1', 'aap_insights_v1']) WITH ORDINALITY AS s(name, i)
        LEFT JOIN pg_catalog.pg_namespace AS n ON n.nspname = s.name
    ) AS q;
    IF v_bad <> 0 THEN RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Owner capability schema is missing or has a PUBLIC grant'; END IF;

    SELECT pg_catalog.array_agg(q.oid ORDER BY q.i), pg_catalog.array_agg(q.fp ORDER BY q.i), pg_catalog.count(*) FILTER (
               WHERE q.oid IS NULL OR q.prokind <> 'f' OR NOT q.prosecdef OR q.provolatile <> 'v' OR q.proparallel <> 'u'
                  OR q.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
                  OR q.digest IS DISTINCT FROM v_digests[q.i::integer])
    INTO v_oids, v_fps, v_bad
    FROM (
        SELECT s.i, p.oid, p.prokind, p.prosecdef, p.provolatile, p.proparallel, p.proconfig,
               -- pg_node_tree includes parser byte offsets; compare the SQL default itself.
               (pg_catalog.to_jsonb(p) - 'prosrc' - 'proargdefaults') ||
                   pg_catalog.jsonb_build_object('defaults', pg_catalog.pg_get_expr(p.proargdefaults, 0)) AS fp,
               pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex') AS digest
        FROM pg_catalog.unnest(v_sigs) WITH ORDINALITY AS s(sig, i)
        LEFT JOIN pg_catalog.pg_proc AS p ON p.oid = pg_catalog.to_regprocedure(s.sig)
    ) AS q;
    IF v_bad <> 0 THEN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Owner capability identity, shape, settings or body drifted'; END IF;

    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc AS p WHERE p.pronamespace IN (v_diag, v_ins)) <> pg_catalog.cardinality(v_caps)
       OR (SELECT pg_catalog.array_agg(n.nspname || '.' || p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')'
               ORDER BY n.nspname COLLATE "C", p.proname COLLATE "C")
           FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
           WHERE p.pronamespace NOT IN (v_diag, v_ins)
             AND (pg_catalog.strpos(p.prosrc, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(p.prosrc, 'aap_insights_v1.') > 0)) IS DISTINCT FROM v_calls
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_views AS v WHERE pg_catalog.strpos(v.definition, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(v.definition, 'aap_insights_v1.') > 0)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_matviews AS v WHERE pg_catalog.strpos(v.definition, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(v.definition, 'aap_insights_v1.') > 0) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Diagnostics or Insights namespace has unexpected functions, callers or view references';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_stat_activity AS a
        WHERE a.datid = (SELECT d.oid FROM pg_catalog.pg_database AS d WHERE d.datname = pg_catalog.current_database())
          AND a.pid <> pg_catalog.pg_backend_pid()
          AND COALESCE(a.backend_type, 'client backend') = 'client backend'
          AND a.usesysid IS NOT NULL
          AND (pg_catalog.has_schema_privilege(a.usesysid, v_diag, 'USAGE') OR pg_catalog.has_schema_privilege(a.usesysid, v_ins, 'USAGE')
               OR EXISTS (SELECT 1 FROM pg_catalog.unnest(v_sigs) AS s(sig) WHERE pg_catalog.has_function_privilege(a.usesysid, s.sig, 'EXECUTE')))
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006', MESSAGE = 'Drain Diagnostics, Insights and Workspace deletion callers before cutover';
    END IF;

    EXECUTE 'ALTER SCHEMA aap_diagnostics_v1 RENAME TO yoao_diagnostics_v1';
    EXECUTE 'ALTER SCHEMA aap_insights_v1 RENAME TO yoao_insights_v1';

    FOR i IN 4..5 LOOP
        v_sig := v_sigs[i];
        v_f := pg_catalog.to_regprocedure(v_sig);
        v_h := (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex') FROM pg_catalog.pg_proc AS p WHERE p.oid = v_f);
        IF v_h IS DISTINCT FROM v_digests[i] THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Workspace caller drifted before cutover: ' || v_sig;
        END IF;
        v_def := pg_catalog.pg_get_functiondef(v_f);
        IF (pg_catalog.length(v_def) - pg_catalog.length(pg_catalog.replace(v_def, 'aap_diagnostics_v1.', ''))) / pg_catalog.length('aap_diagnostics_v1.') <> 1
           OR (pg_catalog.length(v_def) - pg_catalog.length(pg_catalog.replace(v_def, 'aap_insights_v1.', ''))) / pg_catalog.length('aap_insights_v1.') <> (CASE WHEN i = 4 THEN 1 ELSE 0 END) THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Workspace caller references drifted: ' || v_sig;
        END IF;
        v_def := pg_catalog.replace(pg_catalog.replace(v_def, 'aap_diagnostics_v1.', 'yoao_diagnostics_v1.'), 'aap_insights_v1.', 'yoao_insights_v1.');
        IF pg_catalog.strpos(v_def, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(v_def, 'aap_insights_v1.') > 0 THEN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Workspace caller replacement is incomplete: ' || v_sig;
        END IF;
        EXECUTE v_def;
    END LOOP;

    v_new_diag := pg_catalog.to_regnamespace('yoao_diagnostics_v1');
    v_new_ins := pg_catalog.to_regnamespace('yoao_insights_v1');
    IF v_new_diag IS DISTINCT FROM v_diag OR v_new_ins IS DISTINCT FROM v_ins
       OR pg_catalog.to_regnamespace('aap_diagnostics_v1') IS NOT NULL OR pg_catalog.to_regnamespace('aap_insights_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Diagnostics or Insights namespace identity changed';
    END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_class WHERE relnamespace IN (v_new_diag, v_new_ins))
       OR EXISTS (SELECT FROM pg_catalog.pg_type WHERE typnamespace IN (v_new_diag, v_new_ins))
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc AS p WHERE p.pronamespace IN (v_new_diag, v_new_ins)) <> pg_catalog.cardinality(v_caps) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Diagnostics or Insights destination has an unexpected object';
    END IF;

    SELECT pg_catalog.count(*) FILTER (WHERE q.oid IS NULL OR q.oid IS DISTINCT FROM v_ns_oids[q.i::integer]
               OR q.fp IS DISTINCT FROM v_ns_fps[q.i::integer] OR q.public_grant)
    INTO v_bad
    FROM (
        SELECT s.i, n.oid, pg_catalog.to_jsonb(n) - 'nspname' AS fp,
               CASE WHEN n.oid IS NULL THEN false ELSE EXISTS (
                   SELECT 1 FROM pg_catalog.aclexplode(COALESCE(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) AS e WHERE e.grantee = 0)
               END AS public_grant
        FROM pg_catalog.unnest(ARRAY['yoao_diagnostics_v1', 'yoao_insights_v1']) WITH ORDINALITY AS s(name, i)
        LEFT JOIN pg_catalog.pg_namespace AS n ON n.nspname = s.name
    ) AS q;
    IF v_bad <> 0 THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Owner capability schema identity or ACL changed, or a PUBLIC grant exists';
    END IF;

    SELECT pg_catalog.count(*) FILTER (WHERE q.oid IS NULL OR q.oid IS DISTINCT FROM v_oids[q.i::integer]
               OR q.fp IS DISTINCT FROM v_fps[q.i::integer] OR q.digest IS DISTINCT FROM v_post_digests[q.i::integer]),
           pg_catalog.count(*) FILTER (WHERE q.digest IS DISTINCT FROM v_digests[q.i::integer]),
           pg_catalog.count(*) FILTER (WHERE q.digest IS NOT DISTINCT FROM v_digests[q.i::integer])
    INTO v_bad, v_changed, v_unchanged
    FROM (
        SELECT s.i, p.oid, (pg_catalog.to_jsonb(p) - 'prosrc' - 'proargdefaults') ||
                   pg_catalog.jsonb_build_object('defaults', pg_catalog.pg_get_expr(p.proargdefaults, 0)) AS fp,
               pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex') AS digest
        FROM pg_catalog.unnest(v_sigs) WITH ORDINALITY AS s(sig, i)
        LEFT JOIN pg_catalog.pg_proc AS p ON p.oid = CASE WHEN s.i <= 3
            THEN pg_catalog.to_regprocedure(pg_catalog.replace(pg_catalog.replace(s.sig, 'aap_diagnostics_v1.', 'yoao_diagnostics_v1.'), 'aap_insights_v1.', 'yoao_insights_v1.'))
            ELSE pg_catalog.to_regprocedure(s.sig) END
    ) AS q;
    IF v_bad <> 0 OR v_changed <> 2 OR v_unchanged <> 3 THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'Expected exactly two changed and three unchanged capability bodies with preserved identity, ACL and settings';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc AS p WHERE pg_catalog.strpos(p.prosrc, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(p.prosrc, 'aap_insights_v1.') > 0)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_views AS v WHERE pg_catalog.strpos(v.definition, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(v.definition, 'aap_insights_v1.') > 0)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_matviews AS v WHERE pg_catalog.strpos(v.definition, 'aap_diagnostics_v1.') > 0 OR pg_catalog.strpos(v.definition, 'aap_insights_v1.') > 0) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'A live function or view still references a legacy namespace';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS p,
             pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS e
        WHERE p.pronamespace IN (v_new_diag, v_new_ins) AND e.grantee = 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Diagnostics or Insights capability has a PUBLIC grant after cutover';
    END IF;
END;
$cutover$;
