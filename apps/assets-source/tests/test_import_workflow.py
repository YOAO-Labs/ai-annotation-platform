from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import io

import pytest

from aap_assets_source.connectors import SourceChangedError
from aap_assets_source.connectors.base import SourceObject
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)
from aap_assets_source.import_contract import (
    ImportConnection,
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportLimits,
    ImportSourceObject,
    ImportState,
)
from aap_assets_images.contract import ImageFailure, ObjectIdentity
from aap_assets_source_import.__main__ import _handle_failure
from aap_assets_source_import.storage import SourceReadError
from aap_assets_source_import.workflow import (
    ImportWorkflow,
    LeaseUnavailable,
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


def _replace_state(state: ImportState, **updates) -> ImportState:
    value = state.model_dump(mode="python")
    value.update(updates)
    return ImportState.model_validate(value)


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
        _validate_entry_scope(entry, state, {"base_prefix": ""}, publication_id)


@pytest.mark.asyncio
async def test_wrong_scope_next_entry_rolls_back_before_opening_source() -> None:
    state = _state(stage="importing")
    database = _TransactionRecorder(_WrongScopeNextData(state))
    adapter = _NoOpenAdapter()
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    with pytest.raises(ImportFailure, match="invalid_response"):
        await workflow._copy_manifest(state, adapter, {"base_prefix": ""}, keeper)

    assert database.commits == 0
    assert database.rollbacks == 1
    assert adapter.opened == []
    assert state.cursor == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [0, 1])
async def test_null_next_entry_is_only_valid_at_manifest_end(cursor: int) -> None:
    state = _state(stage="importing")
    if cursor == 1:
        state = _replace_state(state, cursor=1, error_count=1)
    database = _TransactionRecorder(_NoEntryData())
    workflow = ImportWorkflow(database, object(), object())

    if cursor == 0:
        with pytest.raises(ImportFailure, match="invalid_response"):
            await workflow._next_entry(state, {"base_prefix": ""}, uuid4())
        assert database.commits == 0
        assert database.rollbacks == 1
    else:
        assert await workflow._next_entry(state, {"base_prefix": ""}, uuid4()) is None
        assert database.commits == 1
        assert database.rollbacks == 0


@pytest.mark.asyncio
async def test_wrong_scope_manifest_reply_rolls_back_before_commit() -> None:
    state = _state()
    database = _TransactionRecorder(_WrongScopeSaveData(state))
    workflow = ImportWorkflow(database, object(), object())

    with pytest.raises(ImportFailure, match="invalid_response"):
        await workflow._save_manifest(state, [])

    assert database.commits == 0
    assert database.rollbacks == 1


@pytest.mark.asyncio
async def test_wrong_scope_finish_reply_rolls_back_before_commit() -> None:
    state = _state(stage="importing")
    database = _TransactionRecorder(_WrongScopeFinishData(state))
    workflow = ImportWorkflow(database, object(), object())

    with pytest.raises(ImportFailure, match="invalid_response"):
        await workflow._finish_completed(state)

    assert database.commits == 0
    assert database.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_error", ["wrong_scope", "jumped_cursor"])
