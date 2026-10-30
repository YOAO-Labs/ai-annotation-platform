from copy import deepcopy
import hashlib

import pytest
from pydantic import ValidationError

from aap_inference_contract.plugin_assets import (
    PluginAssetApprovalUpdate,
    PluginAssetDescriptor,
    plugin_asset_reason,
    resolve_plugin_selection,
)


def example():
    asset = {
        "path": "models/a.bin",
        "sha256": hashlib.sha256(b"asset").hexdigest(),
        "size_bytes": 5,
    }
    descriptor = PluginAssetDescriptor.model_validate(
        {
            "schema_version": 1,
            "plugin_id": "example",
            "plugin_version": "1",
            "artifact": "example/model@sha256:" + "a" * 64,
            "asset_manifest_sha256": "b" * 64,
            "selections": [
                {
                    "model_id": "detect",
                    "variants": {"size": "small"},
                    "asset_free": False,
                    "assets": [asset],
                },
                {
                    "model_id": "detect",
                    "variants": {"size": "large"},
                    "asset_free": False,
                    "assets": [asset],
                },
                {
                    "model_id": "custom",
                    "variants": {},
                    "asset_free": True,
                    "assets": [],
                },
            ],
        }
    )
    update = PluginAssetApprovalUpdate.model_validate(
        {
            "expected_revision": 0,
            "expected_installation_revision": 3,
            "descriptor": descriptor,
            "descriptor_sha256": descriptor.sha256(),
            "approved": [{"model_id": "detect", "variants": {"size": "small"}}],
            "inventory": [asset],
            "container_id": "c" * 64,
            "image_id": "sha256:" + "d" * 64,
        }
    )
    extra = {
        "plugin": {
            "manifest": {
                "plugin_id": "example",
                "version": "1",
                "artifact": descriptor.artifact,
                "asset_manifest_sha256": descriptor.asset_manifest_sha256,
            }
        },
        "plugin_lifecycle": {
            "launch": {"container_id": update.container_id, "image_id": update.image_id}
        },
    }
    return descriptor, update, extra


def test_approval_is_per_exact_selection_and_measured_inventory():
    descriptor, update, extra = example()
    state = {"revision": 1, "decision": update.model_dump(mode="json")}
    assert plugin_asset_reason(extra, state, "detect", {"size": "small"}) is None
    assert (
        plugin_asset_reason(extra, state, "detect", {"size": "large"})
        == "plugin_asset_unapproved"
    )
    assert plugin_asset_reason(extra, state, "custom", {}) == "plugin_asset_unapproved"
    unavailable = deepcopy(state)
    unavailable["decision"]["inventory"] = []
    assert (
        plugin_asset_reason(extra, unavailable, "detect", {"size": "small"})
        == "plugin_asset_unavailable"
    )
    replaced = deepcopy(extra)
    replaced["plugin_lifecycle"]["launch"]["container_id"] = "e" * 64
    assert (
        plugin_asset_reason(replaced, state, "detect", {"size": "small"})
        == "plugin_asset_unavailable"
    )
    assert plugin_asset_reason({}, None, None, {}) is None


def test_invalid_assets_cannot_be_described_or_claimed_as_inventory():
    descriptor, update, _ = example()
    raw = descriptor.model_dump(mode="json")
    raw["selections"][0]["assets"][0]["path"] = "../outside"
    with pytest.raises(ValidationError):
        PluginAssetDescriptor.model_validate(raw)
    raw = descriptor.model_dump(mode="json")
    raw["selections"][0]["asset_free"] = True
    with pytest.raises(ValidationError):
        PluginAssetDescriptor.model_validate(raw)
    body = update.model_dump(mode="json")
    body["inventory"][0]["sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        PluginAssetApprovalUpdate.model_validate(body)


def test_default_variant_resolves_to_the_real_advertised_selection():
    health = {
        "capabilities": {
            "models": [
                {
                    "id": "detect",
                    "task": "detect",
                    "supported_variants": [
                        {
                            "key": "size",
                            "variants": [{"value": "small"}, {"value": "large"}],
                        }
                    ],
                    "default_variants": {"size": "small"},
                }
            ]
        }
    }
    assert resolve_plugin_selection(health, {"task": "detect"}) == (
        "detect",
        {"size": "small"},
    )
    assert resolve_plugin_selection(
        health, {"model_id": "detect", "model_variants": {"size": "large"}}
    ) == ("detect", {"size": "large"})
    assert resolve_plugin_selection(
        health, {"model_id": "detect", "model_variants": {"size": "unknown"}}
    ) == (None, {})
