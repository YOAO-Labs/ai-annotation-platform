"""Short project contention must not break ordinary image uploads or authority."""

import asyncio
from datetime import datetime, timedelta, timezone
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import decode_access_token
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.project import Project
from app.services import comment_attachment_authority as authority
from tests.test_seed_guide_assets import build, context as context


async def post_image(ctx, world):
    return await ctx.http.post(
        f"/api/v1/projects/{world['project_id']}/guide-assets/upload",
        params={"file_name": "contended.png"},
        headers={**world["headers"], "Content-Type": "image/png"},
        content=b"bounded upload bytes",
    )


async def wait_for_project_waiter(engine, holder_pid):
    async with engine.connect() as observer:
        for _ in range(100):
            blocked = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() "
                    "AND :holder=ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            )
            if blocked:
                return
            await asyncio.sleep(0.01)
    raise AssertionError("The real request never waited for the held project row")


@pytest.mark.parametrize("expire", [False, True])
async def test_short_contention_waits_then_rechecks_the_original_credential(
    context, monkeypatch, expire
):
    ctx = context
    world = await build(ctx)
    before_objects = dict(ctx.objects.data)
    maker = async_sessionmaker(ctx.engine, expire_on_commit=False)
    async with maker() as holder:
        identifier = uuid.UUID(world["project_id"])
        await holder.scalar(
            select(Project).where(Project.id == identifier).with_for_update()
        )
        pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(post_image(ctx, world))
        try:
            await wait_for_project_waiter(ctx.engine, pid)
            assert not task.done() and ctx.objects.data == before_objects
            if expire:
                token = world["headers"]["Authorization"].removeprefix("Bearer ")
                expiration = datetime.fromtimestamp(
                    decode_access_token(token)["exp"], timezone.utc
                )
                monkeypatch.setattr(
                    authority, "now", lambda: expiration + timedelta(seconds=1)
                )
            await holder.rollback()
            response = await asyncio.wait_for(task, timeout=3)
            assert response.status_code == (403 if expire else 200), response.text
            if expire:
                assert ctx.objects.data == before_objects
                await ctx.db.rollback()
                assert await ctx.db.scalar(select(GuideAssetUpload.id)) is None
            else:
                row = await ctx.db.get(
                    GuideAssetUpload,
                    uuid.UUID(response.json()["key"].rsplit("/", 1)[1]),
                )
                assert (
                    row.state == "ready"
                    and len(ctx.objects.data) == len(before_objects) + 1
                )
        finally:
            await holder.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_long_contention_remains_bounded_and_creates_no_pending_owner(context):
    ctx = context
    world = await build(ctx)
    before_objects = dict(ctx.objects.data)
    maker = async_sessionmaker(ctx.engine, expire_on_commit=False)
    async with maker() as holder:
        await holder.scalar(
            select(Project)
            .where(Project.id == uuid.UUID(world["project_id"]))
            .with_for_update()
        )
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(post_image(ctx, world), timeout=3)
            elapsed = time.monotonic() - started
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "guide_asset_busy"
            assert 0.8 <= elapsed < 3
        finally:
            await holder.rollback()
        await ctx.db.rollback()
        assert await ctx.db.scalar(select(GuideAssetUpload.id)) is None
        assert ctx.objects.data == before_objects and not ctx.objects.deleted
