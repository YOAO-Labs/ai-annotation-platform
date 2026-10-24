"""Measure real authenticated directory originals in a disposable owned e2e mode.

No production readers, limits or dependencies are replaced. Source registration
uses existing services; the child API receives only runtime credentials. The
launcher owns infrastructure; this tool removes its child and temporary tree.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
PROFILES = [
    ("small-png", (1024, 768), "PNG"),
    ("medium-jpeg", (1920, 1080), "JPEG"),
    ("large-png", (4096, 2048), "PNG"),
]


def tool_digests():
    return {
        name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        for name in ["benchmark_dataset_read.py", "dataset_read_benchmark_api.py"]
    }


def distribution(values):
    values = sorted(values)
    if not values:
        raise ValueError("No observations")
    return {
        "count": len(values),
        "min": min(values),
        "p50": statistics.median(values),
        "p95": values[math.ceil(len(values) * 0.95) - 1],
        "max": max(values),
    }


def build_fixtures(root):
    from PIL import Image

    result = []
    for index, (name, dimensions, media) in enumerate(PROFILES):
        filename = name + (".jpg" if media == "JPEG" else ".png")
        data = random.Random(1200 + index).randbytes(math.prod(dimensions) * 3)
        with Image.frombytes("RGB", dimensions, data) as image:
            image.save(
                root / filename,
                format=media,
                **({"compress_level": 0} if media == "PNG" else {"quality": 90}),
            )
        payload = (root / filename).read_bytes()
        result.append(
            {
                "name": name,
                "filename": filename,
                "dimensions": dimensions,
                "format": media,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return result


def process_sample(pid, source, temporary_root):
    status = Path(f"/proc/{pid}/status").read_text()
    rss = int(
        next(
            line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")
        )
    )
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    result = {
        "rss_kib": rss,
        "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        "source_regular_fds": 0,
        "source_directory_fds": 0,
        "temporary_fds": 0,
        "temporary_bytes": 0,
    }
    for path in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target, info = os.readlink(path), path.stat()
        except FileNotFoundError:
            continue
        if target.startswith(str(source) + "/") or target == str(source):
            result["source_regular_fds"] += int(stat.S_ISREG(info.st_mode))
            result["source_directory_fds"] += int(stat.S_ISDIR(info.st_mode))
        if (
            target.startswith(str(temporary_root) + "/")
            and target.endswith(" (deleted)")
            and stat.S_ISREG(info.st_mode)
        ):
            result["temporary_fds"] += 1
            result["temporary_bytes"] += info.st_size
    return result


async def seeded(directory):
    from pytest import MonkeyPatch
    from sqlalchemy import func, select
    from app.db.base import async_session, engine
    from app.db.models.dataset import Dataset
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.dataset_source import DatasetSource
    from app.db.models.async_job import AsyncJob
    from app.db.models.user import User
    from app.core.security import create_access_token
    from tests.test_dataset_content import create_content_world
    from tests.test_dataset_source_changes import rescan, accept_all_files

    try:
        async with async_session() as db:
            for model in [User, Dataset, Project, Task, DatasetSource, AsyncJob]:
                if await db.scalar(select(func.count()).select_from(model)):
                    raise ValueError("Benchmark requires an empty business database")
            actor = User(
                id=uuid.uuid4(),
                email=f"read-{uuid.uuid4().hex}@example.test",
                name="Read benchmark",
                role="super_admin",
                is_active=True,
                password_hash="benchmark-not-a-login",
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(actor)
            await db.commit()
            with MonkeyPatch.context() as patch:
                world = await create_content_world(db, actor, directory, patch)
                profiles = build_fixtures(world[0])
                await db.commit()
                world = await rescan(db, world)
                accepted = await accept_all_files(db, world)
                await db.commit()
            for item in profiles:
                receipt = accepted[item["filename"]]
                if receipt["sha256"] != item["sha256"]:
                    raise ValueError("Registration did not verify the fixture")
                item["path"] = (
                    f"/api/v1/dataset-items/{receipt['item_id']}/content?sha256={item['sha256']}"
                )
            return create_access_token(str(actor.id), actor.role), profiles, world[0]
    finally:
        await engine.dispose()


async def wait_metrics(directory, identifier, process):
    path, started = directory / f"{identifier}.json", time.monotonic()
    while not path.exists():
        if process.poll() is not None:
            raise RuntimeError("Observed API stopped")
        if time.monotonic() - started > 35:
            raise TimeoutError("Request settlement observation incomplete")
        await asyncio.sleep(0.02)
    return json.loads(path.read_text())


async def api_dependencies_ready(client):
    responses = await asyncio.gather(
        *[client.get(f"/health/{name}") for name in ("db", "redis", "minio")]
    )
    return all(response.status_code == 200 for response in responses)


async def measure(process, base, token, profiles, source, metrics, rounds):
    import httpx

    temporary_root = Path(os.environ["TMPDIR"]).resolve()
    baseline = process_sample(process.pid, source, temporary_root)
    result = {"startup": baseline, "profiles": []}
    async with httpx.AsyncClient(
        base_url=base, headers={"Authorization": "Bearer " + token}, timeout=40
    ) as client:

        async def request(profile, *, abort=False):
            identifier = uuid.uuid4().hex
            started = time.monotonic()
            close_started = None
            digest, size = hashlib.sha256(), 0
            async with client.stream(
                "GET", profile["path"], headers={"X-Read-Benchmark": identifier}
            ) as response:
                response.raise_for_status()
                headers_seconds = time.monotonic() - started
                if (
                    response.headers.get("x-content-sha256") != profile["sha256"]
                    or int(response.headers["content-length"]) != profile["size_bytes"]
                ):
                    raise AssertionError("Response identity mismatch")
                async for chunk in response.aiter_bytes(65536):
                    digest.update(chunk)
                    size += len(chunk)
                    if abort:
                        break
                if abort:
                    close_started = time.monotonic()
            completed = time.monotonic()
            server = await wait_metrics(metrics, identifier, process)
            if not abort and (
                size != profile["size_bytes"] or digest.hexdigest() != profile["sha256"]
            ):
                raise AssertionError("Response bytes mismatch")
            if (
                server["source_content_sessions"] != 1
                or server["source_bytes"] != profile["size_bytes"]
            ):
                raise AssertionError("Source observation incomplete")
            value = {
                "headers_seconds": headers_seconds,
                "response_seconds": completed - started,
                "settled_seconds": time.monotonic() - started,
                "received_bytes": size,
                "server": server,
            }
            if close_started is not None:
                server_settled = server["settled_monotonic"]
                value.update(
                    client_close_seconds=completed - close_started,
                    server_already_settled_at_close=server_settled <= close_started,
                    server_settlement_after_close_seconds=server_settled - close_started
                    if server_settled > close_started
                    else None,
                    observation_after_server_settlement_seconds=time.monotonic()
                    - server_settled,
                )
            return value

        for profile in profiles:
            first = await request(profile)
            item = {
                key: value
                for key, value in profile.items()
                if key not in {"path", "filename"}
            }
            item["first_request"] = first
            item["loads"] = []
            for concurrency in [1, 4]:
                peaks = {key: 0 for key in baseline if key != "cpu_seconds"}
                stop = asyncio.Event()

                async def sample():
                    while not stop.is_set():
                        observation = process_sample(
                            process.pid, source, temporary_root
                        )
                        for key in peaks:
                            peaks[key] = max(peaks[key], observation[key])
                        await asyncio.sleep(0.02)

                sampler = asyncio.create_task(sample())
                before, started = (
                    process_sample(process.pid, source, temporary_root),
                    time.monotonic(),
                )
                semaphore = asyncio.Semaphore(concurrency)

                async def admitted():
                    async with semaphore:
                        return await request(profile)

                try:
                    observations = await asyncio.gather(
                        *[admitted() for _ in range(rounds)]
                    )
                finally:
                    stop.set()
                    await sampler
                after = process_sample(process.pid, source, temporary_root)
                if any(
                    after[key]
                    for key in [
                        "source_regular_fds",
                        "source_directory_fds",
                        "temporary_fds",
                        "temporary_bytes",
                    ]
                ):
                    raise AssertionError("Read resources did not settle")
                item["loads"].append(
                    {
                        "concurrency": concurrency,
                        "wall_seconds": time.monotonic() - started,
                        "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                        "peak": peaks,
                        "after": after,
                        "headers_seconds": distribution(
                            [row["headers_seconds"] for row in observations]
                        ),
                        "response_seconds": distribution(
                            [row["response_seconds"] for row in observations]
                        ),
                        "source_content_sessions": sum(
                            row["server"]["source_content_sessions"]
                            for row in observations
                        ),
                        "source_read_calls": sum(
                            row["server"]["source_read_calls"] for row in observations
                        ),
                        "source_bytes": sum(
                            row["server"]["source_bytes"] for row in observations
                        ),
                        "sql_count": distribution(
                            [row["server"]["sql_count"] for row in observations]
                        ),
                        "sql_seconds": distribution(
                            [row["server"]["sql_seconds"] for row in observations]
                        ),
                        "image_inspection_seconds": distribution(
                            [
                                row["server"]["image_inspection_seconds"]
                                for row in observations
                            ]
                        ),
                    }
                )
            result["profiles"].append(item)
        result["abort"] = await request(profiles[-1], abort=True)
        result["abort"]["phase"] = "response body after full source verification"
        result["abort"]["after"] = process_sample(process.pid, source, temporary_root)
        if result["abort"]["received_bytes"] >= profiles[-1]["size_bytes"] or any(
            result["abort"]["after"][key]
            for key in [
                "source_regular_fds",
                "source_directory_fds",
                "temporary_fds",
                "temporary_bytes",
            ]
        ):
            raise AssertionError("Aborted response cleanup incomplete")
        result["post_abort_read"] = await request(profiles[0])
    return result


async def run(directory, rounds):
    import httpx
    from worktree_env import application_environment

    hashes = tool_digests()
    token, profiles, source = await seeded(directory)
    expected_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.iterdir()
    }
    metrics = directory / "metrics"
    metrics.mkdir()
    port = int(os.environ["PLAYWRIGHT_ISOLATED_API_PORT"])
    environment = application_environment(dict(os.environ))
    environment.update(
        DEBUG="false",
        LOG_LEVEL="WARNING",
        PYTHONDONTWRITEBYTECODE="1",
        STORAGE_FILESYSTEM_ROOTS=json.dumps({"test": str(directory)}),
    )
    base = f"http://127.0.0.1:{port}"
    log_path = directory / "api.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/dataset_read_benchmark_api.py"),
                str(metrics),
                str(port),
            ],
            cwd=ROOT / "apps/api",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with httpx.AsyncClient(base_url=base, timeout=3) as client:
                started = time.monotonic()
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Observed API stopped before readiness")
                    try:
                        if await api_dependencies_ready(client):
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() - started > 120:
                        raise TimeoutError("API readiness observation incomplete")
                    await asyncio.sleep(0.2)
            result = await measure(
                process, base, token, profiles, source, metrics, rounds
            )
            if expected_files != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source.iterdir()
            }:
                raise AssertionError("Source files changed")
            if hashes != tool_digests():
                raise AssertionError("Measurement code changed during the run")
            return {
                "schema": 1,
                "code_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "tool_sha256": hashes,
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "filesystem": subprocess.check_output(
                    ["findmnt", "-n", "-o", "FSTYPE", "--target", str(directory)],
                    text=True,
                ).strip(),
                "scope": "local filesystem reference originals, cache_policy=none; warm OS cache; shared host; runtime API; real TCP; source bytes fully verified",
                "rss_sampling_ms": 20,
                "rounds_per_load": rounds,
                "source_files_unchanged": True,
                "results": result,
            }
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def main():
    from benchmark_dataset_workbench import verified_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    if args.output.exists() or not 20 <= args.rounds <= 50:
        parser.error("Use a new output and 20–50 observations per load")
    backend, _ = verified_environment()
    try:
        with tempfile.TemporaryDirectory(prefix="dataset-read-benchmark-") as temporary:
            directory = Path(temporary)
            try:
                result = asyncio.run(run(directory, args.rounds))
            except BaseException:
                log = directory / "api.log"
                if log.exists():
                    with args.output.with_suffix(".api.log").open("xb") as output:
                        output.write(log.read_bytes()[-1048576:])
                raise
    finally:
        backend.s3.close()
    with args.output.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
