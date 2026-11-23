GRANT USAGE, CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid),
    yoao_jobs_v1.read_job(uuid) TO :"learning_owner";
GRANT SELECT (id, project_id) ON public.training_runs TO :"learning_owner";
GRANT UPDATE (id) ON public.training_runs TO :"learning_owner";
GRANT SELECT (id, run_id, status, artifacts) ON public.training_attempts TO :"learning_owner";
GRANT SELECT (id, project_id, status, snapshot) ON public.training_dataset_versions TO :"learning_owner";
GRANT SELECT (evaluation_id) ON public.active_learning_references TO :"learning_owner";
GRANT SELECT, INSERT ON public.trained_model_versions, public.trained_model_deployments,
    public.project_model_releases, public.project_model_selections, public.model_decisions,
    public.sample_evaluation_runs, public.sample_evaluation_attempts, public.sample_evaluation_samples
    TO :"learning_owner";
GRANT UPDATE (id) ON public.trained_model_versions TO :"learning_owner";
GRANT UPDATE (check_id,status,receipt,trial,error_message,checked_at)
    ON public.trained_model_deployments TO :"learning_owner";
GRANT UPDATE (id) ON public.project_model_releases TO :"learning_owner";
GRANT UPDATE (release_id,generation,updated_at) ON public.project_model_selections TO :"learning_owner";
GRANT UPDATE (status,storage_ref,generation,current_attempt_id,async_job_id,error_code,error_message,
              completed_at,deletion_requested_at) ON public.sample_evaluation_runs TO :"learning_owner";
GRANT UPDATE (status,claim_id,report,error_code,error_message,heartbeat_at,dispatched_at,
              maintenance_at,started_at,completed_at) ON public.sample_evaluation_attempts TO :"learning_owner";
GRANT UPDATE (status,request,evidence,error_code,error_message,produced_attempt_id)
    ON public.sample_evaluation_samples TO :"learning_owner";
GRANT DELETE ON public.sample_evaluation_runs TO :"learning_owner";

SELECT set_config('yoao.learning_owner', :'learning_owner', true);
DO $learning_models$
DECLARE
    v_name text;
    v_owner text := current_setting('yoao.learning_owner');
BEGIN
    FOREACH v_name IN ARRAY ARRAY[
        '_model_version_rows', '_model_deployment_rows', '_model_release_rows', '_model_decision_rows',
        'model_version_lineage', 'model_deployment_lineage', 'model_release_lineage',
        '_evaluation_run_rows', '_evaluation_attempt_rows', '_evaluation_sample_rows',
        'sample_evaluation_lineage', 'sample_evaluation_storage_owners'
    ] LOOP
        EXECUTE format('ALTER VIEW yoao_learning_v1.%I OWNER TO %I', v_name, v_owner);
    END LOOP;
    FOREACH v_name IN ARRAY ARRAY[
        'model_version_detail', 'model_registration_replay',
        'register_model_candidate', 'run_model_candidates', 'run_has_model_candidate',
        'catalog_model_candidates', 'model_candidate_visibility', 'model_candidate_metadata',
        '_filtered_model_candidates', 'list_model_candidates', 'count_model_candidates',
        'model_deployments_for_versions', 'model_deployment_detail', 'hold_model_deployment_for_registry',
        'hold_registry_model_deployments', 'count_active_model_releases', 'hold_model_candidate',
        'hold_prediction_model_reference', 'start_model_deployment_check', 'finish_model_deployment_check',
        'advance_model_deployment_retirement', 'model_release_detail', 'model_release_replay',
        'catalog_model_releases', 'project_model_selection', 'list_project_model_releases',
        'count_project_model_releases', 'publish_project_model_release', 'append_model_decision',
        'list_model_decisions', 'trial_mask_keys', 'trial_mask_referenced',
        'pending_dataset_mask_keys', 'pending_dataset_mask_referenced',
        'evaluation_detail', 'evaluation_for_attempt', 'hold_evaluation_for_current_job',
        'evaluation_replay', 'list_project_evaluations', 'count_project_evaluations',
        'dataset_has_sample_evaluation', 'held_out_evaluation_budget', 'held_out_evaluation_specs',
        'evaluation_attempt_detail', 'evaluation_attempt_history', 'evaluation_sample_detail',
        'evaluation_sample_evidence', 'hold_evaluation_adoption_attempts', 'hold_evaluation_adoption_samples',
        'pin_retained_evaluation_storage', 'create_sample_evaluation', 'queue_evaluation_attempt',
        '_hold_evaluation_attempt', 'claim_evaluation_attempt', 'heartbeat_evaluation_attempt',
        'record_evaluation_dispatch', 'evaluation_maintenance_candidates', 'record_evaluation_maintenance',
        'start_evaluation_sample', 'checkpoint_evaluation_sample', 'fail_evaluation_sample_input',
        'request_evaluation_cancellation', 'lose_evaluation_attempt', 'finish_evaluation_attempt',
        'request_evaluation_deletion', 'purge_deleted_evaluation'
    ] LOOP
        EXECUTE format('ALTER FUNCTION yoao_learning_v1.%I OWNER TO %I', v_name, v_owner);
    END LOOP;
END
$learning_models$;
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
