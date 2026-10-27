"""Local write sidecars preserve the original transport and permit lifetime."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import training_storage_io as io, training_write_accounting
from app.services import conditional_write_accounting, export_zip_accounting
from tests.test_training_storage_targets import target
from app.services import training_dataset_storage as owners
import uuid


def test_protocol_reuses_existing_evidence_owners():
    assert training_write_accounting.Observation is export_zip_accounting.Observation
    assert training_write_accounting.finish is conditional_write_accounting.finish


@pytest.mark.parametrize("accounted", [False, True])
def test_local_transport_owned_only_for_observed_write(accounted):
    options, events = [], []
    recorder = object() if accounted else None

    @contextmanager
    def client(**kwargs):
        options.append(kwargs)
        try:
            yield object()
        finally:
            events.append("closed")

    destination = SimpleNamespace(
        client=client, location_id=uuid.uuid4(), bucket="saved"
    )

    def callback(bound):
        assert bound._write_accounting is recorder
        assert bound.training_bucket == "saved"
        return "unchanged"

    assert io.physical(destination, callback, accounting=recorder) == "unchanged"
    assert options == ([{"owned": True}] if accounted else [{}])
    assert events == ["closed"] and not hasattr(io.storage_service, "_write_accounting")


@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("failure", [None, ValueError("business"), KeyboardInterrupt()])
async def test_run_finishes_after_original_callback_and_actual_permit(
    monkeypatch, settled, failure
):
    destination = target()
    owner = owners.resolve_ref(
        owners.make_ref(destination, 2, owners.prefix_for(uuid.uuid4(), uuid.uuid4()))
    )
    permit = SimpleNamespace(_settled=False)
    events = []
    rec = SimpleNamespace(
        finish=AsyncMock(
            side_effect=lambda **kw: events.append(("finish", kw["settled"]))
        )
    )

    async def original(current):
        assert current is permit
        events.append("acquired")

    async def run(*args, **kwargs):
        assert kwargs["accounting"] is rec
        await kwargs["on_acquired"](permit)
        events.append("native_closed")
        permit._settled = settled
        if failure is not None:
            raise failure
        return "result"

    monkeypatch.setattr(io.storage_access, "run", run)
    if failure is not None:
        with pytest.raises(type(failure)) as caught:
            await io.run(
                object(),
                owner,
                "training_test",
                object(),
                target=destination,
                accounting=rec,
                on_acquired=original,
            )
        assert caught.value is failure
    else:
        assert (
            await io.run(
                object(),
                owner,
                "training_test",
                object(),
                target=destination,
                accounting=rec,
                on_acquired=original,
            )
            == "result"
        )
    assert events == ["acquired", "native_closed", ("finish", settled)]
