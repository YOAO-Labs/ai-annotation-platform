GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"learning_owner", :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.dataset_item_references(uuid, uuid) TO :"learning_owner";
GRANT SELECT, INSERT ON TABLE public.active_learning_rounds,
    public.active_learning_attempts,
    public.active_learning_pool_members,
    public.active_learning_candidates,
    public.active_learning_decisions,
    public.active_learning_references,
    public.active_learning_feedback,
    public.learning_policies,
    public.learning_triggers,
    public.training_studies,
    public.training_trials,
    public.training_recipes,
    public.training_recipe_revisions
    TO :"learning_owner";
GRANT UPDATE ON TABLE public.active_learning_rounds,
    public.active_learning_attempts,
    public.active_learning_candidates,
    public.learning_policies,
    public.learning_triggers,
    public.training_studies,
    public.training_trials,
    public.training_recipes
    TO :"learning_owner";
GRANT DELETE ON TABLE public.learning_policies TO :"learning_owner";
GRANT SELECT (id, project_id, snapshot, snapshot_digest, manifest), UPDATE (status)
    ON TABLE public.training_dataset_versions TO :"learning_owner";
GRANT SELECT (id, project_id, auto_trigger_id, study_id, dataset_id, request_id, config_digest,
    status, authorization) ON TABLE public.training_runs TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.active_learning_original_history(),
    yoao_learning_v1.active_learning_original_decisions(),
    yoao_learning_v1.active_learning_pool_item_count(uuid[]),
    yoao_learning_v1.active_learning_candidate_item_count(uuid[])
    TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_create_round(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb, uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_for_job(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_rounds(uuid, text, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_count(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_attempt(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recent_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_attempt_ids(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidates(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_rounds(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_open_attempt(uuid, integer, uuid, uuid, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_claim_attempt(uuid, integer, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_touch_attempt(uuid, integer, uuid, text, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_debit_round(uuid, integer, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidate_request(uuid, integer, uuid, integer, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidate_evidence(uuid, integer, uuid, integer, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_settle_attempt(uuid, integer, uuid, text, text, text, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_release_cancelled_claim(uuid, integer, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_decision_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_decisions(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_accepted_decisions(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_record_decision(uuid, uuid, uuid, uuid, text, text, uuid, integer[], jsonb, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_feedback_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_feedback(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_receipt_count(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_record_feedback(uuid, uuid, uuid, text, jsonb, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_retain_history(uuid, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_history_reference(uuid, uuid, uuid[], uuid[], uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_history_budget(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_history(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_original_history() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_original_decisions() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_pool_item_count(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_candidate_item_count(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_adoption_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_adoption_candidates(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_pin_storage(uuid, uuid, integer, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy(uuid, text, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_save_policy(uuid, text, integer, uuid, boolean, uuid, jsonb, jsonb, timestamptz, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_disable_policy(uuid, integer, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_delete_policy(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_policies(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy_check_state(uuid, integer, boolean, text, text, timestamptz, timestamptz, timestamptz, text, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_advance_baseline(uuid, integer, uuid, uuid, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_other_active_trigger(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_duplicate(uuid, integer, uuid, text[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_history(uuid, text, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_count(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_build_trigger(uuid, integer, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_progress(uuid, integer, text, text, timestamptz, text, text, text, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_requeue_trigger(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy_claim(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_references(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study(uuid, uuid, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_studies(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_count(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_studies() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_claim(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_create_study(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trials(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_bind_trial(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_progress(uuid, boolean, text, text, timestamptz, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_resume_study(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe(uuid, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipes(uuid, boolean, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_count(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revision(uuid, uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revisions(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revision_count(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_append_recipe(uuid, uuid, integer, uuid, text, text, jsonb, text, jsonb, uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_archive_recipe(uuid, uuid, integer) OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.selection_storage_owners OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.recipe_lineage OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.recipe_revision_lineage OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
