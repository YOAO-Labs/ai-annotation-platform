"""Attachment cleanup must not be bypassed by the old object-store rule."""

from unittest.mock import Mock

from botocore.exceptions import ClientError

from app.services.storage import StorageService


def service(rules):
    storage = StorageService.__new__(StorageService)
    storage.bucket = "owned-annotations"
    storage.client = Mock()
    storage.client.get_bucket_lifecycle_configuration.return_value = {"Rules": rules}
    return storage


def test_removes_only_owned_comment_rule_and_preserves_operator_rule():
    unrelated = {
        "ID": "operator-rule",
        "Status": "Disabled",
        "Filter": {"Prefix": "operator/"},
        "Expiration": {"Days": 17},
    }
    storage = service([{"ID": "comment-attachments-90d"}, unrelated])
    storage._remove_comment_attachment_lifecycle()
    storage.client.put_bucket_lifecycle_configuration.assert_called_once_with(
        Bucket="owned-annotations", LifecycleConfiguration={"Rules": [unrelated]}
    )
    storage.client.delete_bucket_lifecycle.assert_not_called()


def test_deletes_empty_configuration_only_after_removing_owned_rule():
    storage = service([{"ID": "comment-attachments-90d"}])
    storage._remove_comment_attachment_lifecycle()
    storage.client.delete_bucket_lifecycle.assert_called_once_with(
        Bucket="owned-annotations"
    )
    storage.client.put_bucket_lifecycle_configuration.assert_not_called()


def test_does_not_rewrite_configuration_without_owned_rule():
    storage = service([{"ID": "operator-rule"}])
    storage._remove_comment_attachment_lifecycle()
    storage.client.delete_bucket_lifecycle.assert_not_called()
    storage.client.put_bucket_lifecycle_configuration.assert_not_called()


def test_absent_configuration_is_idempotent():
    storage = service([])
    storage.client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NoSuchLifecycleConfiguration"}},
        "GetBucketLifecycleConfiguration",
    )
    storage._remove_comment_attachment_lifecycle()
    storage.client.delete_bucket_lifecycle.assert_not_called()
    storage.client.put_bucket_lifecycle_configuration.assert_not_called()


def test_failed_inspection_does_not_clear_unknown_configuration():
    storage = service([])
    storage.client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetBucketLifecycleConfiguration"
    )
    storage._remove_comment_attachment_lifecycle()
    storage.client.delete_bucket_lifecycle.assert_not_called()
    storage.client.put_bucket_lifecycle_configuration.assert_not_called()
