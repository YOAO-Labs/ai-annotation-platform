from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aap_assets_images import contract as image
from aap_assets_inputs import contract as c
from aap_assets_inputs_runtime.control import InputControl
from input_fixtures import input_request, input_work


@pytest.mark.asyncio
async def test_replay_returns_accepted_source_before_current_head():
    work = input_work()
    database = SimpleNamespace(
        call=AsyncMock(return_value=c.FoundInput(input=work.input))
    )
    images = SimpleNamespace(
        observe_source=AsyncMock(side_effect=image.ImageFailure("source_missing"))
    )
    control = InputControl(database, images, object())
    result = await control.accept(
        c.FindInputRequest(
            job_id=work.input.job_id,
            task_id=work.input.task_id,
            actor_id=work.input.actor_id,
        )
    )
    assert result == work.input
    images.observe_source.assert_not_awaited()
    assert database.call.await_count == 1


@pytest.mark.asyncio
async def test_new_acceptance_carries_observed_identity_into_reservation():
    work = input_work(status="pending")
    database = SimpleNamespace(
        call=AsyncMock(side_effect=[c.FoundInput(input=None), work.input])
    )
    images = SimpleNamespace(observe_source=AsyncMock(return_value=work.source))
    control = InputControl(
        database, images, SimpleNamespace(assert_versioning=AsyncMock())
    )
    request = c.FindInputRequest(
        job_id=work.input.job_id,
        task_id=work.input.task_id,
        actor_id=work.input.actor_id,
    )
    assert await control.accept(request) == work.input
    operation, reserved = database.call.await_args.args
    assert operation == "reserve_accepted_image_input"
    assert reserved.object_identity == work.source.object_identity
    assert reserved.source_snapshot == work.source.source_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason",
    [
        ("pending", "input_not_ready"),
        ("materializing", "input_not_ready"),
        ("failed", "source_changed"),
        ("expired", "input_expired"),
    ],
)
async def test_non_ready_input_never_signs_a_current_source(status, reason):
    work = input_work(status=status)
    database = SimpleNamespace(call=AsyncMock(return_value=work))
    storage = SimpleNamespace(sign_blob=AsyncMock(), verify_blob=AsyncMock())
    control = InputControl(database, object(), storage)
    with pytest.raises(c.InputFailure, match=reason):
        await control.reference(input_request(work))
    storage.sign_blob.assert_not_awaited()
    storage.verify_blob.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_uses_verified_blob_version():
    work = input_work()
    storage = SimpleNamespace(
        verify_blob=AsyncMock(),
        sign_blob=AsyncMock(return_value="http://s3.test/input?versionId=version-a"),
    )
    control = InputControl(
        SimpleNamespace(call=AsyncMock(return_value=work)), object(), storage
    )
    result = await control.reference(input_request(work))
    assert result.input.sha256 == work.blob.sha256
    assert result.version_id == work.blob.version_id
    storage.verify_blob.assert_awaited_once_with(work.blob)
    storage.sign_blob.assert_awaited_once_with(work.blob, expires_in=900)


@pytest.mark.asyncio
async def test_source_verification_cannot_recapture_changed_bytes():
    work = input_work()
    changed = work.source.model_copy(update={"source_generation": 2})
    images = SimpleNamespace(observe_internal_source=AsyncMock(return_value=changed))
    storage = SimpleNamespace(verify_blob=AsyncMock())
    control = InputControl(
        SimpleNamespace(call=AsyncMock(return_value=work)), images, storage
    )
    with pytest.raises(c.InputFailure, match="source_changed"):
        await control.verify_source(input_request(work))
    storage.verify_blob.assert_not_awaited()
