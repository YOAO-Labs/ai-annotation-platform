\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_catalog}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.asset_catalog_role', :'asset_catalog_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_catalog_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('yoao.migration_role') = current_setting('yoao.assets_owner')
       OR current_setting('yoao.migration_role') = current_setting('yoao.asset_catalog_role')
       OR current_setting('yoao.assets_owner') = current_setting('yoao.asset_catalog_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and catalog LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe catalog role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing catalog role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe catalog role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Catalog roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_catalog_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and catalog role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_catalog_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_catalog_role";

REVOKE ALL ON SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_catalog_role";

DO $$
DECLARE
    namespace record;
    catalog_role text := current_setting('yoao.asset_catalog_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_catalog_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_catalog_role";

GRANT SELECT (
    id, display_id, name, description, data_type, is_temporal, file_count,
    created_by, metadata, created_at, updated_at
), INSERT (
    id, display_id, name, description, data_type, is_temporal, file_count,
    created_by, metadata
), UPDATE (name, description, metadata, updated_at)
    ON public.datasets TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.display_seq_datasets TO :"assets_owner";
GRANT SELECT (
    operation_id, actor_id, operation_kind, dataset_id, request, result, created_at
), INSERT (
    operation_id, actor_id, operation_kind, dataset_id, request, result
)
    ON public.dataset_catalog_operations TO :"assets_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._catalog_failure(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._catalog_actor(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._catalog_dataset_summary(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_catalog_datasets(
    uuid, text, text, boolean, integer, bigint
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.get_catalog_dataset(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_catalog_items(uuid, uuid, integer, bigint)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_catalog_dataset(uuid, uuid, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb)
    OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT EXECUTE ON FUNCTION yoao_assets_v1.list_catalog_datasets(
    uuid, text, text, boolean, integer, bigint
), yoao_assets_v1.get_catalog_dataset(uuid, uuid),
    yoao_assets_v1.list_catalog_items(uuid, uuid, integer, bigint),
    yoao_assets_v1.create_catalog_dataset(uuid, uuid, jsonb),
    yoao_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb),
    yoao_assets_v1.list_dataset_projects(uuid, uuid),
    yoao_assets_v1.list_project_datasets(uuid, uuid)
    TO :"asset_catalog_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.guard_spatial_actor(uuid,text),
    yoao_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer),
    yoao_assets_v1.get_spatial_scene(uuid,uuid),
    yoao_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer),
    yoao_assets_v1.read_spatial_poses(uuid,uuid,integer,integer),
    yoao_assets_v1.read_spatial_pose_pairs(jsonb),
    yoao_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer),
    yoao_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb),
    yoao_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb),
    yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid),
    yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb),
    yoao_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb),
    yoao_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.read_pointcloud_preparation(uuid,uuid,integer),
    yoao_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb),
    yoao_assets_v1.read_spatial_camera_target(uuid,uuid,text),
    yoao_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb),
    yoao_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)
    TO :"asset_catalog_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_catalog_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.list_catalog_datasets(uuid,text,text,boolean,integer,bigint)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.get_catalog_dataset(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.list_catalog_items(uuid,uuid,integer,bigint)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.create_catalog_dataset(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.update_catalog_dataset(uuid,uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.list_dataset_projects(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.list_project_datasets(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.guard_spatial_actor(uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.get_spatial_scene(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_poses(uuid,uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_pose_pairs(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_pointcloud_preparation(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_spatial_camera_target(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)')::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'yoao_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Catalog LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Catalog LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct table privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct sequence privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has an unapproved module function grant';
    END IF;
END $$;
\endif

