"""Declared IAM partitions for the owned video-input process qualification."""

from tests.test_assets_images_migration_contract import _module_data_verifier


def test_video_and_image_workers_use_disjoint_accepted_object_namespaces():
    verifier = _module_data_verifier()
    scope = {
        "bucket": "cache-test",
        "datasets_bucket": "datasets-test",
        "legacy_bucket": "legacy-test",
        "inputs_bucket": "accepted-test",
    }
    image = verifier._image_worker_storage_policy(scope)
    video = verifier._media_storage_policy(scope, worker=True)
    control = verifier._media_storage_policy(scope, worker=False)
    image_prefix = "arn:aws:s3:::accepted-test/accepted-inputs/*"
    video_prefix = "arn:aws:s3:::accepted-test/accepted-video-inputs/*"
    for policy, prefix in ((image, image_prefix), (video, video_prefix)):
        accepted = [
            row
            for row in policy["Statement"]
            if any(
                resource.startswith("arn:aws:s3:::accepted-test/")
                for resource in row["Resource"]
            )
        ]
        assert accepted
        assert {resource for row in accepted for resource in row["Resource"]} == {
            prefix
        }
        assert any("s3:PutObject" in row["Action"] for row in accepted)
        assert all(
            "s3:GetObject" not in row["Action"]
            and "s3:DeleteObject" not in row["Action"]
            and "s3:DeleteObjectVersion" not in row["Action"]
            for row in accepted
        )
        reads = [row for row in accepted if "s3:GetObjectVersion" in row["Action"]]
        assert len(reads) == 1
        assert reads[0]["Condition"] == {
            "StringLike": {"s3:versionid": "*?*"},
            "StringNotEquals": {"s3:versionid": "null"},
        }
    control_reads = [
        row
        for row in control["Statement"]
        if "s3:GetObjectVersion" in row["Action"] and image_prefix in row["Resource"]
    ]
    assert len(control_reads) == 1
    assert set(control_reads[0]["Resource"]) == {image_prefix, video_prefix}


def test_cleanup_lists_only_both_input_prefixes_and_requires_version_scoped_delete():
    verifier = _module_data_verifier()
    policy = verifier._cleanup_storage_policy(
        {"bucket": "cache-test", "inputs_bucket": "accepted-test"}
    )
    listings = [
        row for row in policy["Statement"] if "s3:ListBucketVersions" in row["Action"]
    ]
    assert len(listings) == 1
    assert listings[0]["Condition"] == {
        "StringLike": {"s3:prefix": ["accepted-inputs/*", "accepted-video-inputs/*"]}
    }
    deletes = [
        row
        for row in policy["Statement"]
        if "s3:DeleteObject" in row["Action"]
        and any(
            resource.startswith("arn:aws:s3:::accepted-test/")
            for resource in row["Resource"]
        )
    ]
    assert len(deletes) == 1
    assert set(deletes[0]["Resource"]) == {
        "arn:aws:s3:::accepted-test/accepted-inputs/*",
        "arn:aws:s3:::accepted-test/accepted-video-inputs/*",
    }
    assert deletes[0]["Condition"] == {
        "StringLike": {"s3:versionid": "*?*"},
        "StringNotEquals": {"s3:versionid": "null"},
    }
    assert all(
        "s3:GetObject" not in row["Action"] and "s3:PutObject" not in row["Action"]
        for row in policy["Statement"]
    )
