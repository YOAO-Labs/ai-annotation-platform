import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_images import contract as image
from aap_assets_images.policy import ImagePolicy
from aap_assets_inputs import contract as c
from aap_assets_inputs_runtime import worker as input_worker
from aap_assets_inputs_runtime.worker import InputExecutor, InputGarbageCollector
from input_fixtures import BODY, input_work


def world():
    work = input_work(status="materializing")
    token = uuid4()
    claim = c.InputClaim(
        input=work.input,
        source=work.source,
        generation=1,
        lease_token=token,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        blob_key=c.attempt_prefix(work.input.input_id, 1, token) + "source",
    )
    blob = c.InputBlob(
        key=claim.blob_key,
        etag="blob-etag",
        version_id="version-a",
        byte_size=len(BODY),
        sha256=hashlib.sha256(BODY).hexdigest(),
    )
    ready = work.input.model_copy(
        update={"status": "ready", "byte_size": blob.byte_size, "sha256": blob.sha256}
    )
    calls = []

    async def database(operation, request):
        calls.append((operation, request))
        if operation == "claim_accepted_image_input":
            return c.ClaimedInput(claim=claim)
        if operation == "heartbeat_accepted_image_input":
            return c.InputLease(
                input_id=claim.input.input_id,
                generation=claim.generation,
                lease_token=token,
                lease_expires_at=claim.lease_expires_at,
            )
        if operation == "finish_accepted_image_input":
            return ready
        if operation == "fail_accepted_image_input":
            return work.input.model_copy(
                update={"status": "failed", "error": request.error_code}
            )
        raise AssertionError(operation)

    async def download(source, path):
        path.write_bytes(BODY)
        return SimpleNamespace(
            identity=source.object_identity, byte_size=len(BODY), sha256=blob.sha256
        )

    storage = SimpleNamespace(
        assert_versioning=AsyncMock(),
        download_source=AsyncMock(side_effect=download),
        upload_blob=AsyncMock(return_value=blob),
        head_source=AsyncMock(return_value=work.source.object_identity),
    )
    return SimpleNamespace(
        work=work,
        claim=claim,
        blob=blob,
        ready=ready,
        database=SimpleNamespace(call=AsyncMock(side_effect=database)),
        storage=storage,
        calls=calls,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "input_busy", "storage_unavailable"])
async def test_finish_retry_reuses_original_verified_upload(tmp_path, failure):
    state = world()
    native = state.database.call.side_effect
    failed = False

    async def result(operation, request):
        nonlocal failed
        value = await native(operation, request)
        if (
            operation == "finish_accepted_image_input"
            and failure is not None
            and not failed
        ):
            failed = True
            raise c.InputFailure(failure)
        return value

    state.database.call.side_effect = result
    outcome = await InputExecutor(
        state.database, state.storage, temp_root=tmp_path
    ).execute_once(input_id=state.work.input.input_id)
    assert outcome == state.ready
    state.storage.download_source.assert_awaited_once()
    state.storage.upload_blob.assert_awaited_once()
    finish = [
        request
        for operation, request in state.calls
        if operation == "finish_accepted_image_input"
    ]
    assert len(finish) == (2 if failure else 1)
    assert all(request.blob == state.blob for request in finish)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_source_replaced_after_copy_cannot_publish(tmp_path):
    state = world()
    state.storage.head_source.side_effect = image.ImageFailure("source_changed")
    outcome = await InputExecutor(
        state.database, state.storage, temp_root=tmp_path
    ).execute_once()
    assert outcome.status == "failed" and outcome.error == "source_changed"
    assert not any(
        operation == "finish_accepted_image_input" for operation, _ in state.calls
    )
    state.storage.upload_blob.assert_awaited_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["lease_lost", "invalid_response"])
async def test_lost_or_unknown_initial_lease_never_copies_or_overwrites_winner(
    tmp_path, code
):
    state = world()
    native = state.database.call.side_effect

    async def lost(operation, request):
        if operation == "heartbeat_accepted_image_input":
            raise c.InputFailure(code)
        return await native(operation, request)

    state.database.call.side_effect = lost
    assert (
        await InputExecutor(
            state.database, state.storage, temp_root=tmp_path
        ).execute_once()
        is None
    )
    state.storage.download_source.assert_not_awaited()
    assert not any(
        operation in {"finish_accepted_image_input", "fail_accepted_image_input"}
        for operation, _ in state.calls
    )


