"""Dataset outputs and retained reads use B without touching a same-key A copy."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import training_dataset_analysis as analysis
from app.services import training_dataset_storage as owners, training_storage_io as io
from app.services.training_dataset_bridge import TrainingDatasetError
from app.workers import training_dataset as worker
from tests.test_training_storage_consumers import (
    Objects,
    frozen as frozen,
    source_sample,
)
from tests.test_training_storage_targets import target
from tests.training_original_fixtures import staged_image


@pytest.fixture
def destination(frozen):
    client = Objects("https://objects.example.test")
    lifecycle = []

    @contextmanager
    def admitted_client(**kwargs):
        lifecycle.append("open")
        try:
            yield client
        finally:
            lifecycle.append("close")

    destination = replace(target(), _access=SimpleNamespace(client=admitted_client))
    frozen.version.storage_ref = owners.make_ref(
        destination, 2, owners.prefix_for(frozen.version.project_id, frozen.version.id)
    )
    owner = owners.resolve(frozen.version)
    key = owner.key(f"media/{frozen.sample.task_id}")
    client.objects[owner.bucket, key] = frozen.raw
    client.metadata[owner.bucket, key] = {
        "snapshot-digest": frozen.version.snapshot_digest,
        "sha256": frozen.sample.sha256,
    }
    return destination, client, lifecycle


def test_retained_b_read_and_worker_retry_never_touch_a(frozen, destination, tmp_path):
    target, client, lifecycle = destination
    owner = owners.resolve(frozen.version)
    actual = worker._materialize_image(
        target,
        source_sample(frozen),
        owner.prefix.rstrip("/"),
        frozen.version.snapshot_digest,
        tmp_path,
        bucket=owner.bucket,
    )
    assert actual["sha256"] == frozen.sample.sha256
    source, _ = io.physical(
        target,
        lambda bound: analysis.read_media(
            frozen.version.project_id, frozen.version, frozen.sample, transport=bound
        ),
    )
    try:
        assert source.read() == frozen.raw
    finally:
        source.close()
    assert frozen.client.calls == []
    assert {call[1] for call in client.calls} == {owner.bucket}
    assert lifecycle == ["open", "close", "open", "close"]


def test_b_original_media_and_mask_uploads_read_back_from_same_target(
    frozen, destination, tmp_path
):
    target, client, lifecycle = destination
    owner = owners.resolve(frozen.version)
    del client.objects[owner.bucket, owner.key(f"media/{frozen.sample.task_id}")]
    sample = {
        **source_sample(frozen),
        "raster_contract": True,
        "classes": ["shape"],
        "annotations": [],
    }
    stage = staged_image(frozen.raw)
    try:
        result = worker._materialize_image(
            target,
            sample,
            owner.prefix.rstrip("/"),
            frozen.version.snapshot_digest,
            tmp_path,
            bucket=owner.bucket,
            verified_original=stage,
        )
    finally:
        stage.file.close()
    assert result["sha256"] == frozen.sample.sha256
    assert (
        owner.bucket,
        owner.key(f"masks/{frozen.sample.task_id}.json"),
    ) in client.objects
    assert frozen.client.calls == [] and lifecycle == ["open", "close"]


def test_rooted_sync_bypass_refuses_before_using_global_client(
    frozen, destination, tmp_path
):
    owner = owners.resolve(frozen.version)
    with pytest.raises(TrainingDatasetError):
        analysis.read_media(frozen.version.project_id, frozen.version, frozen.sample)
    with pytest.raises(TrainingDatasetError):
        worker.materialize_image(
            source_sample(frozen),
            owner.prefix.rstrip("/"),
            frozen.version.snapshot_digest,
            tmp_path,
            bucket=owner.bucket,
        )
    assert frozen.client.calls == []
