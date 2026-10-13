"""Single source of truth for screenshot seed logical resources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID


SEED_REVISION = "screenshots-2026-08-g"
SEED_MANAGED_BY = "screenshot-seed"
# Persona keys are logical roles consumed by the screenshot seeder; the second
# element is the platform account role, which is employee for project staff so
# they can hold explicit project duties (migration 0174 cut over the old
# platform annotator/reviewer values).
USER_SPECS = {
    "admin": ("admin", "super_admin"),
    "project_admin": ("pm", "project_admin"),
    "annotator": ("anno", "employee"),
    "reviewer": ("qa", "employee"),
}


@dataclass(frozen=True)
class RecordingAnchorSpec:
    """Reviewed, normalized media coordinates consumed by deterministic recordings."""

    key: str
    label: str
    bbox: tuple[float, float, float, float]
    point: tuple[float, float]
    additional_points: tuple[tuple[float, float], ...] = ()
    frame_index: int | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    polyline: tuple[tuple[float, float], ...] = ()
    brush_strokes: tuple[tuple[tuple[float, float], ...], ...] = ()
    positive_stroke: tuple[tuple[float, float], ...] = ()
    negative_stroke: tuple[tuple[float, float], ...] = ()
    negative_point: tuple[float, float] | None = None
    provenance: str = "verified-label-derived"


@dataclass(frozen=True)
class TaskSpec:
    key: str
    file_path: str
    status: str = "pending"
    batch_key: str | None = None
    assignee_key: str | None = None
    reviewer_key: str | None = None
    annotation: bool = False
    recording_anchors: tuple[RecordingAnchorSpec, ...] = ()


@dataclass(frozen=True)
class BatchSpec:
    key: str
    display_id: str
    name: str
    status: str
    annotator_key: str | None = None
    reviewer_key: str | None = None


@dataclass(frozen=True)
class BackendRequirement:
    key: str
    description: str
    required_prompts: tuple[str, ...] = ()
    required_tasks: tuple[str, ...] = ()
    required_inputs: tuple[str, ...] = ()
    required_geometries: tuple[str, ...] = ()
    required_output_attributes: tuple[str, ...] = ()
    tracker_priority: tuple[str, ...] = ()
    interactive: bool = False


@dataclass(frozen=True)
class ProjectSpec:
    display_id: str
    dataset_display_id: str
    data_type: str
    storage_prefix: str
    tasks: tuple[TaskSpec, ...]
    media_paths: tuple[str, ...] = ()
    required_backend: str | None = None
    default_pipeline_model_id: str | None = None
    batches: tuple[BatchSpec, ...] = ()
    require_members: bool = False
    axis_convention: str | None = None


def resolve_project_spec(
    spec: ProjectSpec, dataset_id: UUID, metadata: dict | None
) -> ProjectSpec:
    """Resolve nuScenes logical paths within the importing dataset's namespace."""
    if spec.storage_prefix != "nuscenes-mini/":
        return spec
    source = (metadata or {}).get("source")
    root = source.get("storage_root") if isinstance(source, dict) else None
    if root is None or root == "nuscenes-mini":
        return spec
    from app.services.storage import TRUSTED_NUSCENES_PREFIX

    if root != f"{TRUSTED_NUSCENES_PREFIX}-{dataset_id.hex}":
        raise ValueError("nuScenes seed storage_root does not match its dataset UUID")
    prefix = f"{root}/"
    return replace(
        spec,
        storage_prefix=prefix,
        tasks=tuple(
            replace(
                task,
                file_path=prefix + task.file_path.removeprefix(spec.storage_prefix),
            )
            for task in spec.tasks
        ),
        media_paths=tuple(
            prefix + file_path.removeprefix(spec.storage_prefix)
            for file_path in spec.media_paths
        ),
    )


BACKEND_REQUIREMENTS = {
    "image_interactive": BackendRequirement(
        key="image_interactive",
        description="point + interactive_box + exemplar image interaction",
        required_prompts=("point", "interactive_box", "exemplar"),
        required_tasks=("interactive_seg",),
        required_geometries=("polygon",),
        interactive=True,
    ),
    "video_tracker": BackendRequirement(
        key="video_tracker",
        description="interactive video tracking",
        required_tasks=("tracker",),
        tracker_priority=("sam3_video_interactive", "sam2_video"),
    ),
    "ocr": BackendRequirement(
        key="ocr",
        description="full-image OCR with text attributes",
        required_tasks=("ocr",),
        required_inputs=("full_image",),
        required_geometries=("polygon",),
        required_output_attributes=("text",),
    ),
}


