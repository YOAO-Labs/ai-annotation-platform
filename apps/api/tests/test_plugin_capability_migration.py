from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from tests.migration_helpers import load


REVISION = "0401_plugin_capability_inventory.py"


def historical_installation(device="cpu"):
    ident = str(uuid4())
    manifest = {
        "plugin_id": "libreyolo",
        "version": "1.0",
        "publisher": "example",
        "license": "MIT",
        "artifact": "registry.example/plugin@sha256:" + "a" * 64,
        "sdk_compatibility": "1",
        "entrypoint": ["plugin"],
        "capabilities": ["inference", "detection"],
        "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
        "asset_manifest_sha256": "b" * 64,
        "allowed_asset_sources": [],
    }
    resource = {
        "gpu_resource_id": None,
        "physical_gpu_uuid": None,
        "logical_device": "cpu",
    }
    if device == "cuda":
        manifest["resource"] = {
            "device": "cuda",
            "memory_mb": 1024,
            "gpu_memory_mb": 512,
        }
        resource = {
            "gpu_resource_id": "node/GPU-test",
            "physical_gpu_uuid": "GPU-test",
            "logical_device": "cuda:0",
        }
    launch = {
        "installation_id": ident,
        "registry_id": ident,
        "artifact": manifest["artifact"],
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": resource,
    }
    plugin = {
        "installation_id": ident,
        "manifest": manifest,
        "status": "active",
        "revision": 3,
        "resource": resource,
    }
    lifecycle = {
        "launch": launch,
        "stopped": [],
        "qualification": {"report": {"old": True}, "sha256": "e" * 64},
    }
    return {
        "id": ident,
        "extra_params": {"plugin": plugin, "plugin_lifecycle": lifecycle},
    }


def inventory(row):
    old = row["extra_params"]["plugin"]["manifest"]
    payload = json.dumps(
        old, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return {
        "schema": 1,
        "installations": [
            {
                "installation_id": row["id"],
                "registry_id": row["id"],
                "artifact": old["artifact"],
                "image_id": row["extra_params"]["plugin_lifecycle"]["launch"][
                    "image_id"
                ],
                "old_manifest": json.loads(json.dumps(old)),
                "old_manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "asset_manifest_sha256": old["asset_manifest_sha256"],
                "approval_ref": "admin:inspection",
                "models": [
                    {
                        "id": "detect",
                        "task": "detect",
                        "is_interactive": False,
                        "supported_prompts": ["none"],
                        "supported_inputs": ["full_image", "crop"],
                        "supported_geometric_outputs": ["bbox"],
                    }
                ],
                "pools": {"models": {"cap": 1}},
            }
        ],
    }


def test_operator_inventory_file_rejects_duplicate_keys(tmp_path, monkeypatch):
    migration = load(REVISION)
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(inventory(historical_installation())), encoding="utf-8")
    monkeypatch.setattr(
        migration.context,
        "get_x_argument",
        lambda **_: {"plugin_capabilities_inventory": str(path)},
    )
    assert migration._load_inventory()["schema"] == 1
    path.write_text('{"schema":1,"schema":1,"installations":[]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid plugin capability inventory"):
        migration._load_inventory()


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["installations"].clear(),
        lambda value: value["installations"][0].update(registry_id=str(uuid4())),
        lambda value: value["installations"][0].update(old_manifest_sha256="0" * 64),
        lambda value: value["installations"][0].update(image_id="sha256:" + "f" * 64),
        lambda value: value["installations"][0].update(asset_manifest_sha256="0" * 64),
        lambda value: value["installations"][0]["models"][0].update(
            supported_inputs=["unknown"]
        ),
        lambda value: value["installations"][0]["pools"]["models"].update(cap=True),
    ],
)
def test_inventory_rejects_unproved_or_mismatched_historical_intent(change):
    migration = load(REVISION)
    row = historical_installation()
    value = inventory(row)
    change(value)
    with pytest.raises(RuntimeError):
        migration._validated_inventory(value, [row])
