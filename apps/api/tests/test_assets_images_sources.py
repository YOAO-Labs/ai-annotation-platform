"""Image producer handoffs stay atomic with the published source binding."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aap_assets_source.legacy_upload_contract import decode_legacy_upload_reply
from tests.test_assets_images_control import _seed_actor, _seed_image_item

pytestmark = pytest.mark.asyncio

_PEEK = text(
    "SELECT aap_assets_v1.read_legacy_dataset_upload("
    "CAST(:operation AS uuid), CAST(:actor AS uuid), "
    "CAST(:dataset AS uuid), CAST(:item AS uuid))"
)
_COMPLETE = text(
    "SELECT aap_assets_v1.complete_legacy_dataset_upload("
    "CAST(:operation AS uuid), CAST(:actor AS uuid), "
    "CAST(:dataset AS uuid), CAST(:item AS uuid), "
    ":path, 2048::bigint, :hash, 5000, 5000)"
)


async def test_legacy_peek_replays_receipt_and_checks_current_source_and_actor(
    db_session,
):
    actor = await _seed_actor(db_session)
    dataset, item = await _seed_image_item(db_session, actor_id=actor)
    params = {
        "operation": uuid4(),
        "actor": actor,
        "dataset": dataset,
        "item": item,
        "path": f"image-source/{item}.png",
        "hash": "a" * 64,
    }
    pending = decode_legacy_upload_reply(await db_session.scalar(_PEEK, params))
    assert pending.state == "pending" and pending.file_path == params["path"]
    completed = decode_legacy_upload_reply(await db_session.scalar(_COMPLETE, params))
    assert completed.state == "completed"
    assert (
        decode_legacy_upload_reply(await db_session.scalar(_PEEK, params)) == completed
    )

    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET file_path='replaced/image.png' WHERE id=:id"
        ),
        {"id": item},
    )
    changed = await db_session.scalar(_PEEK, params)
    assert changed["error"]["code"] == "source_changed"
    await db_session.execute(
        text("UPDATE public.users SET is_active=false WHERE id=:id"), {"id": actor}
    )
    inactive = await db_session.scalar(_PEEK, params)
    assert inactive["error"]["code"] == "account_not_active"


async def test_image_delivery_requires_binding_at_commit_and_retains_handoff_audit(
    db_session,
):
    actor = await _seed_actor(db_session)
    for table, operation_column in (
        ("dataset_import_deliveries", "job_id"),
        ("dataset_source_ingest_deliveries", "operation_id"),
    ):
        _, item = await _seed_image_item(db_session, actor_id=actor)
        insert = text(
            f"INSERT INTO public.{table} ({operation_column}, kind, target_id) "
            "VALUES (:operation, :kind, :item)"
        )
        params = {"operation": uuid4(), "kind": "thumbnail", "item": item}
        with pytest.raises(IntegrityError, match="must bind the published source"):
            async with db_session.begin_nested():
                await db_session.execute(insert, params)
                await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert (
            await db_session.scalar(
                text(f"SELECT count(*) FROM public.{table} WHERE target_id=:item"),
                params,
            )
            == 0
        )

        await db_session.execute(insert, params)
        await db_session.execute(
            text(
                "UPDATE public.dataset_items SET media_object_identity=CAST(:identity AS jsonb) WHERE id=:item"
            ),
            {
                "item": item,
                "identity": json.dumps({"etag": "published", "byte_size": 2048}),
            },
        )
        await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        row = (
            await db_session.execute(
                text(
                    f"SELECT delivered_at, lease_token FROM public.{table} WHERE target_id=:item"
                ),
                params,
            )
        ).one()
        assert row.delivered_at is not None and row.lease_token is None
        await db_session.execute(text("SET CONSTRAINTS ALL DEFERRED"))

        video_target = uuid4()
        await db_session.execute(
            insert, {**params, "kind": "video_metadata", "item": video_target}
        )
        assert (
            await db_session.scalar(
                text(
                    f"SELECT delivered_at IS NULL FROM public.{table} WHERE target_id=:item"
                ),
                {"item": video_target},
            )
            is True
        )
