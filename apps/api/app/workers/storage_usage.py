"""Bounded, fenced S3 inventory chunks with a durable dispatch lease."""

import asyncio
from datetime import timedelta
from functools import lru_cache
import logging
import time
import uuid

import boto3
from botocore.config import Config
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.storage_usage import (
    StorageUsageInventory as Inventory,
    StorageUsageSnapshot as Snapshot,
)
from app.services import async_job as jobs, storage_usage as svc
from app.services import storage_usage_categories as categories
from app.services import storage_usage_accounting as accounting
from app.workers.celery_app import celery_app
from app.workers.dataset_quality import _session

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def list_page(name, token):
    kwargs = {"Bucket": name, "MaxKeys": 1000}
    if token:
        kwargs["ContinuationToken"] = token
    page = _client().list_objects_v2(**kwargs)
    objects = page.get("Contents", [])
    next_token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
    if len(objects) > 1000 or (
        page.get("IsTruncated") and (not next_token or next_token == token)
    ):
        raise ValueError("invalid_inventory_page")
    sizes = [obj["Size"] for obj in objects]
    keys = [obj["Key"] for obj in objects]
    if (
        any(type(size) is not int or size < 0 for size in sizes)
        or any(
            not isinstance(key, str) or not key or len(key.encode()) > 1024
            for key in keys
        )
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("invalid_inventory_page")
    return {
        "count": len(objects),
        "bytes": sum(sizes),
        "token": next_token,
        "objects": [{"key": key, "size": size} for key, size in zip(keys, sizes)],
    }


def _publish(job_id, task_id):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        inventory_usage.apply_async(
            args=[str(job_id)],
            task_id=task_id,
            queue="default",
            countdown=1,
            connection=connection,
            retry=False,
        )


async def dispatch(db, job):
    job_id, task_id = job.id, job.celery_task_id
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(_publish, job_id, task_id), 3)
    except Exception:
        log.warning("Storage inventory dispatch unavailable job=%s", job_id)


async def process_page(db, job_id, task_id, generation):
    inventory = await svc.fenced(db, job_id, task_id, generation)
    if inventory is None:
        await db.rollback()
        return False
    await svc.authority(db, inventory)
    if svc.now() - inventory.started_at > timedelta(hours=24):
        await svc.finish(db, inventory, "failed", "inventory_budget_exceeded")
        await db.commit()
        return False
    if inventory.bucket_index >= len(inventory.scope):
        await svc.finish(
            db,
            inventory,
            "failed" if inventory.failed_count else "completed",
            "inventory_failed" if inventory.failed_count else None,
        )
        await db.commit()
        return False
    target = inventory.scope[inventory.bucket_index]
    checkpoint = dict(inventory.checkpoint)
    row = await db.get(Snapshot, target["key"], populate_existing=True)
    row.attempt_status = "running"
    page_started = await db.scalar(select(func.clock_timestamp()))
    checkpoint.setdefault("started_at", page_started.isoformat())
    inventory.checkpoint = checkpoint
    await db.commit()
    # No database lock is held while waiting for the provider. Publication is fenced again.
    error, page, page_categories = None, None, None
    if checkpoint.get("pages", 0) >= svc.MAX_PAGES:
        error = "inventory_budget_exceeded"
    else:
        try:
            page = await asyncio.to_thread(
                list_page, target["name"], checkpoint.get("token")
            )
        except Exception:
            error = "object_listing_failed"
        if page is not None and "objects" in page:
            try:
                page_categories = await categories.classify(
                    db, target["name"], page["objects"]
                )
            except Exception:
                await db.rollback()
                error = "object_classification_failed"
    inventory = await svc.fenced(db, job_id, task_id, generation)
    if inventory is None:
        await db.rollback()
        return False
    await svc.authority(db, inventory)
    row = await db.get(Snapshot, target["key"], populate_existing=True)
    page_completed = await db.scalar(select(func.clock_timestamp()))
    if not error and page is not None and "objects" in page:
        try:
            if page["count"] != len(page["objects"]) or page["bytes"] != sum(
                item["size"] for item in page["objects"]
            ):
                raise ValueError("invalid_inventory_page")
            async with db.begin_nested():
                await accounting.stage_page(
                    db,
                    job_id,
                    target["key"],
                    page["objects"],
                    page_started,
                    page_completed,
                )
        except Exception:
            error = "invalid_inventory_page"
    if error:
        row.attempt_status, row.error_code = "failed", error
        inventory.failed_count += 1
    else:
        checkpoint = {
            "count": checkpoint.get("count", 0) + page["count"],
            "bytes": checkpoint.get("bytes", 0) + page["bytes"],
            "pages": checkpoint.get("pages", 0) + 1,
            "started_at": checkpoint.get("started_at") or svc.now().isoformat(),
            "token": page["token"],
            "objects_complete": checkpoint.get(
                "objects_complete", not checkpoint.get("pages", 0)
            )
            and "objects" in page,
            "category_totals": categories.merge(
                checkpoint.get("category_totals")
                if checkpoint.get("pages", 0)
                else categories.empty(),
                page_categories,
            ),
        }
        if not page["token"]:
            from datetime import datetime

            measured_at = await db.scalar(select(func.clock_timestamp()))
            if checkpoint["objects_complete"]:
                await accounting.complete_bucket(
                    db,
                    row,
                    job_id,
                    datetime.fromisoformat(checkpoint["started_at"]),
                    measured_at,
                )
            row.object_count, row.total_size_bytes = (
                checkpoint["count"],
                checkpoint["bytes"],
            )
            row.measurement_started_at = datetime.fromisoformat(
                checkpoint["started_at"]
            )
            row.measured_at = measured_at
            row.category_totals = categories.stamp(
                checkpoint["category_totals"],
                row.object_count,
                row.total_size_bytes,
                row.measured_at,
            )
            row.attempt_status, row.error_code = "completed", None
            inventory.success_count += 1
    if error or not page["token"]:
        if error:
            await accounting.discard_staging(db, job_id, target["key"])
        inventory.bucket_index += 1
        checkpoint = {}
    inventory.checkpoint = checkpoint
    inventory.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    job = await db.get(AsyncJob, job_id)
    job.result = svc.result(inventory)
    await jobs.update_progress(
        db, job_id, int(inventory.bucket_index * 100 / len(inventory.scope))
    )
    await db.commit()
    return True


