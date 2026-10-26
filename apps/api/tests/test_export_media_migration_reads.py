"""Ready export copies retain their frozen owners through exact migration reads."""

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import export_outputs, export_storage, storage_access
from app.services import storage_migration_artifact_reads as reads
from app.services import storage_migration_copy as copy
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.original_read_plan import ReadRoute
from app.workers import lidar_export as worker

RAW = b"frozen export media\x00\xff"


def record(schema=1, count=2):
    identifier = uuid.UUID(int=100)
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    ref = physical.make_ref(identifier, bucket="saved-export")
    return SimpleNamespace(
        id=identifier,
        input_schema=schema,
        storage_ref=ref,
        created_at=created,
        expires_at=created + timedelta(days=7),
        inventory={
            "schema": 1,
            "objects": {
                ref["key_prefix"] + str(uuid.UUID(int=200 + i)) + ".bin": {
                    "sha256": hashlib.sha256(RAW).hexdigest(),
                    "size_bytes": len(RAW),
                }
                for i in range(count)
            },
        },
    )


def response(route, **changes):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "ContentLength": route.size_bytes,
        "VersionId": route.target_version_id,
        "ETag": route.target_etag,
        "ContentType": "application/octet-stream",
        "Metadata": {
            "aap-copy-plan": route.creation_plan_uuid,
            "aap-copy-object": route.source_object_id,
            "aap-copy-nonce": route.creation_nonce,
        },
        **changes,
    }


def route_for(saved, key, index=1):
    route = ReadRoute(
        publication_id=uuid.UUID(int=1),
        publication_revision=2,
        source_object_id=f"{index:064x}",
        source_location_id=uuid.UUID(saved.storage_ref["location_id"]),
        source_endpoint_url=saved.storage_ref["endpoint_url"],
        source_bucket=saved.storage_ref["bucket"],
        source_object_key=key,
        source_version_id="old-source-version",
        sha256=hashlib.sha256(RAW).hexdigest(),
        size_bytes=len(RAW),
        source_etag='"old-source-etag"',
        is_latest=True,
        target_location_id=uuid.UUID(int=3),
        target_location_revision=4,
        target_connection_id=uuid.UUID(int=5),
        target_connection_revision=6,
        target_endpoint_url="http://target.test:9000",
        target_bucket="shared-migration",
        target_object_key=f"_aap_migration/sealed/{index}",
        target_version_id=f"target-version-{index}",
        target_etag=f'"target-etag-{index}"',
        target_content_sha256="0" * 64,
        creation_nonce=f"copy-nonce-{index}",
        creation_plan_uuid=str(uuid.UUID(int=7)),
        maintenance_revision=8,
    )
    return replace(
        route,
        target_content_sha256=copy.digest(copy.content_target(response(route))),
    )


class Body(io.BytesIO):
    def __init__(self, data, failure=False):
        super().__init__(data)
        self.closes, self.failure = 0, failure

    def close(self):
        self.closes += 1
        if self.failure:
            raise OSError("unknown body close")
        super().close()


class SDK:
    def __init__(self, route):
        self.route = route
        self.meta = SimpleNamespace(endpoint_url=route.target_endpoint_url)
        self.calls, self.bodies = [], []
        self.data, self.body_close_failure = RAW, False
        self.closes, self.after_get = 0, None

    def head_object(self, **params):
        self.calls.append(("head", params))
        return response(self.route)

    def get_object(self, **params):
        self.calls.append(("get", params))
        body = Body(self.data, self.body_close_failure)
        self.bodies.append(body)
        if self.after_get is not None:
            self.after_get()
        return response(self.route, Body=body)

    def generate_presigned_url(self, operation, **params):
        self.calls.append((operation, params))
        return "https://target.test/signed-exact-version"

    def close(self):
        self.closes += 1