@pytest.mark.asyncio
async def test_initial_input_busy_retries_before_source_io(tmp_path):
    state = world()
    native = state.database.call.side_effect
    original_download = state.storage.download_source.side_effect
    heartbeat_requests = []
    events = []

    async def busy_then_confirmed(operation, request):
        if operation == "heartbeat_accepted_image_input":
            heartbeat_requests.append(request)
            events.append("heartbeat")
            if len(heartbeat_requests) <= 2:
                raise c.InputFailure("input_busy")
            result = await native(operation, request)
            events.append("confirmed")
            return result
        return await native(operation, request)

    async def tracked_download(source, path):
        events.append("source_io")
        return await original_download(source, path)

    state.database.call.side_effect = busy_then_confirmed
    state.storage.download_source.side_effect = tracked_download
    outcome = await InputExecutor(
        state.database, state.storage, temp_root=tmp_path
    ).execute_once(input_id=state.work.input.input_id)

    assert outcome == state.ready
    assert len(heartbeat_requests) == 3
    assert events.index("confirmed") < events.index("source_io")
    assert all(
        (request.input_id, request.generation, request.lease_token)
        == (
            state.claim.input.input_id,
            state.claim.generation,
            state.claim.lease_token,
        )
        for request in heartbeat_requests
    )
    state.storage.upload_blob.assert_awaited_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_periodic_input_busy_retries_same_claim(tmp_path):
    state = world()
    native = state.database.call.side_effect
    heartbeat_requests = []
    third_heartbeat = asyncio.Event()

    async def busy_once(operation, request):
        if operation == "heartbeat_accepted_image_input":
            heartbeat_requests.append(request)
            if len(heartbeat_requests) == 2:
                raise c.InputFailure("input_busy")
            if len(heartbeat_requests) >= 3:
                third_heartbeat.set()
        return await native(operation, request)

    state.database.call.side_effect = busy_once
    lease = input_worker._Lease(state.database, state.claim, ImagePolicy())
    lease.interval = 0.01
    await lease.start()
    try:
        await asyncio.wait_for(third_heartbeat.wait(), timeout=2)
        assert len(heartbeat_requests) == 3
        assert all(
            (request.input_id, request.generation, request.lease_token)
            == (
                state.claim.input.input_id,
                state.claim.generation,
                state.claim.lease_token,
            )
            for request in heartbeat_requests
        )
        assert lease.error is None and not lease.lost.is_set()
    finally:
        await lease.close()


@pytest.mark.asyncio
async def test_periodic_true_lease_loss_remains_fenced(tmp_path):
    state = world()
    native = state.database.call.side_effect
    heartbeat_requests = []

    async def lose_after_initial(operation, request):
        if operation == "heartbeat_accepted_image_input":
            heartbeat_requests.append(request)
            if len(heartbeat_requests) > 1:
                raise c.InputFailure("lease_lost")
        return await native(operation, request)

    state.database.call.side_effect = lose_after_initial
    lease = input_worker._Lease(state.database, state.claim, ImagePolicy())
    lease.interval = 0.01
    await lease.start()
    try:
        await asyncio.wait_for(lease.lost.wait(), timeout=1)
        assert lease.error is not None and lease.error.code == "lease_lost"
        with pytest.raises(c.InputFailure, match="lease_lost"):
            lease.check()
        assert len(heartbeat_requests) == 2
        assert all(
            (request.input_id, request.generation, request.lease_token)
            == (
                state.claim.input.input_id,
                state.claim.generation,
                state.claim.lease_token,
            )
            for request in heartbeat_requests
        )
    finally:
        await lease.close()