def parse_backend_requirements(value: str | None) -> frozenset[str]:
    """Keep legacy full validation; an explicit 'none' selects manual capture."""
    if value is None:
        return frozenset(BACKEND_REQUIREMENTS)
    if value == "none":
        return frozenset()
    keys = frozenset(value.split(","))
    unknown = keys - BACKEND_REQUIREMENTS.keys()
    if unknown:
        raise ValueError(f"unknown screenshot backend requirements: {sorted(unknown)}")
    return keys


IMAGE_BATCHES = (
    BatchSpec("draft", "B-SS-DRAFT", "截图 · 待分派", "draft"),
    BatchSpec("active", "B-SS-ACTIVE", "截图 · 待预标", "active"),
    BatchSpec(
        "annotating",
        "B-SS-ANNOTATING",
        "截图 · 标注中",
        "annotating",
        annotator_key="annotator",
        reviewer_key="reviewer",
    ),
    BatchSpec(
        "review",
        "B-SS-REVIEW",
        "截图 · 待审核",
        "reviewing",
        annotator_key="annotator",
        reviewer_key="reviewer",
    ),
    BatchSpec(
        "completed",
        "B-SS-COMPLETE",
        "截图 · 已完成",
        "approved",
        annotator_key="annotator",
        reviewer_key="reviewer",
    ),
)


