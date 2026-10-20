-- Provision exact LOGIN capabilities for the native invitation and Mail v2
-- consumers. The code owner remains Identity; these roles never reach tables.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v labeling_owner=labeling_code -v settings_owner=settings_code
--   -v audit_owner=audit_code
--   -v identity_invitation_role=identity_invitation_client
--   -v identity_mail_v2_role=identity_mail_v2_client
--   -f infra/docker/identity-invitations-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.settings_owner', :'settings_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.identity_invitation_role', :'identity_invitation_role', true);
SELECT set_config('aap.identity_mail_v2_role', :'identity_mail_v2_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('aap.identity_invitation_role'),
        current_setting('aap.identity_mail_v2_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('aap.identity_invitation_role') <> 'identity_invitation_client'
       OR current_setting('aap.identity_mail_v2_role') <> 'identity_mail_v2_client'
       OR (SELECT count(DISTINCT role_name) FROM unnest(role_names) AS role_name) <> 2
       OR EXISTS (
           SELECT 1 FROM unnest(role_names) AS candidate(name)
           WHERE candidate.name = ANY(ARRAY[
               current_setting('aap.identity_owner'), current_setting('aap.workspace_owner'),
               current_setting('aap.labeling_owner'), current_setting('aap.settings_owner'),
               current_setting('aap.audit_owner'), current_setting('aap.migration_role')
           ])
       ) THEN
        RAISE EXCEPTION 'Unexpected Identity invitation role names';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name AND role.rolcanlogin
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
        ) OR pg_catalog.pg_has_role(role_name,current_user,'MEMBER')
          OR EXISTS (
              SELECT 1 FROM pg_catalog.pg_auth_members AS membership
              WHERE membership.member = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
              ) OR membership.roleid = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
              )
          ) THEN
            RAISE EXCEPTION 'Unsafe or inheriting Identity invitation role: %', role_name;
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    identity_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('aap.identity_owner'));
    workspace_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = current_setting('aap.workspace_owner'));
    labeling_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('aap.labeling_owner'));
    settings_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('aap.settings_owner'));
    audit_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                      WHERE rolname = current_setting('aap.audit_owner'));
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR settings_oid IS NULL OR audit_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity invitation code owner';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'aap_identity_invitations_v1.invitation_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_invitations_v1.prepare_invitation_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_invitations_v1.apply_invitation_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_invitations_v1._incompatible_role_detail(text,text)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_groups_v1.resolve_invitation_group(uuid,text)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_audit_v1.record_identity_invitation_event(text,jsonb,jsonb,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_settings_v1.invitation_policy(jsonb)'
           )::oid) IS DISTINCT FROM settings_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_labeling_v1.invitation_work_summary(uuid,uuid,text)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_workspace_v1.grant_invitation_membership(uuid,uuid,text,uuid,uuid)'
           )::oid) IS DISTINCT FROM workspace_oid THEN
        RAISE EXCEPTION 'An invitation cross-owner capability has the wrong owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid IN (
            pg_catalog.to_regprocedure('aap_identity_mail_v2.mail_policy()')::oid,
            pg_catalog.to_regprocedure('aap_identity_mail_v2.claim_mail(jsonb)')::oid,
            pg_catalog.to_regprocedure('aap_identity_mail_v2.renew_mail(jsonb)')::oid,
            pg_catalog.to_regprocedure('aap_identity_mail_v2.finish_mail(jsonb)')::oid
        ) AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Mail v2 functions have the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure(
            'aap_audit_v1.record_identity_invitation_event(text,jsonb,jsonb,jsonb,jsonb)'
        )::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_settings_v1.invitation_policy(jsonb)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_labeling_v1.invitation_work_summary(uuid,uuid,text)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_workspace_v1.invitation_projects(uuid[])')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_workspace_v1.lock_invitation_project(uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_workspace_v1.read_invitation_membership(uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_workspace_v1.grant_invitation_membership(uuid,uuid,text,uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('aap_identity_groups_v1.resolve_invitation_group(uuid,text)')::oid,'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing an invitation owner capability';
    END IF;
    IF pg_catalog.has_table_privilege(identity_oid,'public.projects','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.project_members','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.task_batches','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.system_settings','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.audit_logs','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
        RAISE EXCEPTION 'Identity invitation owner has raw foreign-table access';
    END IF;
    IF NOT pg_catalog.has_column_privilege(
        identity_oid,'public.identity_invitation_operation_receipts','operation_id','SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.identity_invitation_operation_receipts','operation_id','INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.user_invitations','token','SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.user_invitations','token','UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid,'public.project_members','role','INSERT'
    ) THEN
        RAISE EXCEPTION 'Missing Identity invitation private state or Workspace membership capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_invitation_role", :"identity_mail_v2_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_invitation_role", :"identity_mail_v2_role";
DO $$
DECLARE
    namespace record;
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('aap.identity_invitation_role'),
        current_setting('aap.identity_mail_v2_role')
    ] LOOP
        FOR namespace IN
            SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',namespace.nspname,role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_invitations_v1 TO :"identity_invitation_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_invitations_v1.invitation_policy(),
    aap_identity_invitations_v1.prepare_invitation_command(jsonb),
    aap_identity_invitations_v1.apply_invitation_command(jsonb)
    TO :"identity_invitation_role";
