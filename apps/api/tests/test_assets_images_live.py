"""Real independent-process acceptance for Assets image control and workers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import httpx
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from uuid import UUID, uuid4

from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_images.policy import ImagePolicy
from app.application.dataset_deletion import unlink_dataset_from_project
from app.db.models.dataset import DatasetItem, ProjectDataset
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from tests.factory import create_project
from tests.test_asset_storage_cleanup_runtime import (
    _expect_s3_denied,
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_media_live import (
    _free_loopback_port,
    _post,
    _process_log,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _stop_process,
    _wait_control_ready,
    _wait_process_exit,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the module-data verifier's owned PostgreSQL and S3 resources",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
_POLICY = ImagePolicy(auto_generate_pyramids=True, optional_pixels=1_048_576)
_CONTROL_POLICY_ENV = {
    "IMAGE_PYRAMID_AUTO_GENERATE": "true",
    "IMAGE_PYRAMID_OPTIONAL_PIXELS": str(_POLICY.optional_pixels),
}


def _guard_role_url(env_name: str, username: str, test_engine):
    raw = _required_env(env_name)
    try:
        parsed = make_url(raw)
    except Exception:
        pytest.fail(f"the verifier supplied an invalid {username} URL", pytrace=False)
    if not (
        parsed.username == username
        and parsed.password
        and parsed.host == "127.0.0.1"
        and parsed.port is not None
        and parsed.database
        and parsed.database.endswith("_test")
        and parsed.database == test_engine.url.database
    ):
        pytest.fail(f"{username} URL did not match the owned loopback test DB")
    _verify_postgres(parsed)
    return parsed


async def _assert_sql_role_is_narrow(raw_url: str, username: str, table: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(raw_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == username
            with pytest.raises(DBAPIError) as denied_table:
                await connection.execute(text(f"SELECT count(*) FROM {table}"))
            assert getattr(denied_table.value.orig, "sqlstate", None) == "42501"
            await connection.rollback()
            with pytest.raises(DBAPIError) as denied_foreign:
                await connection.execute(
                    text("SELECT aap_identity_v1.read_actor_facts(NULL::uuid)")
                )
            assert getattr(denied_foreign.value.orig, "sqlstate", None) == "42501"
            await connection.rollback()
    finally:
        await engine.dispose()


def _srgb_profile() -> Path:
    for candidate in (
        Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc"),
        Path("/usr/share/color/icc/sRGB.icc"),
        Path("/usr/share/color/icc/sRGB.icm"),
        Path("/usr/share/color/icc/colord/sRGB.icc"),
    ):
        if candidate.is_file():
            return candidate
    # Worker config accepts this absolute path for thumbnail-only tests. Pyramid
    # acceptance checks that the profile exists before starting its real worker.
    return Path("/usr/share/color/icc/sRGB.icc")


async def _wait_marker(
    path: Path,
    process: subprocess.Popen,
    log_path: Path,
    *,
    timeout: float = 60,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            pytest.fail(
                "image worker exited before its processing gate: "
                + _process_log(log_path)
            )
        await asyncio.sleep(0.1)
    pytest.fail("image worker did not reach its processing gate")


async def _wait_job(
    test_engine,
    job_id: UUID,
    process: subprocess.Popen,
    log_path: Path,
    *,
    statuses: set[str],
    timeout: float = 90,
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        async with test_engine.connect() as owner:
            row = (
                (
                    await owner.execute(
                        text(
                            "SELECT job_id,kind,owner_kind,owner_id,generation,status,"
                            "source_generation,source_snapshot,source_facts,spec,result,"
                            "error_code,error_detail,attempt_count,lease_owner,lease_token,"
                            "lease_expires_at,output_prefix,pyramid_asset_id,"
                            "pyramid_generation,last_lease_token,last_outcome "
                            "FROM public.asset_image_jobs WHERE job_id=:job"
                        ),
                        {"job": job_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is not None:
            last = dict(row)
            if last["status"] in statuses:
                return last
        if (
            process.poll() is not None
            and last is not None
            and last["status"] not in statuses
        ):
            break
        await asyncio.sleep(0.1)
    pytest.fail(
        f"image job did not reach {sorted(statuses)}; last={last}; "
        f"worker={_process_log(log_path)}"
    )


class _DropFirstImageResponse(httpx.AsyncBaseTransport):
    """Forward a real request, then discard the first committed response."""

    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.dropped = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        body = await response.aread()
        headers = response.headers
        await response.aclose()
        if (
            not self.dropped
            and request.method == "POST"
            and request.url.path == "/v1/images/commands/prepare_image_media"
        ):
            self.dropped = True
            raise httpx.ReadError(
                "qualification dropped the committed response", request=request
            )
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=body,
            request=request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


@dataclass
class _ImageLiveRuntime:
    test_engine: object
    tmp_path: Path
    settings: object
    control_url: object
    worker_url: object
    resources: dict
    datasets_bucket: str
    legacy_bucket: str
    cache_bucket: str
    endpoint: str
    control_s3: object
    worker_s3: object
    owner_s3: object
    control_access: str
    control_secret: str
    worker_access: str
    worker_secret: str
    control_token: str
    control_client: httpx.AsyncClient
    owner_sessions: async_sessionmaker
    srgb_profile: Path
    processes: list[subprocess.Popen] = field(default_factory=list)
    streams: list[object] = field(default_factory=list)
    source_objects: list[tuple[str, str]] = field(default_factory=list)
    cache_prefixes: list[str] = field(default_factory=list)
    actor_ids: list[UUID] = field(default_factory=list)
    dataset_ids: list[UUID] = field(default_factory=list)
    item_ids: list[UUID] = field(default_factory=list)
    task_ids: list[UUID] = field(default_factory=list)
    project_ids: list[UUID] = field(default_factory=list)
    control_process: subprocess.Popen | None = None
    control_log: Path | None = None

    def start_worker(
        self,
        name: str,
        *,
        gate: tuple[Path, Path] | None = None,
        job_id: UUID | None = None,
    ) -> tuple[subprocess.Popen, Path]:
        log_path = self.tmp_path / f"{name}.log"
        environment = _runtime_environment(
            tmp_path=self.tmp_path,
            role="worker",
            database_url=_runtime_database_url(self.worker_url),
            endpoint=self.endpoint,
            public_endpoint=self.endpoint,
            datasets_bucket=self.datasets_bucket,
            legacy_bucket=self.legacy_bucket,
            cache_bucket=self.cache_bucket,
            access_key=self.worker_access,
            secret_key=self.worker_secret,
            image_worker=True,
            srgb_profile=self.srgb_profile,
            vips_gate=gate,
        )
        command = [sys.executable, "-m", "aap_assets_images_runtime.worker"]
        if job_id is not None:
            command.extend(("--once", "--job-id", str(job_id)))
        process, stream = _start_process(command, environment, log_path)
        self.processes.append(process)
        self.streams.append(stream)
        return process, log_path

    def track_actor(self, actor_id: UUID) -> None:
        self.actor_ids.append(actor_id)

    async def close(self) -> None:
        for gate_path in self.tmp_path.glob("**/release"):
            gate_path.touch(exist_ok=True)
        for process in reversed(self.processes):
            await asyncio.to_thread(_stop_process, process)
        await self.control_client.aclose()
        if self.control_process is not None:
            await asyncio.to_thread(_stop_process, self.control_process)
        for stream in self.streams:
            stream.close()
        if self.owner_s3 is not None:
            for prefix in self.cache_prefixes:
                await _delete_objects(self.owner_s3, self.cache_bucket, [prefix])
            for bucket, key in self.source_objects:
                await asyncio.to_thread(
                    self.owner_s3.delete_object, Bucket=bucket, Key=key
                )
        async with self.test_engine.begin() as owner:
            for owner_kind, owner_ids in (
                ("dataset_item", self.item_ids),
                ("task", self.task_ids),
            ):
                for owner_id in owner_ids:
                    await owner.execute(
                        text(
                            "DELETE FROM public.asset_image_jobs "
                            "WHERE owner_kind=:kind AND owner_id=:owner"
                        ),
                        {"kind": owner_kind, "owner": owner_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.image_pyramid_assets "
                            "WHERE (dataset_item_id=:owner AND :kind='dataset_item') "
                            "OR (task_id=:owner AND :kind='task')"
                        ),
                        {"kind": owner_kind, "owner": owner_id},
                    )
            for task_id in self.task_ids:
                await owner.execute(
                    text("DELETE FROM public.tasks WHERE id=:task"),
                    {"task": task_id},
                )
            for item_id in self.item_ids:
                await owner.execute(
                    text("DELETE FROM public.dataset_items WHERE id=:item"),
                    {"item": item_id},
                )
            for dataset_id in self.dataset_ids:
                await owner.execute(
                    text(
                        "DELETE FROM public.project_datasets WHERE dataset_id=:dataset"
                    ),
                    {"dataset": dataset_id},
                )
                await owner.execute(
                    text("DELETE FROM public.datasets WHERE id=:dataset"),
                    {"dataset": dataset_id},
                )
            for project_id in self.project_ids:
                await owner.execute(
                    text("DELETE FROM public.projects WHERE id=:project"),
                    {"project": project_id},
                )
            for actor_id in self.actor_ids:
                await owner.execute(
                    text(
                        "DELETE FROM public.users WHERE id=:actor "
                        "AND NOT EXISTS (SELECT 1 FROM public.audit_logs "
                        "WHERE actor_id=:actor)"
                    ),
                    {"actor": actor_id},
                )


async def _delete_objects(client, bucket: str, prefixes: list[str]) -> None:
    for prefix in prefixes:
        continuation = None
        while True:
            request = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation is not None:
                request["ContinuationToken"] = continuation
            page = await asyncio.to_thread(client.list_objects_v2, **request)
            keys = [row["Key"] for row in page.get("Contents", [])]
            if keys:
                await asyncio.to_thread(
                    client.delete_objects,
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
                )
            if not page.get("IsTruncated"):
                break
            continuation = page.get("NextContinuationToken")


@asynccontextmanager
async def _image_live_runtime(test_engine, tmp_path: Path):
    control_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "asset_media_control_client",
        test_engine,
    )
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_URL",
        "asset_image_worker_client",
        test_engine,
    )
    if not (
        test_engine.url.database
        and test_engine.url.database.endswith("_test")
        and control_url.database == worker_url.database == test_engine.url.database
        and control_url.port == worker_url.port
    ):
        pytest.fail("image roles did not share the owned test database")
    async with test_engine.connect() as owner:
        if (
            await owner.scalar(text("SELECT current_database()"))
            != control_url.database
        ):
            pytest.fail("owner connection did not target the verifier-owned test DB")

    from app.config import settings

    resources = _verified_resource_manifest(test_engine, control_url)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    legacy_bucket = resources["buckets"]["MINIO_BUCKET"]
    cache_bucket = resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = f"{scheme}://{settings.minio_endpoint}"
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (datasets_bucket, legacy_bucket, cache_bucket):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])

    control_access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY")
    control_secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY")
    worker_access = _required_env("AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_ACCESS_KEY")
    worker_secret = _required_env("AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_SECRET_KEY")
    if (
        (control_access, control_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or (worker_access, worker_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or control_access == worker_access
        or control_secret == worker_secret
    ):
        pytest.fail("image runtimes were given an unscoped or shared S3 identity")
    control_s3 = _local_s3_client(settings, control_access, control_secret)
    worker_s3 = _local_s3_client(settings, worker_access, worker_secret)
    await _assert_sql_role_is_narrow(
        control_url.render_as_string(hide_password=False),
        "asset_media_control_client",
        "public.dataset_items",
    )
    await _assert_sql_role_is_narrow(
        worker_url.render_as_string(hide_password=False),
        "asset_image_worker_client",
        "public.asset_image_jobs",
    )

    srgb_profile = _srgb_profile()
    control_token = secrets.token_urlsafe(32)
    control_port = _free_loopback_port()
    control_log = tmp_path / "image-control.log"
    control_environment = _runtime_environment(
        tmp_path=tmp_path,
        role="control",
        database_url=_runtime_database_url(control_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=datasets_bucket,
        legacy_bucket=legacy_bucket,
        cache_bucket=cache_bucket,
        access_key=control_access,
        secret_key=control_secret,
        token=control_token,
        port=control_port,
        image_control_settings=_CONTROL_POLICY_ENV,
    )
    control_process, control_stream = _start_process(
        [sys.executable, "-m", "aap_assets_media_runtime"],
        control_environment,
        control_log,
    )
    control_origin = f"http://127.0.0.1:{control_port}"
    control_client = httpx.AsyncClient(
        base_url=control_origin, timeout=10, trust_env=False
    )
    runtime = _ImageLiveRuntime(
        test_engine=test_engine,
        tmp_path=tmp_path,
        settings=settings,
        control_url=control_url,
        worker_url=worker_url,
        resources=resources,
        datasets_bucket=datasets_bucket,
        legacy_bucket=legacy_bucket,
        cache_bucket=cache_bucket,
        endpoint=endpoint,
        control_s3=control_s3,
        worker_s3=worker_s3,
        owner_s3=owner_s3,
        control_access=control_access,
        control_secret=control_secret,
        worker_access=worker_access,
        worker_secret=worker_secret,
        control_token=control_token,
        control_client=control_client,
        owner_sessions=async_sessionmaker(test_engine, expire_on_commit=False),
        srgb_profile=srgb_profile,
        control_process=control_process,
        control_log=control_log,
        processes=[control_process],
        streams=[control_stream],
    )
    try:
        await _wait_control_ready(control_client, control_process, control_log)
        worker_environment = _runtime_environment(
            tmp_path=tmp_path,
            role="worker",
            database_url=_runtime_database_url(worker_url),
            endpoint=endpoint,
            public_endpoint=endpoint,
            datasets_bucket=datasets_bucket,
            legacy_bucket=legacy_bucket,
            cache_bucket=cache_bucket,
            access_key=worker_access,
            secret_key=worker_secret,
            image_worker=True,
            srgb_profile=srgb_profile,
        )
        probe = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "aap_assets_images_runtime.worker",
                "--healthcheck",
            ],
            cwd=ROOT,
            env=worker_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert probe.returncode == 0, "restricted image worker login failed healthcheck"
        yield runtime
    finally:
        await runtime.close()


async def _seed_item(
    runtime: _ImageLiveRuntime,
    *,
    actor_id: UUID,
    source_key: str,
    source_bytes: bytes,
    width: int,
    height: int,
) -> tuple[UUID, UUID]:
    dataset_id = item_id = None
    runtime.source_objects.append((runtime.datasets_bucket, source_key))
    runtime.owner_s3.put_object(
        Bucket=runtime.datasets_bucket,
        Key=source_key,
        Body=source_bytes,
        ContentType="image/png",
    )
    async with runtime.owner_sessions.begin() as owner:
        dataset_id = await _seed_dataset(
            owner,
            actor_id=actor_id,
            name=f"image-live-{uuid4().hex[:12]}",
            data_type="image",
            file_count=1,
        )
        runtime.dataset_ids.append(dataset_id)
        item_id = uuid4()
        runtime.item_ids.append(item_id)
        owner.add(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name=f"image-{item_id.hex[:12]}.png",
                file_path=source_key,
                file_type="image",
                file_size=len(source_bytes),
                content_hash=sha256(source_bytes).hexdigest(),
                width=width,
                height=height,
                metadata_={},
            )
        )
    return dataset_id, item_id


def _solid_png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG", optimize=True)
    return output.getvalue()


def _quadrant_png(size: tuple[int, int]) -> bytes:
    from io import BytesIO

    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (
                (240, 24, 24)
                if x < width // 2 and y < height // 2
                else (24, 220, 24)
                if x >= width // 2 and y < height // 2
                else (24, 24, 240)
                if x < width // 2
                else (230, 220, 24)
            )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _write_processing_gate(directory: Path, *, backend: str) -> tuple[Path, Path]:
    gate_directory = directory / "python-gate"
    marker_directory = directory / "python-gate-state"
    gate_directory.mkdir(parents=True)
    marker_directory.mkdir(parents=True)
    if backend not in {"pillow", "vips"}:
        raise ValueError("unsupported image processing gate")
    if backend == "pillow":
        imports = "from PIL import Image as _image_module"
        target = "_image_module.open"
    else:
        imports = "import pyvips as _vips_module"
        target = "_vips_module.Image.new_from_file"
    sitecustomize = f"""
