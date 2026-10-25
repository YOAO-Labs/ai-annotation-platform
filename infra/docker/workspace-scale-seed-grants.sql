-- Grant the fixed local scale-seed capabilities to a dedicated trusted operator.
-- Compose with existing Identity and other-domain operator grants after the
-- ordinary Workspace consumer fence. Ordinary API/worker credentials omit this profile.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v workspace_owner=workspace_code -v assets_owner=assets_code
--   -v labeling_owner=labeling_code -v consumer_role=workspace_scale_seed_client
--   -f infra/docker/workspace-scale-seed-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.scale_seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('aap.scale_seed_operator');
    v_workspace name := current_setting('aap.workspace_owner');
    v_assets name := current_setting('aap.assets_owner');
    v_labeling name := current_setting('aap.labeling_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
 THEN
        RAISE EXCEPTION 'Unexpected database or migration owner for scale seed grants';
    END IF;
    IF (
        SELECT pg_catalog.count(DISTINCT role_name)
        FROM pg_catalog.unnest(ARRAY[
            current_user::name, v_workspace, v_assets, v_labeling, v_consumer
        ]) AS role_name
    ) <> 5 THEN
        RAISE EXCEPTION 'Migration, module owners and scale seed operator must be distinct';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = v_consumer
          AND role.rolcanlogin
          AND NOT (
              role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
              OR role.rolreplication OR role.rolbypassrls
          )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = v_consumer
        )
           OR (membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = v_consumer
        ) AND membership.member <> (SELECT role.oid FROM pg_catalog.pg_roles AS role WHERE role.rolname=current_user))
    ) THEN
        RAISE EXCEPTION 'Scale seed operator must be a non-admin LOGIN without inherited roles';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (v_workspace, v_assets, v_labeling)
          AND (
              role.rolcanlogin
              OR role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
              OR role.rolreplication OR role.rolbypassrls
          )
    ) OR (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (v_workspace, v_assets, v_labeling)
    ) <> 3 THEN
        RAISE EXCEPTION 'Workspace, Assets and Labeling owners must be non-admin NOLOGIN roles';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(ARRAY[
            'public.projects',
            'public.project_members',
            'public.project_templates'
        ]) AS private(relation_name)
        WHERE pg_catalog.has_table_privilege(
                  v_consumer, private.relation_name,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
           OR pg_catalog.has_any_column_privilege(
                  v_consumer, private.relation_name,
                  'SELECT,INSERT,UPDATE,REFERENCES'
              )
    ) THEN
        RAISE EXCEPTION 'Scale seed operator must not access private Workspace tables directly';
    END IF;

    FOREACH v_signature IN ARRAY ARRAY[
        'aap_workspace_v1.create_scale_seed_project(uuid,text)',
        'aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid)',
        'aap_workspace_v1.guard_scale_seed_cleanup(text)',
        'aap_workspace_v1.delete_scale_seed_projects(text,uuid[])',
        'aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)',
        'aap_labeling_v1.write_project_counter_projection(uuid,jsonb)',
        'aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])',
        'aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)',
        'aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)',
        'aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = function.proowner
            WHERE function.oid = v_signature::regprocedure
              AND function.prosecdef
              AND owner.rolname = CASE
                  WHEN pg_catalog.split_part(v_signature, '.', 1)
                       = 'aap_workspace_v1' THEN v_workspace
                  WHEN pg_catalog.split_part(v_signature, '.', 1)
                       = 'aap_assets_v1' THEN v_assets
                  ELSE v_labeling
              END
              AND function.provolatile = 'v'
        ) THEN
            RAISE EXCEPTION 'Unexpected scale seed capability owner or security mode: %', v_signature;
        END IF;
    END LOOP;
END $profile$;

GRANT USAGE ON SCHEMA aap_workspace_v1, aap_assets_v1, aap_labeling_v1
    TO :"consumer_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.create_scale_seed_project(uuid,text),
    aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid),
    aap_workspace_v1.guard_scale_seed_cleanup(text),
    aap_workspace_v1.delete_scale_seed_projects(text,uuid[]),
    aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean),
    aap_labeling_v1.write_project_counter_projection(uuid,jsonb),
    aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[]),
    aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid),
    aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid),
    aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)
    TO :"consumer_role";
GRANT SELECT (id, project_id, display_id)
    ON aap_labeling_v1.batch_rows TO :"consumer_role";

COMMIT;
