"""Independent Notifications reads retain real statement snapshots and pairs."""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_notifications.delivery_read_postgres import (
    NotificationDeliveryReadPostgresData,
)
from app.db.models.notification import Notification
from app.services.notification import NotificationService
from tests.test_workspace_actor_scope_native import _consumer_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


async def test_delivery_reads_keep_separate_snapshots_during_committed_revocation(
    test_engine, monkeypatch
):
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        original_member_read = NotificationDeliveryReadPostgresData.member_pairs
        calls = []

        async def read_members_then_revoke(data, pairs):
            result = await original_member_read(data, pairs)
            calls.append("member")
            async with owner.begin() as writer:
                await writer.execute(
                    text("DELETE FROM public.project_members WHERE id=:id"),
                    {"id": world["membership_id"]},
                )
            return result

        for name in ("reviewer", "owner", "super"):
            original = getattr(NotificationDeliveryReadPostgresData, name + "_pairs")

            async def tracked(data, pairs, *, function=original, label=name):
                calls.append(label)
                return await function(data, pairs)

            monkeypatch.setattr(
                NotificationDeliveryReadPostgresData, name + "_pairs", tracked
            )
        monkeypatch.setattr(
            NotificationDeliveryReadPostgresData,
            "member_pairs",
            read_members_then_revoke,
        )

        rows = [SimpleNamespace(user_id=world["member_id"]) for _ in range(2)]
        scopes = [
            (True, world["project_id"], False),
            (True, world["project_id"], True),
        ]
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            before = (
                await db.execute(
                    text("SELECT current_user,pg_backend_pid(),txid_current()")
                )
            ).one()
            assert before[0] == "identity_api_consumer"
            with pytest.raises(DBAPIError) as denied:
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "SELECT id FROM aap_notifications_v1.notification_facts LIMIT 1"
                        )
                    )
            assert denied.value.orig.sqlstate == "42501"

            service = NotificationService(db)
            assert await service._allowed_delivery_indices(rows, scopes) == {0}
            assert calls == ["member", "reviewer", "owner", "super"]
            monkeypatch.setattr(
                NotificationDeliveryReadPostgresData,
                "member_pairs",
                original_member_read,
            )
            assert await service._allowed_delivery_indices(rows, scopes) == set()
            after = (
                await db.execute(
                    text("SELECT current_user,pg_backend_pid(),txid_current()")
                )
            ).one()
            assert after == before
            await db.rollback()


async def test_delivery_pair_capabilities_do_not_cross_project_and_user_inputs(
    test_engine,
):
    async with _consumer_world(test_engine) as (_owner, api, world, _url):
        missing_actor, missing_project = uuid.uuid4(), uuid.uuid4()
        async with api() as db:
            data = NotificationDeliveryReadPostgresData(db, Notification)
            for method, actor in (
                (data.member_pairs, world["member_id"]),
                (data.reviewer_pairs, world["member_id"]),
                (data.owner_pairs, world["owner_id"]),
                (data.super_pairs, world["owner_id"]),
            ):
                pairs = {
                    (world["project_id"], missing_actor),
                    (missing_project, actor),
                }
                assert await method(pairs) == set()
            await db.rollback()
