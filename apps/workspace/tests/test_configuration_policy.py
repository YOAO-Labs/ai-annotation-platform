from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from aap_workspace.configuration_policy import (
    CLONEABLE_PROJECT_FIELDS,
    ConfigurationPolicyError,
    DatasetKind,
    ProjectKind,
    assert_can_create_scope,
    assert_template_creation_scope,
    assert_template_visible,
    can_edit_template,
    coalesce_legacy_into_tool_bindings,
    data_type_from_type_key,
    dump_project_to_template_payload,
    lookup_classes_for_tool_unit,
    media_kind_aliases,
    project_kind,
    dataset_kind,
    kind_matches,
    kind_mismatch_detail,
    merge_from_source,
    merge_template_into_payload,
    prepare_project_create_payload,
    resolve_project_kind,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ID = UUID("00000000-0000-0000-0000-000000000002")
ORG_ID = UUID("00000000-0000-0000-0000-000000000003")


def test_clone_whitelist_preserves_explicit_values_and_deep_copies_json():
    source = {
        "name": "source is not cloneable",
        "tool_bindings": {"bbox": {"classes": [{"name": "cat"}]}},
        "label_config": {"nested": [1]},
        "ai_enabled": True,
        "rendering_config": None,
        "guide_assets": [{"key": "excluded"}],
    }
    payload = {"ai_enabled": False, "label_config": None}
    result = merge_from_source(payload, source)

    assert result is payload
    assert result["ai_enabled"] is False
    assert result["label_config"] is None
    assert result["tool_bindings"] == source["tool_bindings"]
    result["tool_bindings"]["bbox"]["classes"].append({"name": "dog"})
    assert source["tool_bindings"]["bbox"]["classes"] == [{"name": "cat"}]
    assert "name" not in result
    assert "guide_assets" not in result
    assert tuple(CLONEABLE_PROJECT_FIELDS) == (
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "ai_enabled",
        "label_config",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "rendering_config",
    )


def test_legacy_flat_classes_coalesce_with_existing_binding_and_preserve_others():
    previous = {
        "bbox": {
            "enabled": False,
            "classes": [
                {"name": "cat", "color": "#000001", "order": 7, "alias": "kitty"}
            ],
            "attribute_schema": {"fields": [{"key": "old"}]},
        },
        "keypoint": {"enabled": True, "classes": [{"name": "person"}]},
    }
    original = deepcopy(previous)
    payload = {"classes": ["cat", "dog"], "attribute_schema": None}
    coalesce_legacy_into_tool_bindings(payload, previous, "image-det")

    assert payload["tool_bindings"]["bbox"] == {
        "enabled": True,
        "classes": [
            {"name": "cat", "color": "#000001", "order": 7, "alias": "kitty"},
            {"name": "dog", "order": 1},
        ],
        "attribute_schema": {"fields": [{"key": "old"}]},
    }
    assert payload["tool_bindings"]["keypoint"] == previous["keypoint"]
    assert previous == original


def test_empty_bindings_and_config_precedence_match_legacy_coalescing():
    payload = {
        "tool_bindings": {},
        "classes": ["cat"],
        "classes_config": {
            "cat": {"color": "#abcdef", "order": 4, "alias": "CAT, pet"}
        },
    }
    coalesce_legacy_into_tool_bindings(payload, None, "image-seg")
    assert payload["tool_bindings"]["region"]["classes"] == [
        {"name": "cat", "color": "#abcdef", "order": 4, "alias": "CAT, pet"}
    ]

    untouched = {"tool_bindings": {"bbox": {"enabled": True}}}
    coalesce_legacy_into_tool_bindings(untouched, None, "image-det")
    assert untouched["tool_bindings"] == {"bbox": {"enabled": True}}

    empty_classes = {"classes": [], "classes_config": {"fallback": {"order": 8}}}
    coalesce_legacy_into_tool_bindings(empty_classes, None, "image-det")
    assert empty_classes["tool_bindings"]["bbox"]["classes"] == []


def test_project_kind_cross_fill_inheritance_mismatch_and_scene_policy():
    assert data_type_from_type_key("video-mm") == "video"
    assert resolve_project_kind("lidar", None) == ("lidar", "lidar")
    assert resolve_project_kind(None, "video") == ("video-track", "video")
    assert resolve_project_kind(
        None, None, inherited_type_key="video-track", inherited_data_type=None
    ) == ("video-track", "video")
    assert resolve_project_kind(None, None) == ("image-det", "image")

    with pytest.raises(ConfigurationPolicyError) as exc:
        resolve_project_kind("lidar", "video")
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "type_key='lidar' implies data_type='lidar', "
        "got data_type='video' (媒体维度必须一致)"
    )

    payload = {
        "data_type": "video",
        "scene_mode": True,
        "classes": ["car"],
        "prefer_same_scene_continuation": None,
    }
    with pytest.raises(ConfigurationPolicyError) as scene_error:
        prepare_project_create_payload(payload)
    assert scene_error.value.status_code == 422
    assert scene_error.value.detail == "scene_mode 仅支持 image/lidar 项目"

    payload = {"data_type": "lidar", "scene_mode": True, "classes": ["car"]}
    prepare_project_create_payload(payload)
    assert payload["type_key"] == "lidar"
    assert payload["data_type"] == "lidar"
    assert payload["prefer_same_scene_continuation"] is True
    assert "classes" not in payload


