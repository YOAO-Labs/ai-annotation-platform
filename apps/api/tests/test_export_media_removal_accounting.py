"""Exact-version retirement windows prove current bytes, not disk reclamation."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
import pytest
from sqlalchemy import select

from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import export_media_removal_accounting as removal
from app.services import export_media_storage as physical
from app.services import storage_usage_accounting as usage
from app.services.conditional_write_accounting import finish
from tests import live_provider_guard
from tests.test_export_media_storage import RAW, record
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory

KEY = "exact/%2F/中文/object"
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


class Provider:
    def __init__(self, endpoint="http://saved.example"):
        self.meta = SimpleNamespace(endpoint_url=endpoint, events=HierarchicalEmitter())
        self.rows, self.calls = {}, []
        self.sends, self.hooks, self.status = 1, True, 204
        self.ack_version = "requested"
        self.fail_list = self.lose_ack = False
        self.mutate = None

    def seed(self, version, data=b"old", *, marker=False, key=KEY):
        self.rows.setdefault(key, []).append(
            dict(version=version, data=data, marker=marker)
        )

    def head_object(self, **params):
        rows = self.rows.get(params["Key"], [])
        if not rows or rows[-1]["marker"]:
            headers = {}
            if rows:
                headers = {
                    "x-amz-delete-marker": "true",
                    "x-amz-version-id": rows[-1]["version"],
                }
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404, "HTTPHeaders": headers},
                },
                "HeadObject",
            )
        row = rows[-1]
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": row["version"],
            "ContentLength": len(row["data"]),
            "ETag": hashlib.sha256(row["data"]).hexdigest(),
        }

    def list_object_versions(self, **params):
        if self.fail_list:
            raise OSError("observation unavailable")
        result = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": [],
            "DeleteMarkers": [],
        }
        for key, rows in self.rows.items():
            if not key.startswith(params["Prefix"]):
                continue
            for index, obj in enumerate(rows):
                value = dict(
                    Key=key,
                    VersionId=obj["version"],
                    LastModified=NOW,
                    IsLatest=index == len(rows) - 1,
                )
                if obj["marker"]:
                    result["DeleteMarkers"].append(value)
                else:
                    result["Versions"].append(
                        {
                            **value,
                            "Size": len(obj["data"]),
                            "ETag": hashlib.sha256(obj["data"]).hexdigest(),
                        }
                    )
        return result

    def list_objects_v2(self, **params):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Contents": [
                {"Key": key, "Size": len(rows[-1]["data"])}
                for key, rows in self.rows.items()
                if key.startswith(params["Prefix"]) and rows and not rows[-1]["marker"]
            ],
        }

    def delete_object(self, **params):
        if self.hooks:
            event = "before-parameter-build.s3.DeleteObject"
            for _, response in self.meta.events.emit(event, params=params):
                assert response is None
            for _ in range(self.sends):
                for _, response in self.meta.events.emit("before-send.s3.DeleteObject"):
                    assert response is None
        self.calls.append(dict(params))
        rows = self.rows.get(params["Key"], [])
        removed = next(
            (row for row in rows if row["version"] == params["VersionId"]), None
        )
        if removed:
            rows.remove(removed)
        if self.mutate:
            self.mutate(self)
        if self.lose_ack:
            raise OSError("lost deletion ACK")
        return {
            "ResponseMetadata": {"HTTPStatusCode": self.status},
            "VersionId": params["VersionId"]
            if self.ack_version == "requested"
            else self.ack_version,
            "DeleteMarker": bool(removed and removed["marker"]),
        }


class MemoryRecorder:
    bucket = "saved"

    def __init__(self):
        self.attempts = []

    def begin(self, key, receipt):
        attempt = SimpleNamespace(evidence={"physical_outcome": "unknown"})
        self.attempts.append(attempt)
        return attempt


def observe(client, version):
    recorder = MemoryRecorder()
    observation = removal.Observation(recorder, client, KEY, version)
    response = observation.submit(
        lambda: client.delete_object(Bucket=recorder.bucket, Key=KEY, VersionId=version)
    )
    assert response["ResponseMetadata"]["HTTPStatusCode"] == client.status
    observation.complete(response)
    return observation.attempt.evidence


@pytest.mark.parametrize("status", [200, 204])
@pytest.mark.parametrize(
    "case,expected",
    [("last", -3), ("older", 0), ("fallback", -4), ("marker", 3), ("absent", 0)],
)
def test_exact_removal_and_noop_current_deltas(case, expected, status):
    client = Provider()
    client.status = status
    if case != "absent":
        client.seed("v1")
    if case in {"older", "fallback"}:
        client.seed("v2", b"1234567")
    if case == "marker":
        client.seed("v2", marker=True)
    version = "v2" if case in {"fallback", "marker"} else "v1"
    # A sibling sharing the exact-key prefix is not this object's identity.
    client.seed("foreign", b"unrelated", key=KEY + "-sibling")
    fact = observe(client, version)
    assert fact["current_delta_bytes"] == expected
    assert fact["physical_outcome"] == ("unknown" if case == "absent" else "deleted")
    assert fact.get("no_removal_observed") is (True if case == "absent" else None)
    assert client.calls == [{"Bucket": "saved", "Key": KEY, "VersionId": version}]


@pytest.mark.parametrize(
    "problem",
    [
        "null",
        "no_hooks",
        "retry",
        "ack_missing",
        "ack_wrong",
        "list_denied",
        "concurrent",
        "remaining_changed",
        "bad_marker",
    ],
)
def test_uncertain_evidence_does_not_change_success(problem):
    client = Provider()
    version = "null" if problem == "null" else "v2"
    client.seed("v1")
    client.seed(version, b"current")
    if problem == "no_hooks":
        client.hooks = False
    elif problem == "retry":
        client.sends = 2
    elif problem == "ack_missing":
        client.ack_version = None
    elif problem == "ack_wrong":
        client.ack_version = "other"
    elif problem == "list_denied":
        client.fail_list = True
    elif problem == "concurrent":
        client.mutate = lambda client: client.seed("foreign")
    elif problem == "remaining_changed":
        client.mutate = lambda client: client.rows[KEY][0].update(data=b"changed")
    elif problem == "bad_marker":
        client.rows[KEY][-1]["marker"] = True
        original = client.delete_object

        def wrong(**params):
            return {**original(**params), "DeleteMarker": False}

        client.delete_object = wrong
    fact = observe(client, version)
    assert fact["physical_outcome"] == "unknown"
    assert "current_delta_bytes" not in fact
    assert len(client.calls) == 1


def test_lost_ack_preserves_original_exception_and_unknown():
    client, recorder = Provider(), MemoryRecorder()
    client.seed("v1")
    client.lose_ack = True
    observation = removal.Observation(recorder, client, KEY, "v1")
    with pytest.raises(OSError, match="lost deletion ACK"):
        observation.submit(
            lambda: client.delete_object(Bucket="saved", Key=KEY, VersionId="v1")
        )
    assert observation.attempt.evidence["physical_outcome"] == "unknown"
    assert not client.rows[KEY]


@pytest.mark.parametrize("version", [None, "null", "v1"])
async def test_explicit_delete_windows_are_durable_and_independent(
    accounting_factory, version
):
    values = dict(
        endpoint_url="http://saved.example",
        bucket="saved",
        object_key=KEY,
        producer="export_media_removals",
        owner_id=uuid.uuid4(),
        purpose="temporary",
        kind="delete",
        version=version,
        new_request=True,
    )
    async with accounting_factory() as db:
        first = await usage.begin_in_transaction(db, **values)
        await db.commit()
    async with accounting_factory() as db:
        second = await usage.begin_in_transaction(db, **values)
        await db.commit()
    assert first.fresh and second.fresh and first.id != second.id
    await usage.publish(accounting_factory, first, {}, settled=True)
    await usage.publish(accounting_factory, second, {"current_delta_bytes": 0})
    async with accounting_factory() as db:
        one, two = await db.get(Mutation, first.id), await db.get(Mutation, second.id)
        assert one.state == "unknown" and one.current_delta_bytes is None
        assert two.state == "confirmed" and two.current_delta_bytes == 0
        assert one.expected_version == version and two.expected_version == version


@pytest.mark.parametrize("version", [None, "null"])
async def test_default_delete_still_requires_immutable_version(
    accounting_factory, version
):
    async with accounting_factory() as db:
        with pytest.raises(ValueError, match="immutable_accounting_version_required"):
            await usage.begin_in_transaction(
                db,
                endpoint_url="http://saved.example",
                bucket="saved",
                object_key=KEY,
                producer="prediction_inputs",
                owner_id=uuid.uuid4(),
                purpose="temporary",
                kind="delete",
                version=version,
            )


async def test_recorder_uses_saved_address_and_commits_before_threaded_delete(
    accounting_factory,
):
    client, owner = Provider(), uuid.uuid4()
    client.seed("v1")
    recorder = removal.Recorder(
        accounting_factory,
        "export_media_removals",
        owner,
        "http://saved.example",
        "saved",
        purpose="temporary",
    )
    loop = asyncio.get_running_loop()

    async def check_pending():
        async with accounting_factory() as db:
            (row,) = list(await db.scalars(select(Mutation)))
            assert row.state == "pending" and row.kind == "delete"
            assert row.owner_id == owner and row.object_key == KEY
            assert row.expected_version == "v1"

    def physical():
        observation = removal.Observation(recorder, client, KEY, "v1")
        asyncio.run_coroutine_threadsafe(check_pending(), loop).result(5)
        response = observation.submit(
            lambda: client.delete_object(Bucket="saved", Key=KEY, VersionId="v1")
        )
        observation.complete(response)

    await asyncio.to_thread(physical)
    assert removal.finish is finish
    assert await removal.finish(recorder, None) is None
    async with accounting_factory() as db:
        (row,) = list(await db.scalars(select(Mutation)))
        assert row.physical_outcome == "deleted" and row.current_delta_bytes == -3


@pytest.mark.parametrize("mismatch", ["Bucket", "Key", "VersionId"])
def test_public_parameter_mismatch_cannot_confirm(mismatch):
    client, recorder = Provider(), MemoryRecorder()
    client.seed("v1")
    observation = removal.Observation(recorder, client, KEY, "v1")

    def wrong():
        params = {"Bucket": "saved", "Key": KEY, "VersionId": "v1"}
        client.meta.events.emit(
            "before-parameter-build.s3.DeleteObject",
            params={**params, mismatch: "foreign"},
        )
        client.meta.events.emit("before-send.s3.DeleteObject")
        client.rows[KEY].clear()
        return {"ResponseMetadata": {"HTTPStatusCode": 204}, "VersionId": "v1"}

    observation.complete(observation.submit(wrong))
    assert "current_delta_bytes" not in observation.attempt.evidence


def test_broken_hooks_and_receipt_remain_observational():
    client = Provider()
    client.meta.events = None
    client.hooks = False
    client.seed("v1")
    fact = observe(client, "v1")
    assert fact["physical_outcome"] == "unknown"
    client, recorder = Provider(), MemoryRecorder()
    observation = removal.Observation(recorder, client, KEY, "v1")
    observation.complete({"ResponseMetadata": None})
    assert "current_delta_bytes" not in observation.attempt.evidence


async def facts(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


def media_provider(*, count=1, version="v1"):
    row, client = record(count=count), Provider(physical._runtime_endpoint())
    for key in row.inventory["objects"]:
        client.seed(version, RAW, key=key)
    return row, client


async def media_recorder(factory, row):
    async with factory() as db:
        recorder = removal.recorder(db, row)
    assert recorder.factory.kw["bind"] is factory.kw["bind"]
    return recorder


@pytest.mark.parametrize("version", ["v1", "null"])
async def test_production_inventory_delete_records_each_actual_version(
    accounting_factory, version
):
    row, client = media_provider(count=2, version=version)
    recorder = await media_recorder(accounting_factory, row)
    result = await asyncio.to_thread(
        physical.delete_inventory, client, row, accounting=recorder
    )
    assert result == 2 and len(client.calls) == 2
    assert await removal.finish(recorder, None) is None
    rows = await facts(accounting_factory)
    assert len(rows) == 2
    assert {fact.object_key for fact in rows} == set(row.inventory["objects"])
    assert all(
        fact.owner_id == row.id
        and fact.producer == "export_media_removals"
        and fact.kind == "delete"
        for fact in rows
    )
    if version == "null":
        assert all(
            fact.state == "unknown" and fact.current_delta_bytes is None
            for fact in rows
        )
    else:
        assert all(
            fact.state == "confirmed" and fact.current_delta_bytes == -len(RAW)
            for fact in rows
        )


async def test_partial_inventory_failure_keeps_completed_facts(accounting_factory):
    row, client = media_provider(count=2)
    recorder = await media_recorder(accounting_factory, row)
    original = client.delete_object

    def partial(**params):
        if client.calls:
            raise OSError("second object failed before send")
        return original(**params)

    client.delete_object = partial
    with pytest.raises(physical.ExportMediaStorageError):
        await asyncio.to_thread(
            physical.delete_inventory, client, row, accounting=recorder
        )
    await removal.finish(recorder, None)
    one, two = await facts(accounting_factory)
    assert one.physical_outcome == "deleted" and one.current_delta_bytes == -len(RAW)
    assert two.state == "unknown" and two.current_delta_bytes is None


async def test_intent_failure_blocks_only_the_next_delete(
    accounting_factory, monkeypatch
):
    row, client = media_provider(count=2)
    recorder = await media_recorder(accounting_factory, row)
    original = recorder._begin

    async def begin(key, receipt):
        if recorder.attempts:
            raise RuntimeError("independent ledger unavailable")
        return await original(key, receipt)

    monkeypatch.setattr(recorder, "_begin", begin)
    with pytest.raises(physical.ExportMediaStorageError):
        await asyncio.to_thread(
            physical.delete_inventory, client, row, accounting=recorder
        )
    await removal.finish(recorder, None)
    assert len(client.calls) == 1
    (fact,) = await facts(accounting_factory)
    assert fact.current_delta_bytes == -len(RAW)


@pytest.mark.parametrize("settled", [False, True])
async def test_publication_failure_and_actual_settlement_preserve_business_result(
    accounting_factory, monkeypatch, settled
):
    row, client = media_provider()
    recorder = await media_recorder(accounting_factory, row)
    assert (
        await asyncio.to_thread(
            physical.delete_inventory, client, row, accounting=recorder
        )
        == 1
    )
    failure = RuntimeError("later business failure")
    if settled:

        async def unavailable(*args, **kwargs):
            raise OSError("ledger publication failed")

        monkeypatch.setattr(usage, "publish", unavailable)
    assert await removal.finish(recorder, failure, settled=settled) is failure
    (fact,) = await facts(accounting_factory)
    assert fact.current_delta_bytes is None
    assert fact.state == ("pending" if settled else "unknown")


@pytest.mark.parametrize(
    "owned,single,expected",
    [
        (False, False, {}),
        (True, False, {"owned": True}),
        (True, True, {"owned": True, "single_attempt": True}),
    ],
)
def test_owned_client_option_is_local_and_preserves_single_attempt(
    owned, single, expected
):
    row, client = media_provider()
    row.storage_ref.update(schema=2, binding_generation=2)
    options = []

    @contextmanager
    def factory(**kwargs):
        options.append(kwargs)
        yield client

    target = SimpleNamespace(
        location_id=physical.LEGACY_LOCATION_ID,
        endpoint_url=row.storage_ref["endpoint_url"],
        bucket=row.storage_ref["bucket"],
        key_prefix="",
        client=factory,
    )
    with physical.open_storage(
        row, target=target, owned=owned, single_attempt=single
    ) as actual:
        assert actual is client
    assert options == [expected]


@pytest.mark.parametrize("schema", [1, 2])
async def test_real_minio_inventory_delete_has_immutable_ack_and_current_deltas(
    accounting_factory, schema
):
    from app.config import settings
    from app.services.managed_s3_target import Target
    from app.services.storage import StorageService

    bucket = live_provider_guard.owned_bucket(
        settings.minio_export_bucket, purpose="export"
    )
    client = StorageService().client
    bucket, identifier = settings.minio_export_bucket, uuid.uuid4()
    ref = physical.make_ref(identifier, bucket=bucket)
    key = physical.prefix_for(identifier) + str(uuid.uuid4()) + ".bin"
    data = b"new"
    row = record(
        id=identifier,
        storage_ref=ref,
        inventory={
            "schema": 1,
            "objects": {
                key: {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            },
        },
    )
    if schema == 2:
        ref.update(schema=2, binding_generation=2)
    target = (
        Target(
            physical.LEGACY_LOCATION_ID,
            1,
            None,
            physical._runtime_endpoint(),
            bucket,
            "",
            purpose="export",
            _legacy_client=client,
        )
        if schema == 2
        else None
    )
    recorder = await media_recorder(accounting_factory, row)
    try:
        await asyncio.to_thread(
            client.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        await asyncio.to_thread(
            client.put_object, Bucket=bucket, Key=key, Body=b"older-longer"
        )
        await asyncio.to_thread(client.put_object, Bucket=bucket, Key=key, Body=data)

        def remove():
            with physical.open_storage(row, target=target, owned=True) as owned:
                return physical.delete_inventory(owned, row, accounting=recorder)

        assert await asyncio.to_thread(remove) == 2
        await removal.finish(recorder, None)
        rows = await facts(accounting_factory)
        assert len(rows) == 2
        assert all(
            fact.state == "confirmed" and fact.physical_outcome == "deleted"
            for fact in rows
        )
        assert [fact.current_delta_bytes for fact in rows] == [9, -12]
        assert all(fact.evidence["final_send_count"] == 1 for fact in rows)
        assert all(
            fact.evidence["returned_version"]
            == fact.expected_version
            not in (None, "null")
            for fact in rows
        )
        assert not (
            await asyncio.to_thread(
                client.list_object_versions, Bucket=bucket, Prefix=key
            )
        ).get("Versions")
    finally:
        listing = await asyncio.to_thread(
            client.list_object_versions, Bucket=bucket, Prefix=key
        )
        for version in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            await asyncio.to_thread(
                client.delete_object,
                Bucket=bucket,
                Key=key,
                VersionId=version["VersionId"],
            )
        client.close()
