from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_source.connectors.base import SourceObject
from aap_assets_source.import_contract import (
    ImportConnection,
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportLimits,
    ImportSourceObject,
    ImportState,
)
from aap_assets_source_import.database import EXPECTED_IMPORT_FUNCTIONS
from aap_assets_source_import.__main__ import _handle_failure
from aap_assets_source_import.workflow import (
    ImportWorkflow,
    LeaseKeeper,
    PersistenceBusy,
    WorkerStopping,
)
from aap_assets_source_import.workflow import (
    _collect_manifest,
    _validate_entry_scope,
    destination_relative_path,
)


def _state(
    *,
    max_files: int = 10,
    max_bytes: int = 100,
    stage: str = "enumerating",
    cursor: int = 0,
) -> ImportState:
    return ImportState(
        job_id=uuid4(),
        actor_id=uuid4(),
        dataset_id=uuid4(),
        connection_id=uuid4(),
        source_path="batch",
        recursive=True,
        include_globs=[],
        settings_snapshot=ImportLimits(
            dataset_import_max_files=max_files,
            dataset_import_max_total_bytes=max_bytes,
            version="v1-test",
        ),
        stage=stage,
        cursor=cursor,
        total=1 if stage == "importing" else None,
        total_bytes=1 if stage == "importing" else None,
        added=0,
        skipped=0,
        error_count=0,
        errors=[],
        linked_tasks=0,
        committed_bytes=0,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )


class _Source:
    def __init__(self, objects):
        self.objects = objects

    def list(self, path, recursive, include_globs=None):
        return iter(self.objects)


def _source_object(name: str, size: int = 1) -> SourceObject:
    return SourceObject(
        relpath=name,
        size=size,
        mtime=datetime(2026, 1, 1, tzinfo=timezone.utc),
        etag="listed-version",
    )


def test_manifest_collection_stops_at_file_count_limit() -> None:
    consumed = 0

    def infinite():
        nonlocal consumed
        index = 0
        while True:
            consumed += 1
            yield _source_object(f"file-{index}.jpg")
            index += 1

    class InfiniteSource:
        def list(self, path, recursive, include_globs=None):
            return infinite()

    with pytest.raises(ImportFailure, match="invalid_request"):
        _collect_manifest(InfiniteSource(), _state(max_files=3), lambda: False)
    assert consumed == 4


def test_manifest_collection_enforces_accepted_byte_snapshot() -> None:
    with pytest.raises(ImportFailure, match="invalid_request"):
        _collect_manifest(
            _Source([_source_object("a.bin", 6), _source_object("b.bin", 5)]),
            _state(max_bytes=10),
            lambda: False,
        )


@pytest.mark.parametrize(
    ("relpath", "source_path", "base_prefix", "expected"),
    [
        ("dataset-A/a/img.jpg", "dataset-A", "", "a/img.jpg"),
        ("dataset-A/b/img.png", "dataset-A/", "", "b/img.png"),
        ("a/img.jpg", "", "", "a/img.jpg"),
        ("dataset-A", "dataset-A", "", ""),
        ("dataset-A/a/x.jpg", "root/dataset-A", "root", "a/x.jpg"),
        ("dataset-A/a/x.jpg", "dataset-A", "root", "a/x.jpg"),
        ("batch/a.jpg", "/data/batch", "/data", "a.jpg"),
        ("batch/a.jpg", "batch", "/data", "a.jpg"),
    ],
)
def test_destination_relative_path_preserves_source_subdirectories(
    relpath, source_path, base_prefix, expected
) -> None:
    assert destination_relative_path(relpath, source_path, base_prefix) == expected


def test_native_entry_dataset_scope_is_checked_again_at_runtime() -> None:
    state = _state()
    assert state.lease_token is not None
    item_id = uuid4()
    publication_id = uuid4()
    entry = ImportEntry(
        job_id=state.job_id,
        lease_token=state.lease_token,
        index=0,
        item_id=item_id,
        publication_id=publication_id,
        file_name="nested/photo.jpg",
        file_path=f"dataset-sources/{uuid4()}/{item_id}/{publication_id}/nested/photo.jpg",
        file_type="image",
        source=ImportSourceObject(
            relpath="batch/nested/photo.jpg", size=1, mtime=None, etag="version"
        ),
    )
    with pytest.raises(ImportFailure, match="invalid_response"):
        _validate_entry_scope(entry, state, {"base_prefix": ""})


