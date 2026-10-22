"""Small write-path comparison for the restricted Labeling video owner."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.video_writer_client import VideoWriterHttpClient
from aap_labeling.video_writer_contract import (
    VideoWriterRejected,
    make_video_writer_command,
    video_writer_command_digest,
)
from aap_preannotation_results.video_execution import ExecutionProof
from tests.test_labeling_video_writer_containers import (
    _RestrictedWriterContainer,
    _inspect_image,
)
from tests.test_labeling_video_writer_restore import _seal_window_with_lexemes
from tests.test_video_execution_native import _step
from tests.test_video_window_journal_native import _prepared_execution

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned disposable PostgreSQL resources",
    ),
    pytest.mark.skipif(
        not os.environ.get("AAP_LABELING_VIDEO_WRITER_IMAGE"),
        reason="the verifier did not provide the immutable Labeling owner image",
    ),
]

_JOBS_PER_BINDING = 16
_FRAMES_PER_JOB = 6
_CONCURRENCY = 4
_MAX_BUSY_RETRIES = 20
_RETRY_DELAY_SECONDS = 0.025


_DIRECT_POSTGRES_SCRIPT = r"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time

from aap_labeling.video_writer import VideoWriterManager
from aap_labeling.video_writer_contract import VideoWriterRejected
from aap_labeling.video_writer_postgres import VideoWriterPostgresProvider

commands = json.loads(sys.stdin.read())
dsn = Path("/run/secrets/video_writer_database_url").read_text(encoding="utf-8").strip()
counts = {"native_function_calls": 0, "execute_attempts": 0, "busy_retries": 0}
active = 0
peak = 0

class CountingTransaction:
    def __init__(self, transaction):
        self.transaction = transaction

    async def call(self, action, arguments):
        counts["native_function_calls"] += 1
        return await self.transaction.call(action, arguments)

class CountingProvider(VideoWriterPostgresProvider):
    @asynccontextmanager
    async def transaction(self):
        async with super().transaction() as transaction:
            yield CountingTransaction(transaction)

async def main():
    global active, peak
    manager = VideoWriterManager(
        CountingProvider(dsn, connect_timeout=5, command_timeout=30)
    )
    semaphore = asyncio.Semaphore(4)
    launch_barrier = asyncio.Barrier(4)
    started = time.perf_counter()

    async def publish(command):
        global active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            await launch_barrier.wait()
            request_started = time.perf_counter()
            local_attempts = 0
            local_retries = 0
            try:
                for retry in range(21):
                    counts["execute_attempts"] += 1
                    local_attempts += 1
                    try:
                        receipt = await manager.execute(command)
                    except VideoWriterRejected as error:
                        if error.code != "execution_busy" or retry == 20:
                            raise
                        counts["busy_retries"] += 1
                        local_retries += 1
                        await asyncio.sleep(0.025)
                    else:
                        if (
                            receipt.outcome != "completed"
                            or receipt.status != "pending_review"
                        ):
                            raise RuntimeError(
                                "the direct owner returned a wrong receipt"
                            )
                        return {
                            "operation_id": str(receipt.operation_id),
                            "execute_attempts": local_attempts,
                            "busy_retries": local_retries,
                            "latency_ms": (
                                time.perf_counter() - request_started
                            )
                            * 1000,
                        }
                raise RuntimeError("bounded execution_busy retries exhausted")
            finally:
                active -= 1

    receipts = await asyncio.gather(*(publish(command) for command in commands))
    elapsed_ms = (time.perf_counter() - started) * 1000
    memory = {}
    try:
        memory["owner_process_rss_bytes"] = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/1/status").read_text(encoding="ascii").splitlines()
            if line.startswith("VmRSS:")
        )
    except (OSError, StopIteration, ValueError):
        memory["owner_process_rss_bytes"] = None
    try:
        memory["cgroup_memory_current_bytes"] = int(
            Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip()
        )
    except (OSError, ValueError):
        memory["cgroup_memory_current_bytes"] = None
    print(json.dumps({
        "binding": "direct_postgres",
        "logical_jobs": len(commands),
        "frames_per_job": 6,
        "concurrency_limit": 4,
        "observed_peak": peak,
        "total_elapsed_ms": elapsed_ms,
        "receipts": receipts,
        **counts,
        **memory,
    }, allow_nan=False, sort_keys=True))

asyncio.run(main())
"""


