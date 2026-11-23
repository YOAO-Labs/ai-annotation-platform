"""Fixed policy, study and recipe data capabilities on the caller's transaction."""

from sqlalchemy import Boolean, DateTime, Integer, String, column, table, text
from sqlalchemy.dialects.postgresql import UUID

from app.services.active_learning_data import _execute, _many, _one, _scalar, _statement

_POLICY = text(
    "SELECT * FROM yoao_learning_v1.selection_policy(:project, :task, :policy, :lock)"
)
_SAVE_POLICY = _statement(
    "SELECT * FROM yoao_learning_v1.selection_save_policy(:project, :task, :revision, :actor, :enabled, "
    ":initialization, :config, :authorization, :checked, :next_check, :message)",
    "config",
    "authorization",
)
_DISABLE_POLICY = text(
    "SELECT * FROM yoao_learning_v1.selection_disable_policy(:policy, :revision, :checked, :message)"
)
_DELETE_POLICY = text(
    "SELECT yoao_learning_v1.selection_delete_policy(:policy, :revision)"
)
_DUE_POLICIES = text("SELECT * FROM yoao_learning_v1.selection_due_policies(:at)")
_POLICY_STATE = text(
    "SELECT yoao_learning_v1.selection_policy_check_state(:policy, :revision, :enabled, :state, :message, "
    ":checked, :next_check, :started, :snapshot, :blocked)"
)
_BASELINE = _statement(
    "SELECT yoao_learning_v1.selection_advance_baseline(:policy, :revision, :trigger, :dataset, :baseline)",
    "baseline",
)
_TRIGGER = text(
    "SELECT * FROM yoao_learning_v1.selection_trigger(:trigger, :policy, :mode)"
)
_OTHER_TRIGGER = text(
    "SELECT yoao_learning_v1.selection_other_active_trigger(:project, :policy, :trigger)"
)
_DUPLICATE = text(
    "SELECT yoao_learning_v1.selection_trigger_duplicate(:policy, :revision, :trigger, :digests)"
)
_TRIGGER_HISTORY = text(
    "SELECT * FROM yoao_learning_v1.selection_trigger_history(:project, :task, :limit, :offset)"
)
_TRIGGER_COUNT = text(
    "SELECT yoao_learning_v1.selection_trigger_count(:project, :task)"
)
_BUILD_TRIGGER = text(
    "SELECT yoao_learning_v1.selection_build_trigger(:policy, :revision, :dataset, :digest)"
)
_TRIGGER_PROGRESS = text(
    "SELECT yoao_learning_v1.selection_trigger_progress(:trigger, :revision, :status, :message, :completed, "
    ":candidate, :train, :validation, :changed)"
)
_REQUEUE_TRIGGER = text("SELECT yoao_learning_v1.selection_requeue_trigger(:trigger)")
_POLICY_CLAIM = text(
    "SELECT * FROM yoao_learning_v1.selection_policy_claim(:trigger, :project, :dataset)"
)
_DATASET_REFS = text(
    "SELECT * FROM yoao_learning_v1.selection_dataset_references(:version)"
)
_STUDY = text(
    "SELECT * FROM yoao_learning_v1.selection_study(:project, :study, :request, :lock)"
)
_STUDIES = text(
    "SELECT * FROM yoao_learning_v1.selection_studies(:project, :limit, :offset)"
)
_STUDY_COUNT = text("SELECT yoao_learning_v1.selection_study_count(:project)")
_DUE_STUDIES = text("SELECT * FROM yoao_learning_v1.selection_due_studies()")
_STUDY_CLAIM = text(
    "SELECT * FROM yoao_learning_v1.selection_study_claim(:study, :project, :dataset)"
)
_CREATE_STUDY = _statement(
    "SELECT * FROM yoao_learning_v1.selection_create_study(:study, :project, :actor, :dataset, :initialization, "
    ":request, :request_digest, :name, :plan, :plan_digest, :origin, :authorization)",
    "plan",
    "origin",
    "authorization",
)
_TRIALS = text("SELECT * FROM yoao_learning_v1.selection_trials(:study, :pending)")
_BIND_TRIAL = text("SELECT yoao_learning_v1.selection_bind_trial(:study, :trial, :run)")
_STUDY_PROGRESS = text(
    "SELECT yoao_learning_v1.selection_study_progress(:study, :scheduling, :status, :message, :checked, :completed)"
)
_STUDY_RESUME = text("SELECT yoao_learning_v1.selection_resume_study(:study, :run)")
_RECIPE = text(
    "SELECT * FROM yoao_learning_v1.selection_recipe(:project, :recipe, :lock)"
)
_RECIPES = text(
    "SELECT * FROM yoao_learning_v1.selection_recipes(:project, :archived, :limit, :offset)"
)
_RECIPE_COUNT = text(
    "SELECT yoao_learning_v1.selection_recipe_count(:project, :archived)"
)
_REVISION = text(
    "SELECT * FROM yoao_learning_v1.selection_recipe_revision(:recipe, :revision, :head)"
)
_REVISION_REQUEST = text(
    "SELECT * FROM yoao_learning_v1.selection_recipe_request(:project, :request)"
)
_REVISIONS = text(
    "SELECT * FROM yoao_learning_v1.selection_recipe_revisions(:recipe, :limit, :offset)"
)
_REVISION_COUNT = text(
    "SELECT yoao_learning_v1.selection_recipe_revision_count(:recipe)"
)
_APPEND = _statement(
    "SELECT * FROM yoao_learning_v1.selection_append_recipe(:project, :recipe, :expected, :actor, :name, :description, "
    ":snapshot, :digest, :origin, :initialization, :request, :request_digest)",
    "snapshot",
    "origin",
)
_ARCHIVE = text(
    "SELECT * FROM yoao_learning_v1.selection_archive_recipe(:project, :recipe, :head)"
)

