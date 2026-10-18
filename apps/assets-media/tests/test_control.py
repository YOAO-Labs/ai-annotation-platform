from __future__ import annotations

from uuid import uuid4

import pytest

from aap_assets_media import contract as c
from aap_assets_media_runtime.control import MediaControl
from test_contract import source


class Storage:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.signed = []

    async def head_source(self, key):
        return c.ObjectIdentity(etag="etag", byte_size=12)

    async def head_cache(self, key):
        return (
            c.ObjectIdentity(etag="cache", byte_size=2)
            if key in self.existing
            else None
        )

    async def sign_source(self, key, expires_in):
        self.signed.append(key)
        return "http://s3.local/datasets/" + key

    async def sign_cache(self, key, expires_in):
        assert key in self.existing
        self.signed.append(key)
        return "http://s3.local/cache/" + key


def frame(s, key=None, **changes):
    return c.MediaAsset(
        row_id=uuid4(),
        dataset_item_id=s.dataset_item_id,
        source_generation=s.source_generation,
        timetable_revision=s.timetable_revision,
        kind="frame",
        frame_index=0,
        width=512,
        format="webp",
        status="ready" if key else "pending",
        storage_key=key,
        **changes,
    )


@pytest.mark.asyncio
async def test_drift_is_recorded_before_reference_is_rejected():
    s = source()
    calls = []

    class Database:
        async def call(self, name, request):
            calls.append(name)
            return (
                s
                if name == "read_media_source"
                else s.model_copy(update={"source_drifted": True})
            )

    control = MediaControl(Database(), Storage())
    with pytest.raises(c.MediaFailure, match="source_changed"):
        await control.source_reference(
            c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=s.dataset_item_id)
        )
    assert calls == ["read_media_source", "observe_media_source"]


@pytest.mark.asyncio
async def test_missing_frame_invalidates_and_does_not_repeat_force_command():
    s = source()
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/frame.webp"
    old, new = frame(s, key), frame(s)
    selections = []

    class Database:
        async def call(self, name, request):
            if name in {"read_media_source", "observe_media_source"}:
                return s
            if name == "ensure_video_media":
                selections.append(request.selection)
                return c.MediaAssets(
                    source=s, assets=[old if len(selections) == 1 else new]
                )
            assert name == "invalidate_video_media"
            assert request.invalidation.expected_key == key
            return c.InvalidationResult(source=s, changed=True)

    storage = Storage()
    result = await MediaControl(Database(), storage).assets_reference(
        c.AssetsRequest(
            actor_id=uuid4(),
            dataset_item_id=s.dataset_item_id,
            selection=c.FrameSelection(frame_indices=[0], force=True),
        )
    )
    assert [selection.force for selection in selections] == [True, False]
    assert result.state.assets[0].status == "pending"
    assert result.state.assets[0].url is None
    assert key not in storage.signed


@pytest.mark.asyncio
async def test_ready_frame_verifies_object_before_signing():
    s = source()
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/frame.webp"

    class Database:
        async def call(self, name, request):
            if name in {"read_media_source", "observe_media_source"}:
                return s
            return c.MediaAssets(source=s, assets=[frame(s, key)])

    result = await MediaControl(Database(), Storage([key])).assets_reference(
        c.AssetsRequest(
            actor_id=uuid4(),
            dataset_item_id=s.dataset_item_id,
            selection=c.FrameSelection(frame_indices=[0]),
        )
    )
    assert result.state.assets[0].url.endswith(key)
    assert result.state.assets[0].expires_in == 3600


@pytest.mark.asyncio
async def test_broker_wakeup_failure_does_not_undo_durable_acceptance():
    s = source()
    request = c.ScheduleWorkRequest(
        operation_id=uuid4(),
        dataset_item_id=s.dataset_item_id,
        spec=c.FrameSpec(frame_index=0),
    )
    accepted = c.AcceptedWork(
        operation_id=request.operation_id,
        source=s,
        spec=request.spec,
        work=c.WorkReference(
            job_id=uuid4(),
            generation=1,
            dataset_item_id=s.dataset_item_id,
            source_generation=1,
            kind="frame",
            status="pending",
        ),
    )

    class Database:
        async def call(self, name, value):
            return accepted

    async def unavailable(job, generation):
        raise ConnectionError("broker unavailable")

    assert (
        await MediaControl(Database(), Storage(), wakeup=unavailable).call(
            "schedule_video_media", request
        )
        == accepted
    )


