\set ON_ERROR_STOP on

\if :{?application_role}
\else
\echo 'missing -v application_role=<role>'
\quit 3
\endif
\if :{?collector_role}
\else
\echo 'missing -v collector_role=<role>'
\quit 3
\endif

BEGIN;

SELECT set_config('app.gpu_application_role', :'application_role', true);
SELECT set_config('app.gpu_collector_role', :'collector_role', true);

REVOKE DELETE ON TABLE
    public.gpu_backend_memberships,
    public.gpu_backend_fences
FROM :"application_role";

REVOKE ALL PRIVILEGES ON TABLE
    public.ml_backend_registry,
    public.gpu_backend_memberships,
    public.gpu_backend_fences
FROM :"collector_role";

GRANT USAGE ON SCHEMA public TO :"collector_role";
GRANT SELECT ON TABLE
    public.ml_backend_registry,
    public.gpu_backend_memberships,
    public.gpu_backend_fences
TO :"collector_role";
GRANT UPDATE (backend_registry_id)
    ON TABLE public.gpu_backend_memberships TO :"collector_role";
GRANT UPDATE (backend_registry_id)
    ON TABLE public.gpu_backend_fences TO :"collector_role";
GRANT DELETE ON TABLE
    public.gpu_backend_memberships,
    public.gpu_backend_fences
TO :"collector_role";

DO $$
DECLARE
    application_role text := current_setting('app.gpu_application_role');
    collector_role text := current_setting('app.gpu_collector_role');
    application pg_roles%ROWTYPE;
    collector pg_roles%ROWTYPE;
BEGIN
    IF application_role = collector_role THEN
        RAISE EXCEPTION 'application and collector roles must be distinct';
    END IF;
    SELECT * INTO application FROM pg_roles WHERE rolname = application_role;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'application role does not exist';
    END IF;
    SELECT * INTO collector FROM pg_roles WHERE rolname = collector_role;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'collector role does not exist';
    END IF;
    IF collector.rolsuper OR collector.rolcreaterole OR collector.rolcreatedb
       OR collector.rolreplication OR collector.rolbypassrls THEN
        RAISE EXCEPTION 'collector role has elevated cluster privileges';
    END IF;
    IF application.rolsuper OR application.rolcreaterole
       OR application.rolcreatedb OR application.rolreplication
       OR application.rolbypassrls THEN
        RAISE EXCEPTION 'application role has elevated cluster privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members
        WHERE member IN (application.oid, collector.oid)
          AND set_option
    ) THEN
        RAISE EXCEPTION 'application or collector role can SET ROLE';
    END IF;
    IF has_table_privilege(
           application_role, 'public.gpu_backend_memberships', 'DELETE'
       ) OR has_table_privilege(
           application_role, 'public.gpu_backend_fences', 'DELETE'
       ) THEN
        RAISE EXCEPTION
            'application role still has effective GPU tombstone DELETE privilege';
    END IF;
    IF has_table_privilege(
           collector_role, 'public.ml_backend_registry', 'INSERT,UPDATE,DELETE'
       ) OR has_column_privilege(
           collector_role, 'public.ml_backend_registry', 'id', 'UPDATE'
       ) OR has_table_privilege(
           collector_role, 'public.gpu_backend_memberships', 'INSERT,UPDATE'
       ) OR has_table_privilege(
           collector_role, 'public.gpu_backend_fences', 'INSERT,UPDATE'
       ) THEN
        RAISE EXCEPTION 'collector role exceeds the bounded GC privilege set';
    END IF;
END;
$$;

COMMIT;