def _valid_entry(state: ImportState) -> ImportEntry:
    assert state.lease_token is not None
    item_id = uuid4()
    publication_id = uuid4()
    return ImportEntry(
        job_id=state.job_id,
        lease_token=state.lease_token,
        index=0,
        item_id=item_id,
        publication_id=publication_id,
        file_name="batch/photo.jpg",
        file_path=(
            f"dataset-sources/{state.dataset_id}/{item_id}/{publication_id}/batch/photo.jpg"
        ),
        file_type="image",
        source=ImportSourceObject(
            relpath="batch/photo.jpg", size=1, mtime=None, etag="etag"
        ),
    )


class _SettlementData:
    def __init__(
        self,
        state: ImportState,
        busy_attempts: int,
        failure_code: str = "dataset_busy",
    ) -> None:
        self.state = state
        self.busy_attempts = busy_attempts
        self.failure_code = failure_code
        self.calls = []

    async def complete_entry(self, *arguments):
        self.calls.append(arguments)
        if len(self.calls) <= self.busy_attempts:
            raise ImportFailure(self.failure_code)
        return self.state.model_copy(update={"cursor": 1})


class _SettlementDatabase:
    def __init__(self, data: _SettlementData) -> None:
        self.data = data

    @asynccontextmanager
    async def transaction(self):
        yield self.data


