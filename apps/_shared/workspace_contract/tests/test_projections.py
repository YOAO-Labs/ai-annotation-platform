from aap_workspace_contract import (
    derive_attribute_keys,
    derive_attribute_schema,
    derive_classes_config,
    derive_classes_list,
    resolve_class_visual,
    sanitize_annotation_attributes,
)


def test_enabled_bindings_and_aliases_project_in_stable_order():
    tool_bindings = {
        "bbox": {
            "enabled": True,
            "classes": [
                {
                    "name": "car",
                    "order": 2,
                    "alias_to": {"tool_unit_id": "region", "class_name": "vehicle"},
                },
                {"name": "person", "order": 1, "color": "#fff"},
            ],
            "attribute_schema": {"fields": [{"key": "occluded"}, {"key": "shared"}]},
        },
        "region": {
            "enabled": False,
            "classes": [{"name": "vehicle", "color": "#abc", "alias": "veh"}],
            "attribute_schema": {"fields": [{"key": "shared"}, {"key": "ignored"}]},
        },
    }

    assert derive_classes_list(tool_bindings) == ["person", "car"]
    assert derive_classes_config(tool_bindings) == {
        "car": {"color": "#abc", "order": 2, "alias": "veh"},
        "person": {"color": "#fff", "order": 1},
    }
    assert resolve_class_visual(tool_bindings, tool_bindings["bbox"]["classes"][0]) == (
        "#abc",
        "veh",
    )
    assert derive_attribute_schema(tool_bindings) == {
        "fields": [{"key": "occluded"}, {"key": "shared"}]
    }
    assert derive_attribute_keys(tool_bindings) == {"occluded", "shared"}
    assert sanitize_annotation_attributes(
        {"occluded": True, "private": "drop"}, {"occluded"}
    ) == {"occluded": True}