async def test_invalid_completion_reply_rolls_back_without_advancing(
    reply_error: str,
) -> None:
    state = _state(stage="importing")
    if reply_error == "jumped_cursor":
        state = _replace_state(state, total=3, total_bytes=3)
    entry = _valid_entry(state)
    database = _TransactionRecorder(
        _SettlementReplyData(state, jump_cursor=reply_error == "jumped_cursor")
    )
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())
    completion = ImportEntryCompletion(status="error", error_code="source_changed")

    with pytest.raises(ImportFailure, match="invalid_response"):
        await workflow._complete_with_retry(state, entry, completion, keeper)

    assert database.commits == 0
    assert database.rollbacks == 1
    assert state.cursor == 0


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
        file_name="photo.jpg",
        file_path=(
            f"dataset-sources/{state.dataset_id}/{item_id}/{publication_id}/photo.jpg"
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


class _RejectedSourceData:
    def __init__(self, state: ImportState) -> None:
        self.state = state
        self.next_publications: list = []
        self.completions: list = []
        self.busy_once = True
        self.image_admission = _ImageAdmission()
        self.entry = None

    async def next_entry(self, _job_id, _lease_token, publication_id):
        self.next_publications.append(publication_id)
        if len(self.next_publications) == 1:
            raise ImportFailure("invalid_source_object")
        item_id = uuid4()
        self.entry = ImportEntry(
            job_id=self.state.job_id,
            lease_token=self.state.lease_token,
            index=self.state.cursor,
            item_id=item_id,
            publication_id=publication_id,
            file_name="valid.jpg",
            file_path=(
                f"dataset-sources/{self.state.dataset_id}/{item_id}/"
                f"{publication_id}/valid.jpg"
            ),
            file_type="image",
            source=ImportSourceObject(
                relpath="batch/valid.jpg", size=3, mtime=None, etag="listed-etag"
            ),
        )
        return self.entry

    async def complete_entry(
        self, _job_id, _lease_token, index, publication_id, completion
    ):
        self.completions.append((index, publication_id, completion))
        if completion.status == "error" and self.busy_once:
            self.busy_once = False
            raise ImportFailure("dataset_busy")
        updates = {
            "cursor": index + 1,
            "error_count": self.state.error_count
            + (1 if completion.status == "error" else 0),
            "added": self.state.added + (1 if completion.status == "copied" else 0),
            "committed_bytes": self.state.committed_bytes + (completion.file_size or 0),
        }
        self.state = self.state.model_copy(update=updates)
        return self.state


class _RejectedSourceDatabase:
    def __init__(self, data: _RejectedSourceData) -> None:
        self.data = data

    def __getattr__(self, name):
        return getattr(self.data, name)

    async def allocate(self, request):
        return _managed_target(request)

    async def open_operation(self, creation_id):
        from aap_assets_source.managed_original_contract import (
            ManagedOriginalOperation,
        )

        return ManagedOriginalOperation(operation_id=uuid4(), mode="write")

    async def close_operation(self, creation_id, operation_id, physical_settled):
        return None

    async def original_prepared(self, creation_id, prepared):
        return None

    async def original_writing(self, creation_id):
        return None

    async def original_written(self, creation_id, receipt):
        return None

    async def original_verified(self, creation_id, verification):
        return None

    async def fail_original(self, creation_id, outcome, error_code, evidence):
        return None

    @asynccontextmanager
    async def transaction(self):
        yield self.data


def _managed_target(request: dict) -> ManagedOriginalMaterial:
    from uuid import UUID

    file_path = request["file_path"]
    key_prefix = ""
    return ManagedOriginalMaterial(
        creation_id=uuid4(),
        dataset_id=UUID(request["dataset_id"]),
        location_id=UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"),
        bucket="datasets",
        object_key=file_path,
        key_prefix=key_prefix,
        endpoint_url="http://s3",
        storage_identity="a" * 64,
        config_revision=1,
        binding_generation=1,
        max_bytes=request["max_bytes"],
        content_type=request["content_type"],
        origin=request["origin"],
        mode=request["mode"],
        publication_id=UUID(request["publication_id"]),
        file_path=file_path,
    )


def _managed_result(file_size: int, content_hash: str, *, sha256: str, version="v1"):
    proof = ManagedOriginalProof.verified(
        mode="write",
        sha256=sha256,
        md5=content_hash,
        size_bytes=file_size,
        etag='"etag"',
        version_id=version,
        request_id="request",
        prepared={"mode": "write"},
        verification={
            "sha256": sha256,
            "md5": content_hash,
            "size_bytes": file_size,
            "etag": '"etag"',
            "version_id": version,
        },
    )
    from types import SimpleNamespace

    return SimpleNamespace(
        file_size=file_size,
        content_hash=content_hash,
        dimensions=None,
        object_identity=(
            ObjectIdentity(etag="etag", byte_size=file_size, version_id=version)
            if file_size and content_hash
            else None
        ),
        proof=proof,
    )


class _TransactionRecorder:
    def __init__(self, data) -> None:
        self.data = data
        self.commits = 0
        self.rollbacks = 0
        self.originals: list[tuple] = []

    async def allocate(self, request):
        return _managed_target(request)

    async def open_operation(self, creation_id):
        from uuid import uuid4

        from aap_assets_source.managed_original_contract import (
            ManagedOriginalOperation,
        )

        return ManagedOriginalOperation(operation_id=uuid4(), mode="write")

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None:
        self.originals.append((creation_id, "closed"))

    async def original_prepared(self, creation_id, prepared) -> None:
        self.originals.append((creation_id, "prepared"))

    async def original_writing(self, creation_id) -> None:
        self.originals.append((creation_id, "writing"))

    async def original_written(self, creation_id, receipt) -> None:
        self.originals.append((creation_id, "written"))

    async def original_verified(self, creation_id, verification) -> None:
        self.originals.append((creation_id, "verified"))

    async def fail_original(self, creation_id, outcome, error_code, evidence) -> None:
        self.originals.append((creation_id, outcome))

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.data
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


@pytest.mark.asyncio
async def test_image_admission_failure_rolls_back_item_settlement_and_cursor():
    state = _state(stage="importing")
    entry = _valid_entry(state)
    events = []

    class _Data:
        image_admission = None

        async def complete_entry(self, *_arguments):
            events.append("complete_entry")
            return state.model_copy(update={"cursor": 1, "added": 1})

    class _ImageAdmission:
        async def admit_dataset_item(self, **_request):
            events.append("image_admission")
            raise ImageFailure("source_changed")

    data = _Data()
    data.image_admission = _ImageAdmission()
    database = _TransactionRecorder(data)
    workflow = ImportWorkflow(database, object(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    with pytest.raises(ImportFailure, match="source_changed"):
        await workflow._complete_with_retry(
            state,
            entry,
            ImportEntryCompletion(
                status="copied",
                file_size=1,
                content_hash=hashlib.md5(b"x", usedforsecurity=False).hexdigest(),
            ),
            keeper,
            object_identity=ObjectIdentity(etag="final", byte_size=1),
        )

    assert database.commits == 0
    assert database.rollbacks == 1
    assert state.cursor == 0
    assert events == ["complete_entry", "image_admission"]


class _WrongScopeNextData:
    def __init__(self, state: ImportState) -> None:
        self.state = state

    async def next_entry(self, _job_id, _lease_token, publication_id):
        entry = _valid_entry(self.state)
        return ImportEntry(
            job_id=entry.job_id,
            lease_token=entry.lease_token,
            index=entry.index,
            item_id=entry.item_id,
            publication_id=publication_id,
            file_name=entry.file_name,
            file_path=(
                f"dataset-sources/{uuid4()}/{entry.item_id}/"
                f"{publication_id}/{entry.file_name}"
            ),
            file_type=entry.file_type,
            source=entry.source,
        )


class _NoEntryData:
    async def next_entry(self, *_arguments):
        return None


class _WrongScopeSaveData:
    def __init__(self, state: ImportState) -> None:
        self.state = state

    async def save_manifest(self, _job_id, _lease_token, objects):
        saved = _replace_state(
            self.state,
            actor_id=uuid4(),
            stage="importing",
            total=len(objects),
            total_bytes=0,
        )
        return saved


class _WrongScopeFinishData:
    def __init__(self, state: ImportState, *, stage: str = "completed") -> None:
        self.state = state
        self.stage = stage

    async def finish(self, *_arguments):
        return _replace_state(
            self.state,
            dataset_id=uuid4(),
            stage=self.stage,
            cursor=self.state.total if self.stage == "completed" else self.state.cursor,
            error_count=(
                self.state.total
                if self.stage == "completed"
                else self.state.error_count
            ),
            lease_token=None,
            lease_expires_at=None,
        )


class _SettlementReplyData:
    def __init__(self, state: ImportState, *, jump_cursor: bool = False) -> None:
        self.state = state
        self.jump_cursor = jump_cursor

    async def complete_entry(self, _job_id, _token, index, _publication, _completion):
        cursor = index + (2 if self.jump_cursor else 1)
        return _replace_state(
            self.state,
            actor_id=uuid4() if not self.jump_cursor else self.state.actor_id,
            cursor=cursor,
            error_count=cursor,
        )


class _NoOpenAdapter:
    def __init__(self) -> None:
        self.opened = []

    def open(self, *_args, **_kwargs):
        self.opened.append(True)
        raise AssertionError("wrong-scope entry must be rejected before source I/O")


class _ValidOnlyAdapter:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def open(self, relpath, *, etag=None):
        self.opened.append(relpath)
        assert etag == "listed-etag"
        return io.BytesIO(b"img")


class _ValidOnlyStorage:
    async def transfer(
        self,
        _stream,
        entry,
        *,
        target,
        byte_budget,
        before_io,
        on_bytes,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ):
        assert entry.source.relpath == "batch/valid.jpg"
        assert target.object_key == entry.file_path
        assert byte_budget >= 3
        before_io()
        on_bytes(3)
        return _managed_result(
            3,
            hashlib.md5(b"img", usedforsecurity=False).hexdigest(),
            sha256=hashlib.sha256(b"img").hexdigest(),
        )


class _ImageAdmission:
    def __init__(self):
        self.calls = []

    async def admit_dataset_item(self, **request):
        self.calls.append(request)
        return None


class _BudgetData:
    def __init__(
        self,
        state: ImportState,
        entries: dict[int, tuple[str, bytes]],
        *,
        duplicates: set[int] | None = None,
    ) -> None:
        self.state = state
        self.entries = entries
        self.duplicates = duplicates or set()

    async def next_entry(self, _job_id, _lease_token, publication_id):
        index = self.state.cursor
        name, payload = self.entries[index]
        item_id = uuid4()
        file_name = name
        return ImportEntry(
            job_id=self.state.job_id,
            lease_token=self.state.lease_token,
            index=index,
            item_id=item_id,
            publication_id=publication_id,
            file_name=file_name,
            file_path=(
                f"dataset-sources/{self.state.dataset_id}/{item_id}/"
                f"{publication_id}/{file_name}"
            ),
            file_type="other",
            source=ImportSourceObject(
                relpath=f"batch/{name}",
                size=len(payload),
                mtime=None,
                etag=f"etag-{index}",
            ),
        )

    async def complete_entry(
        self, _job_id, _lease_token, index, _publication_id, completion
    ):
        cursor = index + 1
        updates = {"cursor": cursor}
        if completion.status == "error":
            updates["error_count"] = self.state.error_count + 1
        else:
            updates["committed_bytes"] = (
                self.state.committed_bytes + completion.file_size
            )
            counter = "skipped" if index in self.duplicates else "added"
            updates[counter] = getattr(self.state, counter) + 1
        self.state = _replace_state(self.state, **updates)
        return self.state


class _BudgetAdapter:
    def __init__(self, entries: dict[int, tuple[str, bytes]]) -> None:
        self.entries = entries
        self.opened: list[str] = []

    def open(self, relpath, *, etag=None):
        self.opened.append(relpath)
        index = int(relpath.split("/")[-1].split(".")[0].removeprefix("item"))
        assert etag == f"etag-{index}"
        return io.BytesIO(self.entries[index][1])


class _BudgetStorage:
    def __init__(self, *, fail_after: dict[int, int] | None = None) -> None:
        self.fail_after = fail_after or {}
        self.budgets: list[int] = []

    async def transfer(
        self,
        stream,
        entry,
        *,
        target,
        byte_budget,
        before_io,
        on_bytes,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ):
        self.budgets.append(byte_budget)
        before_io()
        if entry.source.size > byte_budget:
            raise SourceChangedError("source exceeds current byte budget")
        index = int(
            entry.source.relpath.split("/")[-1].split(".")[0].removeprefix("item")
        )
        if index in self.fail_after:
            chunk = stream.read(self.fail_after[index])
            on_bytes(len(chunk))
            raise SourceReadError("simulated partial source read")
        payload = stream.read()
        on_bytes(len(payload))
        return _managed_result(
            len(payload),
            hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            sha256=hashlib.sha256(payload).hexdigest(),
        )


@pytest.mark.asyncio
async def test_committed_success_bytes_are_not_counted_again_in_local_budget() -> None:
    state = _state(stage="importing", max_bytes=6).model_copy(
        update={"total": 2, "total_bytes": 6}
    )
    entries = {0: ("item0.dat", b"abc"), 1: ("item1.dat", b"def")}
    data = _BudgetData(state, entries, duplicates={1})
    database = _TransactionRecorder(data)
    adapter = _BudgetAdapter(entries)
    storage = _BudgetStorage()
    workflow = ImportWorkflow(database, storage, object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    await workflow._copy_manifest(state, adapter, {"base_prefix": ""}, keeper)

    assert storage.budgets == [6, 3]
    assert data.state.cursor == 2
    assert data.state.added == 1
    assert data.state.skipped == 1
    assert data.state.committed_bytes == 6


@pytest.mark.asyncio
async def test_partial_failed_read_reduces_same_worker_budget() -> None:
    state = _state(stage="importing", max_bytes=6).model_copy(
        update={"total": 2, "total_bytes": 6}
    )
    entries = {0: ("item0.dat", b"ab"), 1: ("item1.dat", b"wxyz")}
    data = _BudgetData(state, entries)
    database = _TransactionRecorder(data)
    storage = _BudgetStorage(fail_after={0: 2})
    workflow = ImportWorkflow(database, storage, object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    await workflow._copy_manifest(
        state, _BudgetAdapter(entries), {"base_prefix": ""}, keeper
    )

    assert storage.budgets == [6, 4]
    assert data.state.cursor == 2
    assert data.state.error_count == 1
    assert data.state.added == 1
    assert data.state.committed_bytes == 4


@pytest.mark.asyncio
async def test_reclaimed_worker_restarts_partial_attempt_budget_from_durable_bytes() -> (
    None
):
    after_partial_error = _state(stage="importing", max_bytes=6).model_copy(
        update={"cursor": 1, "total": 2, "total_bytes": 6, "error_count": 1}
    )
    remaining = {1: ("item1.dat", b"wxyz")}
    data = _BudgetData(after_partial_error, remaining)
    database = _TransactionRecorder(data)
    storage = _BudgetStorage()
    workflow = ImportWorkflow(database, storage, object())
    keeper = LeaseKeeper(database, object(), after_partial_error, asyncio.Event())

    await workflow._copy_manifest(
        after_partial_error, _BudgetAdapter(remaining), {"base_prefix": ""}, keeper
    )

    assert storage.budgets == [6]
    assert data.state.cursor == 2
    assert data.state.error_count == 1
    assert data.state.committed_bytes == 4


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
async def test_unrepresentable_source_name_is_recorded_and_next_file_is_copied(
    monkeypatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("aap_assets_source_import.workflow.asyncio.sleep", no_sleep)
    state = _state(stage="importing").model_copy(update={"total": 2, "total_bytes": 8})
    data = _RejectedSourceData(state)
    database = _RejectedSourceDatabase(data)
    adapter = _ValidOnlyAdapter()
    workflow = ImportWorkflow(database, _ValidOnlyStorage(), object())
    keeper = LeaseKeeper(database, object(), state, asyncio.Event())

    await workflow._copy_manifest(state, adapter, {"base_prefix": ""}, keeper)

    assert len(data.next_publications) == 2
    invalid_settlements = [item for item in data.completions if item[0] == 0]
    assert len(invalid_settlements) == 2
    assert {item[1] for item in invalid_settlements} == {data.next_publications[0]}
    assert {item[2].error_code for item in invalid_settlements} == {
        "invalid_source_object"
    }
    assert data.completions[-1][0] == 1
    assert data.completions[-1][2].status == "copied"
    assert adapter.opened == ["batch/valid.jpg"]
    assert data.state.cursor == 2
    assert data.state.error_count == 1
    assert data.state.added == 1
    assert len(data.image_admission.calls) == 1
    assert data.entry is not None
    assert data.image_admission.calls[0]["item_id"] == data.entry.item_id
    assert data.image_admission.calls[0]["publication_id"] == data.entry.publication_id
    assert data.image_admission.calls[0]["object_identity"] == ObjectIdentity(
        etag="etag", byte_size=3, version_id="v1"
    )


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
    def __init__(self, state: ImportState) -> None:
        self.state = state
        self.calls = []

    async def finish(self, *arguments):
        self.calls.append(arguments)
        return _replace_state(
            self.state,
            stage="failed",
            lease_token=None,
            lease_expires_at=None,
        )


class _FatalFailureDatabase:
    def __init__(self, data: _FatalFailureData) -> None:
        self.data = data
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.data
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


@pytest.mark.asyncio
async def test_operation_conflict_is_terminalized_instead_of_reclaimed() -> None:
    state = _state(stage="importing")
    assert state.lease_token is not None
    data = _FatalFailureData(state)
    database = _FatalFailureDatabase(data)

    await _handle_failure(database, state, ImportFailure("operation_conflict"))

    assert data.calls == [(state.job_id, state.lease_token, "operation_conflict")]
    assert database.commits == 1
    assert database.rollbacks == 0


@pytest.mark.asyncio
async def test_wrong_scope_failure_finish_reply_rolls_back_before_commit() -> None:
    state = _state(stage="importing")
    database = _TransactionRecorder(_WrongScopeFinishData(state, stage="failed"))

    await _handle_failure(database, state, ImportFailure("operation_conflict"))

    assert database.commits == 0
    assert database.rollbacks == 1


class _HeartbeatData:
    def __init__(
        self,
        failure_code: str | None = None,
        *,
        failures_before_success: int | None = None,
    ) -> None:
        self.failure_code = failure_code
        self.failures_before_success = failures_before_success
        self.heartbeat_calls = 0
        self.heartbeat_arguments = []
        self.settled = 0
        self.called = asyncio.Event()
        self.succeeded = asyncio.Event()

    async def heartbeat(self, job_id, lease_token, lease_seconds):
        self.heartbeat_calls += 1
        self.heartbeat_arguments.append((job_id, lease_token, lease_seconds))
        self.called.set()
        if self.failure_code is not None and (
            self.failures_before_success is None
            or self.heartbeat_calls <= self.failures_before_success
        ):
            raise ImportFailure(self.failure_code)
        self.state = self.state.model_copy(
            update={
                "lease_expires_at": datetime.now(timezone.utc)
                + timedelta(seconds=lease_seconds)
            }
        )
        self.succeeded.set()
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


class _BlockedHeartbeatData(_HeartbeatData):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = asyncio.Event()

    async def heartbeat(self, job_id, lease_token, lease_seconds):
        self.heartbeat_calls += 1
        self.heartbeat_arguments.append((job_id, lease_token, lease_seconds))
        self.called.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


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
async def test_dataset_busy_heartbeat_retries_same_lease_until_renewed() -> None:
    state = _state(stage="importing")
    data = _HeartbeatData("dataset_busy", failures_before_success=2)
    database = _HeartbeatDatabase(data, state)
    keeper = LeaseKeeper(
        database,
        SimpleNamespace(heartbeat_seconds=0.001, lease_seconds=120),
        state,
        asyncio.Event(),
    )

    async with keeper:
        await asyncio.wait_for(data.succeeded.wait(), timeout=1)
        keeper.check()

    assert data.heartbeat_calls == 3
    assert data.heartbeat_arguments == [
        (state.job_id, state.lease_token, 120),
        (state.job_id, state.lease_token, 120),
        (state.job_id, state.lease_token, 120),
    ]
    assert data.state.lease_expires_at > state.lease_expires_at


@pytest.mark.asyncio
async def test_persistent_dataset_busy_stops_before_known_lease_expiry() -> None:
    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    state = _replace_state(
        _state(stage="importing"),
        lease_expires_at=lease_expires_at,
    )
    data = _HeartbeatData("dataset_busy")
    keeper = LeaseKeeper(
        _HeartbeatDatabase(data, state),
        SimpleNamespace(heartbeat_seconds=0.001, lease_seconds=120),
        state,
        asyncio.Event(),
    )
    started = asyncio.get_running_loop().time()
    stopped_before_expiry = False

    async with keeper:
        await asyncio.wait_for(data.called.wait(), timeout=0.5)
        for _ in range(400):
            try:
                keeper.check()
            except LeaseUnavailable:
                stopped_before_expiry = datetime.now(timezone.utc) < lease_expires_at
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("persistent heartbeat contention must stop by lease expiry")

    assert data.heartbeat_calls >= 1
    assert stopped_before_expiry
    assert asyncio.get_running_loop().time() - started < 2.5
    assert data.succeeded.is_set() is False


@pytest.mark.asyncio
async def test_blocked_heartbeat_is_cancelled_at_known_lease_expiry() -> None:
    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.1)
    state = _replace_state(_state(stage="importing"), lease_expires_at=lease_expires_at)
    data = _BlockedHeartbeatData()
    keeper = LeaseKeeper(
        _HeartbeatDatabase(data, state),
        SimpleNamespace(heartbeat_seconds=0.001, lease_seconds=120),
        state,
        asyncio.Event(),
    )
    started = asyncio.get_running_loop().time()

    async with keeper:
        await asyncio.wait_for(data.called.wait(), timeout=0.2)
        await asyncio.wait_for(data.cancelled.wait(), timeout=0.3)
        with pytest.raises(LeaseUnavailable):
            keeper.check()
        assert keeper.stop_requested_from_thread()

    assert data.heartbeat_calls == 1
    assert asyncio.get_running_loop().time() - started < 0.5


@pytest.mark.asyncio
async def test_shutdown_during_busy_heartbeat_retry_stops_without_another_attempt() -> (
    None
):
    state = _state(stage="importing")
    data = _HeartbeatData("dataset_busy")
    shutdown = asyncio.Event()
    keeper = LeaseKeeper(
        _HeartbeatDatabase(data, state),
        SimpleNamespace(heartbeat_seconds=0.001, lease_seconds=120),
        state,
        shutdown,
    )

    async with keeper:
        await asyncio.wait_for(data.called.wait(), timeout=1)
        shutdown.set()
        with pytest.raises(WorkerStopping):
            keeper.check()

    assert data.heartbeat_calls == 1


@pytest.mark.asyncio
async def test_wrong_scope_heartbeat_reply_rolls_back_before_commit() -> None:
    state = _state(stage="importing")
    data = _HeartbeatData()
    data.state = _replace_state(state, actor_id=uuid4())
    database = _TransactionRecorder(data)
    keeper = LeaseKeeper(
        database,
        SimpleNamespace(heartbeat_seconds=0.01, lease_seconds=120),
        state,
        asyncio.Event(),
    )

    async with keeper:
        await asyncio.wait_for(data.called.wait(), timeout=1)
        for _ in range(100):
            try:
                keeper.check()
            except ImportFailure as exc:
                assert exc.code == "invalid_response"
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("wrong-scope heartbeat must stop the worker")

    assert database.commits == 0
    assert database.rollbacks == 1


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
