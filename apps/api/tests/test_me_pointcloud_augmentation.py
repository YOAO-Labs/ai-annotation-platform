"""Source preference compatibility is normalized before partial or whole-tree merge."""

import copy

import pytest
from pydantic import ValidationError

from app.api.v1.me import _deep_merge_preferences, _normalize_pointcloud_augmentation
from app.schemas.user import UserPreferences


@pytest.mark.parametrize(
    "source,overlay", [("none", False), ("neighbors", True), ("vggt_omega", False)]
)
def test_explicit_source_wins_before_merge(source, overlay):
    payload = {
        "workbench": {
            "pointcloud": {
                "augmentationSource": source,
                "neighborPointOverlay": not overlay,
            }
        }
    }
    original = copy.deepcopy(payload)
    normalized = _normalize_pointcloud_augmentation(payload)
    parsed = UserPreferences.model_validate(normalized).model_dump(exclude_unset=True)
    assert parsed["workbench"]["pointcloud"]["neighborPointOverlay"] is overlay
    assert payload == original


@pytest.mark.parametrize("whole_tree", [False, True])
def test_legacy_enable_write_restores_null_without_replacing_other_settings(whole_tree):
    stored = {
        "workbench": {
            "pointcloud": {
                "augmentationSource": "vggt_omega",
                "neighborPointOverlay": False,
                "neighborPointOverlayK": 3,
                "neighborPointCull": "align",
            },
            "image": {"smoothImage": False},
        }
    }
    incoming = {"workbench": {"pointcloud": {"neighborPointOverlay": True}}}
    if whole_tree:
        incoming["workbench"]["image"] = {"controlPointsSize": 10}
    merged = _deep_merge_preferences(
        stored, _normalize_pointcloud_augmentation(incoming)
    )
    assert merged["workbench"]["pointcloud"] == {
        "augmentationSource": None,
        "neighborPointOverlay": True,
        "neighborPointOverlayK": 3,
        "neighborPointCull": "align",
    }
    assert merged["workbench"]["image"]["smoothImage"] is False


def test_reads_do_not_rewrite_legacy_intent_or_accept_unsupported_sources():
    preferences = UserPreferences.model_validate(
        {"workbench": {"pointcloud": {"displayStyle": "surface"}}}
    )
    assert preferences.workbench.pointcloud.augmentationSource is None
    assert preferences.workbench.pointcloud.neighborPointOverlay is False
    for source in ("combined", [], True, 0):
        with pytest.raises(ValidationError):
            UserPreferences.model_validate(
                _normalize_pointcloud_augmentation(
                    {"workbench": {"pointcloud": {"augmentationSource": source}}}
                )
            )


async def test_source_patch_persists_and_legacy_patch_restores_compatibility(
    httpx_client, annotator
):
    _, token = annotator
    url = "/api/v1/auth/me/preferences"
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.patch(
        url,
        json={
            "workbench": {
                "pointcloud": {
                    "augmentationSource": "vggt_omega",
                    "neighborPointOverlay": True,
                    "neighborPointOverlayK": 3,
                }
            }
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["workbench"]["pointcloud"]["neighborPointOverlay"] is False
    response = await httpx_client.get(url, headers=headers)
    assert (
        response.json()["workbench"]["pointcloud"]["augmentationSource"] == "vggt_omega"
    )
    response = await httpx_client.patch(
        url,
        json={"workbench": {"pointcloud": {"neighborPointOverlay": True}}},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["workbench"]["pointcloud"]["augmentationSource"] is None
    assert response.json()["workbench"]["pointcloud"]["neighborPointOverlayK"] == 3
