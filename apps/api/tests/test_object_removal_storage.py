"""Actual avatar/attachment deletes keep their old reads and independent facts."""

import asyncio
import hashlib
import io
import os
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import avatar, avatar_storage, bug_attachment_storage as bug
from app.services import object_removal_accounting as removal
from app.services import storage
from app.services.managed_s3_target import Target
from tests.test_avatar_storage import record as avatar_record, reference as avatar_ref
from tests.test_bug_attachment_storage import record as bug_record, reference as bug_ref
from tests.test_export_media_removal_accounting import Provider
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory

DATA = b"saved object bytes"


class Objects(Provider):
    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.meta.config = Config(
            connect_timeout=7,
            read_timeout=19,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.closed, self.gets, self.bodies = 0, [], []
        self.close_failure = None

    def get_object(self, **params):
        self.gets.append(dict(params))
        rows = self.rows.get(params["Key"], [])
        row = (
            next((row for row in rows if row["version"] == params["VersionId"]), None)
            if "VersionId" in params
            else (rows[-1] if rows else None)
        )
        if row is None or row["marker"]:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        body = io.BytesIO(row["data"])
        self.bodies.append(body)
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "Body": body,
            "VersionId": row["version"],
            "ContentLength": len(row["data"]),
            "ETag": hashlib.sha256(row["data"]).hexdigest(),
        }

    def delete_object(self, **params):
        if "VersionId" in params:
            return super().delete_object(**params)
        if self.hooks:
            for _, value in self.meta.events.emit(
                "before-parameter-build.s3.DeleteObject", params=params
            ):
                assert value is None
            for _ in range(self.sends):
                for _, value in self.meta.events.emit("before-send.s3.DeleteObject"):
                    assert value is None
        self.calls.append(dict(params))
        self.seed("new-delete-marker", marker=True, key=params["Key"])
        return {
            "ResponseMetadata": {"HTTPStatusCode": 204},
            "DeleteMarker": True,
            "VersionId": "new-delete-marker",
        }

    def close(self):
        self.closed += 1
        if self.close_failure:
            raise self.close_failure


def owner_record(kind, *, bucket=None, schema=1, version="v1", data=DATA, fresh=False):
    if kind == "avatar":
        token = uuid.uuid4().hex if fresh else "0123456789abcdef0123456789abcdef"
        ref = avatar_ref(bucket=bucket or "saved-avatar-bucket", token=token)
        row = avatar_record(token=token, storage_ref=ref)
    else:
        identifiers = (
            {"upload_id": uuid.uuid4(), "owner_id": uuid.uuid4()} if fresh else {}
        )
        ref = bug_ref(bucket=bucket or "saved-bug-bucket", **identifiers)
        row = bug_record(
            storage_ref=ref,
            **(
                {"id": identifiers["upload_id"], "owner_id": identifiers["owner_id"]}
                if fresh
                else {}
            ),
        )
    if schema == 2:
        ref.update(schema=2, binding_generation=2)
    row.sha256, row.size_bytes = hashlib.sha256(data).hexdigest(), len(data)
    row.object_version_id = version
    return row


def target(row, client):
    ref = row.storage_ref
    return Target(
        uuid.UUID(ref["location_id"]),
        ref["config_revision"],
        None,
        ref["endpoint_url"],
        ref["bucket"],
        "",
        _legacy_client=client,
    )


