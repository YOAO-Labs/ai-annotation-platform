"""Bounded daily counters count completed attempts, not fault transitions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import storage_observation_rates as rates
from app.services.content_snapshot import ContentError


def test_invalid_identity_metric_and_absent_attempt_are_safe():
    assert rates.begin(None, uuid.uuid4(), "original_read") is None
    assert rates.begin(uuid.uuid4(), "bad", "original_read") is None
    assert rates.begin(uuid.uuid4(), uuid.uuid4(), "scan") is None
    attempt = rates.begin(str(uuid.uuid4()), str(uuid.uuid4()), "original_read")
    assert isinstance(attempt.location_id, uuid.UUID)
    assert isinstance(attempt.source_id, uuid.UUID)


def test_attempt_is_consumed_once_across_threads():
    attempt = rates.begin(uuid.uuid4(), uuid.uuid4(), "preview_generate")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: attempt.consume(), range(40))) == 1


@pytest.mark.parametrize("code", sorted(rates.FAILURE_CODES))
def test_explicit_strict_source_failure_codes(code):
    assert rates.failure_outcome(ContentError(code)) == "failure"


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.CancelledError(),
        ContentError("configuration_changed"),
        ContentError("content_binding_changed"),
        ContentError("snapshot_capacity_exceeded"),
        ContentError("storage_original_retired"),
        ContentError("storage_maintenance_stopped"),
        ContentError("permission_revoked"),
        RuntimeError("private unknown /source"),
        OSError("private unknown /source"),
    ],
)
def test_admission_unknown_and_cancellation_are_not_source_failure(failure):
    assert rates.failure_outcome(failure) is None


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), PermissionError(), TimeoutError()]
)
def test_safe_native_failures(failure):
    assert rates.failure_outcome(failure) == "failure"


def test_unsettled_and_malformed_exception_metadata_are_excluded():
    failure = ContentError("path_unavailable")
    failure.storage_operation_unsettled = True
    assert rates.failure_outcome(failure) is None
    failure = RuntimeError()
    failure.code, failure.storage_fault_category = {}, []
    assert rates.failure_outcome(failure) is None
    failure = ContentError("connection_unavailable")
    failure.storage_fault_category = "access_denied"
    assert rates.failure_outcome(failure) == "failure"


async def test_none_exclusion_duplicate_and_failed_write_never_retry(monkeypatch):
    persist = AsyncMock(side_effect=RuntimeError("private sql /source"))
    warning = Mock()
    monkeypatch.setattr(rates, "_persist", persist)
    monkeypatch.setattr(rates.log, "warning", warning)
    assert not await rates.finish(None, None, "success")
    excluded = rates.begin(uuid.uuid4(), uuid.uuid4(), "original_read")
    assert not await rates.finish(None, excluded, None)
    assert not await rates.finish(None, excluded, "success")
    persist.assert_not_called()
    failed = rates.begin(uuid.uuid4(), uuid.uuid4(), "original_read")
    assert not await rates.finish(None, failed, "failure")
    assert not await rates.finish(None, failed, "failure")
    persist.assert_awaited_once()
    warning.assert_called_once_with("Storage rate observation could not be persisted")


async def test_cancellation_is_not_swallowed_or_retried(monkeypatch):
    persist = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(rates, "_persist", persist)
    attempt = rates.begin(uuid.uuid4(), uuid.uuid4(), "original_read")
    with pytest.raises(asyncio.CancelledError):
        await rates.finish(None, attempt, "success")
    assert not await rates.finish(None, attempt, "success")
    persist.assert_awaited_once()
