"""The advertised identity must come from the image selected by Compose."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from training_executor.worker import runtime_identity


@pytest.fixture
def controlled_runtime(monkeypatch):
    from training_contract.catalog import ADAPTERS

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            __version__="verified",
            version=SimpleNamespace(cuda="controlled"),
            cuda=SimpleNamespace(
                is_available=lambda: True,
                device_count=lambda: 1,
                get_device_name=lambda index: "controlled GPU",
            ),
        ),
    )
    monkeypatch.setenv("TRAINING_GPU_UUID", "GPU-controlled")
    monkeypatch.setenv("TRAINING_IMAGE_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setattr(
        "training_executor.worker.subprocess.check_output",
        lambda *args, **kwargs: "GPU-controlled\n",
    )
    monkeypatch.setattr(
        "training_executor.worker.importlib.metadata.version",
        lambda name: ADAPTERS[name]["version"] if name in ADAPTERS else "verified",
    )


def test_identity_uses_selected_reference_instead_of_independent_digest(
    controlled_runtime, monkeypatch
):
    monkeypatch.setenv(
        "TRAINING_IMAGE_REFERENCE", "registry.test/training:latest@sha256:" + "a" * 64
    )
    assert runtime_identity()["image_digest"] == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "reference",
    [
        "registry.test/training:latest",
        "sha256:" + "a" * 64,
        "training@sha256:" + "x" * 64,
        "training@sha256:" + "a" * 63,
        "training@sha256:" + "a" * 64 + "@sha256:" + "b" * 64,
    ],
)
def test_identity_rejects_mutable_or_invalid_reference(
    controlled_runtime, monkeypatch, reference
):
    monkeypatch.setenv("TRAINING_IMAGE_REFERENCE", reference)
    with pytest.raises(ValueError, match="immutable"):
        runtime_identity()


def test_compose_selects_the_same_digest_pinned_reference_it_advertises():
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "docker-compose.training.yml").read_text())
    service = compose["services"]["training-executor"]
    assert "@${TRAINING_IMAGE_DIGEST" in service["image"]
    assert service["environment"]["TRAINING_IMAGE_REFERENCE"] == service["image"]


def test_bitmap_consumer_and_segmentation_initialization_are_explicit_capabilities(
    controlled_runtime, monkeypatch
):
    from training_contract.catalog import instance_rle_capability
    from training_contract.initialization import (
        POLICY_DIGEST,
        supported_policy_digests,
    )

    monkeypatch.setenv(
        "TRAINING_IMAGE_REFERENCE", "registry.test/training:latest@sha256:" + "a" * 64
    )
    runtime = runtime_identity()
    assert runtime["training_saved_prefix_schema"] == 2
    from training_contract.input_membership import POLICY_DIGEST as INPUT_POLICY_DIGEST

    assert runtime["input_receipt_policy_digest"] == INPUT_POLICY_DIGEST
    assert runtime["instance_rle_consumer"] == instance_rle_capability(
        runtime["frameworks"]["ultralytics"]
    )
    assert runtime["initialization_policy_digest"] == POLICY_DIGEST
    assert runtime["initialization_policy_digests"] == supported_policy_digests(
        runtime["frameworks"]["ultralytics"]
    )


def test_runtime_reports_only_locally_verified_base_assets(
    controlled_runtime, monkeypatch, tmp_path
):
    import hashlib
    from training_contract.assets import AssetReference, prepare_asset

    content = b"verified local model"
    reference = AssetReference(
        hashlib.sha256(content).hexdigest(), "yolo11n.pt", len(content)
    )
    path = prepare_asset(tmp_path, reference, lambda: [content])
    monkeypatch.setenv("TRAINING_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TRAINING_IMAGE_REFERENCE", "registry.test/training@sha256:" + "a" * 64
    )
    initial = runtime_identity()
    assert initial["base_asset_sha256"] == [reference.sha256]
    path.chmod(0o644)
    path.write_bytes(b"corrupt")
    assert runtime_identity()["base_asset_sha256"] == []
    # Heartbeats use this previously frozen runtime object; no asset transfer or
    # global readiness is inferred from a later change on disk.
    assert initial["base_asset_sha256"] == [reference.sha256]
