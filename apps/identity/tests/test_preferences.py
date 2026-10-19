from __future__ import annotations

import copy
import re

import pytest

from aap_identity.preferences import (
    PreferencePolicyError,
    PreferenceValidationError,
    patch_preferences,
    read_preferences,
)


def _workspace_envelope():
    panels = ["canvas", "task-queue", "class-palette", "inspector", "discussion"]
    return {
        "schemaVersion": 1,
        "snapshot": {
            "layout": {
                "grid": {
                    "root": {
                        "type": "branch",
                        "data": [
                            {
                                "type": "leaf",
                                "data": {"id": "canvas", "views": ["canvas"]},
                            },
                            {
                                "type": "leaf",
                                "data": {"id": "right", "views": panels[1:]},
                            },
                        ],
                    },
                    "width": 1440,
                    "height": 900,
                    "orientation": "HORIZONTAL",
                },
                "panels": {
                    panel: {
                        "id": panel,
                        "contentComponent": "workbench-panel",
                        "renderer": "always",
                    }
                    for panel in panels
                },
                "activeGroup": "canvas",
            },
            "returns": {},
        },
    }


def _workspace_patch(context: str, envelope: dict) -> dict:
    return {
        "workbench": {
            "layout": {
                "workspace": {
                    "engine": "dockview@8",
                    "contexts": {context: envelope},
                }
            }
        }
    }


def _preset(name: str) -> dict:
    return {**_workspace_envelope(), "name": name, "context": "annotate:image"}


def _preset_patch(presets: dict, revision: str = "0") -> dict:
    return {
        "namedPresetsRevision": revision,
        "workbench": {
            "layout": {"workspace": {"engine": "dockview@8", "namedPresets": presets}}
        },
    }


def test_read_preferences_defaults_and_preserves_opaque_subtrees():
    opaque_workspace = {"schemaVersion": 99, "snapshot": {"future": True}}
    opaque_shortcuts = {"schemaVersion": 8, "future": {"binding": "opaque"}}

    response = read_preferences(
        {
            "workbench": {
                "layout": {
                    "leftWidth": 480,
                    "rightWidth": 600,
                    "workspace": opaque_workspace,
                },
                "shortcuts": opaque_shortcuts,
            }
        }
    )

    assert response["namedPresetsRevision"] == "0"
    assert response["workbench"]["layout"]["workspace"] == opaque_workspace
    assert "leftWidth" not in response["workbench"]["layout"]
    assert "rightWidth" not in response["workbench"]["layout"]
    assert response["workbench"]["shortcuts"] == opaque_shortcuts


def test_sparse_patch_deep_merges_but_replaces_atomic_maps():
    existing = {
        "workbench": {
            "image": {"smoothImage": False, "controlPointsSize": 9},
            "layout": {
                "cameraPanels": {"front": {"x": 2}, "rear": {"y": 4}},
            },
        },
        "ai": {"model_by_backend": {"backend-a": "model-a", "backend-b": "model-b"}},
    }

    merged, response = patch_preferences(
        existing,
        {
            "workbench": {
                "image": {"cssImageFilter": "contrast(1.1)"},
                "layout": {"cameraPanels": {"front": {"collapsed": True}}},
            },
            "ai": {"model_by_backend": {"backend-a": "model-new"}},
        },
    )

    assert merged["workbench"]["image"] == {
        "smoothImage": False,
        "controlPointsSize": 9,
        "cssImageFilter": "contrast(1.1)",
    }
    assert merged["workbench"]["layout"]["cameraPanels"] == {
        "front": {"collapsed": True}
    }
    assert merged["ai"]["model_by_backend"] == {
        "backend-a": "model-new",
        "backend-b": "model-b",
    }
    assert response["workbench"]["image"]["smoothImage"] is False
    assert response["workbench"]["image"]["controlPointsSize"] == 9
    assert response["workbench"]["image"]["cssImageFilter"] == "contrast(1.1)"


def test_workspace_context_entries_replace_atomically_and_merge_by_key():
    first = _workspace_envelope()
    second = copy.deepcopy(first)
    second["snapshot"]["layout"]["grid"]["width"] = 1280
    existing, _ = patch_preferences({}, _workspace_patch("annotate:image", first))

    merged, _ = patch_preferences(existing, _workspace_patch("review:video", second))

    contexts = merged["workbench"]["layout"]["workspace"]["contexts"]
    assert contexts == {"annotate:image": first, "review:video": second}

    replacement = copy.deepcopy(first)
    replacement["snapshot"]["layout"]["grid"]["width"] = 1024
    merged, _ = patch_preferences(
        merged, _workspace_patch("annotate:image", replacement)
    )
    contexts = merged["workbench"]["layout"]["workspace"]["contexts"]
    assert contexts == {"annotate:image": replacement, "review:video": second}


def test_named_presets_compare_and_swap_and_advance_revision():
    existing = {}
    merged, response = patch_preferences(
        existing, _preset_patch({"first": _preset("First")})
    )
    revision = response["namedPresetsRevision"]

    assert re.fullmatch(r"[0-9a-f]{32}", revision)
    assert merged["namedPresetsRevision"] == revision
    assert merged["workbench"]["layout"]["workspace"]["namedPresets"] == {
        "first": _preset("First")
    }

    with pytest.raises(PreferencePolicyError) as caught:
        patch_preferences(
            merged,
            _preset_patch({"stale": _preset("Stale")}, revision="0"),
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "named_presets_conflict"
    assert caught.value.detail["currentRevision"] == revision
    assert merged["namedPresetsRevision"] == revision


def test_invalid_patch_returns_safe_locations_without_raw_input():
    with pytest.raises(PreferenceValidationError) as caught:
        patch_preferences({}, {"workbench": {"image": {"unknownField": "private"}}})

    assert caught.value.errors
    assert caught.value.errors[0]["loc"][-1] == "unknownField"
    assert "input" not in caught.value.errors[0]
    assert "private" not in repr(caught.value)


def test_workspace_schema_downgrade_is_a_policy_conflict():
    existing, _ = patch_preferences(
        {}, _workspace_patch("annotate:image", _workspace_envelope())
    )
    older = copy.deepcopy(_workspace_envelope())
    older["schemaVersion"] = 1
    existing["workbench"]["layout"]["workspace"]["contexts"]["annotate:image"][
        "schemaVersion"
    ] = 3

    with pytest.raises(PreferencePolicyError) as caught:
        patch_preferences(existing, _workspace_patch("annotate:image", older))

    assert caught.value.status_code == 409
    assert caught.value.detail == "layout_schema_downgrade"
