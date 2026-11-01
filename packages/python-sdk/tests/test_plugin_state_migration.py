from __future__ import annotations

from copy import deepcopy

import pytest

from ai_annotation.errors import AAPError
from ai_annotation.plugin_deployment import LocalPluginDeployment
from .support_plugin_deployment import Core, LocalDocker, config, report_file


def test_legacy_receipt_migration_requires_same_image_and_immutable_manifest(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    approved = config()
    deployment.prepare(approved)
    deployment.install(report_file(tmp_path))
    legacy = deployment.store.load()
    legacy["config"]["manifest"].pop("models")
    legacy["config"]["manifest"].pop("pools")
    deployment.store.save(legacy)
    original = deepcopy(legacy)

    with pytest.raises(AAPError, match="approved image"):
        deployment.migrate_state(approved.manifest, "sha256:" + "e" * 64)
    assert deployment.store.load() == original

    modified = approved.manifest.model_copy(update={"version": "other"})
    with pytest.raises(AAPError, match="immutable"):
        deployment.migrate_state(modified, "sha256:" + "d" * 64)
    assert deployment.store.load() == original

    result = deployment.migrate_state(approved.manifest, "sha256:" + "d" * 64)
    assert result.installation.status == "active"
    assert deployment.migrate_state(approved.manifest, "sha256:" + "d" * 64) == result
    migrated = deployment.store.load()
    assert migrated["config"]["manifest"] == approved.manifest.model_dump(mode="json")
    snapshot = migrated.pop("legacy_receipt")
    assert snapshot == {key: value for key, value in original.items() if key != "token"}
    migrated["config"]["manifest"] = original["config"]["manifest"]
    assert migrated == original
