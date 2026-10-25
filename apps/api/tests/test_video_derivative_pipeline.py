"""Publication races, precise packet bounds and a real owned video pipeline."""

from copy import deepcopy
import hashlib
import io
import os
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
from urllib.parse import urlsplit

from PIL import Image
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem, VideoFrameIndex
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.db.models.user import User
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import storage_access
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services import video_derivatives as core
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.storage import storage_service
from app.workers import media_derivatives as worker
from tests.test_video_derivative_api import _inventory, _publish, world as world


def packet():
    return {
        "codec_string": "avc1.42e01e",
        "width": 64,
        "height": 48,
        "description": "AQID",
        "samples": [
            {
                "frame_index": 10,
                "pts_ms": -5,
                "duration_ms": 33,
                "is_keyframe": True,
                "size_bytes": 4,
                "offset_in_chunk": 2,
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_index", 9),
        ("frame_index", 11),
        ("frame_index", True),
        ("offset_in_chunk", -1),
        ("offset_in_chunk", 7),
        ("offset_in_chunk", True),
        ("size_bytes", 0),
        ("size_bytes", 100),
        ("is_keyframe", 1),
        ("duration_ms", -1),
        ("pts_ms", 2**31),
    ],
)
def test_packet_receipt_rejects_wrong_generation_range_or_byte_bounds(field, value):
    diagnostics = packet()
    diagnostics["samples"][0][field] = value
    with pytest.raises(ContentError, match="video_derivative_result_invalid"):
        core.validate_samples(diagnostics, 10, {"start_frame": 10, "end_frame": 10})


@pytest.mark.parametrize(
    "field,value",
    [
        ("codec_string", ""),
        ("width", 0),
        ("height", True),
        ("description", "not base64!"),
        ("samples", []),
    ],
)
def test_packet_configuration_must_be_complete_and_bounded(field, value):
    diagnostics = packet()
    diagnostics[field] = value
    with pytest.raises(ContentError):
        core.validate_samples(diagnostics, 10, {"start_frame": 10, "end_frame": 10})


def test_packet_order_preserves_decode_order_and_requires_complete_presentation_set():
    diagnostics = packet()
    second = {**diagnostics["samples"][0], "frame_index": 11}
    diagnostics["samples"] = [second, diagnostics["samples"][0]]
    core.validate_samples(diagnostics, 10, {"start_frame": 10, "end_frame": 11})
    diagnostics["samples"].pop()
    with pytest.raises(ContentError):
        core.validate_samples(diagnostics, 10, {"start_frame": 10, "end_frame": 11})


@pytest.mark.parametrize("code", ["storage_original_busy", "storage_maintenance_busy"])
async def test_claim_busy_retries_without_retiring_untouched_owner(monkeypatch, code):
    expected = object()
    claim = AsyncMock(side_effect=[ContentError(code), expected])
    monkeypatch.setattr(worker, "_claim", claim)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    assert await worker.claim_with_retry(object(), uuid.uuid4()) is expected
    assert claim.await_count == 2


async def test_persistent_short_lock_keeps_unclaimed_attempt_for_redelivery(
    monkeypatch,
):
    claim = AsyncMock(side_effect=ContentError("storage_original_busy"))
    monkeypatch.setattr(worker, "_claim", claim)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    assert await worker.claim_with_retry(object(), uuid.uuid4()) is None
    assert claim.await_count == 8


async def test_authority_refusal_is_never_transient(monkeypatch):
    claim = AsyncMock(side_effect=ContentError("access_revoked"))
    monkeypatch.setattr(worker, "_claim", claim)
    with pytest.raises(ContentError, match="access_revoked"):
        await worker.claim_with_retry(object(), uuid.uuid4())
    assert claim.await_count == 1


async def test_newer_metadata_attempt_prevents_old_publication(world, db_session):
    old, _ = await core.prepare(db_session, world.item.id, world.authority, "metadata")
    token = await objects.claim(db_session, old)
    replacement, _ = await core.prepare(
        db_session, world.item.id, world.authority, "metadata", force=True
    )
    await db_session.commit()
    with pytest.raises(ContentError, match="video_derivative_changed"):
        await core.check(db_session, old, world.authority, token=token)
    assert replacement.id != old.id
    assert world.item.video_output_id is None
    assert world.item.video_building_output_id == replacement.id
    assert old.state == "building"


async def test_derived_dimensions_do_not_revoke_original_but_path_change_does(
    world, db_session
):
    generation = await _publish(db_session, world)
    await core.check(db_session, generation, world.authority)
    world.item.file_path += ".replacement"
    await db_session.flush()
    with pytest.raises(ContentError):
        await core.check(db_session, generation, world.authority)


async def test_new_metadata_generation_invalidates_old_frame_and_timetable(
    world, db_session
):
    generation = await _publish(db_session, world)
    frame = await _publish(
        db_session, world, "frame", core.frame_spec(generation, 3, 512, "webp")
    )
    newer, _ = await core.prepare(
        db_session, world.item.id, world.authority, "metadata", force=True
    )
    token = await objects.claim(db_session, newer)
    result = deepcopy(generation.result)
    result["frames"][1]["pts_ms"] = 55
    await core.publish(
        db_session,
        newer,
        world.authority,
        token,
        result,
        _inventory(newer, result),
        await storage_access.check(db_session, LEGACY_LOCATION_ID),
    )
    await db_session.commit()
    with pytest.raises(ContentError, match="video_metadata_changed"):
        await core.check(db_session, frame, world.authority)
    assert (
        await db_session.scalar(
            select(VideoFrameIndex.pts_ms).where(
                VideoFrameIndex.dataset_item_id == world.item.id,
                VideoFrameIndex.frame_index == 3,
            )
        )
        == 55
    )
    assert frame.result["metadata_output_id"] == str(generation.id)


@pytest.fixture
async def real_video(test_engine, tmp_path, monkeypatch):
    """Committed rows and real local MinIO; every physical object is removed."""
    from app.config import settings

    if os.environ.get("AAP_WORKTREE_MODE") != "test":
        pytest.skip(
            "Real provider integration requires a verified disposable worktree test mode"
        )
    assert settings.database_url.rsplit("/", 1)[-1].endswith("_test")
    prefix = re.match(r"aap-wt-[0-9a-f]{16}-test-", storage_service.datasets_bucket)
    assert prefix and storage_service.media_cache_bucket.startswith(prefix.group())
    monkeypatch.setattr(settings, "video_chunk_size_frames", 4)
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=8:duration=1",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "4",
            "-bf",
            "0",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    raw = path.read_bytes()
    key = f"video-pipeline-tests/{uuid.uuid4()}/source.mp4"
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=raw
    )
    identifiers = []
    user_id = dataset_id = item_id = project_id = task_id = None
    try:
        async with factory() as db:
            user = User(
                email=f"video-pipeline-{uuid.uuid4()}@test.local",
                name="Video pipeline",
                password_hash="not-an-authentication-fixture",
                role="super_admin",
            )
            db.add(user)
            await db.flush()
            user_id = user.id
            dataset = Dataset(
                display_id="D-VPIPE-" + uuid.uuid4().hex[:10],
                name="Video pipeline",
                data_type="video",
                created_by=user.id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="source.mp4",
                file_path=key,
                file_type="video",
                file_size=len(raw),
            )
            db.add(item)
            await db.flush()
            item_id = item.id
            await register_item_asset(
                db, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
            )
            project = Project(
                display_id="P-VPIPE-" + uuid.uuid4().hex[:10],
                name="Frame job",
                type_key="video-track",
                type_label="Video",
                data_type="video",
                owner_id=user_id,
            )
            db.add(project)
            await db.flush()
            project_id = project.id
            task = Task(
                display_id="T-VPIPE-" + uuid.uuid4().hex[:10],
                project_id=project.id,
                dataset_item_id=item.id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type="video",
                status="pending",
            )
            db.add(task)
            await db.flush()
            task_id = task.id
            await db.commit()
            await storage_access.ensure(db, LEGACY_LOCATION_ID)
            source = await originals.pin(db, "dataset_item", item.id)
            authority = await originals.delegated(
                db, originals.system_authorization("dataset_item", item.id, source)
            )
        yield SimpleNamespace(
            factory=factory,
            item_id=item_id,
            authority=authority,
            identifiers=identifiers,
            task_id=task_id,
            user_id=user_id,
        )
    finally:
        async with factory() as db:
            outputs = list(
                await db.scalars(
                    select(Output).where(Output.dataset_item_id == item_id)
                )
            )
            for output in outputs:
                with physical.open_storage() as storage:
                    physical.delete_output(storage, objects.snapshot(output))
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            ids = [output.id for output in outputs]
            if ids:
                assert not list(
                    await db.scalars(
                        select(StorageOperation.id).where(
                            StorageOperation.purpose.in_(
                                [objects.purpose(identifier) for identifier in ids]
                            )
                        )
                    )
                )
                await db.execute(
                    update(Output)
                    .where(Output.id.in_(ids), Output.state.in_(["building", "ready"]))
                    .values(state="retiring")
                )
                await db.execute(
                    update(Output)
                    .where(Output.id.in_(ids), Output.state == "retiring")
                    .values(state="deleted")
                )
                await db.execute(delete(Output).where(Output.id.in_(ids)))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        storage_service.client.delete_object(
            Bucket=storage_service.datasets_bucket, Key=key
        )


