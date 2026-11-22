\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.asset_import_role', :'asset_import_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_import_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('yoao.migration_role') = current_setting('yoao.assets_owner')
       OR current_setting('yoao.migration_role') = current_setting('yoao.asset_import_role')
       OR current_setting('yoao.assets_owner') = current_setting('yoao.asset_import_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and import LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe import role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing import role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe import role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Import roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_import_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and import role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_import_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_import_role";

REVOKE ALL ON SCHEMA public FROM :"asset_import_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_import_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_import_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_import_role";

DO $$
DECLARE
    namespace record;
    import_role text := current_setting('yoao.asset_import_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, import_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, import_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, import_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, import_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_import_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_import_role";

GRANT EXECUTE ON FUNCTION yoao_assets_v1.claim_dataset_import(uuid,integer,uuid),
    yoao_assets_v1.resume_import_original_verification(jsonb),
    yoao_assets_v1.reserve_import_original_read(jsonb),
    yoao_assets_v1.cancel_unstarted_import_original_read(jsonb),
    yoao_assets_v1.begin_operator_auxiliary(jsonb),
    yoao_assets_v1.open_operator_storage_operation(uuid),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.heartbeat_dataset_import(uuid,uuid,integer),
    yoao_assets_v1.read_dataset_import_connection(uuid,uuid,jsonb),
    yoao_assets_v1.save_dataset_import_manifest(uuid,uuid,jsonb),
    yoao_assets_v1.next_dataset_import_entry(uuid,uuid,uuid),
    yoao_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb),
    yoao_assets_v1.finish_dataset_import(uuid,uuid,text),
    yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)
    TO :"asset_import_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_import_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.resume_import_original_verification(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.reserve_import_original_read(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.cancel_unstarted_import_original_read(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_dataset_import(uuid,integer,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_dataset_import(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_dataset_import_connection(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.save_dataset_import_manifest(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.next_dataset_import_entry(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_dataset_import(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_operator_auxiliary(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.open_operator_storage_operation(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_operator_original_prepared(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.mark_operator_original_writing(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_operator_original_written(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)')::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'yoao_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Import LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Import LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Import LOGIN has direct table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(
              role_oid, relation.oid, privilege.name
          )
    ) THEN
        RAISE EXCEPTION 'Import LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Import LOGIN has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND proc.oid <> ALL (allowed_functions)
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Import LOGIN has an unapproved module function grant';
    END IF;
END $$;
COMMIT;
