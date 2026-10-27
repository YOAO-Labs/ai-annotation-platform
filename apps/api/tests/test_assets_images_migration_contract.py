"""Image worker and cleanup storage access boundaries."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
VERIFIER = ROOT / "scripts/verify_module_data_boundary.py"


def _module_data_verifier():
    spec = importlib.util.spec_from_file_location("module_data_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_s3_scopes_read_bound_versions_and_separate_cleanup():
    verifier = _module_data_verifier()
    scope = {
        "bucket": "media-cache-test",
        "datasets_bucket": "datasets-test",
        "legacy_bucket": "annotations-test",
        "inputs_bucket": "accepted-test",
    }
    image_policy = verifier._image_worker_storage_policy(scope)
    control_policy = verifier._media_storage_policy(scope, worker=False)
    cleanup_policy = verifier._cleanup_storage_policy(scope)

    source_arns = {
        "arn:aws:s3:::datasets-test/*",
        "arn:aws:s3:::annotations-test/*",
    }
    derived_arns = {
        "arn:aws:s3:::media-cache-test/image-assets/*",
        "arn:aws:s3:::media-cache-test/image-pyramids/*",
    }
    for policy in (image_policy, control_policy):
        source_statements = [
            statement
            for statement in policy["Statement"]
            if set(statement["Resource"]) & source_arns
        ]
        assert source_statements
        assert all(
            set(statement["Action"]) == {"s3:GetObject", "s3:GetObjectVersion"}
            for statement in source_statements
        )
        assert all(
            "s3:PutObject" not in statement["Action"]
            and "s3:DeleteObject" not in statement["Action"]
            for statement in source_statements
        )
    assert all(
        "s3:PutObject" not in statement["Action"]
        and "s3:DeleteObject" not in statement["Action"]
        for statement in control_policy["Statement"]
    )
    assert all(
        "s3:DeleteObject" not in statement["Action"]
        for statement in image_policy["Statement"]
    )

    image_writes = [
        statement
        for statement in image_policy["Statement"]
        if "s3:PutObject" in statement["Action"]
    ]
    assert len(image_writes) == 2
    assert {
        resource for statement in image_writes for resource in statement["Resource"]
    } == derived_arns | {"arn:aws:s3:::accepted-test/accepted-inputs/*"}
    image_list = [
        statement
        for statement in image_policy["Statement"]
        if "s3:ListBucket" in statement["Action"]
    ]
    assert len(image_list) == 1
    assert image_list[0]["Condition"] == {
        "StringLike": {"s3:prefix": ["image-pyramids/*"]}
    }

    cleanup_actions = {
        action
        for statement in cleanup_policy["Statement"]
        for action in statement["Action"]
    }
    assert cleanup_actions == {
        "s3:ListBucket",
        "s3:DeleteObject",
        "s3:GetBucketVersioning",
        "s3:ListBucketVersions",
        "s3:ListBucketMultipartUploads",
        "s3:DeleteObjectVersion",
        "s3:AbortMultipartUpload",
    }
    assert cleanup_policy["Statement"][1]["Resource"] == sorted(derived_arns)
    assert not any(
        set(statement["Resource"]) & source_arns
        for statement in cleanup_policy["Statement"]
    )


def test_image_source_versioning_is_limited_to_owned_buckets():
    verifier = _module_data_verifier()

    class FakeStorage:
        def __init__(self, owner: str):
            self.owner = owner
            self.versioning_updates = []

        def get_bucket_tagging(self, *, Bucket: str):
            return {
                "TagSet": [
                    {"Key": "aap-worktree-owner", "Value": self.owner},
                    {"Key": "test-bucket", "Value": Bucket},
                ]
            }

        def put_bucket_versioning(self, **kwargs):
            self.versioning_updates.append(kwargs)

        def get_bucket_versioning(self, *, Bucket: str):
            assert Bucket == "owned-source"
            return {"Status": "Enabled"}

    owned = FakeStorage("aap-worktree:test")
    backend = SimpleNamespace(s3=owned, resources={"owner": "aap-worktree:test"})
    verifier._enable_owned_bucket_versioning(backend, "owned-source")
    assert owned.versioning_updates == [
        {
            "Bucket": "owned-source",
            "VersioningConfiguration": {"Status": "Enabled"},
        }
    ]

    unowned = FakeStorage("another-worktree")
    backend.s3 = unowned
    try:
        verifier._enable_owned_bucket_versioning(backend, "unowned-source")
    except RuntimeError as error:
        assert "not owned" in str(error)
    else:
        raise AssertionError("versioning must refuse an unowned source bucket")
    assert unowned.versioning_updates == []