class Target:
    def __init__(self, sdk):
        self.sdk = sdk

    @contextmanager
    def client(self):
        try:
            yield self.sdk
        finally:
            storage_access.close_registered(self.sdk)


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
def test_full_read_and_new_signature_use_actual_target_without_mutating_owner(schema):
    saved = record(schema)
    key = next(iter(saved.inventory["objects"]))
    relative = key[len(saved.storage_ref["key_prefix"]) :]
    before = deepcopy(vars(saved))
    route = route_for(saved, key)
    sdk = SDK(route)
    with reads.client(route, Target(sdk)) as client:
        assert (
            physical.verify_read(client, saved, relative)
            == saved.inventory["objects"][key]
        )
        assert (
            physical.sign_read(client, saved, relative, now=saved.created_at)
            == "https://target.test/signed-exact-version"
        )
        # The old direct endpoints remain strict for writes and creation reads.
        with pytest.raises(physical.ExportMediaStorageError):
            physical.verify_object(client, saved, relative)
        with pytest.raises(physical.ExportMediaStorageError):
            physical.signed_url(client, saved, relative)
    assert vars(saved) == before
    assert sdk.closes == 1 and sdk.bodies[0].closed and sdk.bodies[0].closes == 1
    for method, values in sdk.calls:
        params = values["Params"] if method == "get_object" else values
        assert params["Bucket"] == route.target_bucket
        assert params["Key"] == route.target_object_key
        assert params["VersionId"] == route.target_version_id
    assert sdk.calls[-1][1]["ExpiresIn"] == physical.RETENTION_SECONDS


@pytest.mark.parametrize(
    "changes",
    [
        {"source_location_id": uuid.UUID(int=999)},
        {"source_endpoint_url": "http://other.test"},
        {"source_bucket": "different-owner"},
        {"source_object_key": "different/full/key"},
        {"sha256": "f" * 64},
        {"size_bytes": len(RAW) + 1},
    ],
)
def test_read_and_sign_reject_foreign_logical_receipts_before_io(changes):
    saved = record()
    key = next(iter(saved.inventory["objects"]))
    relative = key[len(saved.storage_ref["key_prefix"]) :]
    route = replace(route_for(saved, key), **changes)
    sdk = SDK(route)
    client = reads.ReadClient(sdk, route)
    for operation in (physical.verify_read, physical.sign_read):
        with pytest.raises(physical.ExportMediaStorageError):
            operation(client, saved, relative)
    assert not sdk.calls


@pytest.mark.parametrize("fault", ["hash", "close", "expired"])
def test_routed_read_retains_full_hash_deadline_and_unknown_close_contract(fault):
    saved = record()
    key = next(iter(saved.inventory["objects"]))
    relative = key[len(saved.storage_ref["key_prefix"]) :]
    route = route_for(saved, key)
    sdk = SDK(route)
    if fault == "hash":
        sdk.data = b"x" * len(RAW)
    elif fault == "close":
        sdk.body_close_failure = True
    else:
        saved.created_at -= timedelta(days=8)
        saved.expires_at -= timedelta(days=8)
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        physical.verify_read(reads.ReadClient(sdk, route), saved, relative)
    if fault == "close":
        assert failure.value.storage_operation_unsettled
    else:
        assert failure.value.code == "export_media_storage_" + (
            "integrity_mismatch" if fault == "hash" else "expired"
        )
    if fault == "expired":
        assert not sdk.calls
    else:
        assert sdk.bodies[0].closes == 1


def test_new_signature_uses_only_remaining_original_retention():
    saved = record()
    key = next(iter(saved.inventory["objects"]))
    relative = key[len(saved.storage_ref["key_prefix"]) :]
    route = route_for(saved, key)
    sdk = SDK(route)
    client = reads.ReadClient(sdk, route)
    physical.sign_read(
        client, saved, relative, now=saved.expires_at - timedelta(seconds=2)
    )
    assert sdk.calls[-1][1]["ExpiresIn"] == 2
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        physical.sign_read(client, saved, relative, now=saved.expires_at)
    assert failure.value.code == "export_media_storage_expired"
    assert len(sdk.calls) == 1