async def test_real_registered_original_native_generation_and_complete_readback(
    real_video,
):
    context = real_video
    async with context.factory() as db:
        output, _ = await core.prepare(
            db, context.item_id, context.authority, "metadata"
        )
        metadata_id = output.id
        await db.commit()
    await worker.run_outputs([str(metadata_id)])
    async with context.factory() as db:
        generation = await db.get(Output, metadata_id)
        assert generation.state == "ready", generation.error_code
        info, frames = core.metadata_result(generation)
        assert (info.width, info.height, info.frame_count, len(frames)) == (
            64,
            48,
            8,
            8,
        )
        prepared = []
        for kind, spec in [
            ("frame", core.frame_spec(generation, 3, 48, "webp")),
            ("chunk", core.chunk_spec(generation, 0)),
            ("chunk", core.chunk_spec(generation, 1)),
        ]:
            candidate, _ = await core.prepare(
                db, context.item_id, context.authority, kind, spec=spec
            )
            prepared.append(candidate.id)
        await db.commit()
    await worker.run_outputs([str(identifier) for identifier in prepared])
    async with context.factory() as db:
        outputs = [
            objects.snapshot(await db.get(Output, identifier))
            for identifier in [metadata_id, *prepared]
        ]
    for output in outputs:
        assert output.state == "ready", output.error_code
        core.validate_result(output, output.result, output.inventory)
        async with context.factory() as db:
            facts = list(
                await db.scalars(
                    select(Mutation).where(
                        Mutation.owner_id == output.id,
                        Mutation.producer == "video_derivative_outputs",
                    )
                )
            )
        assert {fact.object_key for fact in facts} == set(output.inventory["objects"])
        assert all(fact.physical_outcome == "written" for fact in facts)
        assert sum(fact.physical_size_bytes for fact in facts) == sum(
            value["size_bytes"] for value in output.inventory["objects"].values()
        )
        with physical.open_storage() as storage:
            physical.verify_inventory(storage, output, output.inventory)
            if output.kind == "frame":
                raw = physical.read_bytes(
                    storage, output, physical.resolve(output).key("frame.webp")
                )
                with Image.open(io.BytesIO(raw)) as image:
                    assert image.size == (48, 36)
            elif output.kind == "chunk":
                assert len(output.result["diagnostics"]["samples"]) == 4
    async with context.factory() as db:
        assert not list(await db.scalars(select(StorageOperation.id)))


