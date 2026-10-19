"""Least-privilege statements for the accepted-input S3 bucket."""

from scripts.verify_module_data_boundary import (
    _cleanup_storage_policy,
    _image_worker_storage_policy,
    _media_storage_policy,
)


def _actions(policy, resource, *, effect="Allow"):
    return {
        action
        for statement in policy["Statement"]
        if statement["Effect"] == effect and resource in statement["Resource"]
        for action in statement["Action"]
    }


def _statement(policy, resource, action, *, effect="Allow"):
    return next(
        statement
        for statement in policy["Statement"]
        if statement["Effect"] == effect
        and resource in statement["Resource"]
        and action in statement["Action"]
    )


def test_control_can_read_exact_input_versions_but_not_list_or_mutate():
    scope = {
        "bucket": "media-cache",
        "inputs_bucket": "accepted-inputs",
        "datasets_bucket": "datasets",
    }
    policy = _media_storage_policy(scope, worker=False)

    assert _actions(policy, "arn:aws:s3:::accepted-inputs") == {
        "s3:GetBucketVersioning"
    }
    assert _actions(policy, "arn:aws:s3:::accepted-inputs/*") == {"s3:GetObjectVersion"}
    assert _statement(policy, "arn:aws:s3:::accepted-inputs/*", "s3:GetObjectVersion")[
        "Condition"
    ] == {
        "StringLike": {"s3:versionid": "*?*"},
        "StringNotEquals": {"s3:versionid": "null"},
    }
    assert not any(
        statement["Effect"] == "Deny" and "s3:GetObject" in statement["Action"]
        for statement in policy["Statement"]
    )


def test_image_worker_can_read_versions_and_put_attempts_without_deleting():
    scope = {
        "bucket": "media-cache",
        "inputs_bucket": "accepted-inputs",
        "datasets_bucket": "datasets",
        "legacy_bucket": "annotations",
    }
    policy = _image_worker_storage_policy(scope)

    assert _actions(policy, "arn:aws:s3:::accepted-inputs") == {
        "s3:GetBucketVersioning"
    }
    assert _actions(policy, "arn:aws:s3:::accepted-inputs/*") == {
        "s3:GetObjectVersion",
        "s3:PutObject",
    }
    assert _actions(policy, "arn:aws:s3:::accepted-inputs/*", effect="Deny") == set()
    assert _statement(policy, "arn:aws:s3:::accepted-inputs/*", "s3:GetObjectVersion")[
        "Condition"
    ] == {
        "StringLike": {"s3:versionid": "*?*"},
        "StringNotEquals": {"s3:versionid": "null"},
    }


def test_cleanup_can_enumerate_and_delete_input_versions_only():
    scope = {"bucket": "media-cache", "inputs_bucket": "accepted-inputs"}
    policy = _cleanup_storage_policy(scope)

    assert _actions(policy, "arn:aws:s3:::accepted-inputs") == {
        "s3:GetBucketVersioning",
        "s3:ListBucketVersions",
        "s3:ListBucketMultipartUploads",
    }
    assert _actions(policy, "arn:aws:s3:::accepted-inputs/*") == {
        "s3:DeleteObjectVersion",
        "s3:AbortMultipartUpload",
        "s3:DeleteObject",
    }
    assert _statement(policy, "arn:aws:s3:::accepted-inputs/*", "s3:DeleteObject")[
        "Condition"
    ] == {
        "StringLike": {"s3:versionid": "*?*"},
        "StringNotEquals": {"s3:versionid": "null"},
    }
    assert not any(
        "PutBucketVersioning" in statement["Action"]
        or "CreateBucket" in statement["Action"]
        for statement in policy["Statement"]
    )
