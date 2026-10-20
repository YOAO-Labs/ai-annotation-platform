from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aap_workspace.configuration_requests import (
    ProjectCreate,
    ProjectTemplateCreate,
    ProjectTemplateOut,
    ProjectTemplateUpdate,
    ProjectUpdate,
)
from aap_workspace.configuration_values import (
    AttributeField,
    AttributeSchema,
    ClassConfigEntry,
    ProjectRenderingConfig,
    ToolBinding,
    VideoCollaborationConfig,
    VideoModesConfig,
    VideoSamplingConfig,
)
from aap_workspace.mask_configuration_values import (
    MASK_QC_RULE_CODES,
    MaskQCConfig,
)
from aap_workspace.point_quality_configuration_values import (
    POINT_CLOUD_QUALITY_RULE_CODES,
    PointCloudQualityConfig,
)


def test_project_request_models_preserve_defaults_unset_and_null_rules():
    created = ProjectCreate(name="demo", type_label="Detection")
    assert created.classes == []
    assert created.ai_interactive_enabled is True
    assert created.raster_mask_native_editing_enabled is True
    assert created.video_collaboration is None

    patch = ProjectUpdate.model_validate({"name": "renamed"})
    assert patch.model_fields_set == {"name"}
    assert "video_sampling" not in patch.model_fields_set
    assert patch.model_dump(exclude_unset=True) == {"name": "renamed"}

    with pytest.raises(ValidationError, match="video_collaboration cannot be null"):
        ProjectCreate(name="demo", type_label="Detection", video_collaboration=None)
    with pytest.raises(ValidationError, match="mask_qc_config cannot be null"):
        ProjectUpdate(mask_qc_config=None)
    with pytest.raises(
        ValidationError, match="point_cloud_quality_config cannot be null"
    ):
        ProjectUpdate(point_cloud_quality_config=None)
    with pytest.raises(ValidationError, match="type_label 不能为 null"):
        ProjectCreate.model_validate(
            {
                "name": "demo",
                "type_label": None,
                "source_project_id": "00000000-0000-0000-0000-000000000001",
            }
        )


def test_template_requests_preserve_missing_source_and_explicit_null_semantics():
    from_source = ProjectTemplateCreate.model_validate(
        {"source_project_id": "00000000-0000-0000-0000-000000000001"}
    )
    assert from_source.name is None
    assert from_source.type_label is None
    assert from_source.type_key is None

    with pytest.raises(ValidationError, match="name 必填"):
        ProjectTemplateCreate.model_validate({})
    with pytest.raises(ValidationError, match="name 不能为 null"):
        ProjectTemplateCreate.model_validate(
            {
                "name": None,
                "type_label": "Detection",
                "type_key": "image-det",
                "source_project_id": "00000000-0000-0000-0000-000000000001",
            }
        )

    update = ProjectTemplateUpdate.model_validate({"annotation_guide": None})
    assert update.model_fields_set == {"annotation_guide"}
    assert update.model_dump(exclude_unset=True) == {"annotation_guide": None}


def test_template_output_derives_legacy_flat_class_views():
    now = datetime.now(timezone.utc)
    output = ProjectTemplateOut.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "display_id": "TPL-1",
            "name": "Template",
            "type_label": "Detection",
            "type_key": "image-det",
            "created_by": "00000000-0000-0000-0000-000000000002",
            "created_at": now,
            "updated_at": now,
            "tool_bindings": {
                "bbox": {
                    "enabled": True,
                    "classes": [{"name": "cat", "color": "#abcdef", "order": 0}],
                    "attribute_schema": {"fields": []},
                }
            },
        }
    )
    assert output.classes == ["cat"]
    assert output.classes_config["cat"].color == "#abcdef"


def test_tool_configuration_validation_and_normalization():
    assert ClassConfigEntry(alias=" Person, , Worker ").alias == "person,worker"
    assert VideoModesConfig().model_dump() == {"box": True, "track": True}
    with pytest.raises(ValidationError, match="至少保留单帧 / 轨迹之一可用"):
        VideoModesConfig(box=False, track=False)

    with pytest.raises(ValidationError, match="hotkey 仅支持 boolean / select"):
        AttributeSchema(
            fields=[AttributeField(key="note", label="Note", type="text", hotkey="1")]
        )
    with pytest.raises(
        ValidationError, match=r"tool_binding.classes\[\]\.name 在工具单位内重复"
    ):
        ToolBinding.model_validate({"classes": [{"name": "cat"}, {"name": "cat"}]})
    with pytest.raises(
        ValidationError, match="box3dDefaultSize entries must be positive"
    ):
        ProjectRenderingConfig(box3dDefaultSize=(1, 0, 1))


def test_video_sampling_validation_is_unchanged():
    assert VideoSamplingConfig().model_dump() == {
        "mode": "none",
        "target_fps": None,
        "frame_step": None,
    }
    assert VideoSamplingConfig(mode="fps", target_fps=10).target_fps == 10
    assert VideoSamplingConfig(mode="step", frame_step=5).frame_step == 5
    with pytest.raises(ValidationError, match="mode=fps 必须提供 target_fps"):
        VideoSamplingConfig(mode="fps")
    with pytest.raises(
        ValidationError, match="mode=none 时 target_fps / frame_step 应为 None"
    ):
        VideoSamplingConfig(mode="none", target_fps=10)
    with pytest.raises(
        ValidationError, match="启用视频协同时 overlap_frames 必须大于 0"
    ):
        VideoCollaborationConfig(enabled=True)


def test_mask_qc_configuration_defaults_and_rule_allowlist():
    config = MaskQCConfig()
    assert config.schema_version == 1
    assert config.config_revision == 1
    assert config.enabled is True
    assert len(MASK_QC_RULE_CODES) == 12
    assert config.single_frame.near_empty_pixels == 16
    assert config.temporal.sample_step == 1
    with pytest.raises(ValidationError, match="unknown Mask QC rule code: made_up"):
        MaskQCConfig(severity_overrides={"made_up": "warning"})


def test_point_quality_configuration_upgrades_sorts_and_validates():
    config = PointCloudQualityConfig.model_validate(
        {
            "schema_version": 1,
            "enabled_rules": ["track_gap", "low_point_count", "track_gap"],
            "severity_overrides": {"track_gap": "warning", "low_point_count": "off"},
            "class_thresholds": {"vehicle": {"minimum_points": 2}},
        }
    )
    assert config.schema_version == 2
    assert config.enabled_rules == ["low_point_count", "track_gap"]
    assert list(config.severity_overrides) == ["low_point_count", "track_gap"]
    assert config.class_thresholds["vehicle"].minimum_points == 2
    assert len(POINT_CLOUD_QUALITY_RULE_CODES) == 8
    with pytest.raises(
        ValidationError, match="unknown point cloud quality rule: z_rule"
    ):
        PointCloudQualityConfig(enabled_rules=["z_rule"])
    with pytest.raises(
        ValidationError, match="duplicate point cloud quality class: vehicle"
    ):
        PointCloudQualityConfig(class_thresholds={"vehicle": {}, " vehicle ": {}})