class DB:
    def __init__(self, artifact=None, output=None, previous=None):
        self.artifact, self.output, self.previous = artifact, output, previous
        self.commits = self.rollbacks = 0
        self.job = SimpleNamespace(id=uuid.UUID(int=400))

    async def scalar(self, _query):
        return self.artifact

    async def get(self, model, _identifier, **_kwargs):
        return {
            worker.ExportOutput: self.output,
            worker.ExportMediaBundle: self.previous,
            worker.AsyncJob: self.job,
        }[model]

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class Harness:
    def __init__(self, monkeypatch, schema=1):
        self.saved = record(schema)
        self.saved.project_id = uuid.UUID(int=300)
        self.saved.job_id = uuid.UUID(int=400)
        self.saved.zip_output_id = uuid.UUID(int=500)
        self.saved.input_digest = "f" * 64
        self.saved.state = "ready"
        self.mapping = {
            f"source-{i}": key for i, key in enumerate(self.saved.inventory["objects"])
        }
        self.saved._input_snapshot = {
            "schema": schema,
            "media": [{"source_key": key} for key in self.mapping],
        }
        self.saved.input_snapshot = self.saved._input_snapshot
        self.receipt = SimpleNamespace(
            _input_snapshot=self.saved._input_snapshot,
            input_digest=self.saved.input_digest,
            project_id=self.saved.project_id,
            job_id=self.saved.job_id,
        )
        self.output = SimpleNamespace(
            id=self.saved.zip_output_id,
            media_bundle_id=self.saved.id,
            state="ready",
            media="video" if schema == 4 else "image",
            job_id=self.saved.job_id,
            project_id=self.saved.project_id,
            created_at=self.saved.created_at,
            expires_at=self.saved.expires_at,
            sha256=hashlib.sha256(RAW).hexdigest(),
            size_bytes=len(RAW),
            file_count=2,
            format="aap_json",
        )
        self.output.storage_ref = deepcopy(self.saved.storage_ref)
        del self.output.storage_ref["key_prefix"]
        self.output.storage_ref["object_key"] = export_storage.key_for(
            self.output.media,
            self.output.project_id,
            self.output.job_id,
            self.output.id,
        )
        artifact = SimpleNamespace(
            id=uuid.UUID(int=600), output_id=self.output.id, batch_id=None
        )
        self.db = DB(artifact, self.output, self.saved)
        self.routes = {
            key: route_for(self.saved, key, i + 1)
            for i, key in enumerate(self.saved.inventory["objects"])
        }
        self.sdks = {key: SDK(route) for key, route in self.routes.items()}
        self.zip_route = route_for(
            self.output, self.output.storage_ref["object_key"], 50
        )
        self.runs = []
        self.adapter = SimpleNamespace(
            logical_sources=lambda _data: tuple(self.mapping),
            inputs=SimpleNamespace(check_source=AsyncMock(), check=AsyncMock()),
            error=lambda suffix: f"test_export_{suffix}",
        )
        self.guard = AsyncMock()
        self.published = AsyncMock()
        self.prefix = AsyncMock(return_value=deepcopy(self.saved.inventory["objects"]))
        self.recheck = AsyncMock()
        self.invalidate = AsyncMock()
        monkeypatch.setattr(worker, "for_snapshot", lambda _data: self.adapter)
        monkeypatch.setattr(worker.bundles, "snapshot", lambda _row: self.saved)
        monkeypatch.setattr(
            worker.bundles, "source_objects", lambda *_args: self.mapping
        )
        monkeypatch.setattr(export_outputs, "snapshot", lambda _row: self.output)
        monkeypatch.setattr(export_outputs, "couple", Mock())
        monkeypatch.setattr(
            export_outputs, "_read_route", AsyncMock(return_value=self.zip_route)
        )
        monkeypatch.setattr(export_storage, "verify_read", Mock(return_value=True))
        monkeypatch.setattr(worker, "_published", self.published)
        monkeypatch.setattr(worker, "_invalidate", self.invalidate)
        monkeypatch.setattr(publications, "prefix_inventory", self.prefix)
        monkeypatch.setattr(publications, "recheck", self.recheck)
        monkeypatch.setattr(
            reads,
            "resolve",
            AsyncMock(side_effect=lambda _db, _ref, key, *_facts: self.routes[key]),
        )
        monkeypatch.setattr(reads, "run", self.run)
        monkeypatch.setattr(transfer, "_session", self.session)
        for module in (export_storage, physical):
            monkeypatch.setattr(
                module, "io_target", AsyncMock(side_effect=AssertionError("A target"))
            )
        monkeypatch.setattr(
            worker, "_io", AsyncMock(side_effect=AssertionError("A I/O"))
        )
        monkeypatch.setattr(
            physical, "open_storage", Mock(side_effect=AssertionError("A client"))
        )

    @asynccontextmanager
    async def session(self, _db):
        yield self.db

    async def run(self, db, route, purpose, callback, *, on_acquired):
        assert db is self.db
        self.runs.append((route, purpose))
        await on_acquired(SimpleNamespace(revision=route.maintenance_revision))
        sdk = self.sdks.get(route.source_object_key, SDK(route))
        with reads.client(route, Target(sdk)) as client:
            return callback(client)

    async def lookup(self):
        return await worker._lookup(
            self.db, self.receipt, "cache-key", guard=self.guard
        )


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
async def test_shared_cache_lookup_reads_every_exact_media_and_zip_at_target(
    monkeypatch, schema
):
    harness = Harness(monkeypatch, schema)
    saved = deepcopy(vars(harness.saved))
    hit, previous = await harness.lookup()
    assert previous is harness.saved
    assert vars(previous) == saved
    assert hit.read_route == harness.zip_route and hit.gate_revision == 8
    assert hit.media_reads == worker._media_reads(
        previous, tuple(harness.routes.values())
    )
    assert len(harness.runs) == 3
    assert all(
        purpose == transfer.purpose(previous) for _route, purpose in harness.runs
    )
    for key, facts in previous.inventory["objects"].items():
        reads.resolve.assert_any_await(
            harness.db, previous.storage_ref, key, facts["sha256"], facts["size_bytes"]
        )
        assert harness.sdks[key].bodies[0].closed
        assert harness.sdks[key].closes == 1
    assert harness.adapter.inputs.check_source.await_count == 6
    assert harness.prefix.await_count == 2
    harness.invalidate.assert_not_awaited()