_MEMORY_SCRIPT = r"""
import json
from pathlib import Path

values = {
    "owner_process_rss_bytes": None,
    "cgroup_memory_current_bytes": None,
}
try:
    values["owner_process_rss_bytes"] = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/1/status").read_text(encoding="ascii").splitlines()
        if line.startswith("VmRSS:")
    )
except (OSError, StopIteration, ValueError):
    pass
try:
    values["cgroup_memory_current_bytes"] = int(
        Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip()
    )
except (OSError, ValueError):
    pass
print(json.dumps(values, sort_keys=True))
"""


def _exec_in_owned_container(
    owner: _RestrictedWriterContainer,
    script: str,
    *,
    stdin: str = "",
    timeout: int = 180,
) -> str:
    owner._inspect_owned()
    if owner.container_id is None:
        pytest.fail("the owned Labeling writer container is not running")
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "10006:10006",
                owner.container_id,
                "python",
                "-I",
                "-c",
                script,
            ],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the bounded in-container Labeling writer sample timed out")
    if result.returncode != 0:
        pytest.fail("the bounded in-container direct PostgreSQL sample failed")
    return result.stdout.strip()


async def _prepare_one(sessions) -> dict[str, Any]:
    async with sessions.begin() as db:
        job, proof, plan = await _prepared_execution(db)
        if (
            plan.source.frame_count != _FRAMES_PER_JOB
            or plan.to_frame - plan.from_frame + 1 != _FRAMES_PER_JOB
            or len(plan.windows) != 3
        ):
            pytest.fail("the Native producer fixture did not freeze six bbox frames")
        proof = await _renew_claim(db, proof)
        previous = None
        for ordinal in range(len(plan.windows)):
            previous = await _seal_window_with_lexemes(
                db, proof, plan, ordinal, previous
            )
        proof = await _renew_claim(db, proof)

    expected_input = {
        "input_id": str(plan.input_id),
        "request_digest": plan.request_digest,
        "plan_digest": plan.source_plan_digest,
        "source_version_id": plan.source.source_version_id,
        "source_sha256": plan.source.source_sha256,
    }
    command = _make_command(proof, expected_input)
    command_input = command.expected_input.model_dump(mode="json", by_alias=True)
    return {
        "job_id": job.id,
        "proof": proof,
        "expected_input": command_input,
        "command": command,
        "request_digest": video_writer_command_digest(command),
    }


async def _prepare_cohort(sessions) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for _ in range(0, _JOBS_PER_BINDING, _CONCURRENCY):
        entries.extend(
            await asyncio.gather(*(_prepare_one(sessions) for _ in range(_CONCURRENCY)))
        )
    if (
        len(entries) != _JOBS_PER_BINDING
        or len({entry["job_id"] for entry in entries}) != _JOBS_PER_BINDING
        or len({entry["proof"].run_id for entry in entries}) != _JOBS_PER_BINDING
    ):
        pytest.fail("the Native producer fixture did not create distinct claimed jobs")
    return entries


def _make_command(proof: ExecutionProof, expected_input: dict[str, Any]):
    return make_video_writer_command(
        proof,
        outcome="completed",
        expected_input=expected_input,
        frame_results=(),
    )


async def _renew_claim(db, proof: ExecutionProof) -> ExecutionProof:
    renewed = await _step(
        db,
        "renew",
        {**proof.model_dump(mode="json", by_alias=True), "lease_seconds": 300},
    )
    if renewed.get("status") != "ok" or renewed.get("execution", {}).get(
        "run_id"
    ) != str(proof.run_id):
        pytest.fail("a frozen producer claim could not be kept live")
    execution = renewed["execution"]
    current_proof = ExecutionProof.model_validate(
        {name: execution[name] for name in ExecutionProof.model_fields}
    )
    if not (
        current_proof.job_id == proof.job_id
        and current_proof.run_id == proof.run_id
        and current_proof.worker_id == proof.worker_id
        and current_proof.generation == proof.generation
    ):
        pytest.fail("renewal changed the claimed execution generation")
    return current_proof