\if :{?grant_image}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.asset_image_worker_role', :'asset_image_worker_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    migration_owned boolean := current_setting('yoao.assets_owner') = current_user;
    role_names text[] := ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_image_worker_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT name) FROM unnest(role_names) AS name) <> 2
       OR current_setting('yoao.migration_role') = current_setting('yoao.asset_image_worker_role') THEN
        RAISE EXCEPTION 'Assets owner, migration user and image executor must not share the executor LOGIN';
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
        IF migration_owned AND role_name = current_user THEN
            CONTINUE;
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
    IF (NOT migration_owned AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner') AND role.rolcanlogin
    )) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_image_worker_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Dedicated Assets owner must be NOLOGIN and image worker must LOGIN';
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
    worker_role text := current_setting('yoao.asset_image_worker_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
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

REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_image_worker_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_image_worker_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text),
    yoao_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text),
    yoao_assets_v1.finish_image_original_worker(uuid,uuid),
    yoao_assets_v1.begin_accepted_image_original(uuid,bigint,uuid),
    yoao_assets_v1.finish_accepted_image_original(uuid,uuid),
    yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[]),
    yoao_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer),
    yoao_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_image_media(uuid,bigint,uuid,text,text),
    yoao_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb),
    yoao_assets_v1.reconcile_image_media(jsonb,integer),
    yoao_assets_v1.allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer),
    yoao_assets_v1.begin_native_pyramid_member(uuid,uuid,text,text,bigint),
    yoao_assets_v1.submit_native_pyramid_member(uuid),
    yoao_assets_v1.write_native_pyramid_member(uuid,text,text),
    yoao_assets_v1.verify_native_pyramid_member(uuid),
    yoao_assets_v1.abandon_native_pyramid_member(uuid,text),
    yoao_assets_v1.finish_native_pyramid_output(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.claim_accepted_image_input(uuid,uuid,integer),
    yoao_assets_v1.heartbeat_accepted_image_input(uuid,bigint,uuid,integer),
    yoao_assets_v1.finish_accepted_image_input(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_accepted_image_input(uuid,bigint,uuid,text)
    TO :"asset_image_worker_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_image_worker_role')
    );
    worker_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_image_original_worker(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[])')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_image_media(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.reconcile_image_media(jsonb,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_native_pyramid_member(uuid,uuid,text,text,bigint)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.submit_native_pyramid_member(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.write_native_pyramid_member(uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.verify_native_pyramid_member(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.abandon_native_pyramid_member(uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_native_pyramid_output(uuid,bigint,uuid,jsonb)')::oid
    ];
    accepted_input_worker_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_accepted_image_original(uuid,bigint,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_accepted_image_original(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_accepted_image_input(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_accepted_image_input(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_accepted_image_input(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_accepted_image_input(uuid,bigint,uuid,text)')::oid
    ];
BEGIN
    IF current_setting('yoao.assets_owner') = current_user AND EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(worker_functions || accepted_input_worker_functions) AS approved(oid)
        LEFT JOIN pg_catalog.pg_proc AS proc USING (oid)
        WHERE proc.proowner IS DISTINCT FROM current_user::regrole
           OR proc.prosecdef IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION 'Expected migration-owned SECURITY DEFINER image capabilities';
    END IF;
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Image worker LOGIN can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'yoao_assets_v1'
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
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND proc.oid <> ALL (worker_functions)
          AND proc.oid <> ALL (accepted_input_worker_functions)
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Image worker LOGIN has an unapproved module function grant';
    END IF;
END $$;
\endif

\if :{?grant_import}
SELECT set_config('yoao.grant_section_applied', '1', true);
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
    yoao_assets_v1.begin_dataset_import_original(uuid,uuid,jsonb),
    yoao_assets_v1.open_dataset_import_original(uuid,uuid,bigint,uuid,uuid),
    yoao_assets_v1.record_import_original_prepared(jsonb,jsonb),
    yoao_assets_v1.mark_import_original_writing(jsonb),
    yoao_assets_v1.record_import_original_written(jsonb,jsonb),
    yoao_assets_v1.fail_import_original(jsonb,text,text,jsonb),
    yoao_assets_v1.heartbeat_dataset_import(uuid,uuid,integer),
    yoao_assets_v1.save_observed_dataset_import_manifest(uuid,uuid,jsonb),
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
        pg_catalog.to_regprocedure('yoao_assets_v1.save_observed_dataset_import_manifest(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.next_dataset_import_entry(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_dataset_import(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_dataset_import_original(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.open_dataset_import_original(uuid,uuid,bigint,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_import_original_prepared(jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.mark_import_original_writing(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_import_original_written(jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_import_original(jsonb,text,text,jsonb)')::oid,
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
\endif

\if :{?grant_media}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
\if :{?asset_media_control_role}
SELECT set_config('yoao.asset_media_control_role', :'asset_media_control_role', true);
\else
SELECT set_config('yoao.asset_media_control_role', '', true);
\endif
SELECT set_config('yoao.asset_media_worker_role', :'asset_media_worker_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    migration_owned boolean := current_setting('yoao.assets_owner') = current_user;
    role_names text[] := array_remove(ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_media_control_role'),
        current_setting('yoao.asset_media_worker_role')
    ], '');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT name) FROM unnest(role_names) AS name)
       <> cardinality(role_names)
       OR current_user IN (current_setting('yoao.asset_media_control_role'),
                          current_setting('yoao.asset_media_worker_role')) THEN
        RAISE EXCEPTION 'Assets owner, migration user and configured media logins must not share a runtime LOGIN';
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
        IF migration_owned AND role_name = current_user THEN
            CONTINUE;
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
    IF (NOT migration_owned AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner') AND role.rolcanlogin
    )) OR (current_setting('yoao.asset_media_control_role') <> '' AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_media_control_role')
          AND role.rolcanlogin
    )) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_media_worker_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Dedicated Assets owner must be NOLOGIN and configured media roles must LOGIN';
    END IF;
