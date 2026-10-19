"""Manifest preparation progresses in bounded work without changing admission."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_input_manifests import contract as c
from aap_assets_inputs import contract as inputs
from app.services.accepted_input_manifests import (
    InputManifestGateway,
    SqlAlchemyManifestDatabase,
    get_input_manifest_capture,
)

pytestmark = pytest.mark.asyncio


def _header(count):
    return c.ManifestHeader(
        job_id=uuid4(),
        actor_id=uuid4(),
        project_id=uuid4(),
        selection=c.ManifestSelection(predict_mode="overwrite"),
        selection_digest="a" * 64,
        candidate_count=count,
        selected_count=count,
        prepared_count=0,
        status="preparing",
        ready_count=None,
        failed_count=None,
        sealed_digest=None,
        created_at=datetime.now(timezone.utc),
        sealed_at=None,
    )


def _member(header, ordinal):
    task_id = uuid4()
    return c.ManifestMember(
        ordinal=ordinal,
        task_id=task_id,
        project_id=header.project_id,
        task_version=4,
        task_source_generation=2,
        dataset_item_id=None,
        task_source_snapshot="b" * 64,
        file_path="images/original.png",
        file_type="image",
        state="unprepared",
        input=None,
        error=None,
        source={
            "owner": {"kind": "task", "id": task_id},
            "task_id": task_id,
            "project_id": header.project_id,
            "dataset_id": None,
            "item_id": None,
            "source_generation": 2,
            "source_snapshot": "c" * 64,
            "object_identity": None,
            "source_drifted": False,
            "upload_ready": True,
            "file_path": "images/original.png",
            "file_name": "original.png",
            "file_type": "image",
            "bucket_kind": "legacy",
            "file_size": None,
            "content_hash": None,
            "width": 2,
            "height": 3,
            "thumbnail_path": None,
            "blurhash": None,
            "thumbnail_source_generation": None,
            "thumbnail_error": None,
        },
    )


class _Inputs:
    def __init__(self):
        self.records = {}
        self.copies = []
        self.error = None

    async def record(self, request):
        return self.records[request.input_id]

    async def ensure_reference(self, request):
        self.copies.append(request.input_id)
        if self.error:
            raise inputs.InputFailure(self.error)
        record = self.records[request.input_id]
        self.records[request.input_id] = record.model_copy(
            update={"status": "ready", "sha256": "d" * 64, "byte_size": 6}
        )
        return SimpleNamespace(input=self.records[request.input_id])


class _Manifests:
    def __init__(self, count, original_inputs):
        self.header = _header(count)
        self.members = [_member(self.header, index + 1) for index in range(count)]
        self.inputs = original_inputs
        self.prepares, self.pages, self.seals = [], [], 0

    async def record(self, _request):
        return self.header

    async def page(self, request):
        self.pages.append(request)
        end = min(request.cursor + request.page_size, len(self.members))
        return c.ManifestPage(
            manifest=self.header,
            cursor=request.cursor,
            next_cursor=end if end < len(self.members) else None,
            members=self.members[request.cursor : end],
        )

    async def prepare(self, request):
        self.prepares.append(request)
        member = self.members[request.ordinal - 1]
        record = inputs.InputRecord(
            input_id=uuid4(),
            job_id=request.job_id,
            actor_id=request.actor_id,
            project_id=request.project_id,
            task_id=request.task_id,
            status="pending",
            task_version=member.task_version,
            task_source_generation=member.task_source_generation,
            dataset_item_id=None,
            task_source_snapshot=member.task_source_snapshot,
            source_owner=member.source.owner,
            source_generation=member.source.source_generation,
            source_snapshot=member.source.source_snapshot,
            byte_size=None,
            sha256=None,
            error=None,
            created_at=self.header.created_at,
            retire_after=None,
        )
        self.inputs.records[record.input_id] = record
        member = member.model_copy(update={"state": "preparing", "input": record})
        self.members[request.ordinal - 1] = member
        self.header = self.header.model_copy(
            update={"prepared_count": self.header.prepared_count + 1}
        )
        return c.ManifestMemberReceipt(
            **request.model_dump(exclude={"ordinal", "task_id"}), member=member
        )

    async def seal(self, _request):
        self.seals += 1
        for index, member in enumerate(self.members):
            if member.state == "failed":
                continue
            record = (
                self.inputs.records.get(member.input.input_id) if member.input else None
            )
            if record is None or record.status not in {"ready", "failed", "expired"}:
                raise c.ManifestFailure("manifest_not_ready")
            self.members[index] = member.model_copy(
                update={"state": "ready", "input": record}
            )
        failed = sum(member.state == "failed" for member in self.members)
        self.header = self.header.model_copy(
            update={
                "prepared_count": len(self.members),
                "status": "sealed",
                "ready_count": len(self.members) - failed,
                "failed_count": failed,
                "sealed_digest": "e" * 64,
                "sealed_at": datetime.now(timezone.utc),
            }
        )
        return self.header


def _scope(header):
    return c.ManifestScope(
        **header.model_dump(include={"job_id", "actor_id", "project_id"})
    )


async def test_large_selection_progresses_without_a_total_cap_or_repreparing_inputs():
    original = _Inputs()
    owner = _Manifests(1503, original)
    gateway = InputManifestGateway(owner, original)
    for delivery in range(16):
        before = len(owner.prepares)
        if delivery < 15:
            with pytest.raises(c.ManifestFailure, match="manifest_not_ready"):
                await gateway.ensure_sealed(
                    _scope(owner.header), selection_digest="a" * 64
                )
        else:
            sealed = await gateway.ensure_sealed(
                _scope(owner.header), selection_digest="a" * 64
            )
        assert len(owner.prepares) - before <= 100
    assert sealed.status == "sealed" and sealed.ready_count == 1503
    assert len(owner.prepares) == len(original.copies) == 1503
    assert len(set(original.copies)) == 1503
    assert all(request.page_size <= 100 for request in owner.pages)
    assert len((await owner.page(owner.pages[0])).members) == 100
    assert owner.seals == 1


async def test_sealed_replay_returns_historical_barrier_without_storage_io():
    original = _Inputs()
    owner = _Manifests(0, original)
    gateway = InputManifestGateway(owner, original)
    sealed = await gateway.ensure_sealed(
        _scope(owner.header), selection_digest="a" * 64
    )
    assert (
        await gateway.ensure_sealed(_scope(owner.header), selection_digest="a" * 64)
        == sealed
    )
    assert not owner.pages and not owner.prepares and not original.copies
    assert owner.seals == 1


async def test_pending_copy_defers_without_sealing_or_recapturing_its_source():
    original = _Inputs()
    original.error = "input_not_ready"
    owner = _Manifests(1, original)
    gateway = InputManifestGateway(owner, original)
    for _ in range(2):
        with pytest.raises(c.ManifestFailure, match="manifest_not_ready"):
            await gateway.ensure_sealed(_scope(owner.header), selection_digest="a" * 64)
    assert len(owner.prepares) == 1
    assert len(set(original.copies)) == 1
    original.error = None
    assert (
        await gateway.ensure_sealed(_scope(owner.header), selection_digest="a" * 64)
    ).ready_count == 1


@pytest.mark.parametrize(
    "code", ["permission_denied", "job_not_active", "invalid_response"]
)
async def test_global_copy_errors_are_not_readiness_waits(code):
    original = _Inputs()
    original.error = code
    owner = _Manifests(1, original)
    with pytest.raises(c.ManifestFailure, match=code):
        await InputManifestGateway(owner, original).ensure_sealed(
            _scope(owner.header), selection_digest="a" * 64
        )
    assert owner.seals == 0


async def test_wrong_manifest_digest_is_rejected_before_preparation():
    original = _Inputs()
    owner = _Manifests(1, original)
    with pytest.raises(c.ManifestFailure, match="manifest_scope_mismatch"):
        await InputManifestGateway(owner, original).ensure_sealed(
            _scope(owner.header), selection_digest="f" * 64
        )
    assert not owner.prepares and not original.copies


class _Database:
    def __init__(self, raw):
        self.raw, self.events = raw, []

    async def flush(self):
        self.events.append("flush")

    @asynccontextmanager
    async def begin_nested(self):
        self.events.append("savepoint")
        try:
            yield
        except Exception:
            self.events.append("savepoint rollback")
            raise
        self.events.append("savepoint release")

    async def scalar(self, statement, parameters):
        self.events.append((str(statement), parameters))
        return self.raw

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


async def test_capture_uses_caller_transaction_and_canonical_selector():
    header = _header(0)
    db = _Database(
        {
            "schema": c.MANIFEST_SCHEMA,
            "ok": True,
            "data": header.model_dump(mode="json"),
        }
    )
    result = await get_input_manifest_capture()(db).capture(
        c.CaptureManifestRequest(
            **_scope(header).model_dump(), selection=header.selection
        )
    )
    assert result == header
    assert db.events[0:2] == ["flush", "savepoint"]
    statement, parameters = db.events[2]
    assert "aap_assets_v1.capture_accepted_image_input_manifest(" in statement
    assert json.loads(parameters["selection"]) == {
        "batch_id": None,
        "task_ids": None,
        "predict_mode": "overwrite",
    }
    assert db.events[-1] == "savepoint release"
    assert "commit" not in db.events and "rollback" not in db.events


async def test_bad_capture_receipt_rolls_back_only_its_savepoint():
    expected = _header(0)
    wrong = _header(0)
    db = _Database(
        {"schema": c.MANIFEST_SCHEMA, "ok": True, "data": wrong.model_dump(mode="json")}
    )
    with pytest.raises(c.ManifestFailure, match="invalid_response"):
        await get_input_manifest_capture()(db).capture(
            c.CaptureManifestRequest(
                **_scope(expected).model_dump(), selection=expected.selection
            )
        )
    assert db.events[-1] == "savepoint rollback"
    assert "commit" not in db.events and "rollback" not in db.events


async def test_control_binding_cannot_capture_a_workflow_manifest():
    header = _header(0)
    db = _Database(None)
    with pytest.raises(c.ManifestFailure, match="invalid_request"):
        await SqlAlchemyManifestDatabase(db).call(
            "capture_accepted_image_input_manifest",
            c.CaptureManifestRequest(
                **_scope(header).model_dump(), selection=header.selection
            ),
        )
    assert not db.events
