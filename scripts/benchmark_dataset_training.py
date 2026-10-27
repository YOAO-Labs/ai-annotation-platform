"""Measure real training preparation in empty, owned local test resources.

Synthetic managed originals are written and registered outside measurement.
JWT HTTP and an ordinary Celery Worker run unchanged application code. This is
neither a model benchmark nor an Executor deployment/cache qualification.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from benchmark_dataset_read import process_sample, api_dependencies_ready  # noqa: E402
from verify_legacy_storage_rollback import verified_environment, require  # noqa: E402
from verify_storage_migration_inventory import require_empty_fixture  # noqa: E402
from worktree_env import application_environment, atomic_json  # noqa: E402

LABEL = b"0 0.30000000 0.25000000 0.40000000 0.30000000\n"


def fingerprints():
    files = subprocess.check_output(
        ["git", "ls-files", "apps/api/app", "apps/_shared"], cwd=ROOT, text=True
    ).splitlines()
    files += [
        "scripts/" + name
        for name in (
            "benchmark_dataset_training.py",
            "dataset_training_benchmark_runtime.py",
            "benchmark_dataset_read.py",
            "verify_legacy_storage_rollback.py",
            "verify_storage_migration_inventory.py",
            "worktree_env.py",
            "worktree_resources.py",
            "worktree_runtime.py",
        )
    ]
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(
            name.encode() + b"\0" + hashlib.sha256((ROOT / name).read_bytes()).digest()
        )
    return {"files": len(files), "sha256": digest.hexdigest()}


def payloads():
    from PIL import Image

    result = []
    for size, color in (
        ((32, 24), "navy"),
        ((256, 192), "green"),
        ((1024, 768), "red"),
    ):
        output = io.BytesIO()
        with Image.new("RGB", size, color) as image:
            image.save(output, format="PNG")
        data = output.getvalue()
        result.append(
            {
                "data": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": size[0],
                "height": size[1],
                "size_bytes": len(data),
            }
        )
    return result


def verify_package(path, expected, manifest):
    """Check exact members and real bytes independently of the builder."""
    samples = manifest["samples"]
    require(len(samples) == len(expected), "Manifest member count changed")
    require(
        {sample["task_id"] for sample in samples} == set(expected),
        "Manifest identities changed",
    )
    names = {"manifest.json", "classes.json", "data.yaml"}
    with zipfile.ZipFile(path) as archive:
        require(
            json.loads(archive.read("manifest.json")) == manifest,
            "Packaged manifest changed",
        )
        require(
            json.loads(archive.read("classes.json")) == ["vehicle"],
            "Package classes changed",
        )
        for sample in samples:
            facts = expected[sample["task_id"]]
            split = sample["split"]
            require(split in {"train", "val"}, "Unknown sample split")
            require(
                sample["image_path"] == f"images/{split}/{sample['task_id']}.png",
                "Unexpected image path",
            )
            require(
                sample["label_path"] == f"labels/{split}/{sample['task_id']}.txt",
                "Unexpected label path",
            )
            image = archive.read(sample["image_path"])
            require(
                hashlib.sha256(image).hexdigest()
                == facts["sha256"]
                == sample["sha256"],
                "Image content mismatch",
            )
            require(
                len(image) == facts["size_bytes"] == sample["size_bytes"],
                "Image length mismatch",
            )
            require(
                (sample["width"], sample["height"])
                == (facts["width"], facts["height"]),
                "Image dimensions mismatch",
            )
            require(
                archive.read(sample["label_path"]) == LABEL, "Training labels changed"
            )
            require(
                sample["label_sha256"] == hashlib.sha256(LABEL).hexdigest(),
                "Label digest mismatch",
            )
            names.update([sample["image_path"], sample["label_path"]])
        require(
            len(archive.namelist()) == len(names) and set(archive.namelist()) == names,
            "Extra or duplicate ZIP member",
        )
    return {
        "samples": len(samples),
        "members": len(names),
        "labels_sha256": hashlib.sha256(LABEL).hexdigest(),
    }


async def source_rows(factory, world):
    from sqlalchemy import text

    result = {}
    async with factory() as db:
        for table, column, value in (
            ("datasets", "id", world["dataset_id"]),
            ("dataset_items", "dataset_id", world["dataset_id"]),
            ("tasks", "project_id", world["project_id"]),
            ("annotations", "project_id", world["project_id"]),
            ("project_datasets", "project_id", world["project_id"]),
        ):
            rows = await db.scalars(
                text(f"SELECT row_to_json(r) FROM {table} AS r WHERE {column}=:id"),
                {"id": uuid.UUID(value)},
            )
            hashes = sorted(
                hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                for row in rows
            )
            result[table] = {
                "count": len(hashes),
                "sha256": hashlib.sha256(json.dumps(hashes).encode()).hexdigest(),
            }
    return result


async def seed(factory, backend, count, actor, annotator, reviewer, report, output):
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.services.annotation_evidence import freeze_review_contributor_evidence
    from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset

    pictures = payloads()
    started = time.monotonic()
    expected = {}
    async with factory() as db:
        binding = await ensure_legacy_bindings(db)
        project = Project(
            id=uuid.uuid4(),
            display_id=f"P-C6-{count}",
            name=f"Training benchmark {count}",
            type_key="image-det",
            type_label="Detection",
            data_type="image",
            owner_id=actor,
            tool_bindings={
                "bbox": {"enabled": True, "classes": [{"name": "vehicle", "order": 0}]}
            },
            total_tasks=count,
            completed_tasks=count,
        )
        dataset = Dataset(
            id=uuid.uuid4(),
            display_id=f"DS-C6-{count}",
            name=f"Training source {count}",
            data_type="image",
            created_by=actor,
            file_count=count,
        )
        db.add_all([project, dataset])
        await db.flush()
        db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
        world = {
            "project_id": str(project.id),
            "dataset_id": str(dataset.id),
            "bucket": binding.bucket,
        }
        await db.commit()
        for index in range(count):
            picture = pictures[index % len(pictures)]
            key = f"training-benchmark/{dataset.id}/图片-{index:06}.png"
            await asyncio.to_thread(
                backend.s3.put_object,
                Bucket=binding.bucket,
                Key=key,
                Body=picture["data"],
                ContentType="image/png",
            )
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name=f"图片-{index:06}.png",
                file_path=key,
                file_type="image",
                file_size=picture["size_bytes"],
                content_hash=picture["sha256"],
                width=picture["width"],
                height=picture["height"],
            )
            db.add(item)
            await db.flush()
            await register_item_asset(
                db,
                item,
                sha256=picture["sha256"],
                size_bytes=picture["size_bytes"],
                binding=binding,
            )
            now = datetime.now(timezone.utc)
            task = Task(
                id=uuid.uuid4(),
                project_id=project.id,
                dataset_item_id=item.id,
                display_id=f"T-C6-{count}-{index}",
                file_name=item.file_name,
                file_path=key,
                file_type="image",
                status="completed",
                is_labeled=True,
                total_annotations=1,
                assignee_id=annotator,
                reviewer_id=reviewer,
                reviewed_at=now,
                submitted_at=now,
                review_round_id=uuid.uuid4(),
                annotation_contributor_ids=[str(annotator)],
            )
            freeze_review_contributor_evidence(task, submitter_id=annotator)
            db.add(task)
            await db.flush()
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=annotator,
                    source="external_import",
                    annotation_type="bbox",
                    tool_unit_id="bbox",
                    class_name="vehicle",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.3},
                    ground_truth=True,
                )
            )
            expected[str(task.id)] = {
                key: value for key, value in picture.items() if key != "data"
            } | {"object_key": key}
            if (index + 1) % 100 == 0 or index + 1 == count:
                await db.commit()
                report["current"] = {
                    "phase": f"seed-{count}",
                    "created": index + 1,
                    "seconds": time.monotonic() - started,
                }
                atomic_json(output, report)
        await db.commit()
    return world, expected, time.monotonic() - started


def temporary_bytes(root):
    total, files = 0, 0
    for parent, _directories, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(parent) / name
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
                    files += 1
            except FileNotFoundError:
                pass
    return total, files


async def sampled(operation, processes, temporary_root):
    before = {
        name: process_sample(
            process.pid, temporary_root / "unused-source", temporary_root
        )
        for name, process in processes.items()
    }
    peaks = {name: dict(value) for name, value in before.items()}
    baseline, baseline_files = temporary_bytes(temporary_root)
    disk_peak, files_peak, samples = baseline, baseline_files, 0
    work = asyncio.create_task(operation)
    started = time.monotonic()
    try:
        while not work.done():
            for name, process in processes.items():
                require(process.poll() is None, "Owned measurement process stopped")
                value = process_sample(
                    process.pid, temporary_root / "unused-source", temporary_root
                )
                for key in peaks[name]:
                    peaks[name][key] = max(peaks[name][key], value[key])
            disk, files = temporary_bytes(temporary_root)
            disk_peak, files_peak = max(disk_peak, disk), max(files_peak, files)
            samples += 1
            await asyncio.sleep(0.5)
        result = await work
        final = {
            name: process_sample(
                process.pid, temporary_root / "unused-source", temporary_root
            )
            for name, process in processes.items()
        }
        return result, {
            "wall_seconds": time.monotonic() - started,
            "samples": samples,
            "sample_interval_seconds": 0.5,
            "processes": {
                name: {
                    "peak_rss_kib": peaks[name]["rss_kib"],
                    "cpu_seconds": final[name]["cpu_seconds"]
                    - before[name]["cpu_seconds"],
                    "peak_anonymous_temp_bytes": peaks[name]["temporary_bytes"],
                    "final_anonymous_temp_bytes": final[name]["temporary_bytes"],
                }
                for name in processes
            },
            "temporary_directory": {
                "baseline_bytes": baseline,
                "peak_bytes": disk_peak,
                "final_bytes": temporary_bytes(temporary_root)[0],
                "peak_files": files_peak,
            },
        }
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


async def wait_metric(directory, identifier, process, timeout=60):
    started = time.monotonic()
    path = directory / f"{identifier}.json"
    while not path.exists():
        require(process.poll() is None, "Observed process stopped before settlement")
        if time.monotonic() - started > timeout:
            raise TimeoutError("Measurement did not settle")
        await asyncio.sleep(0.05)
    return json.loads(path.read_text())


async def run_phase(label, operation, processes, temp, report, output):
    report["current"] = {
        "phase": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(output, report)
    async with asyncio.timeout(report.get("phase_timeout_seconds", 14400)):
        value, resources = await sampled(operation, processes, temp)
    observation = {"phase": label, "resources": resources}
    if isinstance(value, tuple):
        observation["metrics"] = value[1]
    elif "worker" in value:
        observation["metrics"] = value["worker"]
    else:
        observation["metrics"] = value
    report.setdefault("phase_observations", []).append(observation)
    atomic_json(output, report)
    return value, resources


def require_complete_observation(metrics):
    require(
        metrics.get("complete_observation") is True,
        "Declared measurement context did not fully settle",
    )


async def measure(
    factory,
    world,
    expected,
    api,
    worker,
    metrics,
    base,
    token,
    directory,
    report,
    output,
):
    import httpx
    from app.db.models.async_job import AsyncJob
    from app.db.models.training_dataset import TrainingDatasetVersion

    count = len(expected)
    phase_timeout = report.get("phase_timeout_seconds", 14400)
    processes = {"api": api, "worker": worker}
    temp = Path(os.environ["TMPDIR"])
    path = f"/api/v1/projects/{world['project_id']}/training-datasets"
    selection = {
        "dataset_id": world["dataset_id"],
        "seed": 42,
        "validation_fraction": 0.2,
    }
    async with httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": "Bearer " + token},
        timeout=phase_timeout,
    ) as client:

        async def request(method, path, body=None):
            identifier = uuid.uuid4().hex
            started = time.monotonic()
            response = await client.request(
                method, path, json=body, headers={"X-Training-Benchmark": identifier}
            )
            response_seconds = time.monotonic() - started
            observation = await wait_metric(metrics, identifier, api)
            report["last_observation"] = observation
            atomic_json(output, report)
            require_complete_observation(observation)
            if response.status_code not in {200, 202}:
                try:
                    detail = response.json().get("detail", {})
                except (ValueError, AttributeError):
                    detail = {}
                report["last_failed_request"] = {
                    "status": response.status_code,
                    "observation": observation,
                    "code": detail.get("code") if isinstance(detail, dict) else None,
                }
                atomic_json(output, report)
                raise RuntimeError(f"Training HTTP {response.status_code}")
            return response.json(), {
                "client_response_seconds": response_seconds,
                **observation,
            }

        (preflight, preflight_metrics), preflight_resources = await run_phase(
            f"preflight-{count}",
            request("POST", path + ":preflight", selection),
            processes,
            temp,
            report,
            output,
        )
        report["preflight_summary"] = preflight["summary"]
        atomic_json(output, report)
        require(
            preflight["ready"] and preflight["summary"]["eligible"] == count,
            "Preflight did not accept exact synthetic members",
        )
        body = {
            **selection,
            "name": f"Measured training {count}",
            "request_id": str(uuid.uuid4()),
            "preflight_digest": preflight["digest"],
        }
        (created, create_metrics), create_resources = await run_phase(
            f"create-{count}",
            request("POST", path, body),
            processes,
            temp,
            report,
            output,
        )
        version_id = uuid.UUID(created["id"])

        async def settled():
            started = time.monotonic()
            while True:
                require(worker.poll() is None, "Training Worker stopped")
                async with factory() as db:
                    version = await db.get(TrainingDatasetVersion, version_id)
                    job = await db.get(AsyncJob, version.async_job_id)
                    if version.status not in {"pending", "running"}:
                        require(
                            version.status == "ready",
                            f"Training build ended as {version.status}: {version.error_code}",
                        )
                        require(
                            job.status == "completed",
                            "Version and job terminal state differ",
                        )
                        observation = await wait_metric(
                            metrics, job.celery_task_id, worker
                        )
                        report["last_observation"] = observation
                        atomic_json(output, report)
                        require_complete_observation(observation)
                        return {
                            "manifest": version.manifest,
                            "snapshot_digest": version.snapshot_digest,
                            "package_sha256": version.package_sha256,
                            "size_bytes": version.size_bytes,
                            "worker": observation,
                        }
                if time.monotonic() - started > phase_timeout:
                    raise TimeoutError(
                        "Training build still active after measurement deadline"
                    )
                await asyncio.sleep(0.5)

        built, build_resources = await run_phase(
            f"build-{count}", settled(), processes, temp, report, output
        )
        require(
            built["snapshot_digest"] == preflight["digest"], "Snapshot identity changed"
        )
        downloads = []
        for iteration in range(2):
            signed, _ = await request("GET", f"{path}/{version_id}/download")
            require(
                signed["sha256"] == built["package_sha256"], "Download identity changed"
            )
            target = directory / f"package-{count}.zip"

            async def downloaded():
                digest, size = hashlib.sha256(), 0
                started = time.monotonic()
                with (
                    target.open("wb")
                    if iteration == 0
                    else tempfile.TemporaryFile() as stream
                ):
                    async with httpx.AsyncClient(timeout=phase_timeout) as downloader:
                        async with downloader.stream("GET", signed["url"]) as response:
                            require(
                                response.status_code == 200,
                                "Signed package download refused",
                            )
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                size += len(chunk)
                                digest.update(chunk)
                                stream.write(chunk)
                    require(
                        digest.hexdigest() == built["package_sha256"]
                        and size == built["size_bytes"],
                        "Downloaded package bytes differ",
                    )
                return {
                    "received_bytes": size,
                    "sha256": digest.hexdigest(),
                    "response_seconds": time.monotonic() - started,
                }

            downloaded_result, _ = await run_phase(
                f"download-{count}-{iteration + 1}",
                downloaded(),
                processes,
                temp,
                report,
                output,
            )
            downloads.append(downloaded_result)
        verified = await asyncio.to_thread(
            verify_package,
            directory / f"package-{count}.zip",
            expected,
            built["manifest"],
        )
        (directory / f"package-{count}.zip").unlink()
    return {
        "samples": count,
        "source_bytes": sum(value["size_bytes"] for value in expected.values()),
        "preflight": {"metrics": preflight_metrics, "resources": preflight_resources},
        "create": {"metrics": create_metrics, "resources": create_resources},
        "build": {
            "metrics": built["worker"],
            "resources_after_create_response": build_resources,
        },
        "package": {
            "bytes": built["size_bytes"],
            "sha256": built["package_sha256"],
            "verification": verified,
        },
        "downloads": downloads,
    }


async def qualify(backend, options, directory, report):
    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.models.user import User
    from app.core.security import create_access_token
    from app.workers.celery_app import celery_app
    from worktree_recovery_fixture import database_facts

    engine = create_async_engine(backend.url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    processes = {}
    logs = []
    try:
        async with factory() as db:
            require_empty_fixture(
                await database_facts(db),
                backend.s3,
                set(backend.resources["buckets"].values()),
            )
            people = []
            for role in ("super_admin", "employee", "employee"):
                user = User(
                    id=uuid.uuid4(),
                    email=uuid.uuid4().hex + "@example.test",
                    name="Training benchmark",
                    role=role,
                    is_active=True,
                    password_hash="benchmark-not-a-login",
                    email_verified_at=datetime.now(timezone.utc),
                )
                db.add(user)
                people.append(user)
            await db.commit()
            actor, annotator, reviewer = (person.id for person in people)
        runtime = application_environment(os.environ)
        runtime.update(
            DEBUG="false",
            LOG_LEVEL="WARNING",
            PYTHONDONTWRITEBYTECODE="1",
            MINIO_PUBLIC_URL="",
        )
        metrics = directory / "metrics"
        metrics.mkdir()
        port = int(os.environ.get("PLAYWRIGHT_ISOLATED_API_PORT", "0"))
        if not port:
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        node = f"e12-c6-{uuid.uuid4().hex[:10]}@{socket.gethostname()}"
        for name, args in {
            "api": ["api", str(metrics), str(port)],
            "worker": ["worker", str(metrics), node, "export"],
        }.items():
            log = (directory / f"{name}.log").open("wb")
            logs.append(log)
            processes[name] = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/dataset_training_benchmark_runtime.py"),
                    *args,
                ],
                cwd=ROOT / "apps/api",
                env=runtime,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        started = time.monotonic()
        async with httpx.AsyncClient(base_url=base, timeout=3) as readiness:
            while True:
                require(
                    all(process.poll() is None for process in processes.values()),
                    "Observed runtime stopped during startup",
                )
                try:
                    ready = await api_dependencies_ready(readiness)
                except httpx.HTTPError:
                    ready = False
                registered = await asyncio.to_thread(
                    lambda: (
                        celery_app.control.inspect(
                            destination=[node], timeout=1
                        ).registered()
                        or {}
                    ).get(node, [])
                )
                if (
                    ready
                    and "app.workers.training_dataset.run_training_dataset"
                    in registered
                ):
                    break
                if time.monotonic() - started > 90:
                    raise TimeoutError("Runtime readiness incomplete")
                await asyncio.sleep(0.2)
        report["runtime_ready"] = True
        for count in options.sizes:
            world, expected, setup = await seed(
                factory,
                backend,
                count,
                actor,
                annotator,
                reviewer,
                report,
                options.output,
            )
            before = await source_rows(factory, world)
            token = create_access_token(str(actor), "super_admin")
            value = await measure(
                factory,
                world,
                expected,
                processes["api"],
                processes["worker"],
                metrics,
                base,
                token,
                directory,
                report,
                options.output,
            )
            require(
                before == await source_rows(factory, world),
                "Source business rows changed",
            )
            for facts in expected.values():
                response = await asyncio.to_thread(
                    backend.s3.get_object,
                    Bucket=world["bucket"],
                    Key=facts["object_key"],
                )
                try:
                    digest, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: response["Body"].read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                    require(
                        digest.hexdigest() == facts["sha256"]
                        and size == facts["size_bytes"],
                        "Source original changed",
                    )
                finally:
                    response["Body"].close()
            value.update(
                setup_seconds=setup,
                source_rows_unchanged=True,
                source_objects_unchanged=True,
            )
            report["tiers"].append(value)
            atomic_json(options.output, report)
    except BaseException:
        # Diagnostic files remain private and bounded; the operator owns their
        # later removal. Never copy logs or credential-bearing URLs to stdout.
        for log in logs:
            log.flush()
        for name in processes:
            path = directory / f"{name}.log"
            if path.exists():
                target = options.output.with_name(options.output.stem + f"-{name}.log")
                try:
                    descriptor = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        with path.open("rb") as source:
                            source.seek(max(0, path.stat().st_size - 65536))
                            stream.write(source.read(65536))
                except OSError as exc:
                    report.setdefault("diagnostic_copy_errors", []).append(
                        type(exc).__name__
                    )
        raise
    finally:
        for process in processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in processes.values():
            try:
                await asyncio.to_thread(process.wait, timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.to_thread(process.wait, timeout=5)
        for log in logs:
            log.close()
        report["children_stopped"] = all(
            process.poll() is not None for process in processes.values()
        )
        await engine.dispose()


def main():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    command.add_argument("--phase-timeout-seconds", type=int, default=14400)
    options = command.parse_args()
    if (
        options.phase_timeout_seconds <= 0
        or len(set(options.sizes)) != len(options.sizes)
        or any(not 2 <= count <= 10000 for count in options.sizes)
    ):
        command.error("Sizes must be distinct and within 2..10000")
    backend = verified_environment()
    source = fingerprints()
    report = {
        "status": "running",
        "scope": "synthetic managed training preparation and complete package transfers",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "source_fingerprint": source,
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "phase_timeout_seconds": options.phase_timeout_seconds,
        "tiers": [],
    }
    atomic_json(options.output, report)
    try:
        with tempfile.TemporaryDirectory(prefix="aap-e12-c6-") as temporary:
            asyncio.run(qualify(backend, options, Path(temporary), report))
        require(source == fingerprints(), "Measured source changed during execution")
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            backend.s3.close()
        except Exception as exc:
            report["status"] = "failed"
            report["cleanup_error_type"] = type(exc).__name__
            raise
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            atomic_json(options.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "tiers": [row["samples"] for row in report["tiers"]],
                "children_stopped": report["children_stopped"],
            }
        )
    )


if __name__ == "__main__":
    main()
