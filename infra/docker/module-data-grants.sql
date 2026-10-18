-- Run after migrations as the schema owner. Roles must already exist.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v workspace_owner=workspace_code -v assets_owner=assets_code
--   -v labeling_owner=labeling_code -v workflow_role=append_client
--   -f infra/docker/module-data-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.workflow_role', :'workflow_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('aap.workspace_owner'), current_setting('aap.assets_owner'),
        current_setting('aap.labeling_owner'), current_setting('aap.workflow_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT n) FROM unnest(role_names) AS n) <> 4 THEN
        RAISE EXCEPTION 'Module owners and caller must be distinct roles';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name
              AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
              AND rolcanlogin = (role_name = current_setting('aap.workflow_role'))
        ) OR pg_has_role(role_name, current_user, 'MEMBER') THEN
            RAISE EXCEPTION 'Unsafe or missing module role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT FROM pg_auth_members AS m
            WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = role_name)
        ) THEN
            RAISE EXCEPTION 'Module roles must not inherit other roles: %', role_name;
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_workspace_v1, aap_assets_v1, aap_labeling_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_workspace_v1, aap_assets_v1, aap_labeling_v1 FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"workflow_role";
GRANT USAGE ON SCHEMA public TO :"workspace_owner", :"assets_owner", :"labeling_owner";

-- Function owners can reach only the columns their capabilities require.
GRANT SELECT (id), UPDATE (id) ON public.projects TO :"workspace_owner";
GRANT SELECT (id), UPDATE (id) ON public.datasets TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_id), UPDATE (id)
    ON public.project_datasets TO :"assets_owner";
GRANT SELECT (id, dataset_id, file_name, file_path, file_type)
    ON public.dataset_items TO :"assets_owner";
GRANT UPDATE (id) ON public.dataset_items TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_item_id), INSERT
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, total_tasks), UPDATE (total_tasks)
    ON public.projects TO :"labeling_owner";
GRANT USAGE ON SEQUENCE public.display_seq_tasks TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE ON public.task_append_operations TO :"labeling_owner";

GRANT USAGE ON SCHEMA aap_workspace_v1 TO :"workspace_owner", :"assets_owner";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"assets_owner", :"labeling_owner";
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"labeling_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) TO :"workflow_role";

-- Keep schema ownership with the migrator; caller roles cannot replace code.
GRANT CREATE ON SCHEMA aap_workspace_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_reference(uuid) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM :"workspace_owner";
GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_reference(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'public.projects', 'public.datasets', 'public.dataset_items',
        'public.project_datasets', 'public.tasks', 'public.task_append_operations'
    ] LOOP
        IF has_any_column_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.workflow_role'), target, 'DELETE,TRUNCATE,TRIGGER') THEN
            RAISE EXCEPTION 'Caller has direct privileges on %; use a clean dedicated role', target;
        END IF;
    END LOOP;
    IF has_database_privilege(current_setting('aap.workflow_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_labeling_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Caller must not hold schema or database CREATE privileges';
    END IF;
END $$;
COMMIT;