END $$;

REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_media_worker_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_media_worker_role";
REVOKE ALL ON SCHEMA public FROM :"asset_media_worker_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_media_worker_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_media_worker_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_media_worker_role";
\if :{?asset_media_control_role}
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_media_control_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_media_control_role";
REVOKE ALL ON SCHEMA public FROM :"asset_media_control_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_media_control_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_media_control_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_media_control_role";
\endif

DO $$
DECLARE
    namespace record;
    role_name text;
    logins text[] := array_remove(ARRAY[
        current_setting('yoao.asset_media_control_role'),
        current_setting('yoao.asset_media_worker_role')
    ], '');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
    LOOP
        FOREACH role_name IN ARRAY logins LOOP
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_media_worker_role";
\if :{?asset_media_control_role}
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_media_control_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.begin_media_original_actor(uuid,uuid,uuid,text,text),
    yoao_assets_v1.finish_media_original_read(uuid,uuid),
    yoao_assets_v1.begin_media_original_system(uuid,text,text),
    yoao_assets_v1.finish_media_original_system(uuid,uuid),
    yoao_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text),
    yoao_assets_v1.finish_image_original_read(uuid,uuid),
    yoao_assets_v1.begin_image_original_system(text,uuid,text,text),
    yoao_assets_v1.finish_image_original_system(uuid,uuid),
    yoao_assets_v1.begin_video_member_original(uuid,uuid,uuid,text,bigint,uuid,text,text),
    yoao_assets_v1.begin_video_control_original(uuid,uuid,uuid,uuid,text,text,text),
    yoao_assets_v1.finish_video_control_original(uuid,uuid),
    yoao_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text),
    yoao_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text),
    yoao_assets_v1.finish_tracker_control_original(uuid,uuid),
    yoao_assets_v1.read_media_source(uuid,uuid,uuid),
    yoao_assets_v1.read_media_summaries(uuid[]),
    yoao_assets_v1.ensure_video_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.observe_media_source(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean),
    yoao_assets_v1.read_video_timetable(uuid,uuid,uuid,integer,integer,integer,integer),
    yoao_assets_v1.read_video_samples(uuid,uuid,uuid,integer,integer,integer),
    yoao_assets_v1.read_video_failures(uuid,integer,bigint),
    yoao_assets_v1.retry_video_media(uuid,uuid,jsonb),
    yoao_assets_v1.invalidate_video_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.cancel_video_media(uuid,uuid,uuid),
    yoao_assets_v1.read_media_preferences(),
    yoao_assets_v1.resolve_internal_media_source(uuid,text,jsonb),
    yoao_assets_v1.schedule_video_media(uuid,uuid,jsonb,boolean),
    yoao_assets_v1.read_video_work(uuid,uuid),
    yoao_assets_v1.scan_video_backfill(jsonb,uuid,integer),
    yoao_assets_v1.read_video_media_totals(uuid[]),
    yoao_assets_v1.read_internal_video_frame(uuid,integer,integer,text),
    yoao_assets_v1.read_image_source(uuid,uuid,uuid),
    yoao_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.read_image_summaries(uuid,jsonb,text),
    yoao_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb),
    yoao_assets_v1.retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb),
    yoao_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text),
    yoao_assets_v1.begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid),
    yoao_assets_v1.complete_native_pyramid_read(uuid,uuid),
    yoao_assets_v1.read_image_work(uuid,uuid),
    yoao_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.cancel_image_media(uuid,uuid,uuid),
    yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    yoao_assets_v1.read_internal_image_work(uuid),
    yoao_assets_v1.scan_image_backfill(jsonb,text,uuid,integer),
    yoao_assets_v1.read_image_usage(),
    yoao_assets_v1.find_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.read_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    yoao_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text),
    yoao_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text),
    yoao_assets_v1.read_accepted_video_manifest(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    yoao_assets_v1.prepare_accepted_video_member(uuid,uuid,uuid,text,bigint,uuid,text,bigint,jsonb,text),
    yoao_assets_v1.seal_accepted_video_manifest(uuid,uuid,uuid,text),
    yoao_assets_v1.read_accepted_video_input(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_input_work(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_times(uuid,uuid,uuid,uuid,text,bigint,integer),
    yoao_assets_v1.require_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint),
    yoao_assets_v1.read_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint),
    yoao_assets_v1.read_video_tracker_input(uuid,uuid,uuid),
    yoao_assets_v1.prepare_video_tracker_input(uuid,uuid,uuid,uuid,text,text,bigint,jsonb,text),
    yoao_assets_v1.reference_video_tracker_source(uuid,uuid,uuid,uuid,text,text)
    TO :"asset_media_control_role";
\endif
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.begin_media_original_worker(uuid,bigint,uuid,text,text),
    yoao_assets_v1.finish_media_original_worker(uuid,uuid),
    yoao_assets_v1.begin_accepted_video_original(uuid,bigint,uuid),
    yoao_assets_v1.finish_accepted_video_original(uuid,uuid),
    yoao_assets_v1.begin_tracker_video_original(uuid,bigint,uuid),
    yoao_assets_v1.finish_tracker_video_original(uuid,uuid),
    yoao_assets_v1.claim_video_media(uuid,uuid,integer,text[]),
    yoao_assets_v1.heartbeat_video_media(uuid,bigint,uuid,integer),
    yoao_assets_v1.stage_video_timetable(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.finish_video_media(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_video_media(uuid,bigint,uuid,text,text),
    yoao_assets_v1.expire_video_media(integer,integer,integer),
    yoao_assets_v1.claim_video_media_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_video_media_garbage(uuid,uuid),
    yoao_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer),
    yoao_assets_v1.read_video_media_usage(),
    yoao_assets_v1.claim_accepted_video_input(uuid,uuid,integer),
    yoao_assets_v1.heartbeat_accepted_video_input(uuid,bigint,uuid,integer),
    yoao_assets_v1.stage_accepted_video_times(uuid,bigint,uuid,bigint,jsonb),
    yoao_assets_v1.finish_accepted_video_input(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_accepted_video_input(uuid,bigint,uuid,text),
    yoao_assets_v1.claim_accepted_video_frame(uuid,uuid,integer),
    yoao_assets_v1.heartbeat_accepted_video_frame(uuid,bigint,uuid,integer),
    yoao_assets_v1.finish_accepted_video_frame(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_accepted_video_frame(uuid,bigint,uuid,text),
    yoao_assets_v1.sweep_accepted_video_inputs(integer),
    yoao_assets_v1.claim_video_tracker_input(uuid,uuid,integer),
    yoao_assets_v1.heartbeat_video_tracker_input(uuid,bigint,uuid,integer),
    yoao_assets_v1.stage_video_tracker_times(uuid,bigint,uuid,bigint,jsonb),
    yoao_assets_v1.finish_video_tracker_input(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_video_tracker_input(uuid,bigint,uuid,text),
    yoao_assets_v1.sweep_video_tracker_inputs(integer)
    TO :"asset_media_worker_role";

DO $$
DECLARE
    control_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_media_control_role')
    );
    worker_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_media_worker_role')
    );
    control_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_media_original_actor(uuid,uuid,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_media_original_read(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_media_original_system(uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_media_original_system(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_image_original_read(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_image_original_system(text,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_image_original_system(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_media_source(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_media_summaries(uuid[])')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.ensure_video_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.observe_media_source(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_timetable(uuid,uuid,uuid,integer,integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_samples(uuid,uuid,uuid,integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_failures(uuid,integer,bigint)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.retry_video_media(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.invalidate_video_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.cancel_video_media(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_media_preferences()')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.resolve_internal_media_source(uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.schedule_video_media(uuid,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_work(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.scan_video_backfill(jsonb,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_media_totals(uuid[])')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_internal_video_frame(uuid,integer,integer,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_image_source(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_image_summaries(uuid,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_native_pyramid_read(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_image_work(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.cancel_image_media(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_internal_image_work(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.scan_image_backfill(jsonb,text,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_image_usage()')::oid
    ];
    accepted_input_control_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_video_member_original(uuid,uuid,uuid,text,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_video_control_original(uuid,uuid,uuid,uuid,text,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_video_control_original(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_tracker_control_original(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.find_accepted_image_input(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_image_input(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_manifest(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_manifest_page(uuid,uuid,uuid,text,text,bigint,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_accepted_video_member(uuid,uuid,uuid,text,bigint,uuid,text,bigint,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.seal_accepted_video_manifest(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_input(uuid,uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_input_work(uuid,uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_times(uuid,uuid,uuid,uuid,text,bigint,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.require_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_tracker_input(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_video_tracker_input(uuid,uuid,uuid,uuid,text,text,bigint,jsonb,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.reference_video_tracker_source(uuid,uuid,uuid,uuid,text,text)')::oid
    ];
    worker_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_media_original_worker(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_media_original_worker(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_accepted_video_original(uuid,bigint,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_accepted_video_original(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_tracker_video_original(uuid,bigint,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_tracker_video_original(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_video_media(uuid,uuid,integer,text[])')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_video_media(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.stage_video_timetable(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_video_media(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_video_media(uuid,bigint,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.expire_video_media(integer,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_video_media_garbage(uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_video_media_garbage(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_video_media_usage()')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_accepted_video_input(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_accepted_video_input(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.stage_accepted_video_times(uuid,bigint,uuid,bigint,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_accepted_video_input(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_accepted_video_input(uuid,bigint,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_accepted_video_frame(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_accepted_video_frame(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_accepted_video_frame(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_accepted_video_frame(uuid,bigint,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.sweep_accepted_video_inputs(integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.claim_video_tracker_input(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.heartbeat_video_tracker_input(uuid,bigint,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.stage_video_tracker_times(uuid,bigint,uuid,bigint,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_video_tracker_input(uuid,bigint,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_video_tracker_input(uuid,bigint,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.sweep_video_tracker_inputs(integer)')::oid
    ];
BEGIN
    IF current_setting('yoao.assets_owner') = current_user AND EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(worker_functions ||
            CASE WHEN control_oid IS NULL THEN '{}'::oid[]
                 ELSE control_functions || accepted_input_control_functions END
        ) AS approved(oid)
        LEFT JOIN pg_catalog.pg_proc AS proc USING (oid)
        WHERE proc.proowner IS DISTINCT FROM current_user::regrole
           OR proc.prosecdef IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION 'Expected migration-owned SECURITY DEFINER media capabilities';
    END IF;
    IF pg_catalog.has_database_privilege(control_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(control_oid, current_database(), 'TEMP')
       OR pg_catalog.has_database_privilege(worker_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(worker_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Media logins can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'yoao_assets_v1'
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
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(control_oid, proc.oid, 'EXECUTE')
          AND (ns.nspname <> 'yoao_assets_v1' OR (
              proc.oid <> ALL(control_functions)
              AND proc.oid <> ALL(accepted_input_control_functions)
          ))
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(worker_oid, proc.oid, 'EXECUTE')
          AND (ns.nspname <> 'yoao_assets_v1' OR proc.oid <> ALL(worker_functions))
    ) THEN
        RAISE EXCEPTION 'Media logins have unapproved module function grants';
    END IF;
END $$;
\endif

\if :{?grant_operator}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.asset_operator_role', :'asset_operator_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_operator_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('yoao.migration_role') = current_setting('yoao.assets_owner')
       OR current_setting('yoao.migration_role') = current_setting('yoao.asset_operator_role')
       OR current_setting('yoao.assets_owner') = current_setting('yoao.asset_operator_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and operator LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe operator role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing operator role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls OR role.rolinherit)
        ) THEN
            RAISE EXCEPTION 'Unsafe operator role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Operator roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_operator_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and operator role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_operator_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_operator_role";
REVOKE ALL ON SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_operator_role";

DO $$
DECLARE
    namespace record;
    operator_role text := current_setting('yoao.asset_operator_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_operator_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_operator_role";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"asset_operator_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_first_seed_account()
    TO :"asset_operator_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.publish_operator_item_original(jsonb),
    yoao_assets_v1.operator_item_original_facts(uuid,uuid),
    yoao_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid),
    yoao_assets_v1.begin_saved_operator_original(jsonb),
    yoao_assets_v1.publish_operator_auxiliary(uuid),
    yoao_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid),
    yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid),
    yoao_assets_v1.saved_operator_item_original(uuid,uuid),
    yoao_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid),
    yoao_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid),
    yoao_assets_v1.operator_dataset_state(uuid),
    yoao_assets_v1.operator_scene_state(uuid,uuid),
    yoao_assets_v1.open_saved_operator_original_operation(uuid),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.record_operator_original_verified(uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean),
    yoao_assets_v1.create_operator_image_dataset(uuid,uuid,text,text),
    yoao_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb),
    yoao_assets_v1.append_operator_items(uuid,uuid,jsonb),
    yoao_assets_v1.patch_operator_items(uuid,uuid,jsonb),
    yoao_assets_v1.recount_operator_items(uuid,uuid),
    yoao_assets_v1.read_operator_items(uuid,uuid,integer)
    TO :"asset_operator_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_operator_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure(
            'yoao_identity_v1.read_first_seed_account()'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.publish_operator_item_original(jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.operator_item_original_facts(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.begin_saved_operator_original(jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.publish_operator_auxiliary(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.saved_operator_item_original(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.operator_dataset_state(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.operator_scene_state(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.open_saved_operator_original_operation(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.record_operator_original_prepared(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.mark_operator_original_writing(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.record_operator_original_written(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.record_operator_original_verified(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.append_operator_items(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.patch_operator_items(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.recount_operator_items(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.read_operator_items(uuid,uuid,integer)'
        )::oid
    ];
BEGIN
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Operator LOGIN can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname NOT IN ('yoao_assets_v1', 'yoao_identity_v1')
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Operator LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Operator LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Operator LOGIN has direct table privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has direct sequence privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has an unapproved module function grant';
    END IF;
END $$;
\endif

\if :{?grant_source}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.asset_source_role', :'asset_source_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.assets_owner'),
        current_setting('yoao.asset_source_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('yoao.migration_role') = current_setting('yoao.assets_owner')
       OR current_setting('yoao.migration_role') = current_setting('yoao.asset_source_role')
       OR current_setting('yoao.assets_owner') = current_setting('yoao.asset_source_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and source LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe source role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing source role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe source role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Source roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_source_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and source role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_source_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_source_role";

REVOKE ALL ON SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_source_role";

DO $$
DECLARE
    namespace record;
    source_role text := current_setting('yoao.asset_source_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_source_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_source_role";

GRANT EXECUTE ON FUNCTION yoao_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text),
    yoao_assets_v1.begin_import_original_verification(jsonb),
    yoao_assets_v1.record_import_original_read(jsonb,jsonb),
    yoao_assets_v1.finish_import_original_verification(jsonb,jsonb),
    yoao_assets_v1.recover_import_original_verification(jsonb),
    yoao_assets_v1.status_import_original_verification(jsonb),
    yoao_assets_v1.begin_operator_auxiliary(jsonb),
    yoao_assets_v1.open_operator_storage_operation(uuid),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.record_operator_original_verified(uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean),
    yoao_assets_v1.lock_dataset_upload(uuid,uuid,uuid),
    yoao_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text),
    yoao_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer),
    yoao_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text),
    yoao_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.lock_legacy_dataset_upload(uuid,uuid,uuid,uuid),
    yoao_assets_v1.complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer),
    yoao_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer),
    yoao_assets_v1.read_storage_connection(uuid,uuid),
    yoao_assets_v1.read_storage_connection_policy(uuid,jsonb),
    yoao_assets_v1.capture_storage_connection(uuid,uuid,text),
    yoao_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb),
    yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[]),
    yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean),
    yoao_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb),
    yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    yoao_assets_v1.capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text),
    yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb),
    yoao_assets_v1.capture_native_pyramid_retirement_io(uuid,uuid,uuid),
    yoao_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)
    TO :"asset_source_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.asset_source_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_import_original_verification(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_import_original_read(jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_import_original_verification(jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.recover_import_original_verification(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.status_import_original_verification(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_operator_auxiliary(jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.open_operator_storage_operation(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_operator_original_prepared(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.mark_operator_original_writing(uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_operator_original_written(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_operator_original_verified(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.lock_dataset_upload(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.lock_legacy_dataset_upload(uuid,uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_storage_connection(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_storage_connection_policy(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.capture_storage_connection(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[])')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.capture_native_pyramid_retirement_io(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('yoao_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)')::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'yoao_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Source LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Source LOGIN has direct table privileges';
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
        RAISE EXCEPTION 'Source LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Source LOGIN has direct sequence privileges';
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
        RAISE EXCEPTION 'Source LOGIN has an unapproved module function grant';
    END IF;
END $$;
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
