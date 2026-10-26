"""Mask identities must not inherit the shared bucket's other capabilities."""

import pytest

from scripts.mask_content_s3_policy import mask_content_policy


def test_mask_content_and_cleanup_have_disjoint_mutations_and_fixed_prefix():
    arn = "arn:aws:s3:::annotation-data"
    for cleanup in (False, True):
        policy = mask_content_policy("annotation-data", cleanup=cleanup)
        statements = policy["Statement"]
        object_actions = {
            action
            for item in statements
            if item["Resource"] == [arn + "/raster-masks/sha256/*"]
            for action in item["Action"]
        }
        assert object_actions == {
            "s3:GetObject",
            "s3:DeleteObject" if cleanup else "s3:PutObject",
        }
        assert all(
            resource
            in {arn, arn + "/raster-masks/sha256/*", arn + "/raster-mask-gc/v1/*"}
            for item in statements
            for resource in item["Resource"]
        )
        listings = [item for item in statements if "s3:ListBucket" in item["Action"]]
        assert bool(listings) is cleanup
        if cleanup:
            assert listings[0]["Condition"] == {
                "StringLike": {
                    "s3:prefix": ["raster-masks/sha256/*", "raster-mask-gc/v1/*"]
                }
            }
        journal_actions = {
            action
            for item in statements
            if item["Resource"] == [arn + "/raster-mask-gc/v1/*"]
            for action in item["Action"]
        }
        assert journal_actions == (
            {"s3:GetObject", "s3:PutObject"} if cleanup else set()
        )


@pytest.mark.parametrize("bucket", ["", "*", "annotations/*", "arn:aws:s3:::other"])
def test_policy_rejects_wildcard_or_resource_injection(bucket):
    with pytest.raises(ValueError):
        mask_content_policy(bucket)