async def test_missing_routes_keep_original_source_gate_and_never_grant_target_io(
    monkeypatch,
):
    harness = Harness(monkeypatch)
    harness.routes = dict.fromkeys(harness.routes)
    monkeypatch.setattr(export_outputs, "_read_route", AsyncMock(return_value=None))
    for module in (export_storage, physical):
        monkeypatch.setattr(module, "io_target", AsyncMock(return_value=None))
    original = AsyncMock(side_effect=ContentError("storage_location_maintenance"))
    monkeypatch.setattr(worker, "_io", original)
    with pytest.raises(ContentError) as failure:
        await harness.lookup()
    assert failure.value.code == "storage_location_maintenance"
    assert original.await_args.args[:2] == (harness.db, harness.saved)
    assert original.await_args.kwargs["published"] is True
    assert not harness.runs
    harness.prefix.assert_not_awaited()
    harness.invalidate.assert_not_awaited()


@pytest.mark.parametrize("fault", ["missing", "extra", "sha", "size", "unsealed"])
async def test_complete_sealed_source_prefix_is_required_before_any_target_io(
    monkeypatch, fault
):
    harness = Harness(monkeypatch)
    expected = deepcopy(harness.saved.inventory["objects"])
    first = next(iter(expected))
    if fault == "missing":
        del expected[first]
    elif fault == "extra":
        expected[
            harness.saved.storage_ref["key_prefix"] + str(uuid.UUID(int=999)) + ".bin"
        ] = expected[first]
    elif fault in {"sha", "size"}:
        expected[first]["sha256" if fault == "sha" else "size_bytes"] = (
            "0" * 64 if fault == "sha" else len(RAW) + 1
        )
    else:
        harness.prefix.side_effect = ContentError("read_publication_prefix_incomplete")
    harness.prefix.return_value = expected
    with pytest.raises(ContentError) as failure:
        await harness.lookup()
    assert failure.value.code == "read_publication_prefix_incomplete"
    assert not harness.runs
    harness.invalidate.assert_not_awaited()


@pytest.mark.parametrize("field", [None, *worker._READ_CONTEXT])
async def test_partial_or_inconsistent_media_mapping_cannot_bypass_prefix_proof(
    monkeypatch, field
):
    harness = Harness(monkeypatch)
    key = next(iter(harness.routes))
    route = harness.routes[key]
    if field is None:
        harness.routes[key] = None
    else:
        value = getattr(route, field)
        if isinstance(value, uuid.UUID):
            value = uuid.UUID(int=999)
        elif isinstance(value, int):
            value += 1
        else:
            value += "-changed"
        harness.routes[key] = replace(route, **{field: value})
    with pytest.raises(ContentError) as failure:
        await harness.lookup()
    assert failure.value.code == "read_publication_prefix_incomplete"
    assert not harness.runs
    harness.invalidate.assert_not_awaited()


