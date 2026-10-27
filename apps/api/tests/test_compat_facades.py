"""Compatibility and retirement guards for service-domain APIs.

``FACADE_SPECS`` records any compatibility facade that remains. Each facade must
preserve public symbol identity and explicit import behavior. ``REMOVED_MODULE_SPECS``
records retired modules, canonical implementation modules, and real consumers; cold
subprocesses verify those replacements still import without the retired module loaded.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

_PROJECT_ROOT_PYTHONPATH = "."  # tests run from apps/api; app/ is on path


@dataclass(frozen=True)
class ExportGroup:
    """Frozen public symbols whose objects are defined by one implementation module."""

    module: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class FacadeSpec:
    """One legacy facade and its independently frozen re-export contract."""

    facade_module: str
    public_module: str
    exports: tuple[ExportGroup, ...]
    consumer_modules: tuple[str, ...]

    @property
    def implementation_modules(self) -> tuple[str, ...]:
        return tuple(group.module for group in self.exports)

    @property
    def expected_names(self) -> tuple[str, ...]:
        return tuple(name for group in self.exports for name in group.names)


def _exports(module: str, *names: str) -> ExportGroup:
    return ExportGroup(module=module, names=tuple(names))


FACADE_SPECS: tuple[FacadeSpec, ...] = ()

# Modules physically removed. Each entry records the old facade path,
# the replacement modules (positive controls), and the frozen symbols that must
# NO LONGER be importable from the old path. The negative-import tests below
# verify every removed module is truly gone.
REMOVED_MODULE_SPECS: tuple[FacadeSpec, ...] = (
    FacadeSpec(
        facade_module="app.services.data_manager",
        public_module="app.services.data_management.service",
        exports=(
            _exports(
                "app.services.data_management.service",
                "DataManagerService",
            ),
            _exports(
                "app.services.data_management.schema",
                "build_data_manager_schema",
            ),
            _exports(
                "app.services.data_management.task_metrics",
                "LOW_CONFIDENCE_THRESHOLD",
                "low_confidence_pending_prediction_shapes_expr",
                "pending_prediction_shapes_expr",
                "pending_tracker_jobs_expr",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.task_views",
        public_module="app.services.data_management.views",
        exports=(
            _exports(
                "app.services.data_management.task_filters",
                "apply_task_visibility",
                "compile_filter",
                "visible_tasks_stmt",
            ),
            _exports(
                "app.services.data_management.views",
                "DEFAULT_COLUMNS",
                "TaskViewService",
                "apply_sort",
                "builtin_views",
                "compile_annotation_match_filter",
                "invalid_filter_fields",
                "validate_columns",
                "validate_filter",
                "validate_sort",
            ),
            _exports(
                "app.services.data_management.schema",
                "builtin_view_keys",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.data_manager_cursor",
        public_module="app.services.data_management.cursor",
        exports=(
            _exports(
                "app.services.data_management.cursor",
                "decode_cursor",
                "encode_cursor",
                "keyset_after",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.data_manager_entities",
        public_module="app.services.data_management.entities",
        exports=(
            _exports(
                "app.services.data_management.entities",
                "COMPACT_TRACK_TYPES",
                "DataManagerObjectService",
                "object_from_row",
                "task_dataset_item_id_expr",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.data_manager_entity_filter",
        public_module="app.services.data_management.entity_filters",
        exports=(
            _exports(
                "app.services.data_management.entity_filters",
                "builtin_entity_views",
                "compile_entity_filter",
                "count_entity_filters",
                "invalid_entity_filter_fields",
                "validate_entity_view",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.data_manager_tracks",
        public_module="app.services.data_management.tracks",
        exports=(
            _exports(
                "app.services.data_management.tracks",
                "DataManagerTrackService",
            ),
        ),
        consumer_modules=("app.api.v1.data_manager", "app.api.v1.task_views"),
    ),
    FacadeSpec(
        facade_module="app.services.video_tracker_adapters",
        public_module="app.services.video_tracking.adapters",
        exports=(
            _exports(
                "app.services.video_tracking.adapters",
                "MLBackendVideoTrackerAdapter",
                "MockBboxTrackerAdapter",
                "TrackerAdapter",
                "TrackerContext",
                "get_tracker_adapter",
                "registered_tracker_models",
            ),
        ),
        consumer_modules=(
            "app.api.v1.tasks.video",
            "app.api.v1.video_tracker_jobs",
            "app.workers.video_tracker",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.video_tracker_job_service",
        public_module="app.services.video_tracking.jobs",
        exports=(
            _exports(
                "app.services.video_tracking.jobs",
                "accept_tracker_job",
                "cancel_tracker_job",
                "create_tracker_job",
                "discard_tracker_job",
                "get_tracker_job",
                "list_active_tracker_jobs",
                "list_reviewable_tracker_jobs",
                "tracker_job_out",
            ),
        ),
        consumer_modules=(
            "app.api.v1.tasks.video",
            "app.api.v1.video_tracker_jobs",
            "app.workers.video_tracker",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.video_tracker_runner",
        public_module="app.services.video_tracking.runner",
        exports=(
            _exports(
                "app.services.video_tracking.runner",
                "COMBO_DISCOVERY_WINDOW_FRAMES",
                "MAX_TRACKER_STAGED_BYTES",
                "TrackerEventPublisher",
                "TrackerJobStateConflict",
                "accept_tracker_job",
                "apply_tracker_results",
                "discard_tracker_job",
                "publish_tracker_event",
                "run_tracker_job",
            ),
        ),
        consumer_modules=(
            "app.api.v1.tasks.video",
            "app.api.v1.video_tracker_jobs",
            "app.workers.video_tracker",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export",
        public_module="app.services.exporting.service",
        exports=(
            _exports(
                "app.services.exporting.service",
                "ExportService",
                "UnsupportedExportError",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export_packaging",
        public_module="app.services.exporting.packaging",
        exports=(
            _exports(
                "app.services.exporting.packaging",
                "ALL_EXPORT_TARGETS",
                "IMAGE_EXPORT_TARGETS",
                "LIDAR_EXPORT_TARGETS",
                "PRESIGN_EXPIRES_SECONDS",
                "VIDEO_EXPORT_FORMATS",
                "YOLO_TARGETS",
                "build_export_zip",
                "clean_export_targets",
                "relative_path_from_file_path",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export_cache",
        public_module="app.services.exporting.cache",
        exports=(
            _exports(
                "app.services.exporting.cache",
                "compute_cache_key",
                "lookup",
                "record",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export_video",
        public_module="app.services.exporting.video",
        exports=(
            _exports(
                "app.services.exporting.video",
                "VIDEO_SINGLE_FRAME_GEOMETRY_TYPES",
                "VIDEO_TRACK_GEOMETRY_TYPES",
                "build_coco_frames_seg",
                "build_kitti_labels",
                "build_mot_gt",
                "build_mot_seqinfo",
                "build_yolo_frame_det_labels",
                "build_yolo_frame_seg_labels",
                "effective_fps",
                "points_to_bbox_norm",
                "single_frame_bbox",
                "source_to_grid",
                "track_grid_rows",
                "yolo_seg_line",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export_lidar",
        public_module="app.services.exporting.lidar",
        exports=(
            _exports(
                "app.services.exporting.lidar",
                "BoxExportAttrs",
                "LidarFrameExportCtx",
                "build_kitti_lidar_label_lines",
                "build_nuscenes_frame_records",
                "build_pointmask_label_bytes",
                "category_map_json",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.export_davis",
        public_module="app.services.exporting.davis",
        exports=(
            _exports(
                "app.services.exporting.davis",
                "DAVIS_MAX_OBJECTS",
                "build_davis_palette_png",
                "davis_palette",
                "derive_davis_object_ids",
            ),
        ),
        consumer_modules=(
            "app.api.v1.batches",
            "app.api.v1.projects",
            "app.workers.export",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_arbiter_store",
        public_module="aap_inference.gpu.ledger",
        exports=(
            _exports(
                "aap_inference.gpu.ledger.keys",
                "GPUArbiterKeys",
                "gpu_arbiter_keys",
            ),
            _exports(
                "aap_inference.gpu.ledger.store",
                "GPUArbiterStore",
            ),
            _exports(
                "aap_inference.gpu.ledger.types",
                "GPU_COLD_ADMISSION_OPERATION",
                "GPU_EVICTION_OPERATION",
                "GPUAdmissionResult",
                "GPUAllocation",
                "GPUAllocationState",
                "GPUArbiterStoreError",
                "GPUBackendDomainEvolutionResult",
                "GPUBackendDomainMember",
                "GPUBackendMembershipState",
                "GPUCardSnapshot",
                "GPUEvictionBranchResult",
                "GPUIdleEvictionResult",
                "GPULeaseMutationResult",
                "GPUProofResetCAS",
                "GPUProofResetContext",
                "GPUQueueResult",
                "GPUQueueTicket",
                "GPUReconcileLeaseCleanup",
                "GPUReconcileResult",
                "GPURequestLease",
                "GPURequestLeaseState",
                "GPUTombstoneGCReceipt",
                "GPUTombstoneGCResult",
                "GPUTransitionOwnerResult",
                "GPUTransitionResult",
            ),
            _exports(
                "aap_inference.gpu.ledger.validation",
                "normalize_gpu_backend_max_concurrency",
            ),
        ),
        consumer_modules=(
            "app.api.v1.admin_ml_integrations",
            "app.workers.ml_health",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_admission_signer",
        public_module="aap_inference.gpu.signing",
        exports=(
            _exports(
                "aap_inference.gpu.signing",
                "GPUAdmissionSignerConfigError",
                "GPUAdmissionTokenSigner",
            ),
        ),
        consumer_modules=(
            "aap_inference.gpu.dispatch",
            "aap_inference.gpu.membership_activation",
            "aap_inference.gpu.rollout_control",
            "scripts.validate_gpu_arbitration",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_arbiter_rollout",
        public_module="aap_inference.gpu.rollout_state",
        exports=(
            _exports(
                "aap_inference.gpu.rollout_state",
                "GPUArbiterRolloutConflict",
                "GPUArbiterRolloutDecision",
                "GPUArbiterRolloutSnapshot",
                "GPUArbiterRolloutUnavailable",
                "begin_gpu_arbiter_rollout",
                "block_gpu_arbiter_rollout",
                "classify_gpu_arbiter_rollout",
                "complete_gpu_arbiter_rollout",
                "gpu_arbiter_rollout_snapshot",
                "gpu_rollout_boundary_active",
                "read_gpu_arbiter_rollout",
                "read_gpu_arbiter_rollouts",
                "resolve_gpu_arbiter_rollout",
            ),
        ),
        consumer_modules=(
            "app.api.v1.admin_ml_integrations",
            "aap_inference.model_execution",
            "app.workers.ml_health",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_collector_database",
        public_module="aap_inference.gpu.collector_database",
        exports=(
            _exports(
                "aap_inference.gpu.collector_database",
                "GPUCollectorDatabase",
                "GPUCollectorDatabaseConfigError",
                "load_gpu_collector_database_url",
                "open_gpu_collector_database",
                "validate_gpu_collector_role_boundary",
            ),
        ),
        consumer_modules=("app.workers.ml_health",),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_dispatch_authority",
        public_module="aap_inference.gpu.dispatch",
        exports=(
            _exports(
                "aap_inference.gpu.dispatch",
                "build_gpu_dispatch_context_factory",
            ),
        ),
        consumer_modules=(
            "app.deps",
            "app.workers.frame_preannotate",
            "app.workers.predictions_retry",
            "app.workers.tasks",
            "app.workers.video_tracker",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_membership_activation",
        public_module="aap_inference.gpu.membership_activation",
        exports=(
            _exports(
                "aap_inference.gpu.membership_activation",
                "GPUMembershipPromotionResult",
                "promote_gpu_backend_membership",
                "promote_gpu_resource_memberships",
            ),
        ),
        consumer_modules=("app.workers.ml_health",),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_rollout_control",
        public_module="aap_inference.gpu.rollout_control",
        exports=(
            _exports(
                "aap_inference.gpu.rollout_control",
                "GPURolloutControlResult",
                "advance_gpu_backend_rollout_control",
                "advance_gpu_resource_rollout_control",
            ),
        ),
        consumer_modules=("app.workers.ml_health",),
    ),
    FacadeSpec(
        facade_module="app.services.gpu_arbiter",
        public_module="app.services.gpu_arbiter",
        exports=(
            _exports(
                "aap_inference.gpu.contracts",
                "GPUArbiterDispatchError",
                "GPUArbiterErrorCode",
                "GPUDispatchContextFactory",
                "GPUDispatchGrant",
                "GPUDispatchOperation",
                "GPUDispatchOutcome",
                "GPUDispatchOutcomeChannel",
                "GPUDispatchOutcomeKind",
                "GPUDispatchRequest",
                "GPUDispatchUncertainReason",
                "GPUShadowSessionFactory",
                "gpu_arbiter_failure_record",
                "summarize_gpu_arbiter_failures",
            ),
            _exports(
                "aap_inference.gpu.policy",
                "GPUClaimConfigurationError",
                "GPUShadowCandidate",
                "GPUShadowDecision",
                "any_gpu_resource_effectively_enforced",
                "backend_is_trusted_explicit_cpu",
                "effective_gpu_arbiter_mode",
                "evaluate_gpu_shadow_decision",
                "gpu_shadow_observation_enabled",
                "record_gpu_shadow_dispatch",
                "strict_gpu_loaded_evidence",
                "validate_gpu_claim",
            ),
            _exports(
                "aap_inference.gpu.fences",
                "GPUFenceCounter",
                "GPUFenceExhaustedError",
                "GPUFenceMembershipError",
                "GPUFenceSessionFactory",
                "GPUReadinessDemoter",
                "activate_gpu_backend_membership",
                "advance_gpu_backend_fence",
                "read_gpu_backend_fence",
                "record_gpu_backend_token_expiry",
            ),
            _exports(
                "aap_inference.gpu.proofs",
                "GPUBusyEvictionRuntimeSubjectError",
                "GPUColdRuntimeSubject",
                "GPUColdRuntimeSubjectError",
                "GPUColdTerminalCommitResult",
                "GPUEvictionCancelRuntimeSubjectError",
                "GPUEvictionCommitResult",
                "GPUEvictionDrainHealth",
                "GPUIdleEvictionRuntimeSubject",
                "GPUIdleEvictionRuntimeSubjectError",
                "GPUPreparedColdRuntimeSubject",
                "GPUPreparedEvictionCancelRuntimeSubject",
                "GPUPreparedIdleEvictionRuntimeSubject",
                "GPUResidentRuntimeSubject",
                "GPUResidentRuntimeSubjectError",
                "GPU_LEGACY_MODE_TOKEN_TTL_SECONDS",
                "commit_gpu_cold_terminal_from_health",
                "commit_gpu_eviction_cancel_from_health",
                "commit_gpu_eviction_phase_from_health",
                "prepare_gpu_cold_runtime_generation",
                "prepare_gpu_eviction_cancel_runtime_generation",
                "prepare_gpu_idle_eviction_runtime_generation",
                "read_gpu_busy_eviction_runtime_subject",
                "read_gpu_cold_runtime_subject",
                "read_gpu_eviction_cancel_runtime_subject",
                "read_gpu_eviction_drain_health",
                "read_gpu_idle_eviction_runtime_subject",
                "read_gpu_resident_runtime_subject",
                "record_gpu_resident_runtime_token_expiry",
            ),
            _exports(
                "aap_inference.gpu.control_preparation",
                "GPULegacyAckBlockedError",
                "GPULegacyAckPreparation",
                "GPURolloutControlBlockedError",
                "GPURolloutControlPreparation",
                "prepare_gpu_backend_legacy_ack",
                "prepare_gpu_backend_rollout_control",
            ),
            _exports(
                "aap_inference.gpu.reconciliation",
                "GPUResourceRepairResult",
                "GPUResourceRuntimeObservation",
                "commit_gpu_proof_reset_from_health",
                "disabled_gpu_resource_runtime_observation",
                "observe_gpu_resource_runtime",
                "repair_gpu_resource",
            ),
            _exports(
                "aap_inference.gpu.retirement",
                "GPURetiredLiveProof",
                "GPURetiredProbeResult",
                "GPUTombstoneCollectionResult",
                "collect_gpu_backend_tombstone",
                "probe_retired_gpu_membership",
            ),
            _exports(
                "aap_inference.gpu.diagnostics",
                "build_backend_gpu_config_status",
                "build_resource_summaries",
                "claimed_budget_by_resource",
                "record_unregistered_gpu_shadow_dispatch",
                "unregistered_gpu_loading_blocked",
            ),
        ),
        consumer_modules=(
            "app.api.v1.admin_ml_integrations",
            "app.api.v1.ml_backends",
            "app.api.v1.tasks.annotations",
            "app.deps",
            "app.services.ml_backend",
            "app.services.secondary_inference",
            "app.services.video_tracking.adapters",
            "app.services.video_tracking.runner",
            "app.workers.ml_health",
            "scripts.validate_gpu_arbitration",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.project_delete",
        public_module="app.application.project_deletion",
        exports=(_exports("app.application.project_deletion", "delete_project"),),
        consumer_modules=(
            "app.api.v1.projects",
            "scripts.seed_pointcloud",
            "scripts.seed_screenshot_profile",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.scene",
        public_module="app.services.assets_spatial",
        exports=(
            _exports(
                "app.services.assets_spatial",
                "AssetsSpatialGateway",
                "AssetsSpatialGatewayError",
            ),
            _exports(
                "app.services.assets_spatial_views",
                "spatial_item_references",
                "spatial_scene_references",
            ),
            _exports(
                "app.services.labeling_scene",
                "TaskSceneFrame",
                "SceneTimelineTask",
                "SceneTimelineWindow",
                "SceneFrameIndexInconsistent",
                "SceneTimelineAnnotationSummary",
                "resolve_primary_item_id",
                "resolve_task_scene_frames",
                "get_scene_frame_task_map",
                "get_scene_timeline_window",
                "get_scene_timeline_annotation_summaries",
                "get_neighbors_for_task",
            ),
        ),
        consumer_modules=(
            "app.api.v1.scenes",
            "app.api.v1.tasks.scene_timeline",
            "app.services.data_management.views",
            "app.services.batch",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.scene_pose",
        public_module="app.services.assets_spatial",
        exports=(
            _exports("app.services.assets_spatial", "AssetsSpatialGateway"),
            _exports("aap_assets_catalog.spatial_values", "FramePose"),
        ),
        consumer_modules=(
            "app.api.v1.scenes",
            "app.services.assets_point_cloud_manifest",
            "scripts.backfill_frame_poses",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.scene_inference",
        public_module="app.services.assets_spatial",
        exports=(
            _exports("app.services.assets_spatial", "AssetsSpatialGateway"),
            _exports("aap_assets_catalog.spatial_workflow", "SpatialWorkflow"),
            _exports("aap_assets_source.scene_layout", "plan_scenes"),
        ),
        consumer_modules=(
            "app.api.v1.datasets",
            "app.services.pointcloud_import",
            "scripts.backfill_scenes",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.sensor_calibration",
        public_module="app.services.assets_spatial",
        exports=(
            _exports("app.services.assets_spatial", "AssetsSpatialGateway"),
            _exports(
                "aap_assets_catalog.spatial_values",
                "SensorCalibration",
                "calibration_digest",
            ),
            _exports(
                "aap_assets_catalog.spatial_contract", "normalize_calibration_record"
            ),
        ),
        consumer_modules=(
            "app.api.v1.tasks.multicamera_annotations",
            "app.services.multicamera_annotation",
            "app.services.point_cloud_quality.service",
        ),
    ),
    FacadeSpec(
        facade_module="app.services.axis_sniffer",
        public_module="app.services.assets_spatial",
        exports=(
            _exports("app.services.assets_spatial", "AssetsSpatialGateway"),
            _exports(
                "aap_assets_catalog.axis_sniff",
                "AxisObservation",
                "AxisSniffResult",
                "sniff_axis_convention",
            ),
            _exports("aap_assets_catalog.spatial_values", "SensorCalibration"),
        ),
        consumer_modules=("app.api.v1.datasets", "app.services.assets_spatial"),
    ),
)

_EXPECTED_FACADES: set[str] = set()

# Every module in REMOVED_MODULE_SPECS has been physically deleted.
# FACADE_SPECS is empty; the positive facade tests are inert. The
# REMOVED_MODULE_SPECS negative tests below are the permanent guard.


def _public_names(module: object) -> tuple[str, ...]:
    names = getattr(module, "__all__", None)
    assert isinstance(names, (list, tuple)), "facade must declare __all__"
    assert names, "facade __all__ must not be empty"
    assert all(isinstance(name, str) and name for name in names)
    assert len(names) == len(set(names)), "facade __all__ contains duplicates"
    return tuple(names)


def _child_env() -> dict[str, str]:
    child_env = os.environ.copy()
    child_env.update(
        {
            "PYTHONPATH": _PROJECT_ROOT_PYTHONPATH,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return child_env


def test_all_compatibility_facades_are_registered() -> None:
    """The data-driven suite must not silently omit a landed legacy facade."""
    assert {spec.facade_module for spec in FACADE_SPECS} == _EXPECTED_FACADES
    all_names = [name for spec in FACADE_SPECS for name in spec.expected_names]
    assert len(all_names) == 0  # FACADE_SPECS empty; remaining retired modules deleted
    for spec in FACADE_SPECS:
        assert len(spec.expected_names) == len(set(spec.expected_names)), (
            f"frozen manifest duplicates a name for {spec.facade_module}"
        )


@pytest.mark.parametrize("spec", FACADE_SPECS, ids=lambda s: s.facade_module)
def test_facade_identity(spec: FacadeSpec) -> None:
    import importlib

    facade = importlib.import_module(spec.facade_module)
    public = importlib.import_module(spec.public_module)
    actual_names = _public_names(facade)
    assert set(actual_names) == set(spec.expected_names), (
        f"{spec.facade_module} public contract drifted: "
        f"expected={sorted(spec.expected_names)!r}, actual={sorted(actual_names)!r}"
    )

    for group in spec.exports:
        source = importlib.import_module(group.module)
        for name in group.names:
            old_obj = getattr(facade, name, _Missing)
            public_obj = getattr(public, name, _Missing)
            source_obj = getattr(source, name, _Missing)
            assert old_obj is not _Missing, (
                f"{spec.facade_module}.{name} missing from facade"
            )
            assert public_obj is not _Missing, (
                f"{spec.public_module}.{name} missing from public module"
            )
            assert source_obj is not _Missing, f"{group.module}.{name} missing"
            assert old_obj is public_obj is source_obj, (
                f"{spec.facade_module}.{name} is not the exact object from "
                f"{group.module}.{name}"
            )


class _Missing:
    """Sentinel for missing attributes."""


def test_data_manager_schema_cold_direct_import_has_builtin_view_keys() -> None:
    """Direct schema imports must not rely on views.py registration side effects."""
    probe = textwrap.dedent(
        """
        import sys
        import uuid

        from app.db.models.project import Project
        from app.services.data_management.schema import build_data_manager_schema

        assert "app.services.data_management.views" not in sys.modules
        project = Project(
            id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            display_id="P-DM-COLD",
            name="cold schema",
            type_label="Video tracking",
            type_key="video-track",
            data_type="video",
            scene_mode=False,
            tool_bindings={
                "bbox": {
                    "enabled": True,
                    "classes": ["car"],
                    "attribute_schema": {
                        "fields": [
                            {"key": "color", "type": "select", "required": True}
                        ]
                    },
                }
            },
        )
        schema = build_data_manager_schema(project)
        assert {
            "all",
            "pending",
            "review",
            "feedback-open",
            "ai-review",
            "missing-required-attributes",
            "tracker-review",
            "with-tracks",
        } <= set(schema.builtin_views)
        assert "app.services.data_management.views" not in sys.modules
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT_PYTHONPATH,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        "cold direct schema import failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.parametrize("spec", FACADE_SPECS, ids=lambda s: s.facade_module)
def test_facade_cold_import_both_orders(spec: FacadeSpec) -> None:
    """Import in old→new and new→old order in fresh processes; identity must hold."""
    exports_literal = repr(tuple((group.module, group.names) for group in spec.exports))
    names_literal = repr(spec.expected_names)
    probe = textwrap.dedent(
        f"""
        import importlib
        facade = importlib.import_module("{spec.facade_module}")
        public = importlib.import_module("{spec.public_module}")
        assert set(facade.__all__) == set({names_literal})
        sources = {{module: importlib.import_module(module) for module, _ in {exports_literal}}}
        for module, names in {exports_literal}:
            source = sources[module]
            for name in names:
                assert getattr(facade, name) is getattr(public, name)
                assert getattr(facade, name) is getattr(source, name), name
        """
    )
    probe_rev = textwrap.dedent(
        f"""
        import importlib
        sources = {{module: importlib.import_module(module) for module, _ in {exports_literal}}}
        public = importlib.import_module("{spec.public_module}")
        facade = importlib.import_module("{spec.facade_module}")
        assert set(facade.__all__) == set({names_literal})
        for module, names in {exports_literal}:
            source = sources[module]
            for name in names:
                assert getattr(facade, name) is getattr(public, name)
                assert getattr(facade, name) is getattr(source, name), name
        """
    )
    for label, code in (("old_first", probe), ("new_first", probe_rev)):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=_PROJECT_ROOT_PYTHONPATH,
            capture_output=True,
            text=True,
            env=_child_env(),
        )
        assert result.returncode == 0, (
            f"cold import ({label}) failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


_CONSUMER_MODULES = tuple(
    sorted({module for spec in FACADE_SPECS for module in spec.consumer_modules})
)


@pytest.mark.parametrize("consumer", _CONSUMER_MODULES)
def test_consumer_cold_import_without_facade_preload(consumer: str) -> None:
    """Each real consumer must import first in a process with no facade preloaded."""
    probe = textwrap.dedent(
        f"""
        import importlib
        import sys

        legacy = {tuple(sorted(_EXPECTED_FACADES))!r}
        assert not any(module in sys.modules for module in legacy)
        importlib.import_module({consumer!r})
        loaded = sorted(module for module in legacy if module in sys.modules)
        assert not loaded, f"consumer loaded compatibility facade(s): {{loaded}}"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT_PYTHONPATH,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"cold consumer import failed for {consumer}:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.parametrize("spec", FACADE_SPECS, ids=lambda s: s.facade_module)
def test_facade_no_import_star(spec: FacadeSpec) -> None:
    """Facade source is limited to explicit aliases and one ``__all__`` contract."""
    parts = spec.facade_module.split(".")
    path = Path(*parts).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported_names: set[str] = set()
    offenders: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                offenders.append("uses import star")
                continue
            if node.module == "__future__":
                continue
            if node.level != 0 or node.module not in spec.implementation_modules:
                offenders.append(f"imports unexpected module {node.module!r}")
                continue
            imported_names.update(alias.asname or alias.name for alias in node.names)
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple))
            and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.value.elts
            )
        ):
            continue
        offenders.append(f"contains forbidden {type(node).__name__}")

    facade = __import__(spec.facade_module, fromlist=["*"])
    public_names = set(_public_names(facade))
    # A facade may import private (underscore-prefixed) symbols for test-patch
    # convenience that are not part of the public __all__ contract.  Only the
    # public imported names must match __all__ exactly.
    public_imported = {name for name in imported_names if not name.startswith("_")}
    if public_imported != public_names:
        offenders.append(
            f"public explicit imports {sorted(public_imported)!r} do not match "
            f"__all__ {sorted(public_names)!r}"
        )
    assert not offenders, f"{spec.facade_module}: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# Negative guards for physically removed modules.
# ---------------------------------------------------------------------------

_REMOVED_FACADES = {spec.facade_module for spec in REMOVED_MODULE_SPECS}


@pytest.mark.parametrize("spec", REMOVED_MODULE_SPECS, ids=lambda s: s.facade_module)
def test_removed_module_not_importable(spec: FacadeSpec) -> None:
    """Every removed module must fail to import in all forms."""
    import importlib.util

    old = spec.facade_module
    assert importlib.util.find_spec(old) is None, f"{old} still has a module spec"

    # Direct import must fail
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(old)

    # from app.services import <name> must not expose the attribute
    if old.startswith("app.services."):
        name = old.rsplit(".", 1)[-1]
        import app.services as pkg  # noqa: PLC0415

        assert not hasattr(pkg, name), (
            f"app.services.{name} still accessible as package attribute"
        )

    # A frozen symbol must not be importable from the old path
    first_symbol = spec.expected_names[0]
    with pytest.raises(ModuleNotFoundError):
        __import__(old, fromlist=[first_symbol])


@pytest.mark.parametrize("spec", REMOVED_MODULE_SPECS, ids=lambda s: s.facade_module)
def test_removed_module_cold_import_fails(spec: FacadeSpec) -> None:
    """A cold subprocess confirms the module is gone without test-order side effects."""
    old = spec.facade_module
    probe = textwrap.dedent(
        f"""
        import importlib, importlib.util, sys
        # The old module must not be importable.
        assert importlib.util.find_spec("{old}") is None
        try:
            importlib.import_module("{old}")
            raise SystemExit("import should have failed")
        except ModuleNotFoundError:
            pass
        # Positive control: the replacement modules must still import.
        for module in {[g.module for g in spec.exports]}:
            importlib.import_module(module)
        # Positive control: a real consumer must still import.
        for consumer in {list(spec.consumer_modules)}:
            importlib.import_module(consumer)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT_PYTHONPATH,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"cold import probe for removed module {old} failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_removed_modules_count_matches_manifest() -> None:
    """The removed-module manifest count must match the fixture JSON."""
    import json

    manifest_path = Path(__file__).parent / "_fixtures" / "removed_service_modules.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_old_paths = {m["facade_module"] for m in manifest["modules"]}
    # Every removed spec must be in the manifest; the manifest may have more
    # entries (modules not yet deleted).
    assert _REMOVED_FACADES.issubset(manifest_old_paths), (
        f"removed specs not in manifest: {_REMOVED_FACADES - manifest_old_paths}"
    )