def install(monkeypatch, kind, *, schema=1, version="v1", seed=True):
    row = owner_record(kind, schema=schema, version=version)
    client = Objects(row.storage_ref["endpoint_url"])
    if seed:
        client.seed(
            version or "stored-version", DATA, key=row.storage_ref["object_key"]
        )
    service = SimpleNamespace(
        client=SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=row.storage_ref["endpoint_url"], config=client.meta.config
            )
        ),
        delete_object=Mock(),
    )
    options = []

    def owned(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", owned)
    monkeypatch.setattr(avatar, "storage_service", service)
    monkeypatch.setattr(avatar_storage, "storage_service", service)
    monkeypatch.setattr(bug, "storage_service", service)
    return SimpleNamespace(
        row=row,
        client=client,
        service=service,
        options=options,
        target=target(row, service.client) if schema == 2 else None,
    )


async def recorder(factory, kind, row):
    async with factory() as db:
        return removal.recorder(
            db,
            row,
            producer="avatar_removals"
            if kind == "avatar"
            else "bug_attachment_removals",
            purpose="other_platform",
            **({"owner_id": uuid.UUID(hex=row.token)} if kind == "avatar" else {}),
        )


def remove(kind, row, target, recorder):
    function = avatar.delete_avatar_object if kind == "avatar" else bug.delete
    return function(row, target=target, accounting=recorder)


async def facts(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


@pytest.mark.parametrize("kind", ["avatar", "bug"])
@pytest.mark.parametrize("schema", [1, 2])
async def test_actual_delete_uses_owned_same_config_and_saved_address(
    accounting_factory, monkeypatch, kind, schema
):
    world = install(monkeypatch, kind, schema=schema)
    rec = await recorder(accounting_factory, kind, world.row)
    assert await asyncio.to_thread(remove, kind, world.row, world.target, rec) is None
    assert await removal.finish(rec, None) is None
    (fact,) = await facts(accounting_factory)
    assert fact.state == "confirmed" and fact.current_delta_bytes == -len(DATA)
    assert fact.owner_id == (
        uuid.UUID(hex=world.row.token) if kind == "avatar" else world.row.id
    )
    assert (
        fact.kind == "delete" and fact.object_key == world.row.storage_ref["object_key"]
    )
    assert world.options == [{"client_config": world.client.meta.config}]
    assert world.client.closed == 1 and all(body.closed for body in world.client.bodies)
    assert world.client.calls == [
        {
            "Bucket": world.row.storage_ref["bucket"],
            "Key": world.row.storage_ref["object_key"],
            "VersionId": "v1",
        }
    ]
    world.service.delete_object.assert_not_called()


@pytest.mark.parametrize("kind", ["avatar", "bug"])
@pytest.mark.parametrize("version,schema", [(None, 1), (None, 2), ("null", 1)])
async def test_key_only_and_null_actual_calls_have_unknown_windows(
    accounting_factory, monkeypatch, kind, version, schema
):
    world = install(monkeypatch, kind, schema=schema, version=version)
    rec = await recorder(accounting_factory, kind, world.row)
    assert await asyncio.to_thread(remove, kind, world.row, world.target, rec) is None
    await removal.finish(rec, None)
    (fact,) = await facts(accounting_factory)
    assert (
        fact.state == "unknown"
        and fact.current_delta_bytes is None
        and fact.expected_version == version
    )
    expected = {
        "Bucket": world.row.storage_ref["bucket"],
        "Key": world.row.storage_ref["object_key"],
    }
    if version is not None:
        expected["VersionId"] = version
    assert world.client.calls == [expected]
    assert world.client.closed == 1
    if kind == "bug" and version is None and schema == 1:
        # This legacy shortcut never read or proved absence in the old business path.
        assert world.client.gets == []
    world.service.delete_object.assert_not_called()


@pytest.mark.parametrize("kind", ["avatar", "bug"])
async def test_original_missing_read_returns_without_intent_or_delete(
    accounting_factory, monkeypatch, kind
):
    world = install(monkeypatch, kind, schema=2, version=None, seed=False)
    rec = await recorder(accounting_factory, kind, world.row)
    assert await asyncio.to_thread(remove, kind, world.row, world.target, rec) is None
    await removal.finish(rec, None)
    assert not await facts(accounting_factory)
    assert not world.client.calls and world.client.closed == 1


@pytest.mark.parametrize("kind", ["avatar", "bug"])
async def test_intent_failure_does_not_start_actual_delete(
    accounting_factory, monkeypatch, kind
):
    world = install(monkeypatch, kind)
    rec = await recorder(accounting_factory, kind, world.row)

    async def unavailable(*args):
        raise OSError("ledger intent unavailable")

    monkeypatch.setattr(rec, "_begin", unavailable)
    with pytest.raises(OSError if kind == "avatar" else bug.BugAttachmentStorageError):
        await asyncio.to_thread(remove, kind, world.row, world.target, rec)
    assert not world.client.calls and world.client.closed == 1
    assert not await facts(accounting_factory)


@pytest.mark.parametrize("kind", ["avatar", "bug"])
@pytest.mark.parametrize(
    "uncertain", ["retry", "no_hooks", "no_ack", "no_list", "lost_ack"]
)
async def test_original_delete_results_survive_uncertain_observation(
    accounting_factory, monkeypatch, kind, uncertain
):
    world = install(monkeypatch, kind)
    if uncertain == "retry":
        world.client.sends = 2
    elif uncertain == "no_hooks":
        world.client.hooks = False
    elif uncertain == "no_ack":
        world.client.ack_version = None
    elif uncertain == "no_list":
        world.client.fail_list = True
    else:
        world.client.lose_ack = True
    rec = await recorder(accounting_factory, kind, world.row)
    failure = None
    try:
        result = await asyncio.to_thread(remove, kind, world.row, world.target, rec)
        assert result is None and uncertain != "lost_ack"
    except (OSError, bug.BugAttachmentStorageError) as exc:
        assert uncertain == "lost_ack"
        failure = exc
    assert await removal.finish(rec, failure) is failure
    (fact,) = await facts(accounting_factory)
    assert fact.state == "unknown" and fact.current_delta_bytes is None
    assert len(world.client.calls) == 1 and world.client.closed == 1


@pytest.mark.parametrize("fails", [False, True])
async def test_avatar_completes_only_after_original_absence_proof(
    accounting_factory, monkeypatch, fails
):
    world = install(monkeypatch, "avatar")
    rec = await recorder(accounting_factory, "avatar", world.row)
    sequence = []
    original, protocol = avatar._prove_absent, removal.Observation

    def absence(*args):
        sequence.append("absence")
        if fails:
            raise OSError("original absence failed")
        return original(*args)

    class Observed(protocol):
        def complete(self, response):
            sequence.append("complete")
            return super().complete(response)

    monkeypatch.setattr(avatar, "_prove_absent", absence)
    monkeypatch.setattr(removal, "Observation", Observed)
    failure = None
    try:
        await asyncio.to_thread(remove, "avatar", world.row, world.target, rec)
    except OSError as exc:
        assert fails
        failure = exc
    await removal.finish(rec, failure)
    (fact,) = await facts(accounting_factory)
    assert sequence == (["absence"] if fails else ["absence", "complete"])
    assert fact.current_delta_bytes == (None if fails else -len(DATA))


@pytest.mark.parametrize("kind", ["avatar", "bug"])
async def test_client_close_failure_cannot_confirm_bytes(
    accounting_factory, monkeypatch, kind
):
    world = install(monkeypatch, kind)
    world.client.close_failure = OSError("native close failed")
    rec = await recorder(accounting_factory, kind, world.row)
    with pytest.raises(BaseException) as caught:
        await asyncio.to_thread(remove, kind, world.row, world.target, rec)
    assert getattr(caught.value, "storage_operation_unsettled", False)
    assert await removal.finish(rec, caught.value) is caught.value
    (fact,) = await facts(accounting_factory)
    assert fact.physical_outcome == "deleted" and fact.current_delta_bytes is None


def test_unaccounted_legacy_bug_keeps_original_helper(monkeypatch):
    world = install(monkeypatch, "bug", version=None)
    assert bug.delete(world.row) is None
    world.service.delete_object.assert_called_once_with(
        world.row.storage_ref["object_key"], bucket=world.row.storage_ref["bucket"]
    )
    assert not world.options and not world.client.calls


@pytest.mark.parametrize(
    "kind,schema,known",
    [
        ("avatar", 1, True),
        ("avatar", 2, True),
        ("bug", 1, True),
        ("bug", 2, True),
        ("avatar", 1, False),
        ("bug", 1, False),
    ],
)
async def test_real_minio_physical_delete_and_legacy_unknown(
    accounting_factory, kind, schema, known
):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    bucket = (
        settings.minio_avatars_bucket
        if kind == "avatar"
        else settings.minio_bug_reports_bucket
    )
    assert bucket.startswith("aap-wt-") and "-test-" in bucket
    client = storage.StorageService().client
    row = owner_record(
        kind, bucket=bucket, schema=schema, version=None, data=b"new", fresh=True
    )
    key = row.storage_ref["object_key"]
    rec = await recorder(accounting_factory, kind, row)
    try:
        await asyncio.to_thread(
            client.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        await asyncio.to_thread(
            client.put_object, Bucket=bucket, Key=key, Body=b"older-longer"
        )
        response = await asyncio.to_thread(
            client.put_object, Bucket=bucket, Key=key, Body=b"new"
        )
        if known:
            row.object_version_id = response["VersionId"]
        chosen = target(row, client) if schema == 2 else None
        assert await asyncio.to_thread(remove, kind, row, chosen, rec) is None
        await removal.finish(rec, None)
        (fact,) = await facts(accounting_factory)
        if known:
            assert (
                fact.state == "confirmed"
                and fact.physical_outcome == "deleted"
                and fact.current_delta_bytes == 9
            )
            assert (
                fact.expected_version
                == fact.evidence["returned_version"]
                == response["VersionId"]
            )
            current = await asyncio.to_thread(
                client.head_object, Bucket=bucket, Key=key
            )
            assert current["ContentLength"] == 12
        else:
            assert fact.state == "unknown" and fact.current_delta_bytes is None
            assert fact.expected_version is None
            current = await asyncio.to_thread(
                client.list_object_versions, Bucket=bucket, Prefix=key
            )
            assert len(current["Versions"]) == 2 and len(current["DeleteMarkers"]) == 1
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