async def _renew_cohort(sessions, entries: list[dict[str, Any]]) -> None:
    async with sessions.begin() as db:
        for entry in entries:
            proof = await _renew_claim(db, entry["proof"])
            command = _make_command(proof, entry["expected_input"])
            if command.operation_id != entry["command"].operation_id:
                pytest.fail("renewal changed the frozen operation identity")
            entry["proof"] = proof
            entry["command"] = command
            entry["request_digest"] = video_writer_command_digest(command)


def _command_payload(entries: list[dict[str, Any]]) -> str:
    return json.dumps(
        [entry["command"].model_dump(mode="json", by_alias=True) for entry in entries],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _run_http_sample(
    owner: _RestrictedWriterContainer,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    if owner.origin == "" or owner.container_id is None:
        pytest.fail("the HTTP owner endpoint was not ready")
    requests = 0

    async def count_request(_request) -> None:
        nonlocal requests
        requests += 1

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90),
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [count_request]},
    ) as http_client:
        async with VideoWriterHttpClient(
            owner.origin,
            owner.token,
            timeout_seconds=90,
            client=http_client,
        ) as client:
            semaphore = asyncio.Semaphore(_CONCURRENCY)
            launch_barrier = asyncio.Barrier(_CONCURRENCY)
            active = 0
            peak = 0
            execute_attempts = 0
            busy_retries = 0
            started = time.perf_counter()

            async def publish(entry: dict[str, Any]) -> dict[str, Any]:
                nonlocal active, peak, execute_attempts, busy_retries
                async with semaphore:
                    active += 1
                    peak = max(peak, active)
                    await launch_barrier.wait()
                    request_started = time.perf_counter()
                    local_attempts = 0
                    local_retries = 0
                    try:
                        for retry in range(_MAX_BUSY_RETRIES + 1):
                            execute_attempts += 1
                            local_attempts += 1
                            try:
                                receipt = await client.execute(entry["command"])
                            except VideoWriterRejected as error:
                                if (
                                    error.code != "execution_busy"
                                    or retry == _MAX_BUSY_RETRIES
                                ):
                                    raise
                                busy_retries += 1
                                local_retries += 1
                                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                            else:
                                if (
                                    receipt.outcome != "completed"
                                    or receipt.status != "pending_review"
                                ):
                                    raise RuntimeError(
                                        "the HTTP owner returned a wrong receipt"
                                    )
                                return {
                                    "operation_id": str(receipt.operation_id),
                                    "execute_attempts": local_attempts,
                                    "busy_retries": local_retries,
                                    "latency_ms": (
                                        time.perf_counter() - request_started
                                    )
                                    * 1000,
                                }
                        raise RuntimeError("bounded execution_busy retries exhausted")
                    finally:
                        active -= 1

            try:
                async with asyncio.timeout(240):
                    receipts = await asyncio.gather(
                        *(publish(entry) for entry in entries)
                    )
            except VideoWriterRejected:
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000

    raw_memory = _exec_in_owned_container(owner, _MEMORY_SCRIPT, timeout=20)
    try:
        memory = json.loads(raw_memory)
    except json.JSONDecodeError:
        pytest.fail("the bounded HTTP sample memory report was malformed")
    if not isinstance(memory, dict) or set(memory) != {
        "owner_process_rss_bytes",
        "cgroup_memory_current_bytes",
    }:
        pytest.fail("the bounded HTTP sample memory report was open")
    return {
        "binding": "http",
        "logical_jobs": len(entries),
        "frames_per_job": _FRAMES_PER_JOB,
        "concurrency_limit": _CONCURRENCY,
        "observed_peak": peak,
        "total_elapsed_ms": elapsed_ms,
        "execute_attempts": execute_attempts,
        "busy_retries": busy_retries,
        "http_requests": requests,
        "receipts": receipts,
        **memory,
    }


