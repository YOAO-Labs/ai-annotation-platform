from __future__ import annotations

import hashlib
import json

import pytest

from scripts import validate_managed_lifecycle as validator
from training_contract.assets import AssetUnavailable


def test_approved_weights_require_exact_hash_manifest(tmp_path, monkeypatch) -> None:
    filenames = ["detect.pt", "segment.pt", "pose.pt", "obb.pt"]
    manifest = {}
    for filename in filenames:
        content = filename.encode()
        (tmp_path / filename).write_bytes(content)
        manifest[filename] = hashlib.sha256(content).hexdigest()
    monkeypatch.setenv("VALIDATION_WEIGHT_SHA256_JSON", json.dumps(manifest))

    records = validator._approved_weights(
        checkpoint_dir=tmp_path,
        filenames=filenames,
        approval_ref="models:approved",
    )

    assert {record["name"] for record in records} == set(filenames)
    assert all(record["approval_ref"] == "models:approved" for record in records)
    assert str(tmp_path) not in str(records)


def test_approved_weights_reject_hash_drift(tmp_path, monkeypatch) -> None:
    weight = tmp_path / "detect.pt"
    weight.write_bytes(b"candidate")
    monkeypatch.setenv(
        "VALIDATION_WEIGHT_SHA256_JSON",
        json.dumps({"detect.pt": "0" * 64}),
    )

    with pytest.raises(RuntimeError, match="approved SHA-256"):
        validator._approved_weights(
            checkpoint_dir=tmp_path,
            filenames=["detect.pt"],
            approval_ref="models:approved",
        )


def _prepared(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    legacy = tmp_path / "checkpoints"
    legacy.mkdir()
    content = b"actual prepared bytes"
    sha = hashlib.sha256(content).hexdigest()
    blob = assets / f"{sha}.pt"
    blob.write_bytes(content)
    (assets / "assets.json").write_text(
        json.dumps({"version": 1, "assets": [{"filename": "detect.pt", "sha256": sha}]})
    )
    monkeypatch.setenv("VALIDATION_WEIGHT_SHA256_JSON", json.dumps({"detect.pt": sha}))
    return assets, legacy, blob


@pytest.mark.parametrize("stale_legacy_copy", [False, True])
def test_approved_weights_follow_actual_sha_cache_not_old_checkpoint_copy(
    tmp_path, monkeypatch, stale_legacy_copy
):
    assets, legacy, blob = _prepared(tmp_path, monkeypatch)
    if stale_legacy_copy:
        (legacy / "detect.pt").write_bytes(b"old incorrect checkpoint")
    records = validator._approved_weights(
        checkpoint_dir=legacy,
        model_assets_dir=assets,
        filenames=["detect.pt"],
        approval_ref="models:approved",
    )
    assert records == [
        {
            "kind": "weight",
            "name": blob.name,
            "size_bytes": blob.stat().st_size,
            "sha256": blob.stem,
            "approval_ref": "models:approved",
        }
    ]


@pytest.mark.parametrize("change", ["missing", "corrupt", "malformed_mapping"])
def test_mapped_asset_failure_cannot_approve_legacy_checkpoint(
    tmp_path, monkeypatch, change
):
    assets, legacy, blob = _prepared(tmp_path, monkeypatch)
    (legacy / "detect.pt").write_bytes(blob.read_bytes())
    if change == "missing":
        blob.unlink()
    elif change == "corrupt":
        blob.write_bytes(b"corrupt")
    else:
        (assets / "assets.json").write_text("{}")
    with pytest.raises((AssetUnavailable, ValueError)):
        validator._approved_weights(
            checkpoint_dir=legacy,
            model_assets_dir=assets,
            filenames=["detect.pt"],
            approval_ref="models:approved",
        )


def test_unmapped_weight_preserves_checkpoint_fallback(tmp_path, monkeypatch):
    assets, legacy, blob = _prepared(tmp_path, monkeypatch)
    (assets / "assets.json").write_text('{"version":1,"assets":[]}')
    (legacy / "detect.pt").write_bytes(blob.read_bytes())
    records = validator._approved_weights(
        checkpoint_dir=legacy,
        model_assets_dir=assets,
        filenames=["detect.pt"],
        approval_ref="models:approved",
    )
    assert records[0]["name"] == "detect.pt"


def test_failure_path_emits_strict_nonpassing_evidence() -> None:
    evidence = validator._failed_evidence(RuntimeError("local path must not leak"))

    assert evidence["schema_version"] == "1"
    assert evidence["passed"] is False
    assert "local path" not in str(evidence)
