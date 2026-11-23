"""Exercise the legacy media capabilities through actual execute-only sessions."""

import os
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.video_chapters import VideoChapterWorkflow
from app.db.models.dataset import Dataset, DatasetItem, VideoFrameIndex
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
    ImagePyramidOutput,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.task import VideoMetadata
from app.schemas.video_chapter import VideoChapterCreate, VideoChapterUpdate
from app.services import image_pyramid_data, image_pyramid_objects, task_access
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.managed_original_retirement import managed_storage_identity
from app.services.video_chapter_service import list_chapters
from app.services.video_frame_service import pts_ms_for_frame
from tests.test_video_chapters_api import _video_item_with_task
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and actual ordinary/Assets worker logins",
    ),
]

_PRIVATE = (
    "image_pyramid_assets",
    "image_pyramid_generations",
    "image_pyramid_outputs",
    "video_chunks",
    "video_frame_cache",
    "video_frame_indices",
    "video_chapters",
    "video_derivative_outputs",
    "thumbnail_outputs",
    "asset_image_jobs",
    "asset_image_operations",
    "asset_image_garbage",
    "asset_media_jobs",
    "asset_media_operations",
    "asset_media_garbage",
    "asset_media_timetable_staging",
    "asset_accepted_image_inputs",
    "asset_accepted_image_input_attempts",
    "asset_accepted_image_input_manifests",
    "asset_accepted_image_input_manifest_members",
    "asset_accepted_input_garbage",
    "asset_accepted_video_inputs",
    "asset_accepted_video_input_attempts",
    "asset_accepted_video_manifests",
    "asset_accepted_video_members",
    "asset_accepted_video_frames",
    "asset_accepted_video_frame_attempts",
    "asset_accepted_video_times",
)


