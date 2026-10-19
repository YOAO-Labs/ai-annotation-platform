"""Real S3 journals and delayed HTTP completion on verifier-owned resources."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import os
import subprocess
from uuid import uuid4

import asyncpg
import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_mask_content import codec
from aap_assets_mask_content.gc_client import MaskGcClient
from aap_assets_mask_content import gc_contract as byte
from aap_assets_mask_content.gc_storage import S3MaskGcStore
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.coordinator import run_cycle
from aap_labeling_mask_gc_runtime.database import AsyncpgMaskGcData
from tests.test_asset_storage_cleanup_runtime import _object_exists, _required_env
from tests.test_mask_content_live import (
    _owned_resources,
    _rle,
    _s3_endpoint,
    _start_runtime,
    _stop_runtime,
)
from tests.test_mask_gc_native import _native_call


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_CONTENT_ACCEPTANCE") != "1",
        reason="requires the owned module-data and Mask storage verifier",
    ),
]


def _single_attempt_client(credentials):
    return boto3.client(
        "s3",
        endpoint_url=_s3_endpoint(),
        region_name="us-east-1",
        aws_access_key_id=credentials[0],
        aws_secret_access_key=credentials[1],
        config=Config(
            connect_timeout=3,
            read_timeout=8,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def _seed_payload(owner, bucket):
    content = _rle(uuid4().hex)
    body = codec.canonical_rle_bytes(content)
    reference = codec.build_rle_reference(content)
    key = reference.object_key
    owner.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    head = owner.head_object(Bucket=bucket, Key=key)
    return body, byte.GcCandidate(
        object_key=key,
        etag=head["ETag"].strip('"'),
        last_modified=head["LastModified"],
        byte_size=len(body),
    )


@pytest.mark.parametrize("lost_reply", ["delete", "receipt"])
async def test_s3_reply_loss_survives_restart_without_repeating_payload_delete(
    test_engine, lost_reply
):
    _, bucket, _, owner, _, cleanup = _owned_resources(test_engine)
    client = _single_attempt_client(cleanup)
    body, candidate = _seed_payload(owner, bucket)
    operation = byte.DeleteOperationRequest(
        operation_id=uuid4(),
        object_key=candidate.object_key,
        etag=candidate.etag,
        last_modified=candidate.last_modified,
    )

    class LostReply:
        delete_calls = 0

        def __getattr__(self, name):
            return getattr(client, name)

        def delete_object(self, **kwargs):
            self.delete_calls += 1
            result = client.delete_object(**kwargs)
            if lost_reply == "delete":
                raise ReadTimeoutError(endpoint_url="http://owned-s3")
            return result

        def put_object(self, **kwargs):
            result = client.put_object(**kwargs)
            if lost_reply == "receipt" and kwargs["Key"].endswith("/receipt.json"):
                raise ReadTimeoutError(endpoint_url="http://owned-s3")
            return result

    transport = LostReply()
    store = S3MaskGcStore(transport, bucket)
    try:
        with pytest.raises(byte.MaskGcFailure) as unknown:
            await asyncio.to_thread(store.execute, operation)
        assert unknown.value.code == "storage_unavailable"
        assert not _object_exists(owner, bucket, candidate.object_key)
        restarted = S3MaskGcStore(client, bucket)
        status = restarted.status(
            byte.DeleteStatusRequest(operation_id=operation.operation_id)
        )
        assert status.state == (
            "indeterminate" if lost_reply == "delete" else "completed"
        )
        owner.put_object(Bucket=bucket, Key=candidate.object_key, Body=body)
        assert restarted.execute(operation) == status
        assert transport.delete_calls == 1
        assert _object_exists(owner, bucket, candidate.object_key)
    finally:
        owner.delete_object(Bucket=bucket, Key=candidate.object_key)
        client.close()
        owner.close()


def _start_delayed_byte_runtime(bucket, credentials, tmp_path, monkeypatch):
    entered = tmp_path / "delete-return-pending"
    release = tmp_path / "release-delete-return"
    bootstrap = f"""
from pathlib import Path
import time
from botocore.client import BaseClient
from aap_assets_mask_content_runtime.__main__ import main
original = BaseClient._make_api_call
def invoke(self, name, arguments):
    result = original(self, name, arguments)
    if name == 'DeleteObject' and arguments['Key'].startswith('raster-masks/sha256/'):
        Path({str(entered)!r}).touch()
        deadline = time.monotonic() + 20
        while not Path({str(release)!r}).exists():
            if time.monotonic() > deadline:
                raise TimeoutError('owned delayed return deadline')
            time.sleep(0.01)
    return result
