"""The advertised identity must come from the image selected by Compose."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from training_executor.worker import runtime_identity


@pytest.fixture
def controlled_runtime(monkeypatch):
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
        "training_executor.worker.importlib.metadata.version", lambda name: "verified"
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