async def execute(db, job_id, task_id):
    inventory = await svc.locked(db)
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        not inventory
        or inventory.async_job_id != job_id
        or inventory.status != "pending"
        or not job
        or job.status not in svc.ACTIVE
        or job.celery_task_id != task_id
    ):
        await db.rollback()
        return
    inventory.status = "running"
    generation = inventory.generation
    inventory.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    await jobs.mark_running(db, job_id)
    await db.commit()
    try:
        started = time.monotonic()
        for _ in range(10):
            if not await process_page(db, job_id, task_id, generation):
                return
            if time.monotonic() - started >= 30:
                break
        inventory = await svc.fenced(db, job_id, task_id, generation)
        if inventory:
            inventory.status = "pending"
            inventory.generation += 1
            job = await db.get(AsyncJob, job_id)
            job.celery_task_id = str(uuid.uuid4())
            await dispatch(db, job)
    except Exception as exc:
        await db.rollback()
        inventory = await svc.fenced(db, job_id, task_id, generation)
        if inventory:
            code = (
                "access_revoked"
                if isinstance(exc, HTTPException) and exc.status_code in {401, 403, 404}
                else "configuration_changed"
                if isinstance(exc, HTTPException) and exc.status_code == 409
                else "worker_failed"
            )
            await svc.finish(db, inventory, "failed", code)
            await db.commit()
        log.warning(
            "Storage inventory stopped job=%s exception=%s", job_id, type(exc).__name__
        )


async def recover(db):
    due = await svc.start_due(db)
    if due is not None:
        await dispatch(db, due)
    inventory = await db.scalar(
        select(Inventory)
        .where(Inventory.status.in_(svc.ACTIVE), Inventory.lease_until <= svc.now())
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if not inventory:
        return
    job = (
        await db.get(AsyncJob, inventory.async_job_id, populate_existing=True)
        if inventory.async_job_id
        else None
    )
    try:
        await svc.authority(db, inventory)
    except HTTPException as exc:
        await svc.finish(
            db,
            inventory,
            "failed",
            "access_revoked" if exc.status_code == 403 else "configuration_changed",
        )
        await db.commit()
        return
    if not job or job.status not in svc.ACTIVE:
        await svc.finish(db, inventory, "failed", "worker_failed")
        await db.commit()
        return
    inventory.status = "pending"
    inventory.generation += 1
    inventory.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    job.celery_task_id = str(uuid.uuid4())
    await dispatch(db, job)


@celery_app.task(bind=True, acks_late=True, soft_time_limit=150, time_limit=180)
def inventory_usage(self, job_id):
    asyncio.run(_session(lambda db: execute(db, uuid.UUID(job_id), self.request.id)))


@celery_app.task
def recover_usage_inventories():
    asyncio.run(_session(recover))
