"""Small-object adapters keep the existing deletion proof and durable ownership."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import export_media_removal_accounting as established
from app.services import object_removal_accounting as removal
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory


def test_protocol_exports_are_direct_aliases():
    assert removal.Observation is established.Observation
    assert removal.Recorder is established.Recorder
    assert removal.finish is established.finish


@pytest.mark.parametrize(
    "producer,purpose,explicit_owner",
    [
        ("thumbnail_removals", "media_cache", False),
        ("avatar_removals", "other_platform", True),
        ("bug_attachment_removals", "other_platform", False),
    ],
)
async def test_factory_persists_saved_owner_in_an_independent_thread_window(
    accounting_factory, producer, purpose, explicit_owner
):
    identifier = uuid.uuid4()
    receipt = SimpleNamespace(
        storage_ref={"endpoint_url": "https://saved.example", "bucket": "saved-bucket"}
    )
    if not explicit_owner:
        receipt.id = identifier
    async with accounting_factory() as db:
        recorder = removal.recorder(
            db,
            receipt,
            producer=producer,
            purpose=purpose,
            **({"owner_id": identifier} if explicit_owner else {}),
        )
    assert recorder.factory.kw["bind"] is accounting_factory.kw["bind"]
    key = "saved/%2F/中文/key"
    attempt = await asyncio.to_thread(
        recorder.begin, key, {"version_id": "immutable-v1"}
    )
    async with accounting_factory() as db:
        (row,) = list(await db.scalars(select(Mutation)))
        assert row.id == attempt.handle.id and row.state == "pending"
        assert (
            row.producer == producer
            and row.purpose == purpose
            and row.owner_id == identifier
        )
        assert row.kind == "delete" and row.expected_version == "immutable-v1"
        assert row.scope_key == established.usage.scope_key(
            "https://saved.example", "saved-bucket"
        )
        assert row.object_key == key
    await removal.finish(recorder, None, settled=False)
    async with accounting_factory() as db:
        assert (await db.get(Mutation, attempt.handle.id)).state == "unknown"
