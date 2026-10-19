"""Runtime preparation stays bound to the frozen source before native admission."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_images import contract as image
from aap_assets_input_manifests import contract as c
from aap_assets_input_manifests_runtime.control import ManifestControl


def fixture_values(*, state="unprepared", identity=None):
    job_id, actor_id, project_id, task_id = (uuid4() for _ in range(4))
    source = image.ImageSource(
        owner=image.ImageOwner(kind="task", id=task_id),
        task_id=task_id,
        project_id=project_id,
        dataset_id=None,
        item_id=None,
        source_generation=3,
        source_snapshot="b" * 64,
        object_identity=identity,
        source_drifted=False,
        upload_ready=True,
        file_path="private/original.png",
        file_name="original.png",
        file_type="image",
        bucket_kind="legacy",
        file_size=12,
        content_hash=None,
        width=4,
        height=3,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )
    header = c.ManifestHeader(
        job_id=job_id,
        actor_id=actor_id,
        project_id=project_id,
        selection={"predict_mode": "skip_predicted"},
        selection_digest="a" * 64,
        candidate_count=1,
        selected_count=1,
        prepared_count=0 if state == "unprepared" else 1,
        status="preparing",
        ready_count=None,
        failed_count=None,
        sealed_digest=None,
        created_at=datetime.now(timezone.utc),
        sealed_at=None,
    )
    input_record = None
    error = None
    if state == "preparing":
        from aap_assets_inputs.contract import InputRecord

        input_record = InputRecord(
            input_id=uuid4(),
            job_id=job_id,
            task_id=task_id,
            project_id=project_id,
            actor_id=actor_id,
            status="pending",
            task_version=5,
            task_source_generation=2,
            dataset_item_id=None,
            task_source_snapshot="c" * 64,
            source_owner=source.owner,
            source_generation=source.source_generation,
            source_snapshot=source.source_snapshot,
            byte_size=None,
            sha256=None,
            error=None,
            created_at=datetime.now(timezone.utc),
            retire_after=None,
        )
    elif state == "failed":
        error = "source_changed"
    member = c.ManifestMember(
        ordinal=1,
        task_id=task_id,
        project_id=project_id,
        task_version=5,
        task_source_generation=2,
        dataset_item_id=None,
        task_source_snapshot="c" * 64,
        file_path="private/original.png",
        file_type="image",
        source=source,
        state=state,
        input=input_record,
        error=error,
    )
    page = c.ManifestPage(manifest=header, cursor=0, next_cursor=None, members=[member])
    request = c.PrepareManifestMemberRequest(
        job_id=job_id,
        actor_id=actor_id,
        project_id=project_id,
        selection_digest=header.selection_digest,
        ordinal=1,
        task_id=task_id,
    )
    return source, header, member, page, request


class FakeDatabase:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def call(self, operation, request):
        self.calls.append((operation, request))
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_prepare_observes_frozen_snapshot_before_native_write():
    source, _header, member, page, request = fixture_values()
    observed = source.model_copy(
        update={"object_identity": image.ObjectIdentity(etag="source-v1", byte_size=12)}
    )
    from aap_assets_inputs.contract import InputRecord

    pending_input = InputRecord(
        input_id=uuid4(),
        job_id=request.job_id,
        task_id=request.task_id,
        project_id=request.project_id,
        actor_id=request.actor_id,
        status="pending",
        task_version=member.task_version,
        task_source_generation=member.task_source_generation,
        dataset_item_id=member.dataset_item_id,
        task_source_snapshot=member.task_source_snapshot,
        source_owner=source.owner,
        source_generation=source.source_generation,
        source_snapshot=source.source_snapshot,
        byte_size=None,
        sha256=None,
        error=None,
        created_at=datetime.now(timezone.utc),
        retire_after=None,
    )
    preparing_member = c.ManifestMember.model_validate(
        {
            **member.model_dump(mode="json"),
            "state": "preparing",
            "input": pending_input.model_dump(mode="json"),
        }
    )
    receipt = c.ManifestMemberReceipt(
        job_id=request.job_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        selection_digest=request.selection_digest,
        member=preparing_member,
    )
    database = FakeDatabase(page, receipt)
    images = SimpleNamespace(observe_source=AsyncMock(return_value=observed))

    result = await ManifestControl(database, images).prepare(request)

    assert result == receipt
    assert database.calls[0][0] == "read_accepted_image_input_manifest_page"
    assert database.calls[0][1].cursor == 0
    assert database.calls[0][1].page_size == 1
    images.observe_source.assert_awaited_once_with(
        image.ReadImageSourceRequest(
            actor_id=request.actor_id, task_id=request.task_id
        ),
        expected_source_snapshot=source.source_snapshot,
    )
    operation, native_request = database.calls[1]
    assert operation == "prepare_accepted_image_input_manifest_member"
    assert isinstance(native_request, c.NativePrepareManifestMemberRequest)
    assert native_request.source_snapshot == source.source_snapshot
    assert native_request.object_identity == observed.object_identity
    assert native_request.terminal_error is None


@pytest.mark.asyncio
async def test_replay_returns_existing_input_before_source_head():
    _source, _header, _member, page, request = fixture_values(state="preparing")
    database = FakeDatabase(page)
    images = SimpleNamespace(observe_source=AsyncMock())

    result = await ManifestControl(database, images).prepare(request)

    assert result.member.state == "preparing"
    assert result.member.input.status == "pending"
    assert len(database.calls) == 1
    images.observe_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_source_error_becomes_fixed_member_failure():
    source, _header, _member, page, request = fixture_values()
    database = FakeDatabase(page, AsyncMock())
    database.results[1] = c.ManifestMemberReceipt(
        job_id=request.job_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        selection_digest=request.selection_digest,
        member=page.members[0].model_copy(
            update={"state": "failed", "error": "source_changed"}
        ),
    )
    images = SimpleNamespace(
        observe_source=AsyncMock(side_effect=image.ImageFailure("source_changed"))
    )

    await ManifestControl(database, images).prepare(request)

    operation, native_request = database.calls[1]
    assert operation == "prepare_accepted_image_input_manifest_member"
    assert native_request.source_snapshot == source.source_snapshot
    assert native_request.object_identity is None
    assert native_request.terminal_error == "source_changed"


@pytest.mark.asyncio
async def test_transient_source_error_is_not_persisted_as_member_failure():
    _source, _header, _member, page, request = fixture_values()
    database = FakeDatabase(page)
    images = SimpleNamespace(
        observe_source=AsyncMock(side_effect=image.ImageFailure("storage_unavailable"))
    )

    with pytest.raises(c.ManifestFailure, match="storage_unavailable"):
        await ManifestControl(database, images).prepare(request)
    assert len(database.calls) == 1


@pytest.mark.asyncio
async def test_known_identity_change_fails_member_without_adopting_new_bytes():
    captured = image.ObjectIdentity(etag="captured", byte_size=12)
    source, _header, _member, page, request = fixture_values(identity=captured)
    observed = source.model_copy(
        update={
            "object_identity": image.ObjectIdentity(etag="replacement", byte_size=12)
        }
    )
    receipt = c.ManifestMemberReceipt(
        job_id=request.job_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        selection_digest=request.selection_digest,
        member=page.members[0].model_copy(
            update={"state": "failed", "error": "source_changed"}
        ),
    )
    database = FakeDatabase(page, receipt)
    images = SimpleNamespace(observe_source=AsyncMock(return_value=observed))

    await ManifestControl(database, images).prepare(request)

    assert database.calls[1][1].object_identity is None
    assert database.calls[1][1].terminal_error == "source_changed"


@pytest.mark.asyncio
async def test_prepare_rejects_wrong_member_before_source_io():
    _source, _header, _member, page, request = fixture_values()
    database = FakeDatabase(page)
    images = SimpleNamespace(observe_source=AsyncMock())
    wrong = request.model_copy(update={"task_id": uuid4()})

    with pytest.raises(c.ManifestFailure, match="manifest_scope_mismatch"):
        await ManifestControl(database, images).prepare(wrong)
    images.observe_source.assert_not_awaited()