recipe_lineage_row = table(
    "recipe_lineage",
    column("id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    column("created_at", DateTime(timezone=True)),
    column("archived", Boolean),
    schema="yoao_learning_v1",
)
recipe_revision_lineage_row = table(
    "recipe_revision_lineage",
    column("id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    column("created_at", DateTime(timezone=True)),
    column("recipe_id", UUID(as_uuid=True)),
    column("name", String),
    column("ordinal", Integer),
    column("snapshot_digest", String),
    column("initialization_model_id", UUID(as_uuid=True)),
    column("origin_kind", String),
    column("origin_run_id", String),
    column("origin_attempt_id", String),
    column("origin_recipe_id", String),
    column("origin_revision_id", String),
    column("origin_digest", String),
    column("initialization_kind", String),
    column("initialization_project_id", String),
    column("initialization_model_version_id", String),
    column("initialization_run_id", String),
    column("initialization_attempt_id", String),
    column("initialization_source_digest", String),
    column("initialization_policy_digest", String),
    schema="yoao_learning_v1",
)


async def get_policy(db, project_id, task, *, lock=False):
    return await _one(
        db, _POLICY, project=project_id, task=task, policy=None, lock=lock
    )


async def policy_by_id(db, policy_id, *, lock=False):
    return await _one(db, _POLICY, project=None, task=None, policy=policy_id, lock=lock)


async def save_policy(db, policy, revision):
    return await _one(
        db,
        _SAVE_POLICY,
        project=policy.project_id,
        task=policy.task,
        revision=revision,
        actor=policy.creator_id,
        enabled=policy.enabled,
        initialization=policy.initialization_model_id,
        config=policy.config,
        authorization=policy.authorization,
        checked=policy.last_checked_at,
        next_check=policy.next_check_at,
        message=policy.message,
    )


async def disable_policy(db, policy, message):
    return await _one(
        db,
        _DISABLE_POLICY,
        policy=policy.id,
        revision=policy.revision,
        checked=policy.last_checked_at,
        message=message,
    )


async def delete_policy(db, policy):
    return await _scalar(db, _DELETE_POLICY, policy=policy.id, revision=policy.revision)


async def due_policies(db, at):
    return (await _execute(db, _DUE_POLICIES, at=at)).scalars().all()


async def policy_check_state(db, policy):
    return await _scalar(
        db,
        _POLICY_STATE,
        policy=policy.id,
        revision=policy.revision,
        enabled=policy.enabled,
        state=policy.state,
        message=policy.message,
        checked=policy.last_checked_at,
        next_check=policy.next_check_at,
        started=policy.last_started_at,
        snapshot=policy.last_snapshot_digest,
        blocked=policy.blocked_train_digest,
    )


async def advance_baseline(db, policy, trigger, dataset, baseline):
    return await _scalar(
        db,
        _BASELINE,
        policy=policy.id,
        revision=policy.revision,
        trigger=trigger.id,
        dataset=dataset.id,
        baseline=baseline,
    )


async def get_trigger(db, trigger_id):
    return await _one(db, _TRIGGER, trigger=trigger_id, policy=None, mode="read")


async def lock_active_trigger(db, policy_id):
    return await _one(db, _TRIGGER, trigger=None, policy=policy_id, mode="active_lock")


async def building_trigger(db, policy_id):
    return await _one(db, _TRIGGER, trigger=None, policy=policy_id, mode="building")


async def other_active_trigger(
    db, project_id, *, excluding_policy=None, excluding_trigger=None
):
    return await _scalar(
        db,
        _OTHER_TRIGGER,
        project=project_id,
        policy=excluding_policy,
        trigger=excluding_trigger,
    )


async def duplicate_trigger(db, policy, trigger, digests):
    return await _scalar(
        db,
        _DUPLICATE,
        policy=policy.id,
        revision=policy.revision,
        trigger=trigger.id,
        digests=list(digests),
    )


async def triggers_page(db, project_id, task, limit, offset):
    return await _many(
        db, _TRIGGER_HISTORY, project=project_id, task=task, limit=limit, offset=offset
    )


async def trigger_count(db, project_id, task):
    return await _scalar(db, _TRIGGER_COUNT, project=project_id, task=task)


async def build_trigger(db, policy, dataset, digest):
    return await _scalar(
        db,
        _BUILD_TRIGGER,
        policy=policy.id,
        revision=policy.revision,
        dataset=dataset.id,
        digest=digest,
    )


async def trigger_progress(db, trigger):
    return await _scalar(
        db,
        _TRIGGER_PROGRESS,
        trigger=trigger.id,
        revision=trigger.policy_revision,
        status=trigger.status,
        message=trigger.message,
        completed=trigger.completed_at,
        candidate=trigger.candidate_digest,
        train=trigger.train_digest,
        validation=trigger.validation_digest,
        changed=trigger.changed_samples,
    )


async def requeue_trigger(db, trigger_id):
    return await _scalar(db, _REQUEUE_TRIGGER, trigger=trigger_id)


async def policy_claim(db, run):
    return await _one(
        db,
        _POLICY_CLAIM,
        trigger=run.auto_trigger_id,
        project=run.project_id,
        dataset=run.dataset_id,
    )


async def dataset_references(db, version_id):
    return (await _execute(db, _DATASET_REFS, version=version_id)).one()


async def get_study(db, project_id, study_id, *, lock=False):
    return await _one(
        db, _STUDY, project=project_id, study=study_id, request=None, lock=lock
    )


async def study_request(db, project_id, request_id):
    return await _one(
        db, _STUDY, project=project_id, study=None, request=request_id, lock=False
    )


async def studies_page(db, project_id, limit, offset):
    return await _many(db, _STUDIES, project=project_id, limit=limit, offset=offset)


async def study_count(db, project_id):
    return await _scalar(db, _STUDY_COUNT, project=project_id)


async def due_studies(db):
    return (await _execute(db, _DUE_STUDIES)).scalars().all()


async def study_claim(db, run):
    return await _one(
        db,
        _STUDY_CLAIM,
        study=run.study_id,
        project=run.project_id,
        dataset=run.dataset_id,
    )


async def create_study(db, study):
    return await _one(
        db,
        _CREATE_STUDY,
        study=study.id,
        project=study.project_id,
        actor=study.creator_id,
        dataset=study.dataset_id,
        initialization=study.initialization_model_id,
        request=study.request_id,
        request_digest=study.request_digest,
        name=study.name,
        plan=study.plan,
        plan_digest=study.plan_digest,
        origin=study.origin,
        authorization=study.authorization,
    )


async def next_trial(db, study_id):
    return await _one(db, _TRIALS, study=study_id, pending=True)


async def trials(db, study_id):
    return await _many(db, _TRIALS, study=study_id, pending=False)


async def bind_trial(db, study_id, trial_id, run_id):
    return await _scalar(db, _BIND_TRIAL, study=study_id, trial=trial_id, run=run_id)


async def study_progress(db, study):
    return await _scalar(
        db,
        _STUDY_PROGRESS,
        study=study.id,
        scheduling=study.scheduling,
        status=study.status,
        message=study.message,
        checked=study.last_checked_at,
        completed=study.completed_at,
    )


async def resume_study(db, study_id, run_id):
    return await _scalar(db, _STUDY_RESUME, study=study_id, run=run_id)


async def get_recipe(db, project_id, recipe_id, *, lock=False):
    return await _one(db, _RECIPE, project=project_id, recipe=recipe_id, lock=lock)


async def recipes_page(db, project_id, archived, limit, offset):
    return await _many(
        db, _RECIPES, project=project_id, archived=archived, limit=limit, offset=offset
    )


async def recipe_count(db, project_id, archived):
    return await _scalar(db, _RECIPE_COUNT, project=project_id, archived=archived)


async def get_revision(db, recipe_id, revision_id, head):
    return await _one(db, _REVISION, recipe=recipe_id, revision=revision_id, head=head)


async def revision_request(db, project_id, request_id):
    return await _one(db, _REVISION_REQUEST, project=project_id, request=request_id)


async def revisions_page(db, recipe_id, limit, offset):
    return await _many(db, _REVISIONS, recipe=recipe_id, limit=limit, offset=offset)


async def revision_count(db, recipe_id):
    return await _scalar(db, _REVISION_COUNT, recipe=recipe_id)


async def append_recipe(
    db,
    project_id,
    recipe_id,
    actor_id,
    body,
    value,
    digest,
    origin,
    initialization,
    request_digest,
):
    return await _one(
        db,
        _APPEND,
        project=project_id,
        recipe=recipe_id,
        expected=body.expected_head_revision,
        actor=actor_id,
        name=body.name,
        description=body.description,
        snapshot=value,
        digest=digest,
        origin=origin,
        initialization=initialization,
        request=body.request_id,
        request_digest=request_digest,
    )


async def archive_recipe(db, project_id, recipe_id, expected_head):
    return await _one(
        db, _ARCHIVE, project=project_id, recipe=recipe_id, head=expected_head
    )
