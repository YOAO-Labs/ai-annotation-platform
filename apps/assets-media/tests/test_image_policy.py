from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_assets_images.policy import ImagePolicy, image_policy_from_environment


def test_policy_preserves_existing_image_thresholds_and_boundaries():
    policy = ImagePolicy()
    assert not policy.auto_generate_pyramids
    assert policy.thumbnail_max_source_bytes == 512 * 1024**2
    assert not policy.pyramid_eligible(None, 4096)
    assert not policy.pyramid_required(10_000, 0)
    assert not policy.pyramid_eligible(4095, 4096)
    assert policy.pyramid_eligible(4096, 4096)
    assert not policy.pyramid_required(9999, 5000)
    assert policy.pyramid_required(10_000, 5000)


@pytest.mark.parametrize(
    "patch",
    [
        {"optional_pixels": True},
        {"required_pixels": 1},
        {"max_pixels": 1},
        {"max_source_bytes": 0},
        {"thumbnail_max_source_bytes": 1024},
        {"job_timeout_seconds": 1_800, "lease_seconds": 1_860},
        {"normalization_version": "unknown"},
    ],
)
def test_policy_rejects_ambiguous_or_unsupported_execution_inputs(patch):
    with pytest.raises(ValidationError):
        ImagePolicy(**patch)


def test_policy_is_frozen_but_new_operations_can_capture_new_limits():
    accepted = ImagePolicy()
    future = ImagePolicy(optional_pixels=1024, required_pixels=2048)
    assert not accepted.pyramid_eligible(32, 32)
    assert future.pyramid_eligible(32, 32)
    with pytest.raises(ValidationError):
        accepted.max_pixels = 1


def test_independent_producers_capture_existing_environment_names():
    environment = {
        "IMAGE_PYRAMID_AUTO_GENERATE": "true",
        "IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS": "3600",
        "IMAGE_PYRAMID_LEASE_SECONDS": "3700",
        "IMAGE_PYRAMID_REQUIRED_PIXELS": "60000000",
        "IMAGE_PYRAMID_PROFILE_VERSION": "production-images",
    }
    policy = image_policy_from_environment(environment)
    environment["IMAGE_PYRAMID_LEASE_SECONDS"] = "1800"
    assert policy.auto_generate_pyramids
    assert policy.lease_seconds == 3700
    assert policy.required_pixels == 60_000_000
    assert policy.profile_version == "production-images"
    assert image_policy_from_environment({}) == ImagePolicy()
    with pytest.raises(ValidationError):
        image_policy_from_environment(environment)
    with pytest.raises(ValueError, match="IMAGE_PYRAMID_AUTO_GENERATE"):
        image_policy_from_environment({"IMAGE_PYRAMID_AUTO_GENERATE": "sometimes"})