@pytest.mark.asyncio
async def test_control_cannot_execute_worker_capability():
    with pytest.raises(c.MediaFailure, match="permission_denied"):
        await MediaControl(None, None).call(
            "claim_video_media", c.ClaimRequest(worker_id=uuid4())
        )


@pytest.mark.asyncio
async def test_accepted_background_source_uses_no_actor_and_keeps_export_lifetime():
    s = source()
    observations = []

    class Database:
        async def call(self, name, request):
            assert name == "resolve_internal_media_source"
            observations.append(request)
            return s

    result = await MediaControl(Database(), Storage()).internal_source_reference(
        c.InternalSourceRequest(dataset_item_id=s.dataset_item_id), expires_in=604800
    )
    assert observations[0].object_identity is None
    assert observations[1].source_snapshot == s.source_snapshot
    assert observations[1].object_identity.etag == "etag"
    assert result.original_url.endswith(s.file_path)
    assert result.video_url == result.original_url
    assert result.expires_in == 604800


@pytest.mark.asyncio
async def test_internal_frame_never_signs_cached_frame_after_external_source_drift():
    s = source()
    calls = []

    class Database:
        async def call(self, name, request):
            calls.append(name)
            assert name == "resolve_internal_media_source"
            return (
                s
                if request.object_identity is None
                else s.model_copy(update={"source_drifted": True})
            )

    storage = Storage()
    with pytest.raises(c.MediaFailure, match="source_changed"):
        await MediaControl(Database(), storage).internal_frame_reference(
            c.InternalFrameRequest(dataset_item_id=s.dataset_item_id, frame_index=0)
        )
    assert calls == ["resolve_internal_media_source", "resolve_internal_media_source"]
    assert storage.signed == []


@pytest.mark.asyncio
async def test_source_replacement_during_head_is_rejected_even_without_drift_flag():
    s = source()
    replacement = s.model_copy(
        update={
            "source_snapshot": "b" * 64,
            "source_generation": 2,
            "file_path": "registered/new.mp4",
        }
    )

    class Database:
        async def call(self, name, request):
            return s if name == "read_media_source" else replacement

    storage = Storage()
    with pytest.raises(c.MediaFailure, match="source_changed"):
        await MediaControl(Database(), storage).source_reference(
            c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=s.dataset_item_id)
        )
    assert storage.signed == []


@pytest.mark.asyncio
async def test_missing_cache_cas_cannot_mix_a_new_source_with_old_urls():
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/playback.mp4"
    s = source(video_metadata={"playback_path": key})
    replacement = s.model_copy(
        update={
            "source_snapshot": "b" * 64,
            "source_generation": 2,
            "file_path": "registered/new.mp4",
        }
    )

    class Database:
        async def call(self, name, request):
            if name in {"read_media_source", "observe_media_source"}:
                return s
            assert name == "invalidate_video_media"
            return c.InvalidationResult(source=replacement, changed=False)

    with pytest.raises(c.MediaFailure, match="source_changed"):
        await MediaControl(Database(), Storage()).source_reference(
            c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=s.dataset_item_id)
        )


@pytest.mark.asyncio
async def test_internal_source_is_checked_again_after_implicit_frame_admission():
    s = source()
    replacement = s.model_copy(
        update={"source_snapshot": "b" * 64, "source_generation": 2}
    )

    class Database:
        reads = 0

        async def call(self, name, request):
            if name == "resolve_internal_media_source":
                return s
            if name == "read_internal_video_frame":
                self.reads += 1
                return c.MediaAssets(
                    source=s if self.reads == 1 else replacement, assets=[]
                )
            assert name == "schedule_video_media"
            return c.AcceptedWork(
                operation_id=None,
                source=s,
                spec=request.spec,
                work=c.WorkReference(
                    job_id=uuid4(),
                    generation=1,
                    dataset_item_id=s.dataset_item_id,
                    source_generation=1,
                    kind="frame",
                    status="pending",
                ),
            )

    with pytest.raises(c.MediaFailure, match="source_changed"):
        await MediaControl(Database(), Storage()).internal_frame_reference(
            c.InternalFrameRequest(dataset_item_id=s.dataset_item_id, frame_index=0)
        )