async def test_real_frame_preannotation_uses_explicit_task_original_and_verified_outputs(
    real_video,
):
    from app.workers import frame_preannotate
    from app.services.video_frame_service import build_context_from_task

    context = real_video
    async with context.factory() as db:
        task = await db.get(Task, context.task_id)
        ctx = await build_context_from_task(db, task)
        urls = await frame_preannotate._extract_frames_for_video(db, ctx, [1, 3])
        assert set(urls) == {1, 3}
        assert all(
            urlsplit(url).path.startswith("/api/v1/media-cache/video-derivative/")
            for url in urls.values()
        )
        again = await frame_preannotate._read_frame_urls(db, ctx, [1, 3, 5])
        assert {index: urlsplit(url).path for index, url in again.items()} == {
            index: urlsplit(url).path for index, url in urls.items()
        }
    async with context.factory() as db:
        outputs = list(
            await db.scalars(
                select(Output).where(Output.dataset_item_id == context.item_id)
            )
        )
        assert len(outputs) == 3 and all(output.state == "ready" for output in outputs)
        assert all(
            output.auth_ref["owner_id"] == str(context.task_id) for output in outputs
        )
        assert not list(await db.scalars(select(StorageOperation.id)))
        from app.api.v1 import _test_seed as seed
        from app.api.v1._test_seed_attachments import FixtureScope

        task = await db.get(Task, context.task_id)
        item = await db.get(DatasetItem, context.item_id)
        scope = FixtureScope(
            project_ids=(task.project_id,),
            task_ids=(task.id,),
            dataset_ids=(item.dataset_id,),
        )

        async def resolve(_):
            return scope

        plan = await seed._prepare_fixture_video_cleanup(db, scope)
        await seed._delete_fixture_video_objects(db, plan, resolve)
        await seed._finalize_fixture_video_cleanup(db, plan, resolve)
        await db.commit()
        assert not list(
            await db.scalars(
                select(Output.id).where(Output.dataset_item_id == context.item_id)
            )
        )
