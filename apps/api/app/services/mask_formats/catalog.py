from __future__ import annotations

from app.schemas.mask_format import MaskFormatCapability
from app.services.mask_formats.adapters import AAPJsonAdapter, LegacyPackagingAdapter
from app.services.mask_formats.contracts import MaskFormatDescriptor
from app.services.mask_formats.image_adapters import (
    IMAGE_IMPORT_PARSERS,
    ImageMaskImportAdapter,
)
from app.services.mask_formats.registry import MaskFormatRegistry
from app.services.mask_formats.video_adapters import (
    VIDEO_IMPORT_PARSERS,
    VideoMaskFormatAdapter,
)


def _capability(
    supported: bool,
    *,
    verified: bool = False,
    enabled_for_ui: bool = False,
) -> MaskFormatCapability:
    return MaskFormatCapability(
        supported=supported,
        verified=verified,
        enabled_for_ui=enabled_for_ui,
    )


_DESCRIPTORS = [
    MaskFormatDescriptor(
        format_id="aap_json",
        label="AAP JSON",
        adapter_version="1.0.0",
        manifest_version="1.3",
        media_types=frozenset({"image", "video", "lidar"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    MaskFormatDescriptor(
        format_id="coco",
        label="COCO Instance",
        adapter_version="2.0.0",
        manifest_version="1",
        media_types=frozenset({"image"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    MaskFormatDescriptor(
        format_id="yolo-seg",
        label="YOLO Segmentation",
        adapter_version="2.0.0",
        manifest_version="1",
        media_types=frozenset({"image"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    MaskFormatDescriptor(
        format_id="label-studio-brush",
        label="Label Studio BrushLabels",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"image"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={
            "data_key": {"type": "string", "default": "image"},
            "from_name": {"type": "string", "default": "brush"},
            "to_name": {"type": "string", "default": "image"},
        },
    ),
    MaskFormatDescriptor(
        format_id="binary-png",
        label="Binary PNG per instance",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"image"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    MaskFormatDescriptor(
        format_id="indexed-png",
        label="Indexed PNG instance map",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"image"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={
            "overlap_policy": {
                "type": "string",
                "enum": ["error", "z_order", "larger_area", "smaller_area"],
                "default": "error",
            }
        },
    ),
    MaskFormatDescriptor(
        format_id="coco-frames-seg",
        label="COCO Frames Segmentation",
        adapter_version="2.0.0",
        manifest_version="1",
        media_types=frozenset({"video"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={"frame_base": {"type": "integer", "enum": [0, 1], "default": 1}},
    ),
    MaskFormatDescriptor(
        format_id="davis",
        label="DAVIS",
        adapter_version="2.0.0",
        manifest_version="1",
        media_types=frozenset({"video"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={
            "overlap_policy": {
                "type": "string",
                "enum": ["error", "z_order", "larger_area", "smaller_area"],
                "default": "error",
            }
        },
    ),
    MaskFormatDescriptor(
        format_id="youtube-vos",
        label="YouTube-VOS",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"video"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={
            "sparse_gap_policy": {
                "type": "string",
                "enum": ["outside_gaps", "nearest_hold"],
                "default": "outside_gaps",
            },
            "overlap_policy": {
                "type": "string",
                "enum": ["error", "z_order", "larger_area", "smaller_area"],
                "default": "error",
            },
        },
    ),
    MaskFormatDescriptor(
        format_id="mots",
        label="MOTS",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"video"}),
        import_capability=_capability(True, verified=True, enabled_for_ui=True),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
        option_schema={"frame_base": {"type": "integer", "enum": [0, 1], "default": 0}},
    ),
    MaskFormatDescriptor(
        format_id="nuscenes",
        label="nuScenes",
        adapter_version="2.0.0",
        manifest_version="2",
        media_types=frozenset({"lidar"}),
        import_capability=_capability(False),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    MaskFormatDescriptor(
        format_id="coco-multicamera",
        label="Multi-camera COCO",
        adapter_version="1.0.0",
        manifest_version="1",
        media_types=frozenset({"lidar"}),
        import_capability=_capability(False),
        export_capability=_capability(True, verified=True, enabled_for_ui=True),
    ),
    *[
        MaskFormatDescriptor(
            format_id=format_id,
            label=label,
            adapter_version="1.0.0",
            manifest_version="1",
            media_types=frozenset(media_types),
            import_capability=_capability(False),
            export_capability=_capability(True, verified=True, enabled_for_ui=True),
        )
        for format_id, label, media_types in (
            ("yolo", "YOLO Detection", {"image"}),
            ("yolo-det", "YOLO Detection", {"image"}),
            ("yolo-obb", "YOLO OBB", {"image"}),
            ("voc", "Pascal VOC", {"image"}),
            ("video_json", "Video JSON", {"video"}),
            ("yolo-frames-det", "YOLO Frames Detection", {"video"}),
            ("yolo-frames-seg", "YOLO Frames Segmentation", {"video"}),
            ("mot", "MOT", {"video"}),
            ("kitti", "KITTI", {"video", "lidar"}),
            ("pointmask", "Point Mask", {"lidar"}),
        )
    ],
]


_IMAGE_FORMAT_IDS = frozenset(
    {"coco", "yolo-seg", "label-studio-brush", "binary-png", "indexed-png"}
)
_VIDEO_FORMAT_IDS = frozenset({"coco-frames-seg", "davis", "youtube-vos", "mots"})

registry = MaskFormatRegistry()
for descriptor in _DESCRIPTORS:
    if descriptor.format_id == "aap_json":
        adapter = AAPJsonAdapter(descriptor)
    elif descriptor.format_id in _IMAGE_FORMAT_IDS:
        adapter = ImageMaskImportAdapter(
            descriptor, IMAGE_IMPORT_PARSERS[descriptor.format_id]
        )
    elif descriptor.format_id in _VIDEO_FORMAT_IDS:
        adapter = VideoMaskFormatAdapter(
            descriptor, VIDEO_IMPORT_PARSERS[descriptor.format_id]
        )
    else:
        adapter = LegacyPackagingAdapter(descriptor)
    registry.register(adapter)
