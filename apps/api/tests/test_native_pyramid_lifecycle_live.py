"""Integrated real Native saved-output lifecycle proof.

Covers the physical production storage port (not the image encoder):
production-port member write -> owner receipt registration/verification ->
independent Images control read -> binding switch A->B -> independent cleanup.

Every SQL capability runs through its restricted LOGIN role and every object
operation is an actual S3 request against the verifier's owned MinIO.  The
Source signer runs as its own process under the bounded Native identity; the
executors only receive role tokens.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset import DatasetItem
from app.db.models.storage_connection import StorageConnection
from app.services import media_cache_binding_change
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage_location import register_connection
from app.schemas.storage_location import (
    MediaCacheBindingApply,
    MediaCacheBindingChange,
)
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_media_live import (
    _free_loopback_port,
    _process_log,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _wait_control_ready,
)
from yoao_assets_images import contract as c
from yoao_assets_images.geometry import (
    build_manifest,
    canonical_manifest_bytes,
    expected_tile_dimensions,
    pyramid_levels,
    sha256_digest,
)
from yoao_assets_images.native_storage_client import NativePyramidStorageClient
from yoao_assets_images.policy import ImagePolicy
from yoao_assets_images.transport import ImageGenerationReference
from yoao_assets_images_runtime.config import ImageStorageConfig
from yoao_assets_images_runtime.database import AsyncpgImageDatabase
from yoao_assets_images_runtime.storage import S3ImageStorage
from yoao_assets_media_runtime.fixture_native_outputs import (
    native_execution,
    output_scope,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the module-data verifier's owned PostgreSQL and S3",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
POLICY = ImagePolicy(
    auto_generate_pyramids=True,
    optional_pixels=1_048_576,
    gc_grace_hours=0,
    url_expiry_seconds=10,
)
POLICY_ENV = {
    "IMAGE_PYRAMID_AUTO_GENERATE": "true",
    "IMAGE_PYRAMID_OPTIONAL_PIXELS": str(POLICY.optional_pixels),
    "IMAGE_PYRAMID_GC_GRACE_HOURS": str(POLICY.gc_grace_hours),
    "IMAGE_PYRAMID_URL_EXPIRY_SECONDS": str(POLICY.url_expiry_seconds),
}
WIDTH = HEIGHT = 1024


def _guard_role_url(env_name: str, username: str, test_engine) -> Any:
    raw = _required_env(env_name)
    parsed = make_url(raw)
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


def _png(size: tuple[int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (32, 96, 160)).save(output, format="PNG", optimize=True)
    return output.getvalue()


def _webp(size: tuple[int, int], seed: int) -> bytes:
    output = BytesIO()
    color = ((seed * 37) % 256, (seed * 91) % 256, (seed * 13) % 256)
    Image.new("RGB", size, color).save(output, format="WEBP", quality=60)
    return output.getvalue()


async def _wait_until(check, *, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio_sleep()
    pytest.fail(f"{message}; last={last!r}")


async def asyncio_sleep(seconds: float = 0.2) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGCONT)
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _member_layout(prefix: str, generation: int, source_sha: str):
    """Return real member files for the canonical grid of this source."""
    levels = pyramid_levels(WIDTH, HEIGHT)
    files: dict[str, bytes] = {}
    for level in levels:
        for x in range(level["columns"]):
            for y in range(level["rows"]):
                relative = f"tiles/{level['level']}/{x}/{y}.webp"
                files[relative] = _webp(
                    expected_tile_dimensions(level, x, y),
                    level["level"] * 97 + x * 31 + y,
                )
    overview = _webp((levels[1]["width"], levels[1]["height"]), 7)
    files["overview.webp"] = overview
    manifest = build_manifest(
        generation=generation,
        source_fingerprint="sha256:" + source_sha,
        width=WIDTH,
        height=HEIGHT,
        overview_width=levels[1]["width"],
        overview_height=levels[1]["height"],
        overview_digest=sha256_digest(overview),
        policy=POLICY,
    )
    files["manifest.json"] = canonical_manifest_bytes(manifest)
    return manifest, files


class _Executor:
    """Drive the production Storage port with real SQL roles and S3 PUT/GET."""

    def __init__(self, database, storage: S3ImageStorage, owner_s3):
        self.database = database
        self.storage = storage
        self.owner_s3 = owner_s3
        self.receipts: dict[str, tuple[c.PyramidMemberReceipt, Any]] = {}

    async def upload(self, allocation, relative_key: str, body: bytes):
        path = Path(self.tmp_dir) / relative_key.replace("/", "_")
        path.write_bytes(body)
        digest = sha256(body).hexdigest()
        operation_id = uuid4()
        reservation = c.PyramidMemberReservation(
            operation_id=operation_id,
            output_id=allocation.output_id,
            relative_key=relative_key,
            sha256=digest,
            size_bytes=len(body),
        )
        await self.database.call("begin_native_pyramid_member", reservation)
        await self.database.call(
            "submit_native_pyramid_member",
            c.PyramidMemberOperation(operation_id=operation_id),
        )
        receipt = await self.storage.upload_pyramid_member(
            path, allocation, reservation
        )
        if not isinstance(receipt, c.PyramidMemberReceipt) or (
            receipt.operation_id != operation_id
        ):
            raise AssertionError("production port did not return the admitted receipt")
        status = await self.database.call("write_native_pyramid_member", receipt)
        if status.state != "written":
            raise AssertionError("owner did not record the raw receipt")
        await self.storage.verify_pyramid_member(allocation, reservation, receipt)
        verified = await self.database.call(
            "verify_native_pyramid_member",
            c.PyramidMemberOperation(operation_id=operation_id),
        )
        if verified.state != "verified":
            raise AssertionError("owner did not record the verified member")
        key = allocation.storage_ref.prefix + relative_key
        head = self.owner_s3.head_object(
            Bucket=allocation.storage_ref.bucket, Key=key
        )
        if head.get("ETag") != receipt.etag:
            raise AssertionError("recorded ETag differs from the raw HTTP ETag")
        if head.get("VersionId") != receipt.version_id:
            raise AssertionError("recorded version differs from the raw HTTP version")
        self.receipts[relative_key] = (receipt, head)
        return c.Artifact(key=key, byte_size=len(body), sha256=digest)

    async def produce(self, claim: c.ImageClaim):
        async with native_execution(self.database, "image", claim.job_id) as admitted:
            if not admitted:
                raise AssertionError("ordinary producer execution was not admitted")
            async with output_scope(
                self.database, self.storage, claim, "image", None
            ):
                return await self._produce_admitted(claim)

    async def _produce_admitted(self, claim: c.ImageClaim):
        allocation = await self.database.call(
            "allocate_native_pyramid_output",
            c.NativePyramidAllocationRequest(
                job_id=claim.job_id,
                job_generation=claim.generation,
                lease_token=claim.lease_token,
                asset_id=claim.pyramid_asset_id,
                generation=claim.pyramid_generation,
            ),
        )
        if (
            not isinstance(allocation, c.PyramidOutputAllocation)
            or allocation.job_id != claim.job_id
            or allocation.job_generation != claim.generation
            or allocation.lease_token != claim.lease_token
        ):
            raise AssertionError("allocation escaped the admitted job lease")
        source_sha = claim.source.content_hash
        manifest, files = _member_layout(
            allocation.storage_ref.prefix, claim.pyramid_generation, source_sha
        )
        self.files = files
        artifacts = {}
        for relative, body in files.items():
            artifacts[relative] = await self.upload(allocation, relative, body)
        tiles = [
            c.ImageTileArtifact(
                level=tile_level,
                x=x,
                y=y,
                artifact=artifacts[f"tiles/{tile_level}/{x}/{y}.webp"],
            )
            for tile_level in range(len(manifest["levels"]))
            for x in range(manifest["levels"][tile_level]["columns"])
            for y in range(manifest["levels"][tile_level]["rows"])
        ]
        result = c.PyramidResult(
            source_identity=claim.source.object_identity,
            source_sha256=source_sha,
            manifest=c.ImagePyramidManifest.model_validate(manifest),
            manifest_artifact=artifacts["manifest.json"],
            overview_artifact=artifacts["overview.webp"],
            tile_artifacts=tiles,
            tile_count=sum(
                level["columns"] * level["rows"] for level in manifest["levels"]
            ),
            retained_bytes=sum(item.artifact.byte_size for item in tiles)
            + artifacts["manifest.json"].byte_size
            + artifacts["overview.webp"].byte_size,
        )
        outcome = await self.database.call(
            "finish_native_pyramid_output",
            c.FinishNativePyramidRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=result,
            ),
        )
        assert isinstance(outcome, c.ImageWorkOutcome)
        assert outcome.outcome == "published"
        return allocation, manifest, artifacts


async def _prepare(client: httpx.AsyncClient, token: str, actor_id, item_id, *, force=False):
    request = {
        "actor_id": str(actor_id),
        "task_id": None,
        "dataset_item_id": str(item_id),
        "operation_id": str(uuid4()),
        "purpose": "pyramid",
        "expected_source_snapshot": None,
        "policy": POLICY.model_dump(mode="json"),
        "force": force,
    }
    response = await client.post(
        "/v1/images/commands/prepare_image_media",
        headers={"Authorization": f"Bearer {token}"},
        json=request,
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("ok") is not True:
        pytest.fail(f"image work preparation failed: {response.text[:500]}")
    return payload["data"]


async def _post(client, token, path, body):
    return await client.post(
        path, headers={"Authorization": f"Bearer {token}"}, json=body
    )


async def _assert_generation_reference(client, response, claim, target):
    assert response.status_code == 200, response.text[:500]
    reference = ImageGenerationReference.model_validate(response.json()["data"])
    assert reference.pyramid.status == "ready"
    assert reference.pyramid.generation == claim.pyramid_generation
    assert reference.pyramid_source_generation == claim.source.source_generation
    assert reference.pyramid_source_snapshot == claim.source.source_snapshot
    assert reference.expires_in == POLICY.url_expiry_seconds
    assert reference.overview_url is not None
    signed = urlsplit(reference.overview_url)
    saved = urlsplit(target.endpoint_url)
    assert (signed.scheme, signed.netloc) == (saved.scheme, saved.netloc)
    assert unquote(signed.path) == f"/{target.bucket}/{target.prefix}overview.webp"
    assert parse_qs(signed.query)["X-Amz-Expires"] == [str(reference.expires_in)]
    overview = await client.get(reference.overview_url)
    assert overview.status_code == 200
    assert overview.content == _member_layout(
        target.prefix, claim.pyramid_generation, claim.source.content_hash
    )[1]["overview.webp"]


async def _create_connection(
    sessions, *, actor_id, name: str, bucket: str, prefix: str
):
    async with sessions.begin() as db:
        connection = StorageConnection(
            id=uuid4(),
            name=name,
            kind="s3",
            scope="global",
            config={
                "endpoint": f"{'https' if settings.minio_use_ssl else 'http'}://"
                f"{settings.minio_endpoint}",
                "bucket": bucket,
                "base_prefix": prefix,
                "use_ssl": settings.minio_use_ssl,
            },
            secret_enc=encrypt_secret(
                {
                    "access_key": _required_env(
                        "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_ACCESS_KEY"
                    ),
                    "secret_key": _required_env(
                        "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_SECRET_KEY"
                    ),
                }
            ),
            created_by=actor_id,
        )
        db.add(connection)
        await db.flush()
        location = await register_connection(db, connection)
        return connection.id, location.id


async def test_native_saved_output_lifecycle_across_binding_switch(
    test_engine, tmp_path, native_signer
):
    native_signer.assert_running()
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
    cleanup_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_CLEANUP_URL",
        "asset_cleanup_client",
        test_engine,
    )
    resources = _verified_resource_manifest(test_engine, cleanup_url)
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    cache_bucket = resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (datasets_bucket, cache_bucket, native_signer.connection_bucket):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    if (
        native_signer.connection_access_key
        in {settings.minio_access_key, native_signer.native_access_key}
        or native_signer.native_access_key == settings.minio_access_key
    ):
        pytest.fail("Native proof identities are not independently scoped")

    worker_access = _required_env("AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_ACCESS_KEY")
    worker_secret = _required_env("AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_SECRET_KEY")
    if (worker_access, worker_secret) == (
        settings.minio_access_key,
        settings.minio_secret_key,
    ):
        pytest.fail("image worker reuses the MinIO administrator")

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    control_token = secrets.token_urlsafe(32)
    control_port = _free_loopback_port()
    control_log = tmp_path / "native-images-control.log"
    control_environment = _runtime_environment(
        tmp_path=tmp_path,
        role="control",
        database_url=_runtime_database_url(control_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=datasets_bucket,
        legacy_bucket=native_signer.connection_bucket,
        cache_bucket=cache_bucket,
        access_key=_required_env(
            "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY"
        ),
        secret_key=_required_env(
            "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY"
        ),
        token=control_token,
        port=control_port,
        image_control_settings={
            **POLICY_ENV,
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL": native_signer.url,
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN": native_signer.reader_token,
        },
    )
    control_process, control_stream = _start_process(
        [sys.executable, "-m", "yoao_assets_media_runtime"],
        control_environment,
        control_log,
    )
    cleanup_process = None
    cleanup_stream = None
    created_objects: list[tuple[str, str]] = []
    saved_keys: list[tuple[str, str]] = []
    uploads: list[tuple[str, str, str]] = []
    connection_id = None
    location_id = None
    previous_binding = None
    control_client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{control_port}", timeout=30, trust_env=False)
    proof_succeeded = False
    try:
        await _wait_control_ready(control_client, control_process, control_log)
        async with sessions.begin() as db:
            await ensure_legacy_bindings(db)
            actor_id = await _seed_actor(db, role="super_admin")
            dataset_id = await _seed_dataset(
                db, actor_id=actor_id, name=f"native-live-{uuid4().hex[:8]}"
            )
            source_key = f"image-source/{uuid4()}/native-source.png"
            source_bytes = _png((WIDTH, HEIGHT))
            owner_s3.put_object(
                Bucket=datasets_bucket,
                Key=source_key,
                Body=source_bytes,
                ContentType="image/png",
            )
            created_objects.append((datasets_bucket, source_key))
            item_id = uuid4()
            db.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name=f"native-{item_id.hex[:8]}.png",
                    file_path=source_key,
                    file_type="image",
                    file_size=len(source_bytes),
                    content_hash=sha256(source_bytes).hexdigest(),
                    width=WIDTH,
                    height=HEIGHT,
                    metadata_={},
                )
            )

        worker_db = await AsyncpgImageDatabase.connect(
            _runtime_database_url(worker_url)
        )
        control_db = await AsyncpgImageDatabase.connect(
            _runtime_database_url(control_url),
            allowed_roles=("control",),
        )
        storage_config = ImageStorageConfig(
            endpoint=endpoint,
            public_endpoint=endpoint,
            region="us-east-1",
            datasets_bucket=datasets_bucket,
            legacy_bucket=native_signer.connection_bucket,
            cache_bucket=cache_bucket,
            inputs_bucket=_required_env("MINIO_INPUTS_BUCKET"),
            access_key=worker_access,
            secret_key=worker_secret,
            connect_timeout=2,
            read_timeout=30,
            max_attempts=1,
        )
        writer_client = NativePyramidStorageClient(
            native_signer.url, native_signer.writer_token
        )
        storage = S3ImageStorage(storage_config, native_writer=writer_client)
        executor = _Executor(worker_db, storage, owner_s3)
        executor.tmp_dir = tmp_path
        accepted = await _prepare(control_client, control_token, actor_id, item_id)
        first_job = UUID(accepted["work"]["job_id"])
        claimed = await worker_db.call(
            "claim_image_media",
            c.ClaimImageMediaRequest(
                worker_id=uuid4(), job_id=first_job, lease_seconds=300,
                kinds=["pyramid"],
            ),
        )
        claim = claimed.claim
        assert claim is not None and isinstance(claim.spec, c.PyramidWorkSpec)
        allocation_a, manifest_a, artifacts_a = await executor.produce(claim)
        target_a = allocation_a.storage_ref
        saved_keys.extend(
            (cache_bucket, key) for key in _layout_keys(target_a.prefix)
        )
        assert target_a.bucket == cache_bucket
        assert target_a.key_prefix == ""
        assert target_a.prefix == f"image-pyramids/objects/{allocation_a.output_id}/"
        # Every saved member was read back under its exact recorded version.
        for relative, (receipt, head) in executor.receipts.items():
            params = {
                "Bucket": cache_bucket,
                "Key": target_a.prefix + relative,
                **({"VersionId": receipt.version_id} if receipt.version_id else {}),
            }
            body = owner_s3.get_object(**params)["Body"]
            try:
                assert body.read() == executor.files[relative]
            finally:
                body.close()
        async with test_engine.connect() as owner:
            stored = (
                await owner.execute(
                    text(
                        "SELECT etag, version_id, state "
                        "FROM aap_assets_v1.image_pyramid_native_members "
                        "WHERE output_id=CAST(:output AS uuid)"
                    ),
                    {"output": allocation_a.output_id},
                )
            ).all()
        assert stored and all(row.state == "verified" for row in stored)
        for row in stored:
            # Literal "null" is stored verbatim; an omitted version is SQL NULL.
            assert (row.version_id is None) or isinstance(row.version_id, str)

        # Independent Images read through the real control process and permit.
        read = await _post(
            control_client,
            control_token,
            "/v1/images/generation-reference",
            {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
                "expected_source_snapshot": accepted["source"]["source_snapshot"],
            },
        )
        await _assert_generation_reference(control_client, read, claim, target_a)
        owner_state = await control_db.call(
            "read_image_generation",
            c.ReadImageGenerationRequest(
                actor_id=actor_id,
                dataset_item_id=item_id,
                generation=claim.pyramid_generation,
                profile_version=claim.spec.policy.profile_version,
            ),
        )
        assert owner_state.pyramid.native is True
        native_output = owner_state.pyramid.native_output
        assert native_output is not None
        assert native_output.storage_ref == target_a
        assert native_output.fixture_owned is False
        assert native_output.url_expiry_seconds == POLICY.url_expiry_seconds
        too_long = await _post(
            control_client,
            control_token,
            "/v1/images/generation-reference",
            {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
                "expected_source_snapshot": accepted["source"]["source_snapshot"],
                "expires_in": POLICY.url_expiry_seconds + 1,
            },
        )
        assert too_long.status_code == 422
        async with test_engine.connect() as owner:
            permits = (
                await owner.execute(
                    text(
                        "SELECT state, opened_at, closed_at, output_id "
                        "FROM aap_assets_v1.native_pyramid_read_permits "
                        "WHERE output_id=CAST(:output AS uuid)"
                    ),
                    {"output": allocation_a.output_id},
                )
            ).all()
        assert permits and all(row.state == "closed" for row in permits)
        assert all(
            row.opened_at is not None and row.closed_at >= row.opened_at
            for row in permits
        )

        # A settled read permit never reopens or accepts a different token.
        permit_operation, permit_token = uuid4(), uuid4()
        permit = await control_db.call(
            "begin_native_pyramid_read",
            c.BeginNativePyramidReadRequest(
                actor_id=actor_id,
                task_id=None,
                dataset_item_id=item_id,
                generation=claim.pyramid_generation,
                profile_version=claim.spec.policy.profile_version,
                operation_id=permit_operation,
                token=permit_token,
            ),
        )
        assert permit.output_id == allocation_a.output_id
        with pytest.raises(c.ImageFailure) as conflict:
            await control_db.call(
                "begin_native_pyramid_read",
                c.BeginNativePyramidReadRequest(
                    actor_id=actor_id,
                    task_id=None,
                    dataset_item_id=item_id,
                    generation=claim.pyramid_generation,
                    profile_version=claim.spec.policy.profile_version,
                    operation_id=permit_operation,
                    token=uuid4(),
                ),
            )
        assert conflict.value.code == "invalid_request"
        closure = await control_db.call(
            "complete_native_pyramid_read",
            c.CompleteNativePyramidReadRequest(
                operation_id=permit_operation, token=permit_token
            ),
        )
        assert closure.closed is True
        with pytest.raises(c.ImageFailure) as tombstone:
            await control_db.call(
                "begin_native_pyramid_read",
                c.BeginNativePyramidReadRequest(
                    actor_id=actor_id,
                    task_id=None,
                    dataset_item_id=item_id,
                    generation=claim.pyramid_generation,
                    profile_version=claim.spec.policy.profile_version,
                    operation_id=permit_operation,
                    token=permit_token,
                ),
            )
        assert tombstone.value.code == "operation_conflict"

        # Binding switch A -> B uses the reviewed owner operation; the control
        # process and worker configuration are never restarted or rewritten.
        connection_id, location_id = await _create_connection(
            sessions,
            actor_id=actor_id,
            name=f"native-b-{uuid4().hex[:8]}",
            bucket=native_signer.connection_bucket,
            prefix=native_signer.connection_prefix,
        )
        from app.db.models.user import User

        async with sessions() as db:
            actor = await db.get(User, actor_id)
            if actor is None or actor.role != "super_admin":
                pytest.fail("the seeded binding administrator is not active")
            binding = (
                await db.execute(
                    text(
                        "SELECT location_id, bucket, generation FROM public.storage_bindings "
                        "WHERE purpose='media-cache'"
                    )
                )
            ).one()
            previous_binding = (binding.location_id, binding.bucket, binding.generation)
            change = MediaCacheBindingChange(
                expected_generation=binding.generation,
                target_location_id=location_id,
                target_config_revision=1,
            )
            preview = await media_cache_binding_change.preview(db, change, actor)
            assert preview["can_apply"] is True
            assert preview["target"]["bucket"] == native_signer.connection_bucket
            applied = await media_cache_binding_change.apply(
                db,
                MediaCacheBindingApply(
                    expected_generation=binding.generation,
                    target_location_id=location_id,
                    target_config_revision=1,
                    impact_token=preview["impact_token"],
                ),
                actor,
            )
            assert applied["location_id"] == location_id
            assert applied["bucket"] == native_signer.connection_bucket
            await db.commit()
        assert control_process.poll() is None

        accepted_b = await _prepare(
            control_client, control_token, actor_id, item_id, force=True
        )
        second_job = UUID(accepted_b["work"]["job_id"])
        claimed_b = await worker_db.call(
            "claim_image_media",
            c.ClaimImageMediaRequest(
                worker_id=uuid4(), job_id=second_job, lease_seconds=300,
                kinds=["pyramid"],
            ),
        )
        claim_b = claimed_b.claim
        assert claim_b is not None and claim_b.job_id == second_job
        allocation_b, _manifest_b, _artifacts_b = await executor.produce(claim_b)
        target_b = allocation_b.storage_ref
        saved_keys.extend(
            (target_b.bucket, key) for key in _layout_keys(target_b.prefix)
        )
        # No endpoint/bucket/secret reconfiguration: B comes from the reviewed
        # binding and carries the registered connection coordinates.
        assert target_b.bucket == native_signer.connection_bucket
        assert target_b.key_prefix == (
            native_signer.connection_prefix + "/_aap_managed/media-cache/"
        )
        assert target_b.bucket != target_a.bucket
        assert allocation_b.output_id != allocation_a.output_id
        # Old saved read still resolves A while the current binding is B.
        stale_read = await _post(
            control_client,
            control_token,
            "/v1/images/generation-reference",
            {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
                "expected_source_snapshot": accepted["source"]["source_snapshot"],
                "generation": claim.pyramid_generation,
            },
        )
        await _assert_generation_reference(control_client, stale_read, claim, target_a)
        new_read = await _post(
            control_client,
            control_token,
            "/v1/images/generation-reference",
            {
                "actor_id": str(actor_id),
                "task_id": None,
                "dataset_item_id": str(item_id),
                "expected_source_snapshot": accepted_b["source"]["source_snapshot"],
            },
        )
        await _assert_generation_reference(control_client, new_read, claim_b, target_b)

        # Unknown I/O never retires: its own admitted operation stays open, so
        # retirement can never claim these members.  A drained not-written
        # outcome is the only abandonment that may settle that barrier.
        third = await _prepare(
            control_client, control_token, actor_id, item_id, force=True
        )
        third_job = UUID(third["work"]["job_id"])
        claimed_c = await worker_db.call(
            "claim_image_media",
            c.ClaimImageMediaRequest(
                worker_id=uuid4(), job_id=third_job, lease_seconds=300,
                kinds=["pyramid"],
            ),
        )
        claim_c = claimed_c.claim
        assert claim_c is not None and claim_c.job_id == third_job
        assert claim_c.pyramid_asset_id is not None
        assert claim_c.pyramid_generation is not None
        allocation_c = await worker_db.call(
            "allocate_native_pyramid_output",
            c.NativePyramidAllocationRequest(
                job_id=claim_c.job_id,
                job_generation=claim_c.generation,
                lease_token=claim_c.lease_token,
                asset_id=claim_c.pyramid_asset_id,
                generation=claim_c.pyramid_generation,
            ),
        )
        assert allocation_c.storage_ref.bucket == target_b.bucket
        unknown_operation = uuid4()
        unknown_sha = sha256(b"unknown-probe").hexdigest()
        await worker_db.call(
            "begin_native_pyramid_member",
            c.PyramidMemberReservation(
                operation_id=unknown_operation,
                output_id=allocation_c.output_id,
                relative_key="manifest.json",
                sha256=unknown_sha,
                size_bytes=13,
            ),
        )
        await worker_db.call(
            "submit_native_pyramid_member",
            c.PyramidMemberOperation(operation_id=unknown_operation),
        )
        await worker_db.call(
            "abandon_native_pyramid_member",
            c.PyramidMemberAbandon(operation_id=unknown_operation, state="unknown"),
        )
        purpose = "image_pyramid_" + str(allocation_c.output_id).replace("-", "")
        async with test_engine.connect() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT state FROM aap_assets_v1.image_pyramid_native_members "
                        "WHERE operation_id=CAST(:operation AS uuid)"
                    ),
                    {"operation": unknown_operation},
                )
            ) == "unknown"
            assert await owner.scalar(
                text("SELECT count(*) FROM public.storage_operations WHERE purpose=:p"),
                {"p": purpose},
            ) == 1
            assert not await owner.scalar(
                text(
                    "SELECT count(*) FROM aap_assets_v1.image_pyramid_native_members "
                    "WHERE output_id=CAST(:output AS uuid) "
                    "AND state IN ('verified', 'not_written')"
                ),
                {"output": allocation_c.output_id},
            )
        # The same closure is idempotent and never deletes the unknown permit.
        await worker_db.call(
            "abandon_native_pyramid_member",
            c.PyramidMemberAbandon(operation_id=unknown_operation, state="unknown"),
        )
        async with test_engine.connect() as owner:
            assert await owner.scalar(
                text("SELECT count(*) FROM public.storage_operations WHERE purpose=:p"),
                {"p": purpose},
            ) == 1
        # A drained no-write reservation is the bounded outcome that settles it.
        drained_operation = uuid4()
        await worker_db.call(
            "begin_native_pyramid_member",
            c.PyramidMemberReservation(
                operation_id=drained_operation,
                output_id=allocation_c.output_id,
                relative_key="overview.webp",
                sha256=sha256(b"drained-probe").hexdigest(),
                size_bytes=13,
            ),
        )
        await worker_db.call(
            "submit_native_pyramid_member",
            c.PyramidMemberOperation(operation_id=drained_operation),
        )
        await worker_db.call(
            "abandon_native_pyramid_member",
            c.PyramidMemberAbandon(
                operation_id=drained_operation, state="not_written"
            ),
        )
        async with test_engine.connect() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT state FROM aap_assets_v1.image_pyramid_native_members "
                        "WHERE operation_id=CAST(:operation AS uuid)"
                    ),
                    {"operation": drained_operation},
                )
            ) == "not_written"
        async with sessions.begin() as db:
            retirements = (
                await db.execute(
                    text(
                        "SELECT output_id, closed_at FROM "
                        "aap_assets_v1.native_pyramid_retirements "
                        "WHERE output_id = CAST(:output AS uuid)"
                    ),
                    {"output": allocation_a.output_id},
                )
            ).all()
        assert retirements and retirements[0].closed_at is None
        # Release C's job lease, not its unknown physical-operation barrier.
        failed_c = await worker_db.call(
            "fail_image_media",
            c.FailImageMediaRequest(
                job_id=claim_c.job_id,
                generation=claim_c.generation,
                lease_token=claim_c.lease_token,
                error_code="timeout",
                error_detail="unknown member outcome retained by lifecycle proof",
            ),
        )
        assert failed_c.outcome == "failed"

        held_operation, held_token = uuid4(), uuid4()
        held = await control_db.call(
            "begin_native_pyramid_read",
            c.BeginNativePyramidReadRequest(
                actor_id=actor_id,
                dataset_item_id=item_id,
                generation=claim.pyramid_generation,
                profile_version=claim.spec.policy.profile_version,
                operation_id=held_operation,
                token=held_token,
            ),
        )
        assert held.output_id == allocation_a.output_id

        async def a_claimable():
            async with test_engine.connect() as owner:
                return await owner.scalar(
                    text(
                        "SELECT aap_assets_v1._native_pyramid_retirement_claimable("
                        "CAST(:output AS uuid))"
                    ),
                    {"output": allocation_a.output_id},
                )

        assert await a_claimable() is False
        held_closure = await control_db.call(
            "complete_native_pyramid_read",
            c.CompleteNativePyramidReadRequest(
                operation_id=held_operation, token=held_token
            ),
        )
        assert held_closure.closed is True
        await _wait_until(
            a_claimable,
            timeout=120,
            message="saved A output did not become claimable after its reader closed",
        )

        # Cleanup: real process, restricted SQL role, cleanup token only.
        foreign_uploads = []
        for index in range(120):
            upload = owner_s3.create_multipart_upload(
                Bucket=cache_bucket, Key=f"000-native-foreign-upload/{index:04d}"
            )
            uploads.append((upload["Bucket"], upload["Key"], upload["UploadId"]))
            foreign_uploads.append(upload["UploadId"])
        owned_upload = owner_s3.create_multipart_upload(
            Bucket=cache_bucket, Key=target_a.prefix + "owned-orphan-upload"
        )
        uploads.append((owned_upload["Bucket"], owned_upload["Key"], owned_upload["UploadId"]))
        cleanup_health = _free_loopback_port()
        cleanup_environment = {
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT / "apps/assets-cleanup/src"),
                    str(ROOT / "apps/assets-media/src"),
                )
            ),
            "TMPDIR": str(tmp_path),
            "ASSET_CLEANUP_DATABASE_URL": cleanup_url.set(
                drivername="postgresql"
            ).render_as_string(hide_password=False),
            "ASSET_CLEANUP_S3_ENDPOINT": endpoint,
            "ASSET_CLEANUP_S3_REGION": "us-east-1",
            "ASSET_CLEANUP_BUCKET": cache_bucket,
            "ASSET_CLEANUP_INPUTS_BUCKET": _required_env("MINIO_INPUTS_BUCKET"),
            "ASSET_CLEANUP_S3_ACCESS_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY"
            ),
            "ASSET_CLEANUP_S3_SECRET_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY"
            ),
            "ASSET_CLEANUP_NATIVE_STORAGE_URL": native_signer.url,
            "ASSET_CLEANUP_NATIVE_STORAGE_TOKEN": native_signer.cleanup_token,
            "ASSET_CLEANUP_POLL_SECONDS": "0.2",
            "ASSET_CLEANUP_PAGE_SIZE": "100",
            "ASSET_CLEANUP_HEALTH_PORT": str(cleanup_health),
            "ASSET_CLEANUP_HEALTH_HOST": "127.0.0.1",
        }
        cleanup_log = tmp_path / "native-cleanup.log"
        cleanup_process, cleanup_stream = _start_process(
            [sys.executable, "-m", "yoao_assets_cleanup"],
            cleanup_environment,
            cleanup_log,
        )

        async def members_gone():
            for key in _layout_keys(target_a.prefix):
                try:
                    owner_s3.head_object(Bucket=cache_bucket, Key=key)
                except ClientError as exc:
                    if (
                        exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                        and exc.response["Error"]["Code"] in {
                            "404", "NoSuchKey", "NoSuchVersion"
                        }
                    ):
                        continue
                    raise
                return False
            async with test_engine.connect() as owner:
                return await owner.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM "
                        "aap_assets_v1.native_pyramid_retirement_pages "
                        "WHERE output_id=CAST(:output AS uuid) "
                        "AND state='members_absent')"
                    ),
                    {"output": allocation_a.output_id},
                )

        async def retirement_open():
            async with test_engine.connect() as owner:
                return await owner.scalar(
                    text(
                        "SELECT closed_at IS NULL FROM "
                        "aap_assets_v1.native_pyramid_retirements "
                        "WHERE output_id=CAST(:output AS uuid)"
                    ),
                    {"output": allocation_a.output_id},
                )

        await _wait_until(
            members_gone,
            timeout=120,
            message="cleanup did not delete the saved A members",
        )
        async def owned_upload_refused():
            return (
                f"prefix check failed output_id={allocation_a.output_id} "
                "code=native_prefix_not_empty"
            ) in cleanup_log.read_text(encoding="utf-8")

        await _wait_until(
            owned_upload_refused,
            timeout=120,
            message="cleanup did not refuse the owned upload beyond the first page",
        )
        assert await retirement_open()
        remaining_uploads = {
            item.get("UploadId")
            for page in owner_s3.get_paginator("list_multipart_uploads").paginate(
                Bucket=cache_bucket
            )
            for item in page.get("Uploads", []) or []
        }
        assert set(foreign_uploads) <= remaining_uploads
        assert owned_upload["UploadId"] in remaining_uploads
        owner_s3.abort_multipart_upload(
            Bucket=owned_upload["Bucket"],
            Key=owned_upload["Key"],
            UploadId=owned_upload["UploadId"],
        )
        uploads.remove(
            (owned_upload["Bucket"], owned_upload["Key"], owned_upload["UploadId"])
        )

        async def retirement_closed():
            async with test_engine.connect() as owner:
                return await owner.scalar(
                    text(
                        "SELECT closed_at IS NOT NULL FROM "
                        "aap_assets_v1.native_pyramid_retirements "
                        "WHERE output_id=CAST(:output AS uuid)"
                    ),
                    {"output": allocation_a.output_id},
                )

        # A refused prefix settles its I/O page, not its claim lease. Keep the
        # production lease and wait for its recorded expiry before recovery.
        async with test_engine.connect() as owner:
            remaining_lease = await owner.scalar(
                text(
                    "SELECT GREATEST(0, EXTRACT(EPOCH FROM "
                    "(lease_expires_at-clock_timestamp()))) "
                    "FROM aap_assets_v1.native_pyramid_retirements "
                    "WHERE output_id=CAST(:output AS uuid)"
                ),
                {"output": allocation_a.output_id},
            )
        assert remaining_lease is not None
        await _wait_until(
            retirement_closed,
            timeout=float(remaining_lease) + 120,
            message="cleanup did not settle the owned-upload barrier after lease expiry",
        )
        async with test_engine.connect() as owner:
            page_outcomes = (
                await owner.execute(
                    text(
                        "SELECT state FROM aap_assets_v1."
                        "native_pyramid_retirement_pages "
                        "WHERE output_id=CAST(:output AS uuid) ORDER BY prepared_at, page_id"
                    ),
                    {"output": allocation_a.output_id},
                )
            ).scalars().all()
        assert page_outcomes and page_outcomes[-1] == "prefix_absent"
        # B (still the active generation) and every foreign upload remain.
        for key in _layout_keys(target_b.prefix):
            owner_s3.head_object(Bucket=target_b.bucket, Key=key)
        remaining_uploads = {
            item.get("UploadId")
            for page in owner_s3.get_paginator("list_multipart_uploads").paginate(
                Bucket=cache_bucket
            )
            for item in page.get("Uploads", []) or []
        }
        assert set(foreign_uploads) <= remaining_uploads

        # Publishing another B output makes the previous B output retireable
        # without changing or restarting either physical executor.
        accepted_d = await _prepare(
            control_client, control_token, actor_id, item_id, force=True
        )
        job_d = UUID(accepted_d["work"]["job_id"])
        claimed_d = await worker_db.call(
            "claim_image_media",
            c.ClaimImageMediaRequest(
                worker_id=uuid4(),
                job_id=job_d,
                lease_seconds=300,
                kinds=["pyramid"],
            ),
        )
        assert claimed_d.claim is not None and claimed_d.claim.job_id == job_d
        allocation_d, _, _ = await executor.produce(claimed_d.claim)
        target_d = allocation_d.storage_ref
        saved_keys.extend(
            (target_d.bucket, key) for key in _layout_keys(target_d.prefix)
        )
        assert target_d.bucket == target_b.bucket
        assert target_d.location_id == target_b.location_id
        assert target_d.prefix != target_b.prefix
        assert control_process.poll() is None
        assert cleanup_process.poll() is None

        async def b_retirement_closed():
            async with test_engine.connect() as owner:
                return await owner.scalar(
                    text(
                        "SELECT closed_at IS NOT NULL FROM "
                        "aap_assets_v1.native_pyramid_retirements "
                        "WHERE output_id=CAST(:output AS uuid)"
                    ),
                    {"output": allocation_b.output_id},
                )

        await _wait_until(
            b_retirement_closed,
            timeout=120,
            message="cleanup did not retire the saved registered B output",
        )
        for target in (target_a, target_b):
            for page in owner_s3.get_paginator("list_object_versions").paginate(
                Bucket=target.bucket, Prefix=target.prefix
            ):
                assert not page.get("Versions")
                assert not page.get("DeleteMarkers")
        for relative, (receipt, _) in executor.receipts.items():
            head = owner_s3.head_object(
                Bucket=target_d.bucket, Key=target_d.prefix + relative
            )
            assert head["ETag"] == receipt.etag
            assert head.get("VersionId") == receipt.version_id
        async with test_engine.connect() as owner:
            assert await owner.scalar(
                text(
                    "SELECT count(*) FROM public.storage_operations AS operation "
                    "JOIN aap_assets_v1.image_pyramid_native_members AS member "
                    "ON operation.id=member.operation_permit "
                    "WHERE member.operation_id=CAST(:operation AS uuid)"
                ),
                {"operation": unknown_operation},
            ) == 1
            assert await owner.scalar(
                text(
                    "SELECT aap_assets_v1._native_pyramid_retirement_claimable("
                    "CAST(:output AS uuid))"
                ),
                {"output": allocation_c.output_id},
            ) is False
            assert await owner.scalar(
                text(
                    "SELECT state FROM aap_assets_v1.image_pyramid_native_members "
                    "WHERE operation_id=CAST(:operation AS uuid)"
                ),
                {"operation": unknown_operation},
            ) == "unknown"
        assert control_process.poll() is None
        assert cleanup_process.poll() is None
        proof_succeeded = True
    finally:
        original_failure = sys.exception()
        close_errors = []
        for name, closer in (
            ("storage", "close"),
            ("writer_client", "aclose"),
            ("worker_db", "close"),
            ("control_db", "close"),
            ("control_client", "aclose"),
        ):
            resource = locals().get(name)
            if resource is None:
                continue
            try:
                await getattr(resource, closer)()
            except Exception as exc:
                close_errors.append(exc)
        for process, stream in (
            (cleanup_process, cleanup_stream),
            (control_process, control_stream),
        ):
            try:
                if process is not None:
                    _stop_process(process)
            except Exception as exc:
                close_errors.append(exc)
            finally:
                if stream is not None:
                    stream.close()
        if close_errors:
            if original_failure is not None:
                close_errors.insert(0, original_failure)
            raise BaseExceptionGroup("Native proof closure failed", close_errors)
        # A failed proof leaves its database and objects for inspection. The
        # outer verifier's retention policy owns disposal, not this test.
        if proof_succeeded:
            for bucket, key, upload_id in uploads:
                owner_s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            for bucket, key in created_objects + saved_keys:
                _delete_all_versions(owner_s3, bucket, key)
            async with sessions() as db:
                # This injected unknown operation never issued physical I/O.
                # Remove only that test permit after proving it blocked GC;
                # its unknown member receipt is not rewritten as successful.
                removed = await db.execute(
                    text(
                        "DELETE FROM public.storage_operations AS operation "
                        "USING aap_assets_v1.image_pyramid_native_members AS member "
                        "WHERE operation.id=member.operation_permit "
                        "AND member.operation_id=CAST(:operation AS uuid) "
                        "AND member.output_id=CAST(:output AS uuid) "
                        "AND member.state='unknown' AND operation.purpose=:purpose "
                        "AND operation.location_id=CAST(:location AS uuid)"
                    ),
                    {
                        "operation": unknown_operation,
                        "output": allocation_c.output_id,
                        "purpose": purpose,
                        "location": allocation_c.storage_ref.location_id,
                    },
                )
                assert removed.rowcount == 1
                await db.commit()
                actor = await db.get(User, actor_id)
                current_generation = await db.scalar(
                    text(
                        "SELECT generation FROM public.storage_bindings "
                        "WHERE purpose='media-cache'"
                    )
                )
                previous_revision = await db.scalar(
                    text(
                        "SELECT config_revision FROM public.storage_locations "
                        "WHERE id=CAST(:location AS uuid)"
                    ),
                    {"location": previous_binding[0]},
                )
                change = MediaCacheBindingChange(
                    expected_generation=current_generation,
                    target_location_id=previous_binding[0],
                    target_config_revision=previous_revision,
                )
                preview = await media_cache_binding_change.preview(db, change, actor)
                restored = await media_cache_binding_change.apply(
                    db,
                    MediaCacheBindingApply(
                        **change.model_dump(), impact_token=preview["impact_token"]
                    ),
                    actor,
                )
                assert restored["location_id"] == previous_binding[0]
                assert restored["bucket"] == previous_binding[1]
                assert restored["generation"] > current_generation
                await db.commit()


def _layout_keys(prefix: str) -> list[str]:
    """The saved keys this proof owns for one output prefix."""
    levels = pyramid_levels(WIDTH, HEIGHT)
    keys = [prefix + "manifest.json", prefix + "overview.webp"]
    for level in levels:
        for x in range(level["columns"]):
            for y in range(level["rows"]):
                keys.append(prefix + f"tiles/{level['level']}/{x}/{y}.webp")
    return keys


def _delete_all_versions(client, bucket: str, key: str) -> None:
    for page in client.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=key
    ):
        for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if item.get("Key") == key:
                client.delete_object(
                    Bucket=bucket, Key=key, VersionId=item["VersionId"]
                )