@pytest.mark.asyncio
async def test_initial_input_busy_stops_at_existing_execution_deadline(tmp_path):
    state = world()
    native = state.database.call.side_effect
    heartbeat_requests = []
    policy = ImagePolicy(job_timeout_seconds=1, lease_seconds=62)

    async def continuously_busy(operation, request):
        if operation == "heartbeat_accepted_image_input":
            heartbeat_requests.append(request)
            raise c.InputFailure("input_busy")
        return await native(operation, request)

    state.database.call.side_effect = continuously_busy
    started = time.monotonic()
    outcome = await InputExecutor(
        state.database, state.storage, policy=policy, temp_root=tmp_path
    ).execute_once(input_id=state.work.input.input_id)

    assert outcome.status == "failed" and outcome.error == "timeout"
    assert len(heartbeat_requests) >= 2
    assert all(
        (request.input_id, request.generation, request.lease_token)
        == (
            state.claim.input.input_id,
            state.claim.generation,
            state.claim.lease_token,
        )
        for request in heartbeat_requests
    )
    assert time.monotonic() - started < 1.5
    state.storage.download_source.assert_not_awaited()
    assert [
        request.error_code
        for operation, request in state.calls
        if operation == "fail_accepted_image_input"
    ] == ["timeout"]


@pytest.mark.asyncio
async def test_periodic_input_busy_stops_at_existing_execution_deadline(tmp_path):
    state = world()
    native = state.database.call.side_effect
    heartbeat_requests = []
    policy = ImagePolicy(job_timeout_seconds=1, lease_seconds=62)

    async def busy_after_initial(operation, request):
        if operation == "heartbeat_accepted_image_input":
            heartbeat_requests.append(request)
            if len(heartbeat_requests) > 1:
                raise c.InputFailure("input_busy")
        return await native(operation, request)

    state.database.call.side_effect = busy_after_initial
    lease = input_worker._Lease(state.database, state.claim, policy)
    lease.interval = 0.01
    await lease.start()
    entered = asyncio.Event()
    released = asyncio.Event()

    async def blocked_operation():
        entered.set()
        await released.wait()
        return state.ready

    started = time.monotonic()
    task = asyncio.create_task(lease.call(blocked_operation))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(c.InputFailure, match="timeout"):
            await task
        assert lease.error is not None and lease.error.code == "timeout"
        assert len(heartbeat_requests) >= 2
        assert time.monotonic() - started < 1.5
    finally:
        released.set()
        await lease.close()


@pytest.mark.asyncio
async def test_cancellation_during_source_transfer_never_publishes_or_fails(tmp_path):
    state = world()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_download(_source, _path):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.storage.download_source.side_effect = blocked_download
    task = asyncio.create_task(
        InputExecutor(state.database, state.storage, temp_root=tmp_path).execute_once(
            input_id=state.work.input.input_id
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    state.storage.upload_blob.assert_not_awaited()
    assert not any(
        operation in {"finish_accepted_image_input", "fail_accepted_image_input"}
        for operation, _ in state.calls
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_resource_limit_fails_before_source_transfer(tmp_path):
    state = world()
    outcome = await InputExecutor(
        state.database,
        state.storage,
        temp_root=tmp_path,
        policy=ImagePolicy(max_source_bytes=1),
    ).execute_once()
    assert outcome.error == "resource_limit"
    state.storage.download_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_upload_digest_cannot_finish(tmp_path):
    state = world()
    state.storage.upload_blob.return_value = state.blob.model_copy(
        update={"sha256": "0" * 64}
    )
    outcome = await InputExecutor(
        state.database, state.storage, temp_root=tmp_path
    ).execute_once()
    assert outcome.error == "input_corrupt"
    assert not any(
        operation == "finish_accepted_image_input" for operation, _ in state.calls
    )


@pytest.mark.asyncio
async def test_cleanup_uses_only_cleanup_capabilities():
    garbage = c.InputGarbage(
        garbage_id=uuid4(),
        lease_token=uuid4(),
        prefix=c.attempt_prefix(uuid4(), 1, uuid4()),
    )
    db = SimpleNamespace(
        call=AsyncMock(
            side_effect=[
                c.InputGarbageBatch(items=[garbage]),
                c.InputGarbageAck(garbage_id=garbage.garbage_id, completed=True),
            ]
        )
    )
    storage = SimpleNamespace(delete_attempt_versions=AsyncMock())
    assert await InputGarbageCollector(db, storage).collect_once() == 1
    assert [call.args[0] for call in db.call.await_args_list] == [
        "claim_accepted_input_garbage",
        "complete_accepted_input_garbage",
    ]
    storage.delete_attempt_versions.assert_awaited_once_with(garbage.prefix)
