"""Real-process Mask GC cutover checks using a standalone Node HTTP peer."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_assets_mask_content import gc_contract as byte
from aap_assets_mask_content.gc_client import MaskGcClient
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc_runtime.database import AsyncpgMaskGcData
from tests.test_asset_storage_cleanup_runtime import _object_exists, _required_env
from tests.test_mask_content_live import (
    _free_loopback_port,
    _owned_resources,
    _start_runtime,
    _stop_runtime,
)
from tests.test_mask_gc_native import _native_call

ROOT = Path(__file__).resolve().parents[3]
NODE_PEER = Path(__file__).parent / "fixtures" / "mask_gc_cutover_peer.mjs"
_SCHEMA = "aap-labeling-mask-gc/v1"
_PAGE_OBJECTS = byte.MAX_GC_PAGE + 1
_MAX_LIST_PAGES = 100

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_CONTENT_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_GC_CUTOVER_ACCEPTANCE") != "1",
        reason="requires the owned module-data, Mask storage, and cutover verifier",
    ),
]


def _node_binary() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.fail("the cutover peer acceptance requires Node.js 18 or newer")
    version = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=5,
        check=False,
    )
    match = re.fullmatch(r"v([0-9]+)\.[0-9]+\.[0-9]+\s*", version.stdout)
    if version.returncode != 0 or match is None or int(match.group(1)) < 18:
        pytest.fail("the cutover peer acceptance requires Node.js 18 or newer")
    return executable


def _run_node_peer(origin: str, token: str, request: dict) -> dict:
    result = subprocess.run(
        [_node_binary(), str(NODE_PEER)],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "AAP_MASK_GC_PEER_URL": origin,
            "AAP_MASK_GC_PEER_TOKEN": token,
        },
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("the standalone Node peer could not complete the Mask GC HTTP call")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("the standalone Node peer returned an invalid bounded result")
    if not isinstance(response, dict) or set(response) != {"responses"}:
        pytest.fail("the standalone Node peer returned an unexpected result shape")
    return response


def _wait_gc_runtime(process: subprocess.Popen, origin: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("the Labeling Mask GC process exited before readiness")
        try:
            response = httpx.get(origin + "/health/ready", timeout=1, trust_env=False)
            if response.status_code == 200 and response.text == "ok":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    pytest.fail("the Labeling Mask GC process did not become ready")


def _start_gc_runtime(test_engine, content_origin, content_token, tmp_path):
    database_url = _postgres_dsn(test_engine)
    token = secrets.token_urlsafe(32)
    port = _free_loopback_port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "MASK_GC_DATABASE_URL": database_url,
        "MASK_GC_TOKEN": token,
        "MASK_GC_CONTENT_URL": content_origin,
        "MASK_GC_CONTENT_TOKEN": content_token,
        "MASK_GC_HOST": "127.0.0.1",
        "MASK_GC_PORT": str(port),
        "MASK_GC_POLL_ENABLED": "false",
    }
    log_stream = (tmp_path / "mask-gc-cutover.log").open("wb")
    process = subprocess.Popen(
        [
            os.environ.get("AAP_MASK_GC_RUNTIME_PYTHON", sys.executable),
            "-m",
            "aap_labeling_mask_gc_runtime",
        ],
        cwd=tmp_path
        if os.environ.get("AAP_MASK_GC_RUNTIME_PYTHON")
        else ROOT / "apps" / "labeling-mask-gc" / "src",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        _wait_gc_runtime(process, origin)
    except BaseException:
        _stop_runtime(process, log_stream)
        raise
    return process, log_stream, origin, token


def _postgres_dsn(test_engine) -> str:
    dsn = make_url(_required_env("AAP_MODULE_DATA_LABELING_MASK_GC_URL"))
    target = test_engine.url
    if (dsn.host, dsn.port, dsn.database) != (
        target.host,
        target.port,
        target.database,
    ):
        pytest.fail("the Labeling Mask GC login does not target the owned test DB")
    return dsn.set(drivername="postgresql").render_as_string(hide_password=False)


async def _gc_table_snapshot(test_engine) -> tuple[tuple[str, str], ...]:
    async with test_engine.connect() as connection:
        tables = (
            await connection.execute(
                text(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname='public' AND tablename LIKE 'mask_gc_%' "
                    "ORDER BY tablename"
                )
            )
        ).scalars()
        snapshot = []
        for table in tables:
            if re.fullmatch(r"mask_gc_[a-z0-9_]+", table) is None:
                pytest.fail("the owned Mask GC table list contains an invalid name")
            value = await connection.scalar(
                text(
                    "SELECT COALESCE("
                    "jsonb_agg(to_jsonb(source) ORDER BY to_jsonb(source)::text), "
                    "'[]'::jsonb)::text "
                    f'FROM public."{table}" AS source'
                )
            )
            snapshot.append((table, value))
        return tuple(snapshot)


async def _reference_count(test_engine):
    async with test_engine.connect() as db:
        result = await _native_call(
            db,
            "inspect_mask_gc_page",
            {
                "page": {"items": [], "after_key": None, "complete": True},
                "include_reference_count": True,
            },
        )
    assert result["ok"] is True, result
    return result["data"]["referenced"]


def _list_s3_prefix(owner, bucket: str, prefix: str) -> list[dict]:
    values = []
    continuation = None
    for _ in range(_MAX_LIST_PAGES):
        request = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation is not None:
            request["ContinuationToken"] = continuation
        page = owner.list_objects_v2(**request)
        values.extend(page.get("Contents", []))
        if not page.get("IsTruncated", False):
            return values
        continuation = page.get("NextContinuationToken")
        if not continuation:
            pytest.fail("owned S3 listing omitted its bounded continuation token")
    pytest.fail("owned S3 fixture listing exceeded its page bound")


def _high_shard_fixture(owner, bucket: str, count: int) -> tuple[str, list[str], bytes]:
    prefix = "raster-masks/sha256/ff/ff/"
    existing = _list_s3_prefix(owner, bucket, prefix)
    suffixes = []
    for item in existing:
        match = re.fullmatch(
            r"ffff([0-9a-f]{60})\.json(?:\.gz)?",
            item["Key"].rsplit("/", 1)[-1],
        )
        if match is None:
            pytest.fail("owned Mask fixture namespace contains a noncanonical key")
        suffixes.append(int(match.group(1), 16))
    start = max(suffixes, default=0) + 1
    if start + _PAGE_OBJECTS >= 1 << 240:
        pytest.fail("owned Mask fixture namespace has no bounded free key range")
    cursor = (
        max(item["Key"] for item in existing)
        if existing
        else f"{prefix}ffff{start - 1:060x}.json"
    )
    keys = [f"{prefix}ffff{start + index:060x}.json" for index in range(count)]
    return cursor, keys, b'{"fixture":"mask-gc-cutover-peer"}'


def _put_objects(owner, bucket: str, keys: list[str], body: bytes) -> None:
    def put(key: str) -> None:
        owner.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(put, keys))


def _delete_objects(owner, bucket: str, keys: list[str]) -> None:
    for start in range(0, len(keys), 1000):
        response = owner.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": key} for key in keys[start : start + 1000]],
                "Quiet": True,
            },
        )
        if response.get("Errors"):
            pytest.fail("owned Mask GC fixture objects could not be removed")


def _object_facts(items: list[dict]) -> tuple[tuple[str, str, int, str], ...]:
    return tuple(
        sorted(
            (
                item["Key"],
                item["ETag"].strip('"'),
                int(item["Size"]),
                item["LastModified"].isoformat(),
            )
            for item in items
        )
    )


def _success_data(response: dict, index: int = 0) -> dict:
    values = response["responses"]
    assert isinstance(values, list) and len(values) > index
    result = values[index]
    assert result["status"] == 200
    assert result["schema"] == _SCHEMA
    assert result["ok"] is True
    assert isinstance(result["data"], dict)
    return result["data"]


async def test_node_http_dry_run_continues_one_bounded_page_without_mutation(
    test_engine, tmp_path
):
    _node_binary()
    _, bucket, _, owner, _, cleanup = _owned_resources(test_engine)
    content_process = gc_process = None
    content_log = gc_log = None
    fixture_keys = []
    try:
        cursor, fixture_keys, body = _high_shard_fixture(owner, bucket, _PAGE_OBJECTS)
        _put_objects(owner, bucket, fixture_keys, body)
        facts_before = _object_facts(
            _list_s3_prefix(owner, bucket, "raster-masks/sha256/ff/ff/")
        )
        assert len(facts_before) >= _PAGE_OBJECTS
        assert set(fixture_keys) <= {fact[0] for fact in facts_before}
        content_process, content_log, content_origin, content_token = _start_runtime(
            "cleanup", bucket=bucket, credentials=cleanup, tmp_path=tmp_path
        )
        gc_process, gc_log, gc_origin, gc_token = _start_gc_runtime(
            test_engine, content_origin, content_token, tmp_path
        )
        state_before = await _gc_table_snapshot(test_engine)
        reference_count = await _reference_count(test_engine)

        result = _run_node_peer(
            gc_origin,
            gc_token,
            {
                "mode": "dry-run-continuation",
                "after_key": cursor,
                "scan_pages": 1,
                "delete_limit": 1,
            },
        )
        first = _success_data(result)
        second = _success_data(result, 1)
        for stats in (first, second):
            assert stats["dry_run"] is True
            assert stats["scan_pages"] == 1
            assert stats["referenced"] == reference_count
            assert stats["next_after_key"] is not None
        assert first["scanned"] == byte.MAX_GC_PAGE
        assert first["eligible"] == 0
        assert first["scan_complete"] is False
        assert second["scanned"] == 1
        assert second["eligible"] == 0
        assert second["scan_complete"] is True
        assert second["next_after_key"] != first["next_after_key"]

        assert await _gc_table_snapshot(test_engine) == state_before
        assert (
            _object_facts(_list_s3_prefix(owner, bucket, "raster-masks/sha256/ff/ff/"))
            == facts_before
        )
    finally:
        _stop_runtime(gc_process, gc_log)
        _stop_runtime(content_process, content_log)
        if fixture_keys:
            _delete_objects(owner, bucket, fixture_keys)
        owner.close()


async def test_node_http_run_deletes_staged_old_orphan_and_new_upload_is_retained(
    test_engine, tmp_path
):
    _node_binary()
    _, bucket, _, owner, _, cleanup = _owned_resources(test_engine)
    content_process = gc_process = None
    content_log = gc_log = None
    byte_client = None
    pool = None
    sweep_id = None
    object_key = None
    candidate = None
    try:
        cursor, fixture_keys, body = _high_shard_fixture(owner, bucket, 1)
        object_key = fixture_keys[0]
        owner.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=body,
            ContentType="application/json",
        )
        head = owner.head_object(Bucket=bucket, Key=object_key)
        candidate = byte.GcCandidate(
            object_key=object_key,
            etag=head["ETag"].strip('"'),
            last_modified=head["LastModified"],
            byte_size=len(body),
        )
        content_process, content_log, content_origin, content_token = _start_runtime(
            "cleanup", bucket=bucket, credentials=cleanup, tmp_path=tmp_path
        )
        dsn = _postgres_dsn(test_engine)
        # The GC protocol preserves ListObjects' timestamp precision; HTTP HEAD
        # can round it to seconds. Compare the receipt to its actual scan identity.
        byte_client = MaskGcClient(content_origin, content_token)
        observed = await asyncio.to_thread(
            byte_client.scan, byte.GcScanRequest(after_key=cursor, limit=1)
        )
        assert len(observed.items) == 1 and observed.items[0].object_key == object_key
        candidate = observed.items[0]
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=1,
            timeout=5,
            command_timeout=5,
        )
        data = AsyncpgMaskGcData(pool, timeout_seconds=5)
        fixture_worker = uuid4()
        claimed = await data.call(
            "claim_mask_gc_sweep",
            c.ClaimMaskGcSweepRequest(worker_id=fixture_worker),
        )
        if claimed.sweep is not None:
            sweep_id = claimed.sweep.sweep_id
        assert claimed.sweep is not None and claimed.sweep.complete is False
        assert claimed.sweep.after_key is None
        cutoff = candidate.last_modified + timedelta(days=1)
        async with test_engine.begin() as db:
            changed = await db.execute(
                text(
                    "UPDATE public.mask_gc_sweeps "
                    "SET cutoff=:cutoff, after_key=:cursor, "
                    "lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE sweep_id=:sweep_id AND worker_id=:worker_id "
                    "AND complete IS FALSE"
                ),
                {
                    "cutoff": cutoff,
                    "cursor": cursor,
                    "sweep_id": sweep_id,
                    "worker_id": fixture_worker,
                },
            )
            assert changed.rowcount == 1

        gc_process, gc_log, gc_origin, gc_token = _start_gc_runtime(
            test_engine, content_origin, content_token, tmp_path
        )
        reference_count = await _reference_count(test_engine)
        result = _run_node_peer(
            gc_origin,
            gc_token,
            {
                "mode": "run",
                "request": {"dry_run": False, "scan_pages": 1, "delete_limit": 1},
            },
        )
        stats = _success_data(result)
        assert stats["dry_run"] is False
        assert stats["scan_pages"] == 1
        assert stats["scanned"] == 1
        assert stats["staged"] == 1
        assert stats["eligible"] == 1
        assert stats["delete_claims"] == 1
        assert stats["completed"] == 1
        assert stats["deleted"] == 1
        assert stats["referenced"] == reference_count
        assert stats["scan_complete"] is True
        assert stats["next_after_key"] == candidate.object_key
        assert not _object_exists(owner, bucket, candidate.object_key)

        async with test_engine.connect() as db:
            operation_id = await db.scalar(
                text(
                    "SELECT operation_id FROM public.mask_gc_intents "
                    "WHERE object_key=:key"
                ),
                {"key": candidate.object_key},
            )
            epoch, active_operation = (
                await db.execute(
                    text(
                        "SELECT epoch, active_operation_id "
                        "FROM public.mask_gc_key_epochs WHERE object_key=:key"
                    ),
                    {"key": candidate.object_key},
                )
            ).one()
        assert operation_id is not None
        assert epoch == 1 and active_operation is None
        receipt = await asyncio.to_thread(
            byte_client.status,
            byte.DeleteStatusRequest(operation_id=operation_id),
        )
        assert receipt.state == "completed" and receipt.outcome == "deleted"
        assert receipt.request.object_key == candidate.object_key
        assert receipt.request.etag == candidate.etag
        assert receipt.request.last_modified == candidate.last_modified

        owner.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=body,
            ContentType="application/json",
        )
        assert _object_exists(owner, bucket, object_key)
        async with test_engine.begin() as db:
            begun = await _native_call(
                db,
                "begin_mask_content_write",
                {"object_keys": [candidate.object_key]},
            )
        assert begun["ok"] is True
        assert begun["data"]["items"] == [
            {"object_key": candidate.object_key, "epoch": 1}
        ]

        state_before = await _gc_table_snapshot(test_engine)
        retained_result = _run_node_peer(
            gc_origin,
            gc_token,
            {
                "mode": "run",
                "request": {
                    "dry_run": True,
                    "scan_pages": 1,
                    "delete_limit": 1,
                    "after_key": cursor,
                },
            },
        )
        retained = _success_data(retained_result)
        assert retained["dry_run"] is True
        assert retained["scanned"] == 1
        assert retained["eligible"] == 0
        assert retained["referenced"] == reference_count
        assert retained["scan_complete"] is True
        assert retained["next_after_key"] == candidate.object_key
        assert await _gc_table_snapshot(test_engine) == state_before
        assert _object_exists(owner, bucket, candidate.object_key)
        receipt_after_reupload = await asyncio.to_thread(
            byte_client.status,
            byte.DeleteStatusRequest(operation_id=operation_id),
        )
        assert receipt_after_reupload == receipt
    finally:
        if byte_client is not None:
            byte_client.close()
        _stop_runtime(gc_process, gc_log)
        _stop_runtime(content_process, content_log)
        if pool is not None:
            await pool.close()
        if sweep_id is not None and candidate is not None:
            async with test_engine.begin() as db:
                await db.execute(
                    text("DELETE FROM public.mask_gc_key_epochs WHERE object_key=:key"),
                    {"key": candidate.object_key},
                )
                await db.execute(
                    text("DELETE FROM public.mask_gc_intents WHERE object_key=:key"),
                    {"key": candidate.object_key},
                )
                await db.execute(
                    text("DELETE FROM public.mask_gc_sweeps WHERE sweep_id=:id"),
                    {"id": sweep_id},
                )
        if object_key is not None:
            owner.delete_object(Bucket=bucket, Key=object_key)
        owner.close()