PROJECT_SPECS = {
    "image_demo": ProjectSpec(
        display_id="P-COCO8",
        dataset_display_id="DS-COCO8",
        data_type="image",
        storage_prefix="coco8-dev/",
        tasks=(
            TaskSpec(
                "clean",
                "coco8-dev/train/screenshot_01.jpg",
                batch_key="draft",
                recording_anchors=(
                    RecordingAnchorSpec(
                        key="primary_truck",
                        label="truck",
                        bbox=(0.851625, 0.304, 1.0, 0.569333),
                        point=(0.925812, 0.436667),
                    ),
                ),
            ),
            TaskSpec(
                "predicted",
                "coco8-dev/train/screenshot_02.jpg",
                batch_key="draft",
            ),
            TaskSpec(
                "annotating",
                "coco8-dev/train/screenshot_03.jpg",
                status="in_progress",
                batch_key="annotating",
                assignee_key="annotator",
                recording_anchors=(
                    RecordingAnchorSpec(
                        key="primary_vehicle",
                        label="car",
                        bbox=(0.42, 0.48, 0.56, 0.75),
                        point=(0.49, 0.62),
                        polygon=(
                            (0.44, 0.49),
                            (0.515, 0.49),
                            (0.54, 0.55),
                            (0.55, 0.64),
                            (0.535, 0.715),
                            (0.485, 0.74),
                            (0.425, 0.715),
                            (0.4, 0.64),
                            (0.41, 0.55),
                        ),
                        brush_strokes=(
                            ((0.435, 0.545), (0.515, 0.545)),
                            ((0.42, 0.585), (0.53, 0.585)),
                            ((0.415, 0.625), (0.535, 0.625)),
                            ((0.42, 0.665), (0.53, 0.665)),
                            ((0.435, 0.705), (0.515, 0.705)),
                        ),
                        positive_stroke=((0.48, 0.52), (0.49, 0.70)),
                        negative_stroke=((0.46, 0.725), (0.52, 0.725)),
                    ),
                    RecordingAnchorSpec(
                        key="review_vehicle_left",
                        label="car",
                        bbox=(0.07875, 0.48, 0.27, 0.72),
                        point=(0.174375, 0.6),
                        polygon=(
                            (0.12, 0.49),
                            (0.225, 0.49),
                            (0.255, 0.54),
                            (0.27, 0.63),
                            (0.25, 0.705),
                            (0.175, 0.72),
                            (0.095, 0.7),
                            (0.07875, 0.62),
                            (0.09, 0.54),
                        ),
                    ),
                    RecordingAnchorSpec(
                        key="review_vehicle_right",
                        label="car",
                        bbox=(0.655, 0.315, 0.805, 0.525),
                        point=(0.73, 0.42),
                        polygon=(
                            (0.69, 0.33),
                            (0.765, 0.33),
                            (0.792, 0.375),
                            (0.805, 0.45),
                            (0.787, 0.51),
                            (0.735, 0.525),
                            (0.675, 0.505),
                            (0.655, 0.445),
                            (0.665, 0.375),
                        ),
                        provenance="reviewed-media-derived",
                    ),
                    RecordingAnchorSpec(
                        key="lane_marking",
                        label="lane marking",
                        bbox=(0.561, 0.321, 0.706, 0.979),
                        point=(0.626, 0.675),
                        polyline=(
                            (0.561, 0.321),
                            (0.581, 0.454),
                            (0.599, 0.571),
                            (0.626, 0.675),
                            (0.654, 0.774),
                            (0.683, 0.885),
                            (0.706, 0.979),
                        ),
                        provenance="reviewed-media-derived",
                    ),
                ),
            ),
            TaskSpec(
                "submitted",
                "coco8-dev/train/screenshot_04.jpg",
                status="review",
                batch_key="review",
                assignee_key="annotator",
                annotation=True,
            ),
            TaskSpec(
                "review",
                "coco8-dev/val/screenshot_05.jpg",
                status="review",
                batch_key="review",
                assignee_key="annotator",
                reviewer_key="reviewer",
                annotation=True,
            ),
            TaskSpec(
                "completed",
                "coco8-dev/val/screenshot_06.jpg",
                status="completed",
                batch_key="completed",
                assignee_key="annotator",
                reviewer_key="reviewer",
                annotation=True,
            ),
            TaskSpec("spare_1", "coco8-dev/val/screenshot_07.jpg", batch_key="active"),
            TaskSpec("spare_2", "coco8-dev/val/screenshot_08.jpg", batch_key="draft"),
        ),
        required_backend="image_interactive",
        batches=IMAGE_BATCHES,
        require_members=True,
    ),
    "video_demo": ProjectSpec(
        display_id="P-VIDEO-DEV",
        dataset_display_id="DS-VIDEO-DEV",
        data_type="video",
        storage_prefix="tracking-car-dev/",
        tasks=(
            TaskSpec(
                "tracking",
                "tracking-car-dev/tracking_scene.mp4",
                recording_anchors=(
                    RecordingAnchorSpec(
                        key="left_bus_f0",
                        label="bus",
                        frame_index=0,
                        bbox=(0.064, 0.3, 0.322, 0.81),
                        point=(0.19, 0.72),
                        additional_points=((0.19, 0.36),),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="right_bus_f0",
                        label="bus",
                        frame_index=0,
                        bbox=(0.775, 0.306, 0.988, 0.704),
                        point=(0.875, 0.42),
                        additional_points=((0.88, 0.66),),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="front_truck_f0",
                        label="truck",
                        frame_index=0,
                        bbox=(0.49, 0.455, 0.72, 0.82),
                        point=(0.61, 0.74),
                        brush_strokes=(
                            ((0.515, 0.51), (0.66, 0.51)),
                            ((0.505, 0.56), (0.672, 0.56)),
                            ((0.5, 0.61), (0.68, 0.61)),
                            ((0.505, 0.66), (0.675, 0.66)),
                            ((0.515, 0.71), (0.665, 0.71)),
                            ((0.53, 0.76), (0.65, 0.76)),
                        ),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="left_bus_f1",
                        label="bus",
                        frame_index=1,
                        bbox=(0.064, 0.3, 0.323, 0.812),
                        point=(0.196, 0.551),
                        polyline=((0.24, 0.54), (0.26, 0.525)),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="left_bus_f4",
                        label="bus",
                        frame_index=4,
                        bbox=(0.065, 0.3, 0.325, 0.815),
                        point=(0.197, 0.72),
                        additional_points=((0.197, 0.36),),
                        negative_point=(0.345, 0.4),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="front_truck_f4",
                        label="truck",
                        frame_index=4,
                        bbox=(0.492, 0.455, 0.722, 0.825),
                        point=(0.612, 0.742),
                        negative_point=(0.755, 0.755),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="right_bus_f4",
                        label="bus",
                        frame_index=4,
                        bbox=(0.776, 0.305, 0.989, 0.706),
                        point=(0.876, 0.422),
                        additional_points=((0.881, 0.662),),
                        negative_point=(0.748, 0.56),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="front_truck_f5",
                        label="truck",
                        frame_index=5,
                        bbox=(0.459, 0.32, 0.721, 0.832),
                        point=(0.594, 0.597),
                        brush_strokes=(((0.64, 0.53), (0.69, 0.68)),),
                        provenance="reviewed-frame-derived",
                    ),
                    RecordingAnchorSpec(
                        key="front_truck_f8",
                        label="truck",
                        frame_index=8,
                        bbox=(0.46, 0.32, 0.724, 0.835),
                        point=(0.596, 0.6),
                        provenance="reviewed-frame-derived",
                    ),
                ),
            ),
        ),
        required_backend="video_tracker",
    ),
    "pointcloud_demo": ProjectSpec(
        display_id="P-PC-DEV",
        dataset_display_id="DS-PC-DEV",
        data_type="lidar",
        storage_prefix="nuscenes-mini/",
        tasks=(
            TaskSpec(
                "frame_000",
                "nuscenes-mini/scene-0061/lidar/scene-0061_000000.pcd",
                recording_anchors=(
                    RecordingAnchorSpec(
                        key="foreground_object",
                        label="object",
                        bbox=(0.045, 0.245, 0.326, 0.758),
                        point=(0.185, 0.5),
                        provenance="reviewed-nuscenes-front-camera",
                    ),
                ),
            ),
            *(
                TaskSpec(
                    f"frame_{index:03d}",
                    f"nuscenes-mini/scene-0061/lidar/scene-0061_{index:06d}.pcd",
                )
                for index in range(1, 39)
            ),
        ),
        media_paths=(
            *(
                f"nuscenes-mini/scene-0061/lidar/scene-0061_{index:06d}.pcd"
                for index in range(39)
            ),
            *(
                f"nuscenes-mini/scene-0061/camera/{channel}/scene-0061_{index:06d}.jpg"
                for channel in (
                    "CAM_FRONT",
                    "CAM_FRONT_RIGHT",
                    "CAM_BACK_RIGHT",
                    "CAM_BACK",
                    "CAM_BACK_LEFT",
                    "CAM_FRONT_LEFT",
                )
                for index in range(39)
            ),
            *(
                f"nuscenes-mini/scene-0061/calib/camera/{channel}.json"
                for channel in (
                    "CAM_FRONT",
                    "CAM_FRONT_RIGHT",
                    "CAM_BACK_RIGHT",
                    "CAM_BACK",
                    "CAM_BACK_LEFT",
                    "CAM_FRONT_LEFT",
                )
            ),
        ),
        axis_convention="iso_8855",
    ),
    "pointcloud_multicam_demo": ProjectSpec(
        display_id="P-PC-MULTI",
        dataset_display_id="DS-PC-MULTI",
        data_type="lidar",
        storage_prefix="pc-multicam-dev/",
        tasks=(TaskSpec("frame_000", "pc-multicam-dev/lidar/000000.pcd"),),
        media_paths=(
            "pc-multicam-dev/lidar/000000.pcd",
            *(
                f"pc-multicam-dev/camera/{role}/000000.jpg"
                for role in (
                    "front",
                    "front_left",
                    "front_right",
                    "back",
                    "back_left",
                    "back_right",
                )
            ),
            *(
                f"pc-multicam-dev/calib/camera/{role}.json"
                for role in (
                    "front",
                    "front_left",
                    "front_right",
                    "back",
                    "back_left",
                    "back_right",
                )
            ),
        ),
        axis_convention="apollo",
    ),
    "ocr_demo": ProjectSpec(
        display_id="P-OCR",
        dataset_display_id="DS-OCR",
        data_type="image",
        storage_prefix="ocr-dev/",
        tasks=(TaskSpec("ocr", "ocr-dev/ch_en_num.jpg"),),
        required_backend="ocr",
        default_pipeline_model_id="ocr-e2e",
    ),
}
