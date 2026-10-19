-- Grant the exact Assets video/image control and video-worker capabilities.
-- Run after migrations and module-data/catalog/source/import/operator grants.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v assets_owner=assets_code
--   -v asset_media_control_role=asset_media_control_client
--   -v asset_media_worker_role=asset_media_worker_client
--   -f infra/docker/assets-media-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.asset_media_control_role', :'asset_media_control_role', true);
SELECT set_config('aap.asset_media_worker_role', :'asset_media_worker_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.assets_owner'),
        current_setting('aap.asset_media_control_role'),
        current_setting('aap.asset_media_worker_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT name) FROM unnest(role_names) AS name) <> 3 THEN
        RAISE EXCEPTION 'Assets owner, media control and media worker roles must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe media role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing media role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls OR role.rolinherit)
        ) THEN
            RAISE EXCEPTION 'Unsafe media role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Media roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.assets_owner') AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_media_control_role')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_media_worker_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and media roles must LOGIN';
    END IF;
END $$;

REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;
REVOKE ALL ON DATABASE :"expected_database"
    FROM :"asset_media_control_role", :"asset_media_worker_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"asset_media_control_role", :"asset_media_worker_role";
REVOKE ALL ON SCHEMA public
    FROM :"asset_media_control_role", :"asset_media_worker_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public
    FROM :"asset_media_control_role", :"asset_media_worker_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
    FROM :"asset_media_control_role", :"asset_media_worker_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public
    FROM :"asset_media_control_role", :"asset_media_worker_role";

DO $$
DECLARE
    namespace record;
    control_role text := current_setting('aap.asset_media_control_role');
    worker_role text := current_setting('aap.asset_media_worker_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I, %I',
            namespace.nspname, control_role, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I, %I',
            namespace.nspname, control_role, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I, %I',
            namespace.nspname, control_role, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I, %I',
            namespace.nspname, control_role, worker_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_assets_v1
    FROM :"asset_media_control_role", :"asset_media_worker_role";
GRANT USAGE ON SCHEMA aap_assets_v1
    TO :"asset_media_control_role", :"asset_media_worker_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.read_media_source(uuid,uuid,uuid),
    aap_assets_v1.read_media_summaries(uuid[]),
    aap_assets_v1.ensure_video_media(uuid,uuid,uuid,jsonb),
    aap_assets_v1.observe_media_source(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean),
    aap_assets_v1.read_video_timetable(uuid,uuid,uuid,integer,integer,integer,integer),
    aap_assets_v1.read_video_samples(uuid,uuid,uuid,integer,integer,integer),
    aap_assets_v1.read_video_failures(uuid,integer,bigint),
    aap_assets_v1.retry_video_media(uuid,uuid,jsonb),
    aap_assets_v1.invalidate_video_media(uuid,uuid,uuid,jsonb),
    aap_assets_v1.cancel_video_media(uuid,uuid,uuid),
    aap_assets_v1.read_media_preferences(),
    aap_assets_v1.resolve_internal_media_source(uuid,text,jsonb),
    aap_assets_v1.schedule_video_media(uuid,uuid,jsonb,boolean),
    aap_assets_v1.read_video_work(uuid,uuid),
    aap_assets_v1.scan_video_backfill(jsonb,uuid,integer),
    aap_assets_v1.read_video_media_totals(uuid[]),
    aap_assets_v1.read_internal_video_frame(uuid,integer,integer,text),
    aap_assets_v1.read_image_source(uuid,uuid,uuid),
    aap_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.read_image_summaries(uuid,jsonb,text),
    aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text),
    aap_assets_v1.read_image_work(uuid,uuid),
    aap_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    aap_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb),
    aap_assets_v1.cancel_image_media(uuid,uuid,uuid),
    aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    aap_assets_v1.read_internal_image_work(uuid),
    aap_assets_v1.scan_image_backfill(jsonb,text,uuid,integer),
    aap_assets_v1.read_image_usage(),
    aap_assets_v1.find_accepted_image_input(uuid,uuid,uuid),
    aap_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.read_accepted_image_input(uuid,uuid,uuid),
    aap_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid),
    aap_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid),
    aap_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    aap_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text),
    aap_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text)
    TO :"asset_media_control_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.claim_video_media(uuid,uuid,integer,text[]),
    aap_assets_v1.heartbeat_video_media(uuid,bigint,uuid,integer),
    aap_assets_v1.stage_video_timetable(uuid,bigint,uuid,jsonb),
    aap_assets_v1.finish_video_media(uuid,bigint,uuid,jsonb),
    aap_assets_v1.fail_video_media(uuid,bigint,uuid,text,text),
    aap_assets_v1.expire_video_media(integer,integer,integer),
    aap_assets_v1.claim_video_media_garbage(uuid,integer,integer),
    aap_assets_v1.complete_video_media_garbage(uuid,uuid),
    aap_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer),
    aap_assets_v1.read_video_media_usage()
    TO :"asset_media_worker_role";

