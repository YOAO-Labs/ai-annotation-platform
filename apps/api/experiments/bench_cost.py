"""Bounded localhost cost breakdown for the isolated audit-summary experiment."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import select
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date
from itertools import chain, repeat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pydantic

API_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
GO_DIR = ROOT / "apps/audit-summary"
sys.path.insert(0, str(API_DIR))

from app.services.audit_summary import build_monthly_summary  # noqa: E402
from experiments import audit_summary  # noqa: E402

MONTH = "2024-02"
START = date(2024, 2, 1)
END = date(2024, 3, 1)
ROWS = 10_000
CAPACITY_BLOCKS = 5
MAX_BYTES = 8 * 1024 * 1024


def go_build_env():
    return {
        **{
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "GOCACHE", "GOMODCACHE")
            if key in os.environ
        },
        "GOPROXY": "off",
        "GOTOOLCHAIN": "local",
    }


def make_rows():
    roles = (
        "annotator",
        "reviewer",
        "super_admin",
        "project_manager",
        "dataset_manager",
        "trainer",
        "viewer",
        "system",
    )
    statuses = (0, 2, 3, 4, 5)
    return [
        SimpleNamespace(
            day=date(2024, 2, i % 29 + 1),
            action=f"action.{i % 32:02d}",
            target_type=f"target.{i % 16:02d}",
            actor_role=roles[i % len(roles)],
            status_family=statuses[i % len(statuses)],
            event_count=10_000_000_000_000_000 + i % 97,
        )
        for i in range(ROWS)
    ]


async def encode(rows):
    data = bytearray()
    async for chunk in audit_summary._request_body(MONTH, None, rows):
        data.extend(chunk)
    return bytes(data)


def sync_ms(fn, samples, iterations):
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        for _ in range(iterations):
            fn()
        values.append(1000 * (time.perf_counter() - start) / iterations)
    return statistics.median(values)


async def async_ms(fn, samples, iterations):
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        for _ in range(iterations):
            await fn()
        values.append(1000 * (time.perf_counter() - start) / iterations)
    return statistics.median(values)


async def post(client, url, content):
    async with client.stream(
        "POST",
        f"{url}/v1/audit/monthly-summary",
        content=content,
        headers={"Content-Type": "application/json"},
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Go summary returned {response.status_code}")
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError("Go response exceeded the local 8 MiB limit")
            chunks.append(chunk)
    return b"".join(chunks)


async def post_rows(client, url, rows):
    sent = [0]

    async def body():
        async for chunk in audit_summary._request_body(MONTH, None, rows):
            sent[0] += len(chunk)
            yield chunk

    raw = await post(client, url, body())
    summary = audit_summary._decode_summary(
        json.loads(raw), month=MONTH, materialized_through=None
    )
    return summary, raw, sent[0]


def go_benchmarks(request_path):
    env = go_build_env() | {
        "AAP_AUDIT_BENCH_REQUEST": str(request_path),
    }
    result = subprocess.run(
        [
            "go",
            "test",
            "-run=^$",
            "-bench=^Benchmark(DecodeRequest|Summarize|EncodeResponse)$",
            "-benchtime=200ms",
            "-count=3",
            "-benchmem",
            ".",
        ],
        cwd=GO_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    runs = {
        name: []
        for name in (
            "BenchmarkDecodeRequest",
            "BenchmarkSummarize",
            "BenchmarkEncodeResponse",
        )
    }
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = "-".join(fields[0].split("-")[:-1])
        if name not in runs:
            continue
        runs[name].append(
            {
                "iterations": int(fields[1]),
                "ms": float(fields[2]) / 1_000_000,
                "bytes": int(fields[fields.index("B/op") - 1]),
                "allocs": int(fields[fields.index("allocs/op") - 1]),
            }
        )
    if any(len(values) != 3 for values in runs.values()):
        raise RuntimeError(
            f"incomplete Go benchmarks:\n{result.stdout}\n{result.stderr}"
        )
    return runs


def go_server(binary):
    process = subprocess.Popen(
        [str(binary), "-listen", "127.0.0.1:0"],
        cwd=GO_DIR,
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            raise RuntimeError("Go server did not report readiness")
        ready = json.loads(process.stdout.readline())
        assert ready["event"] == "ready" and ready["service"] == "audit-summary"
        return process, f"http://{ready['address']}"
    except Exception:
        stop_server(process)
        raise


def stop_server(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream:
            stream.close()


async def rss_sampler(pids, stop, peaks, counts):
    while True:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, pids))],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2:
                pid, rss = map(int, fields)
                peaks[pid] = max(peaks.get(pid, 0), rss)
                counts[pid] = counts.get(pid, 0) + 1
        if stop.is_set():
            return
        await asyncio.sleep(0.05)


async def main():
    rows = make_rows()
    request = await encode(rows)
    assert len(json.loads(request)["rows"]) == ROWS
    assert len(request) < MAX_BYTES
    expected = build_monthly_summary(
        month=MONTH,
        month_start=START,
        month_end=END,
        materialized_through=None,
        rows=rows,
    ).model_dump(mode="json")
    py_reducer = sync_ms(
        lambda: build_monthly_summary(
            month=MONTH,
            month_start=START,
            month_end=END,
            materialized_through=None,
            rows=rows,
        ),
        5,
        10,
    )
    py_encode = await async_ms(lambda: encode(rows), 5, 2)

    with tempfile.TemporaryDirectory(prefix="aap-audit-cost-") as temp:
        temp = Path(temp)
        request_path = temp / "request.json"
        request_path.write_bytes(request)
        go_runs = go_benchmarks(request_path)
        binary = temp / "audit-summary"
        subprocess.run(
            ["go", "build", "-trimpath", "-o", str(binary), "."],
            cwd=GO_DIR,
            env=go_build_env(),
            check=True,
        )
        process, url = go_server(binary)
        try:
            fresh = await audit_summary.monthly_audit_summary(
                month=MONTH, materialized_through=None, rows=rows, base_url=url
            )
            assert fresh.model_dump(mode="json") == expected
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                reused, response, sent = await post_rows(client, url, rows)
                assert reused.model_dump(mode="json") == expected
                assert sent == len(request)
                py_decode = sync_ms(
                    lambda: audit_summary._decode_summary(
                        json.loads(response), month=MONTH, materialized_through=None
                    ),
                    5,
                    10,
                )
                client_init = await async_ms(_new_client, 5, 20)
                wire_http = await async_ms(lambda: post(client, url, request), 5, 2)

                paths = {"new": [], "reused": []}
                for sample in range(5):
                    names = ("new", "reused") if sample % 2 == 0 else ("reused", "new")
                    for name in names:
                        start = time.perf_counter()
                        for _ in range(5):
                            if name == "new":
                                await audit_summary.monthly_audit_summary(
                                    month=MONTH,
                                    materialized_through=None,
                                    rows=rows,
                                    base_url=url,
                                )
                            else:
                                await post_rows(client, url, rows)
                        paths[name].append(1000 * (time.perf_counter() - start) / 5)

                repeated = chain.from_iterable(repeat(rows, CAPACITY_BLOCKS))
                sent_capacity = [0]

                async def capacity_body():
                    async for chunk in audit_summary._request_body(
                        MONTH, None, repeated
                    ):
                        sent_capacity[0] += len(chunk)
                        yield chunk

                stop = asyncio.Event()
                peaks, counts = {}, {}
                sampler = asyncio.create_task(
                    rss_sampler([os.getpid(), process.pid], stop, peaks, counts)
                )
                await asyncio.sleep(0)
                try:
                    capacity_raw = await post(client, url, capacity_body())
                    capacity = audit_summary._decode_summary(
                        json.loads(capacity_raw), month=MONTH, materialized_through=None
                    )
                    assert (
                        capacity.totals.event_count
                        == fresh.totals.event_count * CAPACITY_BLOCKS
                    )
                    assert (
                        capacity.totals.error_count
                        == fresh.totals.error_count * CAPACITY_BLOCKS
                    )
                    assert sent_capacity[0] <= MAX_BYTES
                finally:
                    stop.set()
                    await sampler
        finally:
            stop_server(process)

    def go_metric(name, field):
        return statistics.median(run[field] for run in go_runs[name])

    result = {
        "date": time.strftime("%Y-%m-%d %Z"),
        "host": f"{platform.platform()} ({platform.machine()})",
        "python": platform.python_version(),
        "httpx": httpx.__version__,
        "pydantic": pydantic.__version__,
        "go": subprocess.run(
            ["go", "version"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "rows": ROWS,
        "request_bytes": len(request),
        "response_bytes": len(response),
        "python_reducer_ms": py_reducer,
        "python_encode_ms": py_encode,
        "python_decode_validate_ms": py_decode,
        "client_init_ms": client_init,
        "go_runs": go_runs,
        "go_medians": {name: go_metric(name, "ms") for name in go_runs},
        "go_bytes": {name: go_metric(name, "bytes") for name in go_runs},
        "go_allocs": {name: go_metric(name, "allocs") for name in go_runs},
        "wire_http_ms": wire_http,
        "new_client_ms": statistics.median(paths["new"]),
        "reused_client_ms": statistics.median(paths["reused"]),
        "client_samples": paths,
        "capacity_rows": ROWS * CAPACITY_BLOCKS,
        "capacity_bytes": sent_capacity[0],
        "rss_kib": {"python": peaks.get(os.getpid()), "go": peaks.get(process.pid)},
        "rss_samples": min(counts.values(), default=0),
    }
    print(json.dumps(result, indent=2))


async def _new_client():
    async with httpx.AsyncClient(timeout=10, trust_env=False):
        pass


if __name__ == "__main__":
    asyncio.run(main())