import sys as _sys
import time as _time
from pathlib import Path as _Path
_spec = getattr(_sys.modules.get('__main__'), '__spec__', None)
if getattr(_spec, 'name', None) == 'aap_assets_images_runtime.processing':
    {imports}
    _original = {target}
    def _gated(*args, **kwargs):
        _Path({str(marker_directory / "entered")!r}).touch()
        _release = _Path({str(marker_directory / "release")!r})
        _deadline = _time.monotonic() + 180
        while not _release.exists() and _time.monotonic() < _deadline:
            _time.sleep(0.05)
        return _original(*args, **kwargs)
    {target} = _gated
"""
    (gate_directory / "sitecustomize.py").write_text(sitecustomize, encoding="utf-8")
    return gate_directory, marker_directory


def _track_job_prefix(runtime: _ImageLiveRuntime, job: dict) -> None:
    if job["kind"] == "pyramid":
        assert job["pyramid_asset_id"] is not None
        prefix = f"image-pyramids/{job['pyramid_asset_id']}/"
    else:
        prefix = f"image-assets/{job['job_id']}/"
    runtime.cache_prefixes.append(prefix)


async def _accept_image_work(
    runtime: _ImageLiveRuntime,
    *,
    actor_id: UUID,
    item_id: UUID | None = None,
    task_id: UUID | None = None,
    purpose: str,
    operation_id: UUID | None = None,
) -> tuple[dict, dict]:
    request = {
        "actor_id": str(actor_id),
        "task_id": str(task_id) if task_id is not None else None,
        "dataset_item_id": str(item_id) if item_id is not None else None,
        "operation_id": str(operation_id or uuid4()),
        "purpose": purpose,
        "expected_source_snapshot": None,
        "policy": _POLICY.model_dump(mode="json"),
        "force": False,
    }
    response = await _post(
        runtime.control_client,
        runtime.control_token,
        "/v1/images/commands/prepare_image_media",
        request,
    )
    assert response.status_code == 200, response.text[:500]
    reply = response.json()
    assert reply["ok"] is True, reply
    data = reply["data"]
    assert data["operation_id"] == request["operation_id"]
    assert data["work"]["kind"] == purpose
    job = await _read_job(runtime.test_engine, UUID(data["work"]["job_id"]))
    _track_job_prefix(runtime, job)
    return data, request


async def _read_job(test_engine, job_id: UUID) -> dict:
    async with test_engine.connect() as owner:
        row = (
            (
                await owner.execute(
                    text(
                        "SELECT job_id,kind,owner_kind,owner_id,generation,status,"
                        "source_generation,source_snapshot,source_facts,spec,result,"
                        "error_code,error_detail,attempt_count,lease_owner,lease_token,"
                        "lease_expires_at,output_prefix,pyramid_asset_id,"
                        "pyramid_generation,last_lease_token,last_outcome "
                        "FROM public.asset_image_jobs WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


async def _assert_image_storage_scopes(
    runtime: _ImageLiveRuntime, *, source_key: str, image_job_id: UUID
) -> None:
    head = await asyncio.to_thread(
        runtime.owner_s3.head_object,
        Bucket=runtime.datasets_bucket,
        Key=source_key,
    )
    version_id = head.get("VersionId") or "null"
    body = runtime.worker_s3.get_object(
        Bucket=runtime.datasets_bucket,
        Key=source_key,
        VersionId=version_id,
    )["Body"]
    assert body.read()
    body.close()

    for bucket in (runtime.datasets_bucket, runtime.legacy_bucket):
        probe_key = f"image-source-denied/{uuid4()}.png"
        _expect_s3_denied(
            lambda bucket=bucket, key=probe_key: runtime.worker_s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=b"image worker must not publish into source buckets",
            )
        )
    source_delete = runtime.worker_s3.delete_object
    _expect_s3_denied(
        lambda: source_delete(Bucket=runtime.datasets_bucket, Key=source_key)
    )

    probe_key = f"image-assets/{image_job_id}/g999999/a{uuid4()}/thumbnail.webp"
    runtime.owner_s3.put_object(
        Bucket=runtime.cache_bucket,
        Key=probe_key,
        Body=b"worker deletion must be denied",
        ContentType="image/webp",
    )
    try:
        _expect_s3_denied(
            lambda: runtime.worker_s3.delete_object(
                Bucket=runtime.cache_bucket, Key=probe_key
            )
        )
    finally:
        await asyncio.to_thread(
            runtime.owner_s3.delete_object,
            Bucket=runtime.cache_bucket,
            Key=probe_key,
        )


async def test_independent_image_worker_publishes_thumbnail_after_response_loss_and_actor_disable(
    test_engine, tmp_path
):
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        actor_id = item_id = None
        source_key = f"image-source/{uuid4()}/original.png"
        source_bytes = _solid_png((32, 24), (240, 20, 30))
        try:
            async with runtime.owner_sessions.begin() as owner:
                actor_id = await _seed_actor(owner, role="super_admin")
            runtime.track_actor(actor_id)
            _dataset_id, item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=source_key,
                source_bytes=source_bytes,
                width=32,
                height=24,
            )

            operation_id = uuid4()
            request = {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
                "operation_id": str(operation_id),
                "purpose": "thumbnail",
                "expected_source_snapshot": None,
                "policy": _POLICY.model_dump(mode="json"),
                "force": False,
            }
            transport = _DropFirstImageResponse()
            async with httpx.AsyncClient(
                base_url=str(runtime.control_client.base_url),
                timeout=10,
                trust_env=False,
                transport=transport,
            ) as uncertain_client:
                with pytest.raises(httpx.ReadError):
                    await uncertain_client.post(
                        "/v1/images/commands/prepare_image_media",
                        headers={"Authorization": f"Bearer {runtime.control_token}"},
                        json=request,
                    )
                assert transport.dropped is True
                replay = await uncertain_client.post(
                    "/v1/images/commands/prepare_image_media",
                    headers={"Authorization": f"Bearer {runtime.control_token}"},
                    json=request,
                )
            assert replay.status_code == 200, replay.text[:500]
            accepted = replay.json()["data"]
            assert accepted["operation_id"] == str(operation_id)
            job_id = UUID(accepted["work"]["job_id"])
            runtime.cache_prefixes.append(f"image-assets/{job_id}/")
            async with runtime.test_engine.connect() as owner:
                receipt_count = await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_image_operations "
                        "WHERE operation_id=:operation"
                    ),
                    {"operation": operation_id},
                )
                receipt_job_id = await owner.scalar(
                    text(
                        "SELECT result #>> '{data,work,job_id}' "
                        "FROM public.asset_image_operations WHERE operation_id=:operation"
                    ),
                    {"operation": operation_id},
                )
                same_owner_job_count = await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_image_jobs "
                        "WHERE job_id=:job AND owner_kind='dataset_item' "
                        "AND owner_id=:item AND kind='thumbnail'"
                    ),
                    {"job": job_id, "item": item_id},
                )
            assert receipt_count == 1
            assert receipt_job_id == accepted["work"]["job_id"]
            assert same_owner_job_count == 1

            async with runtime.test_engine.begin() as owner:
                await owner.execute(
                    text("UPDATE public.users SET is_active=false WHERE id=:actor"),
                    {"actor": actor_id},
                )
            worker, worker_log = runtime.start_worker(
                "image-thumbnail-worker", job_id=job_id
            )
            assert (
                await _wait_process_exit(
                    worker,
                    timeout=90,
                    message="independent image thumbnail worker did not finish",
                )
                == 0
            ), _process_log(worker_log)
            job = await _wait_job(
                runtime.test_engine,
                job_id,
                worker,
                worker_log,
                statuses={"ready"},
            )
            assert job["result"]["kind"] == "thumbnail"
            assert job["attempt_count"] == 1
            async with runtime.test_engine.connect() as owner:
                actor_active = await owner.scalar(
                    text("SELECT is_active FROM public.users WHERE id=:actor"),
                    {"actor": actor_id},
                )
                item = (
                    await owner.execute(
                        text(
                            "SELECT thumbnail_path,blurhash,media_source_generation,"
                            "media_materialized_generation,media_source_drifted "
                            "FROM public.dataset_items WHERE id=:item"
                        ),
                        {"item": item_id},
                    )
                ).one()
            assert actor_active is False
            assert item.media_source_drifted is False
            assert item.media_materialized_generation == item.media_source_generation
            assert item.blurhash == job["result"]["blurhash"] and item.blurhash
            assert item.thumbnail_path == job["result"]["artifact"]["key"]
            assert item.thumbnail_path.startswith(
                f"image-assets/{job_id}/g{job['generation']}/a"
            )
            assert item.thumbnail_path.endswith("/thumbnail.webp")

            async with runtime.test_engine.begin() as owner:
                await owner.execute(
                    text("UPDATE public.users SET is_active=true WHERE id=:actor"),
                    {"actor": actor_id},
                )
            source_request = {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
            }
            reference_response = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/source-reference",
                source_request,
            )
            assert reference_response.status_code == 200, reference_response.text[:500]
            reference = reference_response.json()["data"]
            assert (
                reference["source"]["thumbnail_source_generation"]
                == reference["source"]["source_generation"]
            )
            assert reference["source"]["thumbnail_path"] == item.thumbnail_path
            assert reference["source"]["blurhash"] == item.blurhash
            assert reference["thumbnail_url"]
            original = await runtime.control_client.get(reference["original_url"])
            thumbnail = await runtime.control_client.get(reference["thumbnail_url"])
            assert original.status_code == 200 and original.content == source_bytes
            assert thumbnail.status_code == 200 and thumbnail.content
            from io import BytesIO

            with Image.open(BytesIO(thumbnail.content)) as rendered:
                rendered.load()
                assert rendered.format == "WEBP"
                assert rendered.size == (256, 192)
                red, green, blue = rendered.convert("RGB").getpixel((120, 90))
                assert abs(red - 240) <= 8
                assert green <= 30 and blue <= 40
            await _assert_image_storage_scopes(
                runtime, source_key=source_key, image_job_id=job_id
            )
        finally:
            if (
                source_key
                and (runtime.datasets_bucket, source_key) not in runtime.source_objects
            ):
                runtime.source_objects.append((runtime.datasets_bucket, source_key))


async def test_independent_vips_pyramid_is_current_grid_and_expired_lease_is_fenced(
    test_engine, tmp_path
):
    if not _srgb_profile().is_file():
        pytest.fail(
            "the real pyramid qualification requires an installed sRGB ICC profile"
        )
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        source_key = f"image-source/{uuid4()}/pyramid.png"
        source_bytes = _quadrant_png((1024, 1024))
        try:
            async with runtime.owner_sessions.begin() as owner:
                actor_id = await _seed_actor(owner, role="super_admin")
            runtime.track_actor(actor_id)
            _dataset_id, item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=source_key,
                source_bytes=source_bytes,
                width=1024,
                height=1024,
            )
            accepted, _request = await _accept_image_work(
                runtime,
                actor_id=actor_id,
                item_id=item_id,
                purpose="pyramid",
            )
            job_id = UUID(accepted["work"]["job_id"])
            gate = _write_processing_gate(tmp_path / "pyramid-lease", backend="vips")
            stale_worker, stale_log = runtime.start_worker(
                "image-pyramid-expired-worker", gate=gate, job_id=job_id
            )
            await _wait_marker(gate[1] / "entered", stale_worker, stale_log)
            claimed = await _read_job(runtime.test_engine, job_id)
            assert claimed["status"] == "running" and claimed["lease_token"]
            assert claimed["attempt_count"] == 1
            stale_token = claimed["lease_token"]
            stale_prefix = claimed["output_prefix"]
            assert stale_prefix.startswith(
                f"image-pyramids/{claimed['pyramid_asset_id']}/"
            )
            runtime.cache_prefixes.append(
                f"image-pyramids/{claimed['pyramid_asset_id']}/"
            )

            active_competitor, active_log = runtime.start_worker(
                "image-pyramid-active-competitor", job_id=job_id
            )
            assert (
                await _wait_process_exit(
                    active_competitor,
                    timeout=30,
                    message="active-lease competitor did not exit",
                )
                == 0
            ), _process_log(active_log)
            still_claimed = await _read_job(runtime.test_engine, job_id)
            assert still_claimed["status"] == "running"
            assert still_claimed["lease_token"] == stale_token
            assert still_claimed["attempt_count"] == 1

            os.killpg(stale_worker.pid, signal.SIGSTOP)
            async with runtime.test_engine.begin() as owner:
                expired = await owner.execute(
                    text(
                        "UPDATE public.asset_image_jobs SET lease_expires_at=now() "
                        "- interval '1 second' WHERE job_id=:job "
                        "AND status='running' AND lease_token=:lease"
                    ),
                    {"job": job_id, "lease": stale_token},
                )
            assert expired.rowcount == 1
            replacement, replacement_log = runtime.start_worker(
                "image-pyramid-replacement-worker", job_id=job_id
            )
            assert (
                await _wait_process_exit(
                    replacement,
                    timeout=120,
                    message="replacement image worker did not publish the pyramid",
                )
                == 0
            ), _process_log(replacement_log)
            ready = await _wait_job(
                runtime.test_engine,
                job_id,
                replacement,
                replacement_log,
                statuses={"ready"},
                timeout=30,
            )
            assert ready["attempt_count"] == 2
            assert ready["output_prefix"] != stale_prefix
            async with runtime.test_engine.connect() as owner:
                garbage = (
                    await owner.execute(
                        text(
                            "SELECT prefix,reason FROM public.asset_image_garbage "
                            "WHERE prefix=:prefix"
                        ),
                        {"prefix": stale_prefix},
                    )
                ).first()
            assert garbage is not None and garbage.reason == "attempt_abandoned"

            (gate[1] / "release").touch()
            os.killpg(stale_worker.pid, signal.SIGCONT)
            await _wait_process_exit(
                stale_worker,
                timeout=90,
                message="expired image worker did not stop after replacement",
            )
            async with runtime.test_engine.connect() as owner:
                current = (
                    await owner.execute(
                        text(
                            "SELECT asset.active_generation,generation.status,"
                            "generation.output_prefix,generation.manifest "
                            "FROM public.image_pyramid_assets AS asset "
                            "JOIN public.image_pyramid_generations AS generation "
                            "ON generation.asset_id=asset.id "
                            "WHERE asset.id=:asset AND generation.generation=:generation"
                        ),
                        {
                            "asset": ready["pyramid_asset_id"],
                            "generation": ready["pyramid_generation"],
                        },
                    )
                ).one()
            assert current.active_generation == ready["pyramid_generation"]
            assert current.status == "ready"
            assert current.output_prefix != stale_prefix

            generation_response = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/generation-reference",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(item_id),
                    "expected_source_snapshot": accepted["source"]["source_snapshot"],
                },
            )
            assert generation_response.status_code == 200, generation_response.text[
                :500
            ]
            generation = generation_response.json()["data"]
            assert generation["pyramid"]["status"] == "ready"
            manifest = generation["manifest"]
            assert manifest["width"] == 1024 and manifest["height"] == 1024
            assert manifest["tileSize"] == 512 and manifest["overlap"] == 1
            levels = manifest["levels"]
            assert len(levels) == 11
            for index, level in enumerate(levels):
                scale = 1 << index
                width = max(1, (1024 + scale - 1) // scale)
                height = max(1, (1024 + scale - 1) // scale)
                assert level == {
                    "level": index,
                    "scaleFactor": scale,
                    "width": width,
                    "height": height,
                    "columns": (width + 511) // 512,
                    "rows": (height + 511) // 512,
                }
            overview = await runtime.control_client.get(generation["overview_url"])
            assert overview.status_code == 200 and overview.content
            from io import BytesIO

            with Image.open(BytesIO(overview.content)) as rendered_overview:
                rendered_overview.load()
                assert rendered_overview.format == "WEBP"
                assert rendered_overview.size == (
                    manifest["overview"]["width"],
                    manifest["overview"]["height"],
                )

            tile_response = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/asset-references",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(item_id),
                    "expected_source_snapshot": accepted["source"]["source_snapshot"],
                    "items": [
                        {
                            "kind": "tile",
                            "generation": generation["pyramid"]["generation"],
                            "level": 0,
                            "x": 0,
                            "y": 0,
                        }
                    ],
                },
            )
            assert tile_response.status_code == 200, tile_response.text[:500]
            tile = tile_response.json()["data"]["items"][0]
            tile_download = await runtime.control_client.get(tile["url"])
            assert tile_download.status_code == 200 and tile_download.content
            from aap_assets_images.geometry import expected_tile_dimensions

            expected_size = expected_tile_dimensions(levels[0], 0, 0)
            with Image.open(BytesIO(tile_download.content)) as rendered_tile:
                rendered_tile.load()
                assert rendered_tile.format == "WEBP"
                assert rendered_tile.size == expected_size
                assert rendered_tile.convert("RGB").getpixel((100, 100))[0] > 150
        finally:
            runtime.source_objects.append((runtime.datasets_bucket, source_key))


async def test_image_source_overwrite_cancels_claim_and_keeps_only_garbage_prefix(
    test_engine, tmp_path
):
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        source_key = f"image-source/{uuid4()}/overwrite.png"
        source_bytes = _solid_png((48, 32), (20, 180, 45))
        replacement_bytes = _solid_png((48, 32), (210, 30, 70))
        try:
            async with runtime.owner_sessions.begin() as owner:
                actor_id = await _seed_actor(owner, role="super_admin")
            runtime.track_actor(actor_id)
            _dataset_id, item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=source_key,
                source_bytes=source_bytes,
                width=48,
                height=32,
            )
            accepted, _request = await _accept_image_work(
                runtime,
                actor_id=actor_id,
                item_id=item_id,
                purpose="thumbnail",
            )
            job_id = UUID(accepted["work"]["job_id"])
            gate = _write_processing_gate(
                tmp_path / "source-overwrite", backend="pillow"
            )
            worker, worker_log = runtime.start_worker(
                "image-source-overwrite-worker", gate=gate, job_id=job_id
            )
            await _wait_marker(gate[1] / "entered", worker, worker_log)
            claimed = await _read_job(runtime.test_engine, job_id)
            old_prefix = claimed["output_prefix"]
            assert claimed["status"] == "running" and old_prefix
            runtime.cache_prefixes.append(f"image-assets/{job_id}/")

            runtime.owner_s3.put_object(
                Bucket=runtime.datasets_bucket,
                Key=source_key,
                Body=replacement_bytes,
                ContentType="image/png",
            )
            observed = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/source-reference",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(item_id),
                },
            )
            assert observed.status_code == 409, observed.text[:500]
            assert observed.json()["error"]["code"] == "source_changed"
            (gate[1] / "release").touch()
            await _wait_process_exit(
                worker,
                timeout=90,
                message="overwritten-source worker did not terminate",
            )
            row = await _wait_job(
                runtime.test_engine,
                job_id,
                worker,
                worker_log,
                statuses={"canceled", "failed"},
                timeout=20,
            )
            assert row["status"] == "canceled"
            assert row["error_code"] == "source_changed"
            async with runtime.test_engine.connect() as owner:
                source = (
                    await owner.execute(
                        text(
                            "SELECT media_source_drifted,media_source_generation,"
                            "thumbnail_path,blurhash,media_materialized_generation "
                            "FROM public.dataset_items WHERE id=:item"
                        ),
                        {"item": item_id},
                    )
                ).one()
                garbage = (
                    await owner.execute(
                        text(
                            "SELECT prefix,reason FROM public.asset_image_garbage "
                            "WHERE prefix=:prefix"
                        ),
                        {"prefix": old_prefix},
                    )
                ).first()
            assert source.media_source_drifted is True
            assert (
                source.media_source_generation > accepted["source"]["source_generation"]
            )
            assert source.thumbnail_path is None and source.blurhash is None
            assert (
                source.media_materialized_generation != source.media_source_generation
            )
            assert garbage is not None and garbage.reason == "source_changed"
        finally:
            runtime.source_objects.append((runtime.datasets_bucket, source_key))


async def test_unlink_cancels_task_image_work_and_retains_attempt_garbage(
    test_engine, tmp_path
):
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        dataset_source_key = f"image-source/{uuid4()}/link.png"
        task_source_key = f"task-image-source/{uuid4()}/original.png"
        dataset_source = _solid_png((48, 32), (100, 90, 80))
        task_source = _solid_png((48, 32), (35, 170, 70))
        runtime.source_objects.extend(
            [
                (runtime.datasets_bucket, dataset_source_key),
                (runtime.legacy_bucket, task_source_key),
            ]
        )
        try:
            runtime.owner_s3.put_object(
                Bucket=runtime.datasets_bucket,
                Key=dataset_source_key,
                Body=dataset_source,
                ContentType="image/png",
            )
            runtime.owner_s3.put_object(
                Bucket=runtime.legacy_bucket,
                Key=task_source_key,
                Body=task_source,
                ContentType="image/png",
            )
            async with runtime.owner_sessions.begin() as owner:
                actor_id = await _seed_actor(owner, role="super_admin")
                runtime.track_actor(actor_id)
                dataset_id = await _seed_dataset(
                    owner,
                    actor_id=actor_id,
                    name=f"image-unlink-{uuid4().hex[:12]}",
                    data_type="image",
                    file_count=1,
                )
                runtime.dataset_ids.append(dataset_id)
                linked_item_id = uuid4()
                runtime.item_ids.append(linked_item_id)
                task_id = uuid4()
                runtime.task_ids.append(task_id)
                owner.add(
                    DatasetItem(
                        id=linked_item_id,
                        dataset_id=dataset_id,
                        file_name="linked-source.png",
                        file_path=dataset_source_key,
                        file_type="image",
                        file_size=len(dataset_source),
                        content_hash=sha256(dataset_source).hexdigest(),
                        width=48,
                        height=32,
                        metadata_={},
                    )
                )
                project = await create_project(
                    owner,
                    owner_id=actor_id,
                    name=f"image-unlink-project-{uuid4().hex[:8]}",
                    type_key="image",
                    type_label="Image",
                )
                project_id = project.id
                runtime.project_ids.append(project_id)
                owner.add_all(
                    [
                        ProjectDataset(dataset_id=dataset_id, project_id=project_id),
                        Task(
                            id=task_id,
                            project_id=project_id,
                            dataset_item_id=None,
                            display_id=f"T-IMAGE-UNLINK-{task_id.hex[:12]}",
                            file_name="task-image.png",
                            file_path=task_source_key,
                            file_type="image",
                        ),
                        TaskDatasetItemLink(
                            task_id=task_id,
                            dataset_item_id=linked_item_id,
                            role="primary_lidar",
                        ),
                    ]
                )
            accepted, _request = await _accept_image_work(
                runtime,
                actor_id=actor_id,
                task_id=task_id,
                purpose="thumbnail",
            )
            job_id = UUID(accepted["work"]["job_id"])
            gate = _write_processing_gate(tmp_path / "task-unlink", backend="pillow")
            worker, worker_log = runtime.start_worker(
                "image-task-unlink-worker", gate=gate, job_id=job_id
            )
            await _wait_marker(gate[1] / "entered", worker, worker_log)
            claimed = await _read_job(runtime.test_engine, job_id)
            prefix = claimed["output_prefix"]
            assert claimed["owner_kind"] == "task" and claimed["owner_id"] == task_id
            assert claimed["status"] == "running" and claimed["lease_token"]
            assert prefix and prefix.startswith(f"image-assets/{job_id}/")
            runtime.cache_prefixes.append(f"image-assets/{job_id}/")

            async with runtime.owner_sessions.begin() as owner:
                receipt = await unlink_dataset_from_project(
                    owner,
                    dataset_id,
                    project_id,
                    actor_id=actor_id,
                    cleanup_bucket=runtime.cache_bucket,
                    operation_id=uuid4(),
                )
                assert receipt is not None and receipt["deleted_tasks"] == 1
            canceled = await _read_job(runtime.test_engine, job_id)
            assert canceled["status"] == "canceled"
            assert canceled["error_detail"] == "task_unlinked"
            async with runtime.test_engine.connect() as owner:
                tombstone = (
                    await owner.execute(
                        text(
                            "SELECT prefix,reason,not_before,completed_at "
                            "FROM public.asset_image_garbage WHERE prefix=:prefix"
                        ),
                        {"prefix": prefix},
                    )
                ).first()
                task_exists = await owner.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM public.tasks WHERE id=:task)"),
                    {"task": task_id},
                )
            assert tombstone is not None and tombstone.reason == "task_unlinked"
            assert tombstone.completed_at is None
            assert task_exists is False

            canceled_competitor, competitor_log = runtime.start_worker(
                "image-canceled-task-competitor", job_id=job_id
            )
            assert (
                await _wait_process_exit(
                    canceled_competitor,
                    timeout=30,
                    message="worker for canceled Task image job did not exit",
                )
                == 0
            ), _process_log(competitor_log)
            assert (await _read_job(runtime.test_engine, job_id))[
                "status"
            ] == "canceled"

            # The old process can finish its local encoder after unlink, but its
            # accepted attempt remains canceled and its immutable prefix tombstone
            # is retained for the delayed garbage sweeper.
            (gate[1] / "release").touch()
            await _wait_process_exit(
                worker,
                timeout=90,
                message="unlinked Task worker did not terminate after its gate opened",
            )
            final_job = await _read_job(runtime.test_engine, job_id)
            assert final_job["status"] == "canceled"
            async with runtime.test_engine.connect() as owner:
                final_tombstone = await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_image_garbage "
                        "WHERE prefix=:prefix AND reason='task_unlinked' "
                        "AND completed_at IS NULL"
                    ),
                    {"prefix": prefix},
                )
                task_thumbnail = await owner.scalar(
                    text("SELECT thumbnail_path FROM public.tasks WHERE id=:task"),
                    {"task": task_id},
                )
            assert final_tombstone == 1
            assert task_thumbnail is None
        finally:
            runtime.source_objects.extend(
                [
                    (runtime.datasets_bucket, dataset_source_key),
                    (runtime.legacy_bucket, task_source_key),
                ]
            )
