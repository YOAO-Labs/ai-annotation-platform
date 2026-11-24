"""Inference, model catalog, routing, predictions, and ML plugin installations manifest."""

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
    module_id="inference",
    owner_packages=(
        "yoao-inference",
        "yoao-inference-contract",
        "yoao-lifecycle-contract",
        "yoao-preannotation-results",
        "yoao-protocol-v2",
    ),
    routers=(
        RouterMount(module_path="app.api.v1.model_catalog", tags=("model-catalog",)),
        RouterMount(module_path="app.api.v1.admin_backend_operations"),
        RouterMount(
            module_path="app.api.v1.admin_ml_integrations",
            prefix="/admin/ml-integrations",
            tags=("admin-ml-integrations",),
        ),
        RouterMount(
            module_path="app.api.v1.admin_preannotate",
            tags=("admin-preannotate",),
        ),
        RouterMount(
            module_path="app.api.v1.admin_alias_freq",
            tags=("admin-alias-freq",),
        ),
        RouterMount(
            module_path="app.api.v1.ml_backends",
            prefix="/projects/{project_id}/ml-backends",
            tags=("ml-backends",),
        ),
        RouterMount(
            module_path="app.api.v1.ml_capabilities",
            prefix="/ml-capabilities",
            tags=("ml-capabilities",),
        ),
        RouterMount(module_path="app.api.v1.predictions", tags=("predictions",)),
        RouterMount(
            module_path="app.api.v1.admin_plugin_installations",
            surface="root",
            prefix="/api/v1",
        ),
    ),
    celery_includes=(
        "app.workers.tasks",
        "app.workers.ml_health",
        "app.workers.predictions_retry",
        "app.workers.prediction_input_cleanup",
        "app.workers.prediction_partition",
        "app.workers.frame_preannotate",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="recover-prediction-inputs",
            task="app.workers.prediction_input_cleanup.recover_prediction_inputs",
            schedule=60.0,
        ),
        BeatScheduleSpec(
            name="ensure-future-prediction-partitions",
            task="app.workers.prediction_partition.ensure_future_prediction_partitions",
            schedule=CronSpec(day_of_month=25, hour=3, minute=30),
        ),
        BeatScheduleSpec(
            name="check-ml-backends-health",
            task="app.workers.ml_health.check_ml_backends_health",
            schedule=CronSpec(minute="*"),
            options=(("expires", 55),),
        ),
        BeatScheduleSpec(
            name="repair-gpu-arbiter-resources",
            task="app.workers.ml_health.repair_gpu_arbiter_resources",
            schedule=CronSpec(minute="*"),
            options=(("expires", 55),),
        ),
        BeatScheduleSpec(
            name="publish-ml-backend-stats",
            task="app.workers.ml_health.publish_ml_backend_stats",
            schedule=timedelta(seconds=1),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "gpu_arbiter_rollout",
            "gpu_backend_cancel_intent",
            "gpu_backend_fence",
            "gpu_backend_membership",
            "ml_backend_pool",
            "ml_backend_registry",
            "prediction",
            "prediction_execution",
            "prediction_input",
        ),
        native_managed_tables=(
            ("public", "batch_result_executions"),
            ("public", "batch_result_members"),
            ("public", "batch_result_bundles"),
            ("public", "batch_result_payloads"),
            ("public", "batch_result_parts"),
            ("public", "prediction_retry_attempts"),
            ("public", "prediction_retry_deliveries"),
            ("yoao_inference_routing_v1", "video_execution_scope_permits"),
            ("yoao_inference_backend_endpoint_v1", "endpoint_secret"),
            ("yoao_inference_routing_v1", "video_restore_records"),
            ("yoao_inference_routing_v1", "video_restore_contexts"),
            ("yoao_inference_routing_v1", "video_restore_scopes"),
            ("yoao_inference_routing_v1", "video_restore_attempts"),
            ("yoao_inference_routing_v1", "video_restore_scope_retirements"),
            ("yoao_inference_routing_v1", "video_restore_no_scope_receipts"),
            ("yoao_inference_routing_v1", "video_restore_function_restore"),
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.inference.yml",
            tier="optional_split",
            services=("inference",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.inference-development.yml",
            tier="dev_override",
            overrides=("inference",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.inference-workers-development.yml",
            tier="dev_override",
            overrides=(
                "celery-worker",
                "celery-worker-cpu",
                "celery-worker-gpu",
                "celery-worker-gpu-control",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml.yml",
            tier="ml_plugin",
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml-gsam2.yml",
            tier="ml_plugin",
            services=("grounded-sam2-backend",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml-onnxtools.yml",
            tier="ml_plugin",
            services=("onnxtools-backend",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml-rapidocr.yml",
            tier="ml_plugin",
            services=("rapidocr-backend",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml-sam3.yml",
            tier="ml_plugin",
            services=("sam3-backend",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.ml-yolo.yml",
            tier="ml_plugin",
            services=("yolo-backend",),
        ),
    ),
)