async def test_bad_target_bytes_invalidate_only_index_but_withdrawal_does_not(
    monkeypatch,
):
    harness = Harness(monkeypatch)
    key = next(iter(harness.sdks))
    harness.sdks[key].data = b"x" * len(RAW)
    assert await harness.lookup() is None
    harness.invalidate.assert_awaited_once_with(
        harness.db, harness.db.artifact.id, harness.output.id
    )
    harness = Harness(monkeypatch)
    withdrawn = False

    def withdraw():
        nonlocal withdrawn
        withdrawn = True

    async def check(*_args, **_kwargs):
        if withdrawn:
            raise ContentError("read_publication_changed")

    next(iter(harness.sdks.values())).after_get = withdraw
    harness.recheck.side_effect = check
    with pytest.raises(ContentError) as failure:
        await harness.lookup()
    assert failure.value.code == "read_publication_changed"
    harness.invalidate.assert_not_awaited()
    assert all(body.closed for sdk in harness.sdks.values() for body in sdk.bodies)


@pytest.mark.parametrize("boundary", [1, 2, 3])
async def test_routed_ready_read_rechecks_original_authority_at_all_boundaries(
    monkeypatch, boundary
):
    harness = Harness(monkeypatch)
    calls = 0

    async def guard(_db):
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise ContentError("access_revoked")

    route = next(iter(harness.routes.values()))
    with pytest.raises(ContentError) as failure:
        await worker._read_io(
            harness.db, harness.saved, route, lambda _client: True, guard=guard
        )
    assert failure.value.code == "access_revoked"
    assert calls == boundary


@pytest.mark.parametrize("fault", [None, "withdrawn", "inventory", "frozen"])
async def test_final_cache_publication_fences_complete_media_read_set(
    monkeypatch, fault
):
    from app.services.exporting import output_authority
    from app.workers import export as legacy

    harness = Harness(monkeypatch)
    hit, previous = await harness.lookup()
    monkeypatch.setattr(worker, "_guard", AsyncMock())
    prepared = object()
    prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(export_outputs, "prepare_download", prepare)
    sign = AsyncMock(return_value="https://target.test/zip")
    monkeypatch.setattr(export_outputs, "sign_cached", sign)
    complete = AsyncMock()
    monkeypatch.setattr(worker.jobs, "mark_complete", complete)
    monkeypatch.setattr(output_authority, "validate", AsyncMock())
    monkeypatch.setattr(legacy, "_emit_export_notification", AsyncMock())
    monkeypatch.setattr(legacy, "_publish_export_notification", AsyncMock())
    if fault == "withdrawn":
        harness.recheck.side_effect = ContentError("read_publication_changed")
    elif fault == "inventory":
        harness.prefix.return_value = {}
    elif fault == "frozen":
        next(iter(previous.inventory["objects"].values()))["sha256"] = "a" * 64
    if fault is None:
        await worker._finish_cache(
            harness.db,
            harness.receipt,
            hit,
            previous,
            task_id="worker",
            name="saved.zip",
        )
        sign.assert_awaited_once()
        complete.assert_awaited_once()
        assert sign.await_args.kwargs["prepared"] is prepared
        assert complete.await_args.kwargs["result"]["expires_at"] == (
            previous.expires_at.isoformat()
        )
        harness.prefix.assert_awaited_with(
            harness.db,
            hit.media_reads.routes[0],
            prefix=hit.media_reads.prefix,
            lock=True,
        )
    else:
        with pytest.raises(ContentError):
            await worker._finish_cache(
                harness.db,
                harness.receipt,
                hit,
                previous,
                task_id="worker",
                name="saved.zip",
            )
        sign.assert_not_awaited()
        complete.assert_not_awaited()
    harness.adapter.inputs.check.assert_awaited_once_with(
        harness.db, harness.receipt._input_snapshot, lock=True
    )
    prepare.assert_awaited_once_with(
        harness.db, hit.output, download_name="saved.zip", expected_revision=8
    )
