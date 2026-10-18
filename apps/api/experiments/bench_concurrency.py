"""Bounded local concurrency observations for the isolated audit-summary peer."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments import audit_summary, bench_cost  # noqa: E402

ROW_COUNTS = (10_000, 50_000)
CONCURRENCY_LEVELS = (1, 2, 4, 8)
SERVER_MAX_CONCURRENT = 2
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_ROUNDS = 20


def _local_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--base-url must be a valid loopback URL"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("--base-url must be an HTTP loopback URL")
    return value.rstrip("/")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return round(sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)], 3)


def _memory_kib(pid: int) -> tuple[str, int | None]:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return "process_VmHWM_high_water_kib", int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable", None
    value = result.stdout.strip()
    return "sampled_process_RSS_kib", int(value) if value.isdigit() else None


async def _watch_memory(pid: int | None, stop: asyncio.Event) -> dict[str, object]:
    result: dict[str, object] = {
        "peer_pid": pid,
        "measurement": "unavailable",
        "max_kib": None,
        "samples": 0,
        "interval_ms": 50,
    }
    if pid is None:
        return result
    while True:
        measurement, value = await asyncio.to_thread(_memory_kib, pid)
        result["measurement"] = measurement
        if value is not None:
            result["max_kib"] = max(result["max_kib"] or 0, value)
            result["samples"] += 1
        if stop.is_set():
            return result
        await asyncio.sleep(0.05)


async def _request(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    payload: bytes,
    oracle: dict,
    rows: int,
    concurrency: int,
    round_number: int,
    request_number: int,
) -> dict[str, object]:
    started = time.perf_counter()
    sample: dict[str, object] = {
        "rows": rows,
        "concurrency": concurrency,
        "round": round_number,
        "request": request_number,
        "request_bytes": len(payload),
    }
    try:
        response = await client.post(
            f"{base_url}/v1/audit/monthly-summary",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
        sample["status"] = response.status_code
        body = response.json()
        if response.status_code == 200:
            summary = audit_summary._decode_summary(
                body, month=bench_cost.MONTH, materialized_through=None
            )
            if summary.model_dump(mode="json") == oracle:
                sample["outcome"] = "accepted"
            else:
                sample["outcome"] = "error"
                sample["error"] = "response_did_not_match_python_oracle"
        elif response.status_code == 503:
            if (
                concurrency > SERVER_MAX_CONCURRENT
                and isinstance(body, dict)
                and body.get("code") == "service_overloaded"
                and body.get("retryable") is True
            ):
                sample["outcome"] = "overloaded"
                sample["code"] = body["code"]
                sample["retryable"] = body["retryable"]
            else:
                sample["outcome"] = "error"
                sample["error"] = "unexpected_or_invalid_503"
        else:
            sample["outcome"] = "error"
            sample["error"] = f"unexpected_http_status_{response.status_code}"
    except Exception as exc:
        sample["outcome"] = "error"
        sample["error"] = f"{type(exc).__name__}: {exc}"
    sample["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return sample


async def _burst(
    client: httpx.AsyncClient,
    base_url: str,
    scenario: dict[str, object],
    concurrency: int,
    round_number: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    ready = 0
    release = asyncio.Event()
    rows = int(scenario["rows"])
    payload = scenario["payload"]
    oracle = scenario["oracle"]
    assert isinstance(payload, bytes) and isinstance(oracle, dict)

    async def run(index: int) -> dict[str, object]:
        nonlocal ready
        ready += 1
        if ready == concurrency:
            release.set()
        await release.wait()
        return await _request(
            client,
            base_url=base_url,
            payload=payload,
            oracle=oracle,
            rows=rows,
            concurrency=concurrency,
            round_number=round_number,
            request_number=index,
        )

    started = time.perf_counter()
    samples = await asyncio.gather(*(run(index) for index in range(concurrency)))
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    burst = {
        "rows": rows,
        "concurrency": concurrency,
        "round": round_number,
        "elapsed_ms": elapsed_ms,
        "accepted": sum(item["outcome"] == "accepted" for item in samples),
        "overloaded_503": sum(item["outcome"] == "overloaded" for item in samples),
        "errors": sum(item["outcome"] == "error" for item in samples),
    }
    return burst, samples


async def _scenarios() -> list[dict[str, object]]:
    scenarios = []
    for row_count in ROW_COUNTS:
        rows = bench_cost.make_rows(row_count)
        oracle = bench_cost.build_monthly_summary(
            month=bench_cost.MONTH,
            month_start=bench_cost.START,
            month_end=bench_cost.END,
            materialized_through=None,
            rows=rows,
        ).model_dump(mode="json")
        payload = await bench_cost.encode(rows)
        if len(payload) > MAX_REQUEST_BYTES:
            raise RuntimeError("synthetic request exceeds the 64 MiB experiment limit")
        scenarios.append({"rows": row_count, "oracle": oracle, "payload": payload})
    return scenarios


async def _run(base_url: str, rounds: int, timeout: float, peer_pid: int | None):
    scenarios = await _scenarios()
    stop = asyncio.Event()
    memory_task = asyncio.create_task(_watch_memory(peer_pid, stop))
    samples = []
    bursts = []
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=max(CONCURRENCY_LEVELS)),
            trust_env=False,
        ) as client:
            health = await client.get(f"{base_url}/health")
            health.raise_for_status()
            if health.json() != {"service": "audit-summary", "protocol": 1}:
                raise RuntimeError("unexpected audit-summary health response")
            for scenario in scenarios:
                for concurrency in CONCURRENCY_LEVELS:
                    for round_number in range(1, rounds + 1):
                        burst, request_samples = await _burst(
                            client, base_url, scenario, concurrency, round_number
                        )
                        bursts.append(burst)
                        samples.extend(request_samples)
    finally:
        stop.set()
        memory = await memory_task

    groups = []
    for rows in ROW_COUNTS:
        for concurrency in CONCURRENCY_LEVELS:
            requests = [
                item
                for item in samples
                if item["rows"] == rows and item["concurrency"] == concurrency
            ]
            elapsed_seconds = (
                sum(
                    item["elapsed_ms"]
                    for item in bursts
                    if item["rows"] == rows and item["concurrency"] == concurrency
                )
                / 1000
            )
            successes = [item for item in requests if item["outcome"] == "accepted"]
            rejected = [item for item in requests if item["outcome"] == "overloaded"]
            errors = [item for item in requests if item["outcome"] == "error"]
            groups.append(
                {
                    "rows": rows,
                    "concurrency": concurrency,
                    "attempts": len(requests),
                    "accepted": len(successes),
                    "overloaded_503": len(rejected),
                    "errors": len(errors),
                    "error_rate": round(len(errors) / len(requests), 6),
                    "accepted_per_second": round(len(successes) / elapsed_seconds, 3),
                    "success_latency_ms_p50": _percentile(
                        [item["latency_ms"] for item in successes], 0.50
                    ),
                    "success_latency_ms_p95": _percentile(
                        [item["latency_ms"] for item in successes], 0.95
                    ),
                    "overload_latency_ms_p50": _percentile(
                        [item["latency_ms"] for item in rejected], 0.50
                    ),
                    "overload_latency_ms_p95": _percentile(
                        [item["latency_ms"] for item in rejected], 0.95
                    ),
                    "burst_latency_ms_p50": _percentile(
                        [
                            item["elapsed_ms"]
                            for item in bursts
                            if item["rows"] == rows
                            and item["concurrency"] == concurrency
                        ],
                        0.50,
                    ),
                    "burst_latency_ms_p95": _percentile(
                        [
                            item["elapsed_ms"]
                            for item in bursts
                            if item["rows"] == rows
                            and item["concurrency"] == concurrency
                        ],
                        0.95,
                    ),
                }
            )
    return {
        "experiment": "audit-summary-local-concurrency",
        "host": f"{platform.platform()} ({platform.machine()})",
        "python": platform.python_version(),
        "go": _go_version(),
        "base_url": base_url,
        "rounds_per_scenario": rounds,
        "concurrency_levels": CONCURRENCY_LEVELS,
        "maximum_concurrent_summaries": SERVER_MAX_CONCURRENT,
        "maximum_request_bytes": MAX_REQUEST_BYTES,
        "percentile_method": "nearest_rank",
        "request_timeout_seconds": timeout,
        "memory": memory,
        "scenario_sizes": [
            {"rows": item["rows"], "request_bytes": len(item["payload"])}
            for item in scenarios
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "groups": groups,
        "bursts": bursts,
        "samples": samples,
        "unexpected_errors": sum(item["outcome"] == "error" for item in samples),
    }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=_local_url)
    parser.add_argument("--peer-pid", type=int)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.rounds <= MAX_ROUNDS:
        parser.error(f"--rounds must be between 1 and {MAX_ROUNDS}")
    if not 0 < args.timeout <= 30:
        parser.error("--timeout must be greater than 0 and at most 30 seconds")
    if args.peer_pid is not None and (args.base_url is None or args.peer_pid <= 0):
        parser.error("--peer-pid requires --base-url and a positive PID")
    return args


async def _execute(args: argparse.Namespace, base_url: str, peer_pid: int | None):
    result = await _run(base_url, args.rounds, args.timeout, peer_pid)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["unexpected_errors"] else 0


def _go_version() -> str | None:
    try:
        result = subprocess.run(
            ["go", "version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=bench_cost.go_build_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def main() -> int:
    args = _args()
    try:
        if args.base_url is not None:
            return asyncio.run(_execute(args, args.base_url, args.peer_pid))
        with tempfile.TemporaryDirectory(prefix="aap-audit-concurrency-") as temp:
            binary = Path(temp) / "audit-summary"
            subprocess.run(
                ["go", "build", "-trimpath", "-o", str(binary), "."],
                cwd=bench_cost.GO_DIR,
                env=bench_cost.go_build_env(),
                check=True,
                capture_output=True,
                timeout=60,
            )
            process, base_url = bench_cost.go_server(binary)
            try:
                return asyncio.run(_execute(args, base_url, process.pid))
            finally:
                bench_cost.stop_server(process)
    except (
        OSError,
        RuntimeError,
        ValueError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {"experiment": "audit-summary-local-concurrency", "error": str(exc)}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