@pytest.mark.asyncio
async def test_settlement_retries_persistence_contention_with_same_receipt(
    monkeypatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_assets_source_import.workflow.asyncio.sleep", no_sleep)
    state = _state(stage="importing")
    entry = _valid_entry(state)
    completion = ImportEntryCompletion(status="error", error_code="source_changed")
    data = _SettlementData(state, busy_attempts=2)
    database = _SettlementDatabase(data)
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    completed = await workflow._complete_with_retry(state, entry, completion, keeper)

    assert completed.cursor == 1
    assert len(data.calls) == 3
    assert {call[3] for call in data.calls} == {entry.publication_id}
    assert {call[4] for call in data.calls} == {completion}


@pytest.mark.asyncio
async def test_exhausted_settlement_contention_leaves_cursor_for_recovery(
    monkeypatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_assets_source_import.workflow.asyncio.sleep", no_sleep)
    state = _state(stage="importing")
    entry = _valid_entry(state)
    completion = ImportEntryCompletion(status="error", error_code="source_changed")
    data = _SettlementData(state, busy_attempts=10)
    database = _SettlementDatabase(data)
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    with pytest.raises(PersistenceBusy):
        await workflow._complete_with_retry(state, entry, completion, keeper)
    assert len(data.calls) == 5
    assert state.cursor == 0


@pytest.mark.asyncio
async def test_operation_conflict_is_not_misclassified_as_transient_contention(
    monkeypatch,
) -> None:
    async def no_sleep(_seconds):
        pytest.fail("operation_conflict must not be retried")

    monkeypatch.setattr("aap_assets_source_import.workflow.asyncio.sleep", no_sleep)
    state = _state(stage="importing")
    entry = _valid_entry(state)
    completion = ImportEntryCompletion(status="error", error_code="source_changed")
    data = _SettlementData(state, busy_attempts=10, failure_code="operation_conflict")
    database = _SettlementDatabase(data)
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    with pytest.raises(ImportFailure, match="operation_conflict"):
        await workflow._complete_with_retry(state, entry, completion, keeper)
    assert len(data.calls) == 1


class _FatalFailureData:
    def __init__(self) -> None:
        self.calls = []

    async def finish(self, *arguments):
        self.calls.append(arguments)
        return SimpleNamespace(stage="failed")


class _FatalFailureDatabase:
    def __init__(self, data: _FatalFailureData) -> None:
        self.data = data

    @asynccontextmanager
    async def transaction(self):
        yield self.data


@pytest.mark.asyncio
async def test_operation_conflict_is_terminalized_instead_of_reclaimed() -> None:
    state = _state(stage="importing")
    assert state.lease_token is not None
    data = _FatalFailureData()
    database = _FatalFailureDatabase(data)

    await _handle_failure(database, state, ImportFailure("operation_conflict"))

    assert data.calls == [(state.job_id, state.lease_token, "operation_conflict")]


def test_database_role_is_limited_to_the_frozen_seven_capabilities() -> None:
    assert EXPECTED_IMPORT_FUNCTIONS == {
        "claim_dataset_import(uuid,integer,uuid)",
        "heartbeat_dataset_import(uuid,uuid,integer)",
        "read_dataset_import_connection(uuid,uuid,jsonb)",
        "save_dataset_import_manifest(uuid,uuid,jsonb)",
        "next_dataset_import_entry(uuid,uuid,uuid)",
        "complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)",
        "finish_dataset_import(uuid,uuid,text)",
    }


class _HeartbeatData:
    def __init__(self, failure_code: str | None = None) -> None:
        self.failure_code = failure_code
        self.heartbeat_calls = 0
        self.settled = 0
        self.called = asyncio.Event()

    async def heartbeat(self, *_arguments):
        self.heartbeat_calls += 1
        self.called.set()
        if self.failure_code is not None:
            raise ImportFailure(self.failure_code)
        return self.state

    async def complete_entry(self, *_arguments):
        self.settled += 1
        return self.state.model_copy(update={"cursor": 1})


class _HeartbeatDatabase:
    def __init__(self, data: _HeartbeatData, state: ImportState) -> None:
        self.data = data
        self.data.state = state

    @asynccontextmanager
    async def transaction(self):
        yield self.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code", ["import_cancelled", "job_terminal", "lease_lost"]
)
async def test_heartbeat_terminal_state_stops_worker_before_settlement(
    failure_code: str,
) -> None:
    state = _state(stage="importing")
    data = _HeartbeatData(failure_code=failure_code)
    database = _HeartbeatDatabase(data, state)
    keeper = LeaseKeeper(
        database,
        SimpleNamespace(heartbeat_seconds=0.01, lease_seconds=120),
        state,
        asyncio.Event(),
    )
    workflow = ImportWorkflow(database, object(), object())
    entry = _valid_entry(state)
    completion = ImportEntryCompletion(status="error", error_code="source_changed")

    async with keeper:
        await asyncio.wait_for(data.called.wait(), timeout=1)
        with pytest.raises(ImportFailure, match=failure_code):
            await workflow._complete_with_retry(state, entry, completion, keeper)

    assert data.heartbeat_calls == 1
    assert data.settled == 0


@pytest.mark.asyncio
async def test_shutdown_event_prevents_followup_work_without_releasing_lease() -> None:
    state = _state(stage="importing")
    data = _HeartbeatData()
    database = _HeartbeatDatabase(data, state)
    shutdown = asyncio.Event()
    keeper = LeaseKeeper(
        database,
        SimpleNamespace(heartbeat_seconds=1, lease_seconds=120),
        state,
        shutdown,
    )
    shutdown.set()

    with pytest.raises(WorkerStopping):
        keeper.check()
    assert data.heartbeat_calls == 0


def test_import_connection_uses_standard_base64_wrapped_fernet_bytes() -> None:
    from cryptography.fernet import Fernet

    from aap_assets_source.connectors.crypto import encrypt_secret

    key = Fernet.generate_key()
    encrypted = base64.b64encode(
        encrypt_secret({"access_key": "access", "secret_key": "secret"}, key)
    ).decode("ascii")
    connection = ImportConnection(
        job_id=uuid4(),
        lease_token=uuid4(),
        kind="s3",
        config={"endpoint": "http://example.test", "bucket": "incoming"},
        secret_enc=encrypted,
        allowlist=[],
    )
    workflow = ImportWorkflow(
        object(),
        object(),
        SimpleNamespace(connector_encryption_key=key.decode()),
    )

    assert workflow._decrypt_connection(connection) == {
        "access_key": "access",
        "secret_key": "secret",
    }


def test_manifest_wire_budget_stops_large_paths_before_materializing_all_files() -> (
    None
):
    state = _state(max_files=50_000, max_bytes=50_000)
    count = 0

    def long_paths():
        nonlocal count
        while True:
            count += 1
            yield SourceObject(
                relpath=f"{count:05d}-" + "x" * 4000,
                size=0,
                mtime=None,
                etag=None,
            )

    class LongPathSource:
        def list(self, path, recursive, include_globs=None):
            return long_paths()

    with pytest.raises(ImportFailure, match="invalid_request"):
        _collect_manifest(LongPathSource(), state, lambda: False)
    assert count < state.settings_snapshot.dataset_import_max_files