@pytest.mark.parametrize(
    ("selector", "role"),
    [
        (
            "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
            "identity_embedded_api_consumer",
        ),
        ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"),
        ("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime"),
    ],
    ids=["lan-embedded", "lan-isolated-issuer", "legacy-runtime"],
)
async def test_media_ledgers_require_fixed_transitional_capabilities(
    test_engine, selector, role
):
    endpoint = _api_url(test_engine)
    runtime = make_url(os.environ[selector])
    assert (runtime.host, runtime.port, runtime.database, runtime.username) == (
        endpoint.host,
        endpoint.port,
        endpoint.database,
        role,
    )
    engine = create_async_engine(runtime, hide_parameters=True)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    business = async_sessionmaker(engine, expire_on_commit=False)
    actor_id, outsider_id, asset_id, output_id = (uuid.uuid4() for _ in range(4))
    profile = "legacy-cutover-" + output_id.hex
    old_endpoint, old_bucket = "http://historical-cache.invalid", "historical-cache"
    prefix = f"retained/_aap_managed/media-cache/image-pyramids/objects/{output_id}/"
    saved = {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid.uuid4()),
        "config_revision": 3,
        "binding_generation": 42,
        "endpoint_url": old_endpoint,
        "bucket": old_bucket,
        "prefix": prefix,
        "storage_identity": managed_storage_identity(old_endpoint, old_bucket),
        "key_prefix": "retained/_aap_managed/media-cache/",
    }
    inventory = {
        "schema": 1,
        "objects": {
            prefix + "overview.webp": {"sha256": "1" * 64, "size_bytes": 27},
            prefix + "manifest.json": {"sha256": "2" * 64, "size_bytes": 64},
        },
    }
    item = task = image = None
    try:
        async with owner.begin() as db:
            db.add_all(
                [
                    User(
                        id=actor_id,
                        email=f"chapter-{actor_id.hex}@test.invalid",
                        name="Chapter manager",
                        role="super_admin",
                        password_hash="not-a-credential",
                    ),
                    User(
                        id=outsider_id,
                        email=f"chapter-{outsider_id.hex}@test.invalid",
                        name="Unrelated viewer",
                        role="employee",
                        password_hash="not-a-credential",
                    ),
                ]
            )
            await db.flush()
            item = await _video_item_with_task(db, actor_id)
            task = await db.scalar(select(Task).where(Task.dataset_item_id == item.id))
            image = DatasetItem(
                dataset_id=item.dataset_id,
                file_name="historical.webp",
                file_path="historical.webp",
                file_type="image",
                metadata_={},
            )
            db.add(image)
            db.add(VideoFrameIndex(dataset_item_id=item.id, frame_index=7, pts_ms=284))
            await db.flush()
            db.add(
                ImagePyramidAsset(
                    id=asset_id,
                    dataset_item_id=image.id,
                    profile_version=profile,
                    active_generation=1,
                )
            )
            out = ImagePyramidOutput(
                id=output_id,
                lease_token=uuid.uuid4(),
                asset_id=asset_id,
                generation=1,
                storage_ref=saved,
                state="building",
            )
            db.add(out)
            await db.flush()
            out.state, out.inventory = "ready", inventory
            db.add(
                ImagePyramidGeneration(
                    asset_id=asset_id,
                    generation=1,
                    output_id=output_id,
                    status="ready",
                    source_identity="legacy",
                    normalization_version="legacy",
                )
            )
        async with business() as db:
            assert (
                await db.execute(text("SELECT session_user, current_user"))
            ).one() == (role, role)
            for table in _PRIVATE:
                with pytest.raises(DBAPIError) as refusal:
                    async with db.begin_nested():
                        await db.execute(text(f"SELECT 1 FROM public.{table} LIMIT 1"))
                assert refusal.value.orig.sqlstate == "42501", table
            for capability in (
                "yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[])",
                "yoao_assets_v1.claim_video_media(uuid,uuid,integer,text[])",
                "yoao_assets_v1._legacy_media_cache_target_current(jsonb)",
                "yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text)",
            ):
                assert not await db.scalar(
                    text("SELECT has_function_privilege(:capability,'EXECUTE')"),
                    {"capability": capability},
                )
            assert (
                await image_pyramid_data.read_asset(
                    db, "dataset_item", image.id, profile
                )
            ).id == asset_id
            assert (
                await image_pyramid_data.read_asset(
                    db, "dataset_item", item.id, profile
                )
                is None
            )
            [(asset, generation)] = await image_pyramid_data.summaries(
                db, [item.id, image.id], [], profile
            )
            assert asset.active_generation == generation.generation == 1
            assert generation.output_id == output_id
            historical = await image_pyramid_objects.get_output(
                db, output_id, lock=True
            )
            assert historical.storage_ref == saved and historical.inventory == inventory
            metadata = VideoMetadata.model_validate(item.metadata_["video"])
            assert await pts_ms_for_frame(db, item.id, 7, metadata) == 284
            assert await pts_ms_for_frame(db, image.id, 7, metadata) == 280
            target = await get_core_row(db, task_row, task.id)
            access = await task_access.resolve_task_access(db, target, actor_id)
            workflow = VideoChapterWorkflow(db)
            chapter = await workflow.create(
                dataset_item_id=item.id,
                task_id=task.id,
                actor_id=actor_id,
                expected_access=access,
                payload=VideoChapterCreate(
                    start_frame=3,
                    end_frame=15,
                    title="Caption",
                    frame_step=3,
                    source="sampled",
                ),
            )
            visible = await list_chapters(db, item.id, task.id, actor_id)
            assert [(row.title, row.frame_step, row.source) for row in visible] == [
                ("Caption", 3, "sampled")
            ]
            assert await list_chapters(db, item.id, task.id, outsider_id) == []
            changed, delta = await workflow.update(
                dataset_item_id=item.id,
                task_id=task.id,
                actor_id=actor_id,
                expected_access=access,
                chapter_id=chapter.id,
                payload=VideoChapterUpdate(end_frame=18),
            )
            assert changed.end_frame == 18 and delta == {"end_frame": 18}
            await db.rollback()
            assert await list_chapters(db, item.id, task.id, actor_id) == []
    finally:
        await engine.dispose()
        async with owner.begin() as db:
            await db.execute(
                delete(ImagePyramidAsset).where(ImagePyramidAsset.id == asset_id)
            )
            for state in ("retiring", "deleted"):
                await db.execute(
                    update(ImagePyramidOutput)
                    .where(ImagePyramidOutput.id == output_id)
                    .values(state=state)
                )
            await db.execute(
                delete(ImagePyramidOutput).where(ImagePyramidOutput.id == output_id)
            )
            if task is not None:
                await db.execute(delete(Task).where(Task.id == task.id))
                await db.execute(delete(Project).where(Project.id == task.project_id))
            if item is not None:
                await db.execute(delete(Dataset).where(Dataset.id == item.dataset_id))
            await db.execute(delete(User).where(User.id.in_((actor_id, outsider_id))))


@pytest.mark.parametrize(
    "selector",
    [
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_URL",
    ],
)
async def test_packaged_media_actors_do_not_inherit_legacy_business_commands(
    test_engine, selector
):
    endpoint = _api_url(test_engine)
    runtime = make_url(os.environ[selector])
    assert (runtime.host, runtime.port, runtime.database) == (
        endpoint.host,
        endpoint.port,
        endpoint.database,
    )
    engine = create_async_engine(runtime, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT session_user")) == runtime.username
            for capability in (
                "yoao_assets_v1.read_legacy_pyramid_generation(uuid,integer)",
                "yoao_assets_v1.lock_legacy_video_chapter(uuid,uuid,uuid,uuid)",
                "yoao_assets_v1.legacy_video_claim_attempt(uuid,uuid,integer)",
                "yoao_assets_v1.publish_legacy_thumbnail(text,uuid,uuid,jsonb,jsonb,text,timestamptz)",
            ):
                assert not await db.scalar(
                    text("SELECT has_function_privilege(:capability,'EXECUTE')"),
                    {"capability": capability},
                )
    finally:
        await engine.dispose()
