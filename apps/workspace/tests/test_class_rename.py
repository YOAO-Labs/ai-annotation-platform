from __future__ import annotations

from copy import deepcopy

import pytest

from aap_workspace.class_rename import rename_class_bindings
from aap_workspace.configuration_policy import ConfigurationPolicyError


def test_selected_unit_renames_only_matching_classes_and_preserves_extras():
    bindings = {
        "bbox": {
            "enabled": True,
            "future_binding_field": {"keep": True},
            "classes": [
                {"name": "Car", "alias": "vehicle", "future_class_field": 7},
                {"name": "Truck", "color": "blue"},
            ],
        },
        "disabled": {
            "enabled": False,
            "classes": [{"name": "Car", "alias_to": "automobile"}],
        },
    }
    original = deepcopy(bindings)

    result = rename_class_bindings(
        bindings, old_name="Car", new_name="Vehicle", tool_unit_id="bbox"
    )

    assert result["bbox"] == {
        "enabled": True,
        "future_binding_field": {"keep": True},
        "classes": [
            {
                "name": "Vehicle",
                "alias": "vehicle",
                "future_class_field": 7,
            },
            {"name": "Truck", "color": "blue"},
        ],
    }
    assert result["disabled"] == original["disabled"]
    assert bindings == original


def test_omitted_unit_visits_all_stored_units_in_order_including_disabled():
    bindings = {
        "region": {
            "enabled": True,
            "classes": [{"name": "Car", "order": 1}],
        },
        "disabled": {
            "enabled": False,
            "classes": [{"name": "Car", "alias_to": "car"}],
        },
        "legacy": None,
        "malformed": {"enabled": True, "classes": [None, "Car"]},
    }

    result = rename_class_bindings(
        bindings, old_name="Car", new_name="Vehicle", tool_unit_id=None
    )

    assert list(result) == list(bindings)
    assert result["region"]["classes"] == [{"name": "Vehicle", "order": 1}]
    assert result["disabled"]["classes"] == [{"name": "Vehicle", "alias_to": "car"}]
    assert result["legacy"] is None
    assert result["malformed"] == bindings["malformed"]
    assert bindings["region"]["classes"][0]["name"] == "Car"


def test_unknown_selected_unit_precedes_class_lookup():
    with pytest.raises(ConfigurationPolicyError) as exc_info:
        rename_class_bindings(
            {"bbox": {"classes": []}},
            old_name="Missing",
            new_name="Vehicle",
            tool_unit_id="unknown",
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "tool_unit_id 'unknown' 不存在"


def test_missing_old_class_keeps_the_legacy_not_found_error():
    with pytest.raises(ConfigurationPolicyError) as exc_info:
        rename_class_bindings(
            {"bbox": {"classes": [{"name": "Truck"}]}},
            old_name="Car",
            new_name="Vehicle",
            tool_unit_id="bbox",
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "类别 'Car' 不存在"


def test_conflict_in_selected_unit_keeps_exact_legacy_error_and_input_unchanged():
    bindings = {
        "bbox": {
            "classes": [
                {"name": "Car", "alias": "vehicle"},
                {"name": "Vehicle", "color": "red"},
            ]
        }
    }
    original = deepcopy(bindings)

    with pytest.raises(ConfigurationPolicyError) as exc_info:
        rename_class_bindings(
            bindings, old_name="Car", new_name="Vehicle", tool_unit_id="bbox"
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "类别 'Vehicle' 在工具单位 'bbox' 已存在"
    assert bindings == original


def test_all_unit_conflict_order_is_preserved_without_partial_mutation():
    bindings = {
        "first": {"classes": [{"name": "Car", "alias": "one"}]},
        "second": {
            "classes": [
                {"name": "Car", "alias": "two"},
                {"name": "Vehicle"},
            ]
        },
        "third": {"classes": [{"name": "Car", "alias": "three"}]},
    }
    original = deepcopy(bindings)

    with pytest.raises(ConfigurationPolicyError) as exc_info:
        rename_class_bindings(
            bindings, old_name="Car", new_name="Vehicle", tool_unit_id=None
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "类别 'Vehicle' 在工具单位 'second' 已存在"
    assert bindings == original


def test_names_are_used_verbatim_and_equal_names_are_not_a_noop_here():
    bindings = {"bbox": {"classes": [{"name": "Car"}]}}
    with pytest.raises(ConfigurationPolicyError) as missing:
        rename_class_bindings(
            bindings, old_name="Car ", new_name="Vehicle", tool_unit_id="bbox"
        )
    assert missing.value.status_code == 404
    assert missing.value.detail == "类别 'Car ' 不存在"

    with pytest.raises(ConfigurationPolicyError) as conflict:
        rename_class_bindings(
            bindings, old_name="Car", new_name="Car", tool_unit_id="bbox"
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail == "类别 'Car' 在工具单位 'bbox' 已存在"
