-- Grant a Labeling core read LOGIN only fixed row locks and public projections.
-- Run after migrations, module-data-grants.sql and lan-production-grants.sql.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v labeling_owner=labeling_code -v core_read_role=labeling_core_read_client \
--   -f infra/docker/labeling-core-read-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.core_read_role', :'core_read_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('aap.core_read_role'));
    v_function oid;
    v_signature text;
BEGIN
    IF current_database()<>current_setting('aap.expected_database')
       OR current_user<>current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_client IS NULL
       OR v_owner=v_client
       OR current_user::pg_catalog.regrole=v_client THEN
        RAISE EXCEPTION 'Labeling core read data roles are missing or overlap';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_owner AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_client AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Unsafe Labeling core read data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)',
        'aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)',
        'aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)',
        'aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)',
        'aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)',
        'aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)',
        'aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)',
        'aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)',
        'aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)',
        'aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)',
        'aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])',
        'aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.proretset
          AND function.prorettype = CASE v_signature
              WHEN 'aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)' THEN 'aap_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)' THEN 'aap_labeling_v1.task_dataset_item_link_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)' THEN 'aap_labeling_v1.batch_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)' THEN 'aap_labeling_v1.annotation_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)' THEN 'aap_labeling_v1.video_segment_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)' THEN 'aap_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)' THEN 'aap_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)' THEN 'aap_labeling_v1.prediction_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)' THEN 'aap_labeling_v1.prediction_meta_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)' THEN 'aap_labeling_v1.video_segment_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])' THEN 'aap_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])' THEN 'aap_labeling_v1.annotation_rows'::pg_catalog.regtype
          END
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling core read data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"core_read_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"core_read_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('aap.core_read_role');
BEGIN
    FOR v_schema IN
        SELECT namespace.nspname
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
    END LOOP;
END;
$revoke$;

REVOKE ALL ON FUNCTION aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
    FROM :"core_read_role", PUBLIC;
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"core_read_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
    TO :"core_read_role";
GRANT SELECT ON aap_labeling_v1.task_rows,
    aap_labeling_v1.batch_rows,
    aap_labeling_v1.annotation_rows,
    aap_labeling_v1.prediction_rows,
    aap_labeling_v1.prediction_meta_rows,
    aap_labeling_v1.failed_prediction_rows,
    aap_labeling_v1.task_lock_rows,
    aap_labeling_v1.task_dataset_item_link_rows,
    aap_labeling_v1.scene_track_rows,
    aap_labeling_v1.scene_track_interval_rows,
    aap_labeling_v1.video_segment_rows,
    aap_labeling_v1.video_tracker_job_rows,
    aap_labeling_v1.annotation_draft_rows
    TO :"core_read_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('aap.core_read_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])'),
        pg_catalog.to_regprocedure('aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])')
    ];
    v_allowed_relations oid[] := ARRAY[
        pg_catalog.to_regclass('aap_labeling_v1.task_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.batch_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.annotation_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.prediction_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.prediction_meta_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.failed_prediction_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.task_lock_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.task_dataset_item_link_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.scene_track_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.scene_track_interval_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.video_segment_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.video_tracker_job_rows'),
        pg_catalog.to_regclass('aap_labeling_v1.annotation_draft_rows')
    ];
    v_projection oid;
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'aap_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'aap_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN is missing its exact seven-function profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        JOIN pg_catalog.pg_type AS return_type
          ON return_type.oid=function.prorettype
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND function.prokind IN ('f','p','a','w')
          AND NOT (return_type.typnamespace='pg_catalog'::pg_catalog.regnamespace
              AND return_type.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid='pg_catalog.pg_proc'::pg_catalog.regclass
                AND dependency.objid=function.oid
                AND dependency.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass
                AND dependency.deptype='e'
          )
          AND (pg_catalog.left(namespace.nspname,4)='aap_'
               OR function.prosecdef
               OR EXISTS (
                   SELECT FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                       pg_catalog.acldefault('f',function.proowner))) AS acl
                   WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                     AND pg_catalog.pg_has_role(v_client,acl.grantee,'USAGE')
               ))
          AND NOT (function.oid=ANY(v_allowed))
          AND pg_catalog.has_function_privilege(v_client,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','aap_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN has foreign schema privileges';
    END IF;
    IF pg_catalog.array_position(v_allowed_relations,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'Labeling core read public projections are missing';
    END IF;
    FOREACH v_projection IN ARRAY v_allowed_relations LOOP
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_class WHERE oid=v_projection AND relkind='v')
           OR NOT pg_catalog.has_table_privilege(v_client,v_projection,'SELECT')
           OR pg_catalog.has_table_privilege(v_client,v_projection,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_client,v_projection,'INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Labeling core read public projection is not SELECT-only';
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT relation.oid, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r','p','v','m','f','S')
          AND NOT (relation.oid=ANY(v_allowed_relations))
    LOOP
        IF (v_relation.relkind <> 'S' AND (
                pg_catalog.has_table_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Labeling core read data LOGIN has data privileges outside its public projections';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relowner=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspowner=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_database AS database
        WHERE database.datdba=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.proowner=v_client
    ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling core read data command is executable by PUBLIC';
    END IF;
END;
$verify$;
COMMIT;
