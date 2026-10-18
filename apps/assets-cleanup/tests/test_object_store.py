import threading
from uuid import uuid4

import pytest

from aap_assets_cleanup.object_store import (
    CleanupStopped,
    S3PrefixDeleter,
    StorageFailure,
)


class _Client:
    def __init__(self, pages, delete_results=(), *, stop_after_delete=None):
        self.pages = list(pages)
        self.delete_results = list(delete_results)
        self.stop_after_delete = stop_after_delete
        self.list_calls = []
        self.delete_calls = []

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        return self.pages.pop(0)

    def delete_objects(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.stop_after_delete is not None:
            self.stop_after_delete.set()
        return self.delete_results.pop(0) if self.delete_results else {}


def _deleter(client):
    deleter = object.__new__(S3PrefixDeleter)
    deleter.bucket = "media-cache"
    deleter.client = client
    return deleter


def test_prefix_deletion_checks_every_page_and_uses_only_configured_bucket():
    asset_id = uuid4()
    prefix = f"image-pyramids/{asset_id}/"
    client = _Client(
        [
            {
                "Contents": [{"Key": prefix + "manifest.json"}],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            },
            {"Contents": [{"Key": prefix + "0/0/0.webp"}], "IsTruncated": False},
        ]
    )

    deleted = _deleter(client).delete_prefix(prefix, threading.Event())

    assert deleted == 2
    assert [call["Prefix"] for call in client.list_calls] == [prefix, prefix]
    assert client.list_calls[1]["ContinuationToken"] == "next-page"
    assert all(call["Bucket"] == "media-cache" for call in client.list_calls)
    assert all(call["Bucket"] == "media-cache" for call in client.delete_calls)


def test_out_of_scope_or_malformed_keys_never_reach_delete_objects():
    asset_id = uuid4()
    prefix = f"image-pyramids/{asset_id}/"
    out_of_scope = _Client(
        [
            {
                "Contents": [{"Key": "image-pyramids/neighbor/keep.webp"}],
                "IsTruncated": False,
            }
        ]
    )

    with pytest.raises(StorageFailure, match="out_of_scope"):
        _deleter(out_of_scope).delete_prefix(prefix, threading.Event())
    with pytest.raises(StorageFailure, match="invalid_asset_prefix"):
        _deleter(_Client([])).delete_prefix("image-pyramids/", threading.Event())
    assert out_of_scope.delete_calls == []


@pytest.mark.parametrize(
    "prefix",
    [
        lambda asset, job, lease: f"image-assets/{job}/g2/a{lease}/",
        lambda asset, job, lease: f"image-pyramids/{asset}/g2/a{lease}/",
        lambda asset, job, lease: f"image-pyramids/{asset}/g2/",
    ],
)
def test_image_prefixes_are_deletable_for_whole_generation_and_attempts(prefix):
    asset_id, job_id, lease_id = uuid4(), uuid4(), uuid4()
    scope = prefix(asset_id, job_id, lease_id)
    client = _Client(
        [{"Contents": [{"Key": scope + "thumbnail.webp"}], "IsTruncated": False}]
    )

    assert _deleter(client).delete_prefix(scope, threading.Event()) == 1
    assert client.list_calls[0]["Prefix"] == scope
    assert client.delete_calls[0]["Delete"]["Objects"] == [
        {"Key": scope + "thumbnail.webp"}
    ]


@pytest.mark.parametrize(
    "prefix",
    [
        "image-assets/",
        f"image-assets/{uuid4()}/",
        f"image-assets/{uuid4()}/g0/a{uuid4()}/",
        f"image-assets/{uuid4()}/g1/",
        f"thumbnails/{uuid4()}.webp",
        "../image-assets/",
    ],
)
def test_image_deleter_rejects_non_attempt_or_raw_key_prefixes(prefix):
    client = _Client([])

    with pytest.raises(StorageFailure, match="invalid_asset_prefix"):
        _deleter(client).delete_prefix(prefix, threading.Event())

    assert client.list_calls == []
    assert client.delete_calls == []


def test_probe_lists_only_image_owned_prefixes():
    client = _Client([{"Contents": [], "IsTruncated": False}] * 2)

    _deleter(client).probe()

    assert [call["Prefix"] for call in client.list_calls] == [
        "image-assets/",
        "image-pyramids/",
    ]
    assert all(call["MaxKeys"] == 1 for call in client.list_calls)


def test_partial_s3_delete_errors_leave_the_outbox_ack_for_the_caller():
    asset_id = uuid4()
    prefix = f"image-pyramids/{asset_id}/"
    client = _Client(
        [{"Contents": [{"Key": prefix + "locked.webp"}], "IsTruncated": False}],
        delete_results=[
            {"Errors": [{"Code": "AccessDenied", "Key": prefix + "locked.webp"}]}
        ],
    )

    with pytest.raises(StorageFailure, match="s3_delete_rejected_1") as failure:
        _deleter(client).delete_prefix(prefix, threading.Event())

    assert "locked.webp" not in str(failure.value)


def test_shutdown_after_a_partial_page_leaves_the_prefix_unacknowledged():
    asset_id = uuid4()
    prefix = f"image-pyramids/{asset_id}/"
    stopping = threading.Event()
    client = _Client(
        [
            {
                "Contents": [{"Key": prefix + "0.webp"}],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            }
        ],
        stop_after_delete=stopping,
    )

    with pytest.raises(CleanupStopped):
        _deleter(client).delete_prefix(prefix, stopping)

    assert len(client.delete_calls) == 1
    assert len(client.list_calls) == 1
