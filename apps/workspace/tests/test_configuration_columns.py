from __future__ import annotations

import math
import uuid
from datetime import date

import pytest

from aap_workspace.configuration_columns import (
    ConfigurationColumnError,
    dirty_config_columns,
    encode_config_columns,
)


def test_project_codec_keeps_typed_scalar_and_json_null_encodings_separate():
    names, values = encode_config_columns(
        "project",
        {
            "annotation_guide": "line one\x00line two",
            "due_date": date(2026, 10, 21),
            "ai_enabled": False,
            "maximum_annotations": 3,
            "box_threshold": 0.35,
            "video_sampling": None,
            "mask_qc_config": {
                "description": "nul\x00inside",
                "threshold": math.nan,
                "positive_infinity": math.inf,
                "negative_infinity": -math.inf,
            },
        },
    )

    assert names == [
        "annotation_guide",
        "due_date",
        "ai_enabled",
        "maximum_annotations",
        "box_threshold",
        "video_sampling",
        "mask_qc_config",
    ]
    assert values[:5] == [
        "line one\x00line two",
        "2026-10-21",
        "false",
        "3",
        "0.35",
    ]
    assert values[5] == "null"
    assert values[6] == (
        '{"description":"nul\\u0000inside","negative_infinity":-Infinity,'
        '"positive_infinity":Infinity,"threshold":NaN}'
    )


def test_template_codec_allows_only_configuration_and_insertion_fields():
    organization_id = uuid.uuid4()
    source_project_id = uuid.uuid4()
    names, values = encode_config_columns(
        "template",
        {
            "name": "Quality",
            "type_label": "检测",
            "type_key": "image-det",
            "description": None,
            "scope": "organization",
            "organization_id": organization_id,
            "source_project_id": source_project_id,
            "label_config": None,
        },
    )

    assert names == [
        "name",
        "type_label",
        "type_key",
        "description",
        "scope",
        "organization_id",
        "source_project_id",
        "label_config",
    ]
    assert values == [
        "Quality",
        "检测",
        "image-det",
        None,
        "organization",
        str(organization_id),
        str(source_project_id),
        "null",
    ]

    for resource_kind, column in (
        ("project", "ml_backend_pool_id"),
        ("project", "owner_id"),
        ("project", "display_id"),
        ("template", "created_by"),
        ("template", "usage_count"),
    ):
        with pytest.raises(ConfigurationColumnError):
            encode_config_columns(resource_kind, {column: uuid.uuid4()})


def test_codec_rejects_wrong_scalar_types_and_noncanonical_dates():
    cases = [
        ("project", {"ai_enabled": 1}),
        ("project", {"maximum_annotations": True}),
        ("project", {"box_threshold": True}),
        ("project", {"due_date": "not-a-date"}),
        ("template", {"organization_id": "not-a-uuid"}),
        ("template", {"name": b"name"}),
    ]
    for resource_kind, payload in cases:
        with pytest.raises(ConfigurationColumnError):
            encode_config_columns(resource_kind, payload)


def test_dirty_columns_use_python_equality_before_storage_casts():
    current = {
        "box_threshold": 0.3499999940395355,
        "ai_enabled": 1,
        "tool_bindings": {"bbox": {"enabled": True, "classes": ["cat"]}},
        "annotation_guide": None,
    }
    payload = {
        "box_threshold": 0.35,
        "ai_enabled": True,
        "tool_bindings": {"bbox": {"classes": ["cat"], "enabled": True}},
        "annotation_guide": None,
    }

    assert dirty_config_columns(current, payload) == ["box_threshold"]
    assert dirty_config_columns(current, {"ai_enabled": 1}) == []
    assert dirty_config_columns({}, {"annotation_guide": None}) == ["annotation_guide"]
    with pytest.raises(ConfigurationColumnError):
        dirty_config_columns({}, {"owner_id": uuid.uuid4()})