GRANT USAGE ON SCHEMA aap_identity_mail_v2 TO :"identity_mail_v2_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_mail_v2.mail_policy(),
    aap_identity_mail_v2.claim_mail(jsonb),
    aap_identity_mail_v2.renew_mail(jsonb),
    aap_identity_mail_v2.finish_mail(jsonb)
    TO :"identity_mail_v2_role";

DO $$
DECLARE
    role_name text;
    role_oid oid;
    allowed_schema text;
    actual_functions text[];
    expected_functions text[];
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('aap.identity_invitation_role'),
        current_setting('aap.identity_mail_v2_role')
    ] LOOP
        role_oid := (SELECT role.oid FROM pg_catalog.pg_roles AS role WHERE role.rolname=role_name);
        IF role_name = current_setting('aap.identity_invitation_role') THEN
            allowed_schema := 'aap_identity_invitations_v1';
            expected_functions := ARRAY[
                'aap_identity_invitations_v1.apply_invitation_command(jsonb)',
                'aap_identity_invitations_v1.invitation_policy()',
                'aap_identity_invitations_v1.prepare_invitation_command(jsonb)'
            ];
        ELSE
            allowed_schema := 'aap_identity_mail_v2';
            expected_functions := ARRAY[
                'aap_identity_mail_v2.claim_mail(jsonb)',
                'aap_identity_mail_v2.finish_mail(jsonb)',
                'aap_identity_mail_v2.mail_policy()',
                'aap_identity_mail_v2.renew_mail(jsonb)'
            ];
        END IF;
        SELECT COALESCE(pg_catalog.array_agg(
            namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
            ORDER BY namespace.nspname, function.proname
        ),ARRAY[]::text[]) INTO actual_functions
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
        WHERE left(namespace.nspname,4)='aap_' AND function.prokind IN ('f','p')
          AND pg_catalog.has_function_privilege(role_oid,function.oid,'EXECUTE');
        IF actual_functions IS DISTINCT FROM expected_functions THEN
            RAISE EXCEPTION 'Identity invitation role has unexpected function surface: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE left(namespace.nspname,4) = 'aap_'
              AND namespace.nspname <> allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid,namespace.oid,'USAGE')
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r','p','v','m','f')
              AND (pg_catalog.has_table_privilege(role_oid,relation.oid,
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(role_oid,relation.oid,
                       'SELECT,INSERT,UPDATE,REFERENCES'))
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_proc AS function ON function.pronamespace=namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND left(namespace.nspname,4) <> 'aap_'
              AND function.prokind IN ('f','p') AND function.prosecdef
              AND pg_catalog.has_function_privilege(role_oid,function.oid,'EXECUTE')
        ) OR pg_catalog.has_database_privilege(role_oid,current_database(),'CREATE,TEMP') THEN
            RAISE EXCEPTION 'Identity invitation role has non-function capabilities: %', role_name;
        END IF;
    END LOOP;
END $$;
COMMIT;