BaseClient._make_api_call = invoke
main()
"""
    original_popen = subprocess.Popen

    def start(command, **kwargs):
        assert command[1:] == ["-m", "aap_assets_mask_content_runtime"]
        return original_popen([command[0], "-c", bootstrap], **kwargs)

    with monkeypatch.context() as context:
        context.setattr(subprocess, "Popen", start)
        process = _start_runtime(
            "cleanup", bucket=bucket, credentials=credentials, tmp_path=tmp_path
        )
    return process, entered, release


async def test_late_http_delete_keeps_durable_fence_until_exact_receipt(
    test_engine, tmp_path, monkeypatch
):
    _, bucket, _, owner, _, cleanup = _owned_resources(test_engine)
    body, candidate = _seed_payload(owner, bucket)
    dsn = make_url(_required_env("AAP_MODULE_DATA_LABELING_MASK_GC_URL"))
    assert (dsn.host, dsn.port, dsn.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    pool = await asyncpg.create_pool(
        dsn.set(drivername="postgresql").render_as_string(hide_password=False),
        min_size=1,
        max_size=1,
    )
    data = AsyncpgMaskGcData(pool)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    worker = uuid4()
    process = log = client = None
    release = None
    try:
        sweep = (
            await data.call(
                "claim_mask_gc_sweep", c.ClaimMaskGcSweepRequest(worker_id=worker)
            )
        ).sweep
        assert sweep is not None
        # S3 cannot backdate fixtures. Move only this owned pass's cutoff past
        # the freshly seeded object; production age checks remain unchanged.
        async with maker.begin() as db:
            changed = await db.execute(
                text(
                    "UPDATE mask_gc_sweeps SET cutoff=:cutoff WHERE sweep_id=:id AND worker_id=:worker"
                ),
                {
                    "cutoff": candidate.last_modified + timedelta(days=1),
                    "id": sweep.sweep_id,
                    "worker": worker,
                },
            )
            assert changed.rowcount == 1
        staged = await data.call(
            "stage_mask_gc_page",
            c.StageMaskGcPageRequest(
                sweep_id=sweep.sweep_id,
                generation=sweep.generation,
                lease_token=sweep.lease_token,
                after_key=None,
                page=byte.GcScanPage(
                    items=(candidate,), after_key=candidate.object_key, complete=True
                ),
            ),
        )
        assert staged.staged == 1
        runtime, entered, release = _start_delayed_byte_runtime(
            bucket, cleanup, tmp_path, monkeypatch
        )
        process, log, origin, token = runtime
        client = MaskGcClient(origin, token, timeout_seconds=0.1)
        stats = await run_cycle(data, client, worker_id=worker, delete_limit=1)
        assert stats.indeterminate == 1 and stats.durable_uncertainties == 1
        assert entered.exists()
        assert not _object_exists(owner, bucket, candidate.object_key)
        async with maker.begin() as db:
            fence = await _native_call(
                db, "read_mask_write_epochs", {"object_keys": [candidate.object_key]}
            )
            assert fence["error"]["code"] == "content_deleting"
            operation_id = await db.scalar(
                text("SELECT operation_id FROM mask_gc_intents WHERE object_key=:key"),
                {"key": candidate.object_key},
            )
        independent_s3 = _single_attempt_client(cleanup)
        try:
            status = S3MaskGcStore(independent_s3, bucket).status(
                byte.DeleteStatusRequest(operation_id=operation_id)
            )
            assert status.state == "indeterminate"
        finally:
            independent_s3.close()
        release.touch()
        for _ in range(100):
            try:
                status = await asyncio.to_thread(
                    client.status, byte.DeleteStatusRequest(operation_id=operation_id)
                )
                if status.state == "completed":
                    break
            except byte.MaskGcFailure:
                pass
            await asyncio.sleep(0.02)
        assert status.state == "completed"
        client.close()
        _stop_runtime(process, log)
        process = log = None
        process, log, origin, token = _start_runtime(
            "cleanup", bucket=bucket, credentials=cleanup, tmp_path=tmp_path
        )
        client = MaskGcClient(origin, token)
        owner.put_object(Bucket=bucket, Key=candidate.object_key, Body=body)
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE mask_gc_intents SET next_check_at=clock_timestamp() WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
        resumed = await run_cycle(data, client, worker_id=worker, delete_limit=1)
        assert resumed.completed == 1
        assert _object_exists(owner, bucket, candidate.object_key)
        async with maker.begin() as db:
            stale = await _native_call(
                db,
                "lock_mask_write_epochs",
                {"items": [{"object_key": candidate.object_key, "epoch": 0}]},
            )
            assert stale["error"]["code"] == "content_epoch_changed"
    finally:
        if release is not None:
            release.touch()
        if client is not None:
            client.close()
        _stop_runtime(process, log)
        await pool.close()
        async with maker.begin() as db:
            await db.execute(
                text("DELETE FROM mask_gc_key_epochs WHERE object_key=:key"),
                {"key": candidate.object_key},
            )
            await db.execute(
                text("DELETE FROM mask_gc_intents WHERE object_key=:key"),
                {"key": candidate.object_key},
            )
            await db.execute(
                text("DELETE FROM mask_gc_sweeps WHERE worker_id=:worker"),
                {"worker": worker},
            )
        owner.delete_object(Bucket=bucket, Key=candidate.object_key)
        owner.close()
