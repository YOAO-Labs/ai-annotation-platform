"""Online learning, training datasets, runs, evaluations, models, and active learning manifest."""

from __future__ import annotations

from datetime import timedelta

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="learning",
    owner_packages=("yoao-training-contract",),
    routers=(
        RouterMount(
            module_path="app.api.v1.training_datasets", tags=("training-datasets",)
        ),
        RouterMount(
            module_path="app.api.v1.project_task_datasets",
            tags=("training-datasets",),
        ),
        RouterMount(
            module_path="app.api.v1.training_dataset_analysis",
            tags=("training-datasets",),
        ),
        RouterMount(module_path="app.api.v1.training_runs", tags=("training-runs",)),
        RouterMount(
            module_path="app.api.v1.training_studies", tags=("training-studies",)
        ),
        RouterMount(
            module_path="app.api.v1.sample_evaluations",
            tags=("sample-evaluations",),
        ),
        RouterMount(
            module_path="app.api.v1.active_learning", tags=("active-learning",)
        ),
        RouterMount(module_path="app.api.v1.training_hub", tags=("training-hub",)),
        RouterMount(
            module_path="app.api.v1.training_runs",
            attr="executor_router",
            tags=("training-executor",),
        ),
        RouterMount(
            module_path="app.api.v1.training_previews", tags=("training-previews",)
        ),
        RouterMount(
            module_path="app.api.v1.training_previews",
            attr="executor_router",
            tags=("training-executor",),
        ),
        RouterMount(
            module_path="app.api.v1.training_observability",
            tags=("training-runs",),
        ),
        RouterMount(
            module_path="app.api.v1.training_observability",
            attr="executor_router",
            tags=("training-executor",),
        ),
        RouterMount(module_path="app.api.v1.trained_models", tags=("trained-models",)),
        RouterMount(module_path="app.api.v1.model_lineage", tags=("trained-models",)),
        RouterMount(
            module_path="app.api.v1.learning_policies", tags=("learning-policies",)
        ),
        RouterMount(
            module_path="app.api.v1.training_recipes", tags=("training-recipes",)
        ),
    ),
    celery_includes=(
        "app.workers.training_dataset",
        "app.workers.sample_evaluations",
        "app.workers.active_learning",
        "app.workers.training_preview_cleanup",
        "app.workers.training_observability_cleanup",
        "app.workers.learning_policy",
        "app.workers.training_study",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="maintain-active-learning-rounds",
            task="app.workers.active_learning.maintain_rounds",
            schedule=CronSpec(minute="*"),
        ),
        BeatScheduleSpec(
            name="maintain-sample-evaluations",
            task="app.workers.sample_evaluations.maintain_evaluations",
            schedule=CronSpec(minute="*"),
        ),
        BeatScheduleSpec(
            name="purge-training-observations",
            task="app.workers.training_observability_cleanup.purge_observations",
            schedule=CronSpec(minute=0),
        ),
        BeatScheduleSpec(
            name="purge-expired-training-previews",
            task="app.workers.training_preview_cleanup.purge_expired_previews",
            schedule=CronSpec(minute="*"),
        ),
        BeatScheduleSpec(
            name="check-learning-policies",
            task="app.workers.learning_policy.check_policies",
            schedule=CronSpec(minute="*"),
        ),
        BeatScheduleSpec(
            name="check-training-studies",
            task="app.workers.training_study.check_studies",
            schedule=timedelta(seconds=10),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "active_learning",
            "learning_policy",
            "model_decision",
            "project_task_dataset",
            "sample_evaluation",
            "trained_model",
            "training_dataset",
            "training_dataset_analysis",
            "training_observability",
            "training_preview",
            "training_recipe",
            "training_run",
            "training_storage_deletion",
            "training_study",
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.training.yml",
            tier="optional_split",
            services=("training-executor",),
        ),
    ),
)