async def _run_direct_postgres_sample(
    owner: _RestrictedWriterContainer,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = await asyncio.to_thread(
        _exec_in_owned_container,
        owner,
        _DIRECT_POSTGRES_SCRIPT,
        stdin=_command_payload(entries),
        timeout=240,
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        pytest.fail("the bounded direct PostgreSQL sample report was malformed")
    expected_keys = {
        "binding",
        "logical_jobs",
        "frames_per_job",
        "concurrency_limit",
        "observed_peak",
        "total_elapsed_ms",
        "receipts",
        "native_function_calls",
        "execute_attempts",
        "busy_retries",
        "owner_process_rss_bytes",
        "cgroup_memory_current_bytes",
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        pytest.fail("the direct PostgreSQL sample returned an open result envelope")
    return result


def _assert_sample_receipts(
    sample: dict[str, Any], entries: list[dict[str, Any]], binding: str
) -> None:
    expected = {str(entry["command"].operation_id) for entry in entries}
    actual_rows = sample.get("receipts")
    if not isinstance(actual_rows, list) or len(actual_rows) != len(entries):
        pytest.fail(f"the {binding} sample did not return one receipt per job")
    if any(not isinstance(row, dict) for row in actual_rows):
        pytest.fail(f"the {binding} sample returned an open receipt summary")
    actual = {row.get("operation_id"): row for row in actual_rows}
    if len(actual) != len(entries) or set(actual) != expected:
        pytest.fail(f"the {binding} sample did not return distinct operation receipts")
    for row in actual.values():
        if not (
            set(row)
            == {"operation_id", "execute_attempts", "busy_retries", "latency_ms"}
            and isinstance(row.get("latency_ms"), (int, float))
            and row["latency_ms"] >= 0
            and row.get("execute_attempts") == row.get("busy_retries", -1) + 1
            and row.get("busy_retries") >= 0
            and row.get("busy_retries") <= _MAX_BUSY_RETRIES
        ):
            pytest.fail(f"the {binding} receipt summary did not match")
    expected_keys = {
        "binding",
        "logical_jobs",
        "frames_per_job",
        "concurrency_limit",
        "observed_peak",
        "total_elapsed_ms",
        "execute_attempts",
        "busy_retries",
        "receipts",
        "owner_process_rss_bytes",
        "cgroup_memory_current_bytes",
        "native_function_calls" if binding == "direct_postgres" else "http_requests",
    }
    if not (
        set(sample) == expected_keys
        and sample.get("binding") == binding
        and sample.get("logical_jobs") == _JOBS_PER_BINDING
        and sample.get("frames_per_job") == _FRAMES_PER_JOB
        and sample.get("concurrency_limit") == _CONCURRENCY
        and sample.get("observed_peak") == _CONCURRENCY
        and sample.get("execute_attempts") >= sample.get("logical_jobs")
        and sample.get("execute_attempts")
        <= _JOBS_PER_BINDING * (_MAX_BUSY_RETRIES + 1)
        and sample.get("busy_retries")
        == sample.get("execute_attempts") - sample.get("logical_jobs")
        and sample.get("busy_retries") <= _JOBS_PER_BINDING * _MAX_BUSY_RETRIES
        and sum(row["execute_attempts"] for row in actual_rows)
        == sample.get("execute_attempts")
        and sum(row["busy_retries"] for row in actual_rows)
        == sample.get("busy_retries")
        and (
            sample.get("native_function_calls", 0) > 0
            if binding == "direct_postgres"
            else sample.get("http_requests", 0) >= sample.get("execute_attempts", 0)
        )
    ):
        pytest.fail(f"the {binding} sample counts did not match its bounded workload")


def _record_sample(request, sample: dict[str, Any]) -> None:
    metadata = {
        "scope": "local finite bbox write sample; not production capacity",
        "producer": "existing Native mock_bbox checkpoint fixture; no inference call",
        "model_calls": 0,
        "limitations": [
            "not a production capacity gate",
            "does not cover maximum buffers or large Mask payloads",
            "does not compare latency with the legacy writer",
        ],
        "caller_and_database_limits": (
            "PostgreSQL runs outside the owner container; the HTTP pytest caller is "
            "outside its cgroup; the direct PostgreSQL baseline adds a Python process "
            "inside the same owner cgroup"
        ),
        **sample,
    }
    request.node.user_properties.append(
        (
            "labeling_video_writer_finite_write_sample",
            json.dumps(
                metadata,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    )


async def _assert_published_rows(engine, entries: list[dict[str, Any]]) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        for entry in entries:
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT job.status AS job_status,job.revision,"
                            "job.staged_result,"
                            "receipt.operation_id,receipt.run_id,"
                            "receipt.request_digest,receipt.outcome,receipt.status "
                            "AS receipt_status "
                            "FROM public.video_tracker_jobs AS job "
                            "JOIN aap_labeling_v1.video_writer_receipts AS receipt "
                            "ON receipt.job_id=job.id WHERE job.id=:job"
                        ),
                        {"job": entry["job_id"]},
                    )
                )
                .mappings()
                .one()
            )
            command = entry["command"]
            staged = row["staged_result"]
            evidence = staged["tracker_input_evidence"]
            if not (
                row["job_status"] == "pending_review"
                and row["revision"] == 1
                and row["operation_id"] == command.operation_id
                and row["run_id"] == command.execution.run_id
                and row["request_digest"] == entry["request_digest"]
                and row["outcome"] == "completed"
                and row["receipt_status"] == "pending_review"
            ):
                pytest.fail("the owner did not persist the expected immutable receipt")
            if not (
                len(staged["results"]) == _FRAMES_PER_JOB
                and [value["frame_index"] for value in staged["results"]]
                == list(range(_FRAMES_PER_JOB))
                and all(
                    value["geometry"]["type"] == "bbox" for value in staged["results"]
                )
                and all(value["geometry"]["x"] == 0.0 for value in staged["results"])
                and all(value["geometry"]["y"] == 1e-5 for value in staged["results"])
                and all(value["geometry"]["w"] == 0.3 for value in staged["results"])
                and all(value["geometry"]["h"] == 0.4 for value in staged["results"])
                and all(value["outside"] is False for value in staged["results"])
                and all(value["instance_id"] is None for value in staged["results"])
                and all(value["primary"] is False for value in staged["results"])
                and all(value["confidence"] == 1e-7 for value in staged["results"])
                and staged["output_geometry"] == "bbox"
                and evidence["input_id"] == entry["expected_input"]["input_id"]
                and evidence["request_digest"]
                == entry["expected_input"]["request_digest"]
                and evidence["plan_digest"] == entry["expected_input"]["plan_digest"]
                and evidence["source_version_id"]
                == entry["expected_input"]["source_version_id"]
                and evidence["source_sha256"]
                == entry["expected_input"]["source_sha256"]
            ):
                pytest.fail("the owner changed canonical rows or source fingerprints")


async def test_labeling_video_writer_finite_bbox_write_sample(
    test_engine, tmp_path, request
):
    image_ref = os.environ["AAP_LABELING_VIDEO_WRITER_IMAGE"]
    image_id, _ = _inspect_image(image_ref)
    owner = _RestrictedWriterContainer(
        dsn=os.environ["AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL"],
        output_geometry="bbox",
        mask_url="",
        mask_token="",
        image_id=image_id,
        tmp_path=tmp_path,
    )
    try:
        owner.start()
        await owner.wait_ready()
        sessions = async_sessionmaker(test_engine, expire_on_commit=False)
        direct_entries = await _prepare_cohort(sessions)
        http_entries = await _prepare_cohort(sessions)
        await _renew_cohort(sessions, direct_entries)
        direct_sample = await _run_direct_postgres_sample(owner, direct_entries)
        _record_sample(request, direct_sample)
        _assert_sample_receipts(direct_sample, direct_entries, "direct_postgres")
        await _assert_published_rows(test_engine, direct_entries)

        await _renew_cohort(sessions, http_entries)
        http_sample = await _run_http_sample(owner, http_entries)
        _record_sample(request, http_sample)
        _assert_sample_receipts(http_sample, http_entries, "http")
        await _assert_published_rows(test_engine, http_entries)

        direct_ids = {entry["job_id"] for entry in direct_entries}
        http_ids = {entry["job_id"] for entry in http_entries}
        direct_runs = {entry["proof"].run_id for entry in direct_entries}
        http_runs = {entry["proof"].run_id for entry in http_entries}
        direct_operations = {entry["command"].operation_id for entry in direct_entries}
        http_operations = {entry["command"].operation_id for entry in http_entries}
        if (
            direct_ids & http_ids
            or direct_runs & http_runs
            or direct_operations & http_operations
        ):
            pytest.fail("the two bindings reused claimed write operations")
    finally:
        owner.stop()
