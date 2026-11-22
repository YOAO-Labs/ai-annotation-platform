\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.e2e_fixture_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.e2e_fixture_operator');
    v_identity name := current_setting('yoao.identity_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR (right(lower(current_database()),5)<>'_test' AND right(lower(current_database()),4)<>'_e2e')
       OR v_consumer IN (current_user, v_identity, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_identity, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Identity E2E fixture operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_identity AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'E2E fixture operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(ARRAY['public.users','public.identity_account_presence','public.user_invitations','public.password_reset_tokens','public.organization_members']) private(name) WHERE has_table_privilege(v_consumer,private.name,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR has_any_column_privilege(v_consumer,private.name,'SELECT,INSERT,UPDATE,REFERENCES')) THEN
        RAISE EXCEPTION 'Apply the ordinary Identity consumer fence first';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_roles owner ON owner.oid=c.relowner
        WHERE c.oid='yoao_identity_fixture_v1.account_profiles'::regclass
          AND c.relkind='v' AND owner.rolname=v_identity
          AND COALESCE(c.reloptions @> ARRAY['security_barrier=true'],false)
          AND ARRAY(SELECT a.attname::text FROM pg_attribute a
                    WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                    ORDER BY a.attnum)=ARRAY['id','email','name','role','group_name','group_id','status','is_active','disabled_kind','disabled_at','disabled_by','disabled_reason','email_verified_at','last_login_at','last_seen_at','password_admin_reset_at','deactivation_requested_at','deactivation_scheduled_at','preferences','avatar_ref','created_at']
    ) THEN
        RAISE EXCEPTION 'Unexpected E2E account read relation';
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA yoao_identity_fixture_v1 TO %I',v_consumer);
    EXECUTE format('GRANT SELECT ON yoao_identity_fixture_v1.account_profiles TO %I',v_consumer);
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_identity_fixture_v1.create_account(uuid,text,text,text,text)',
        'yoao_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)',
        'yoao_identity_fixture_v1.create_invitations(jsonb)',
        'yoao_identity_fixture_v1.delete_accounts(boolean,text[])',
        'yoao_identity_fixture_v1.delete_reset_tokens(uuid[])',
        'yoao_identity_fixture_v1.delete_invitations(uuid[])',
        'yoao_identity_fixture_v1.delete_organization_members(uuid[])'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef AND p.provolatile='v'
              AND owner.rolname=v_identity
        ) THEN
            RAISE EXCEPTION 'Unexpected E2E fixture function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
COMMIT;
