-- Grant the exact Assets image-media polling-worker capabilities.
-- Run after migrations and module-data/media grants.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v assets_owner=assets_code
--   -v asset_image_worker_role=asset_image_worker_client
--   -f infra/docker/assets-image-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.asset_image_worker_role', :'asset_image_worker_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.assets_owner'),
        current_setting('aap.asset_image_worker_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT name) FROM unnest(role_names) AS name) <> 2
       OR current_setting('aap.migration_role') = current_setting('aap.assets_owner')
       OR current_setting('aap.migration_role') = current_setting('aap.asset_image_worker_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and image worker roles must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe image worker role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing image worker role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls OR role.rolinherit)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe image worker role: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.assets_owner') AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_image_worker_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and image worker must LOGIN';
    END IF;
END $$;

REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_image_worker_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_image_worker_role";
REVOKE ALL ON SCHEMA public FROM :"asset_image_worker_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_image_worker_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_image_worker_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_image_worker_role";

DO $$
DECLARE
    namespace record;
    worker_role text := current_setting('aap.asset_image_worker_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, worker_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_assets_v1 FROM :"asset_image_worker_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_image_worker_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.claim_image_media(uuid,uuid,integer,text[]),
    aap_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer),
    aap_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb),
    aap_assets_v1.fail_image_media(uuid,bigint,uuid,text,text),
    aap_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb),
    aap_assets_v1.reconcile_image_media(jsonb,integer)
    TO :"asset_image_worker_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_image_worker_role')
    );
    worker_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_assets_v1.claim_image_media(uuid,uuid,integer,text[])')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.fail_image_media(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.reconcile_image_media(jsonb,integer)')::oid
    ];
BEGIN
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Image worker LOGIN can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN can create objects in a schema';
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
          AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has direct table privileges';
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
          AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has direct column privileges';
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
              pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE')
          )
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND proc.oid <> ALL (worker_functions)
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has an unapproved module function grant';
    END IF;
END $$;
COMMIT;