def test_project_and_dataset_kind_helpers_share_media_alias_policy():
    assert media_kind_aliases("point_cloud") == ("lidar", "point_cloud", "pointcloud")
    assert media_kind_aliases(None) == ("image",)

    project = project_kind("point_cloud", 1)
    dataset = dataset_kind("lidar", True)
    assert project == ProjectKind(data_type="lidar", scene_mode=True)
    assert dataset == DatasetKind(data_type="lidar", has_scenes=True)
    assert kind_matches(project, dataset)
    assert kind_mismatch_detail(project, dataset) is None

    no_scenes = dataset_kind("lidar", False)
    assert not kind_matches(project, no_scenes)
    assert kind_mismatch_detail(project, no_scenes) == (
        "scene 模式不匹配(scene 项目只能关联 has_scenes=true 的数据集)"
    )
    other_kind = dataset_kind("video", True)
    assert kind_mismatch_detail(project, other_kind) == (
        "data_type 不匹配(项目 lidar / 数据集 video)"
    )


def test_template_dump_and_merge_deepcopy_skip_null_and_respect_caller():
    source_project = {
        "type_label": "Detection",
        "type_key": "image-det",
        "tool_bindings": {"bbox": {"classes": [{"name": "cat"}]}},
        "annotation_guide": "Guide",
        "guide_assets": [{"key": "not cloned"}],
    }
    dumped = dump_project_to_template_payload(source_project)
    assert dumped["annotation_guide"] == "Guide"
    assert "guide_assets" not in dumped
    dumped["tool_bindings"]["bbox"]["classes"].clear()
    assert source_project["tool_bindings"]["bbox"]["classes"] == [{"name": "cat"}]

    template = {
        "type_label": "Template",
        "tool_bindings": {"bbox": {"classes": [{"name": "dog"}]}},
        "sampling": None,
        "annotation_guide": "Template guide",
    }
    payload = {"type_label": "Caller", "sampling": None, "guide_assets": []}
    merge_template_into_payload(template, payload)
    assert payload["type_label"] == "Caller"
    assert payload["sampling"] is None
    assert payload["annotation_guide"] == "Template guide"
    payload["tool_bindings"]["bbox"]["classes"].clear()
    assert template["tool_bindings"]["bbox"]["classes"] == [{"name": "dog"}]


def test_template_visibility_edit_and_create_errors_keep_order_and_scope_rules():
    private_template = {"scope": "private", "created_by": ACTOR_ID}
    assert_template_visible(private_template, {"id": ACTOR_ID, "role": "employee"}, [])
    assert can_edit_template(private_template, {"id": ACTOR_ID, "role": "employee"})
    assert can_edit_template(
        {"scope": "private", "created_by": OTHER_ID},
        {"id": ACTOR_ID, "role": "super_admin"},
    )
    assert_template_visible(
        {"scope": "organization", "organization_id": ORG_ID, "created_by": OTHER_ID},
        {"id": ACTOR_ID, "role": "employee"},
        [ORG_ID],
    )
    assert_template_visible(
        {"scope": "public", "created_by": OTHER_ID},
        {"id": ACTOR_ID, "role": "employee"},
        [],
    )
    assert_template_visible(
        {"scope": "organization", "organization_id": None, "created_by": OTHER_ID},
        {"id": ACTOR_ID, "role": "employee"},
        [],
    )

    with pytest.raises(ConfigurationPolicyError) as hidden:
        assert_template_visible(
            {"scope": "private", "created_by": OTHER_ID},
            {"id": ACTOR_ID, "role": "employee"},
            [],
        )
    assert (hidden.value.status_code, hidden.value.detail) == (404, "模板不存在")

    with pytest.raises(ConfigurationPolicyError) as public_scope:
        assert_template_creation_scope(
            "public", None, {"id": ACTOR_ID, "role": "employee"}
        )
    assert (public_scope.value.status_code, public_scope.value.detail) == (
        403,
        "仅超级管理员可创建公共模板",
    )

    with pytest.raises(ConfigurationPolicyError) as missing_org:
        assert_template_creation_scope(
            "organization", None, {"id": ACTOR_ID, "role": "super_admin"}
        )
    assert (missing_org.value.status_code, missing_org.value.detail) == (
        400,
        "organization scope 必须指定 organization_id",
    )
    assert_can_create_scope("organization", {"id": ACTOR_ID, "role": "employee"})


def test_lookup_classes_only_returns_enabled_tool_classes():
    bindings = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car"}, {"name": ""}, {"label": "ignored"}],
        },
        "region": {"enabled": False, "classes": [{"name": "mask"}]},
    }
    assert lookup_classes_for_tool_unit(bindings, "bbox") == ["car"]
    assert lookup_classes_for_tool_unit(bindings, "region") == []
    assert lookup_classes_for_tool_unit(bindings, "polyline") == []