DO $$
DECLARE
    control_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_media_control_role')
    );
    worker_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_media_worker_role')
    );
    control_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_assets_v1.read_media_source(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_media_summaries(uuid[])')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.ensure_video_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.observe_media_source(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_timetable(uuid,uuid,uuid,integer,integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_samples(uuid,uuid,uuid,integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_failures(uuid,integer,bigint)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.retry_video_media(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.invalidate_video_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.cancel_video_media(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_media_preferences()')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.resolve_internal_media_source(uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.schedule_video_media(uuid,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_work(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.scan_video_backfill(jsonb,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_media_totals(uuid[])')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_internal_video_frame(uuid,integer,integer,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_image_source(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_image_summaries(uuid,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_image_work(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.cancel_image_media(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_internal_image_work(uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.scan_image_backfill(jsonb,text,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_image_usage()')::oid
    ];
    accepted_input_control_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_assets_v1.find_accepted_image_input(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_accepted_image_input(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text)')::oid
    ];
    worker_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_assets_v1.claim_video_media(uuid,uuid,integer,text[])')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.heartbeat_video_media(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.stage_video_timetable(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.finish_video_media(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.fail_video_media(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.expire_video_media(integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.claim_video_media_garbage(uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.complete_video_media_garbage(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_video_media_usage()')::oid
    ];
BEGIN
    IF pg_catalog.has_database_privilege(control_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(control_oid, current_database(), 'TEMP')
       OR pg_catalog.has_database_privilege(worker_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(worker_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Media logins can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND (pg_catalog.has_schema_privilege(control_oid, ns.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(worker_oid, ns.oid, 'USAGE'))
    ) THEN
        RAISE EXCEPTION 'Media logins have access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(control_oid, ns.oid, 'CREATE')
               OR pg_catalog.has_schema_privilege(worker_oid, ns.oid, 'CREATE'))
    ) THEN
        RAISE EXCEPTION 'Media logins can create objects in a schema';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
            ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(control_oid, relation.oid, privilege.name)
               OR pg_catalog.has_table_privilege(worker_oid, relation.oid, privilege.name))
    ) THEN
        RAISE EXCEPTION 'Media logins have direct table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_any_column_privilege(control_oid, relation.oid, privilege.name)
               OR pg_catalog.has_any_column_privilege(worker_oid, relation.oid, privilege.name))
    ) THEN
        RAISE EXCEPTION 'Media logins have direct column privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(control_oid, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(control_oid, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(control_oid, relation.oid, 'UPDATE')
              OR pg_catalog.has_sequence_privilege(worker_oid, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(worker_oid, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(worker_oid, relation.oid, 'UPDATE')
          )
    ) THEN
        RAISE EXCEPTION 'Media logins have direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(control_oid, proc.oid, 'EXECUTE')
          AND (ns.nspname <> 'aap_assets_v1' OR (
              proc.oid <> ALL(control_functions)
              AND proc.oid <> ALL(accepted_input_control_functions)
          ))
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(worker_oid, proc.oid, 'EXECUTE')
          AND (ns.nspname <> 'aap_assets_v1' OR proc.oid <> ALL(worker_functions))
    ) THEN
        RAISE EXCEPTION 'Media logins have unapproved module function grants';
    END IF;
END $$;
COMMIT;
