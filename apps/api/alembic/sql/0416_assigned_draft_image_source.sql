-- Restore source access for the existing effective-assignee Workbench edit scope.
DO $migration$
DECLARE
    v_before text := $before$                        v_visible := v_batch_status IN ('active', 'annotating', 'rejected');$before$;
    v_after text := $after$                        v_visible := v_batch_status IN ('active', 'annotating', 'rejected');
                        IF v_batch_status = 'draft'
                           AND v_project_role = 'annotator'
                           AND v_assigned
                           AND v_task.status IN ('pending', 'in_progress', 'rejected') THEN
                            v_visible := true;
                        END IF;$after$;
    v_definition text := pg_catalog.pg_get_functiondef(
        'aap_labeling_v1.read_image_task_source(uuid,uuid,boolean)'::pg_catalog.regprocedure
    );
BEGIN
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before, '')) <> pg_catalog.length(v_before)
       OR pg_catalog.strpos(v_definition, v_after) <> 0 THEN
        RAISE EXCEPTION 'Unexpected image source visibility guard';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_before, v_after);
END
$migration$;
