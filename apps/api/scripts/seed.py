"""
初始化种子数据：管理员、测试用户、示例项目。
用法：
    cd apps/api
    PYTHONPATH=. uv run python scripts/seed.py
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

sys.path.insert(0, str(__file__.rsplit("/", 1)[0]))
from seed_pointcloud import (  # noqa: E402
    cleanup_legacy_dev_seed_objects,
    seed_nuscenes_scene,
)
from seed_coco8 import seed_coco8  # noqa: E402  (依赖 sys.path 先扩)
from seed_video import seed_video  # noqa: E402  (视频 video-track 夹具)
from seed_assets import (  # noqa: E402  (版本化网络素材)
    SeedAssetError,
    ensure_profile,
    select_profile,
)

from app.config import settings
from app.services.identity_commands import IdentityCommandService

if settings.environment == "production":
    print("[seed] refusing to run with environment=production", file=sys.stderr)
    raise SystemExit(2)

engine = create_async_engine(settings.database_url, echo=False, hide_parameters=True)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

USERS = [
    {
        "email": "admin",
        "name": "超级管理员",
        "password": "123456",
        "role": "super_admin",
        "group_name": None,
    },
    {
        "email": "pm",
        "name": "张明轩",
        "password": "123456",
        "role": "project_admin",
        "group_name": "管理组",
    },
    {
        "email": "qa",
        "name": "李晓华",
        "password": "123456",
        "role": "employee",
        "group_name": "质检组",
    },
    {
        "email": "anno",
        "name": "王芳",
        "password": "123456",
        "role": "employee",
        "group_name": "标注组A",
    },
    {
        "email": "viewer",
        "name": "赵观察",
        "password": "123456",
        "role": "viewer",
        "group_name": None,
    },
    {
        "email": "anno2",
        "name": "刘洋",
        "password": "123456",
        "role": "employee",
        "group_name": "标注组A",
    },
    {
        "email": "anno3",
        "name": "陈思远",
        "password": "123456",
        "role": "employee",
        "group_name": "标注组B",
    },
]

DEMO_PROJECT_DISPLAY_IDS = (
    "P-COCO8",
    "P-VIDEO-DEV",
    "P-PC-DEV",
)
DEMO_PROJECT_MEMBERS = (
    ("anno", "annotator"),
    ("anno2", "annotator"),
    ("anno3", "annotator"),
    ("qa", "reviewer"),
    ("viewer", "viewer"),
)


async def _ensure_demo_memberships(
    db: AsyncSession,
    *,
    members: tuple[tuple[str, str], ...] = DEMO_PROJECT_MEMBERS,
    display_ids: tuple[str, ...] = DEMO_PROJECT_DISPLAY_IDS,
    assigner_emails: tuple[str, ...] = ("pm", "admin"),
) -> int:
    """Idempotently grant the demo personas their project duties.

    Platform identity never authorizes project access on its own; a member row
    with an explicit duty is required.  Managers (``project_admin``) and the
    super administrator act through ownership, so they are deliberately not
    given member rows.

    Persona references are resolved again after the fixture transactions.
    ``members`` and ``display_ids`` default to the seeded demo contract; tests
    pass their own values because persona emails are globally unique.
    """

    wanted_emails = {email for email, _ in members} | set(assigner_emails)
    identity = IdentityCommandService(db)
    by_email = await identity.lookup_seed_accounts(wanted_emails)
    assigner = next(
        (by_email.get(email) for email in assigner_emails if by_email.get(email)), None
    )
    if assigner is None:
        return 0
    from sqlalchemy.orm import aliased
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember
    from app.services.seed_membership_data import SeedMembershipWrites
    from app.services.workspace_read_relations import project_member_facts, project_rows

    project_row = aliased(Project, project_rows(), adapt_on_names=True)
    member_row = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
    projects = list(
        (
            await db.execute(
                select(project_row).where(project_row.display_id.in_(display_ids))
            )
        ).scalars()
    )
    writes = SeedMembershipWrites(db)
    changed = 0
    for project in projects:
        for email, role in members:
            user_id = by_email.get(email)
            if user_id is None:
                continue
            await writes.flush_before_read()
            member = await db.scalar(
                select(member_row).where(
                    member_row.project_id == project.id,
                    member_row.user_id == user_id,
                )
            )
            if member is None:
                writes.add(
                    project_id=project.id,
                    user_id=user_id,
                    role=role,
                    assigned_by=assigner,
                )
                changed += 1
            elif writes.role(member) != role:
                writes.change(member, role=role, assigned_by=assigner)
                changed += 1
    if changed:
        await writes.commit()
    return changed


async def _ensure_seed_accounts(db: AsyncSession) -> dict[str, uuid.UUID]:
    """Create missing personas while retaining existing identities and parent commit."""
    created_users: dict[str, uuid.UUID] = {}
    identity = IdentityCommandService(db)

    for data in USERS:
        user_id, created = await identity.ensure_seed_persona(**data)
        created_users[data["email"]] = user_id
        if not created:
            print(f"  skip  {data['email']} (已存在)")
            continue
        print(f"  add   {data['email']}  [{data['role']}]")

    await db.commit()
    return created_users


async def _enqueue_seed_video_backfill(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    actor_id: uuid.UUID,
    accepted_before: datetime,
    video_delegations: dict,
) -> uuid.UUID:
    """Persist the demo's video work and its dispatch before publishing it."""
    from app.db.models.async_job import AsyncJobKind
    from app.services import async_job
    from app.services.jobs_dispatch import (
        prepare_job_dispatch,
        publish_prepared_dispatch,
    )
    from app.services.jobs_queue import dispatch_job

    job = await async_job.create_job(
        db,
        kind=AsyncJobKind.MEDIA_BACKFILL.value,
        user_id=actor_id,
        project_id=None,
        payload={
            "dataset_id": str(dataset_id),
            "purpose": "thumbnail",
            "accepted_before": accepted_before.isoformat(),
            "operation_id": str(uuid.uuid4()),
            "media_backfill_kind": "aap-media-backfill/v1",
            "video_delegations": video_delegations,
        },
    )
    task_name = "app.workers.media.run_media_backfill"
    execution_id = str(job.id)
    arguments = [execution_id]
    options = {"queue": "media"}
    lease = await prepare_job_dispatch(
        db,
        job_id=job.id,
        task_name=task_name,
        execution_id=execution_id,
        args=arguments,
        kwargs={},
        options=options,
    )
    await db.commit()
    await publish_prepared_dispatch(
        db,
        lease,
        publish=lambda: dispatch_job(
            task_name=task_name,
            execution_id=execution_id,
            args=arguments,
            kwargs={},
            options=options,
        ),
    )
    return job.id


async def seed(
    *,
    profile: str = "demo",
    cache_dir: Path | None = None,
    asset_dir: Path | None = None,
    video: Path | None = None,
    offline: bool = False,
) -> None:
    if profile == "online-training":
        await _seed_online_training_profile(
            cache_dir=cache_dir, asset_dir=asset_dir, offline=offline
        )
        return
    if profile != "demo":
        raise ValueError(f"unknown seed profile: {profile}")

    async with Session() as db:
        created_users = await _ensure_seed_accounts(db)
        admin_id = created_users["admin"]
        owner_id = created_users["pm"]

        try:
            info = await seed_coco8(db, owner_id=owner_id)
            await db.commit()
            if info is None:
                print("  skip  image P-COCO8 (已存在)")
            elif "images" not in info:
                units = ",".join(info.get("added_tool_units", []))
                print(f"  repair image P-COCO8  tool_units={units}")
            else:
                print(
                    f"  add   image {info['project']}  "
                    f"images={info['images']} tasks={info['tasks']} "
                    f"pred={info['predictions']}"
                )
        except Exception as e:  # noqa: BLE001 — 夹具/MinIO 不可用时不阻断 seed
            await db.rollback()
            print(f"  WARN  coco8 夹具跳过: {e}")

        if video is not None:
            try:
                info = await seed_video(db, owner_id=owner_id, video=video)
                await db.commit()
                if info is None:
                    print("  skip  video P-VIDEO-DEV (已存在)")
                else:
                    from app.workers.media import generate_video_metadata

                    generate_video_metadata.delay(
                        info["item_id"], delegation=info["video_delegation"]
                    )
                    m = info["video_meta"]
                    print(
                        f"  add   video {info['project']}  "
                        f"{m['width']}x{m['height']} {m['fps']}fps "
                        f"frames={m['frame_count']} tasks={info['tasks']}"
                    )
            except Exception as e:  # noqa: BLE001 — 夹具/MinIO/ffprobe 不可用时不阻断 seed
                await db.rollback()
                print(f"  WARN  video 夹具跳过: {e}")
        else:
            print("  skip  video P-VIDEO-DEV (使用 --video 提供素材)")

        try:
            nu, stale_storage_keys = await seed_nuscenes_scene(
                db, owner_id=admin_id, cache_dir=cache_dir, offline=offline
            )
            await db.commit()
            cleanup_legacy_dev_seed_objects(stale_storage_keys)
            scenes = ", ".join(
                f"{s['name']}({s['frames']}帧{'·已存在' if s.get('skipped') else ''})"
                for s in nu["scenes"]
            )
            action = "replace" if nu.get("replaced_legacy") else "ready  "
            print(
                f"  {action} nuscenes {nu['total_items']} items "
                f"batches={nu['batches']}  {scenes}"
            )
        except Exception as e:  # noqa: BLE001 — demo 模式下大型夹具可选
            await db.rollback()
            print(f"  WARN  nuScenes mini 夹具跳过: {e}")

        member_count = await _ensure_demo_memberships(db)
        if member_count:
            print(f"  add   project memberships={member_count}")

    from app.db.models.dataset import Dataset, DatasetItem
    from app.services import video_derivative_originals as originals
    from app.services.content_snapshot import ContentError

    async with Session() as db:
        ds_rows = await db.execute(
            select(Dataset.id, Dataset.display_id, Dataset.data_type).where(
                Dataset.data_type.in_(["image", "video"])
            )
        )
        for ds_id, disp, data_type in ds_rows.all():
            if data_type == "image":
                from yoao_assets_images.contract import ImageBackfillSelection
                from app.services.assets_images import image_policy
                from scripts.image_media_backfill import accept_image_backfill

                accepted = await accept_image_backfill(
                    ImageBackfillSelection(
                        owner_kind="dataset_item",
                        dataset_id=ds_id,
                        purpose="thumbnail",
                        only_missing=True,
                        accepted_before=datetime.now(UTC),
                    ),
                    operation_id=uuid.uuid4(),
                    policy=image_policy(),
                )
                print(
                    f"  image  accepted thumbnail backfill → {disp} "
                    f"operation={accepted.operation_id} "
                    f"cutoff={accepted.spec.selection.accepted_before.isoformat()} "
                    f"job={accepted.work.job_id}"
                )
                continue
            accepted_before = datetime.now(UTC)
            video_delegations = {}
            video_ids = await db.scalars(
                select(DatasetItem.id).where(
                    DatasetItem.dataset_id == ds_id,
                    DatasetItem.file_type == "video",
                )
            )
            for item_id in video_ids:
                try:
                    source = await originals.pin(db, "dataset_item", item_id)
                except ContentError as exc:
                    if exc.code not in {
                        "no_readable_original",
                        "content_unverified",
                        "video_derivative_input_unsupported",
                        "directory_cache_not_supported",
                        "reference_operation_not_supported",
                    }:
                        raise
                    print(
                        f"  media  skip video {item_id}: {exc.code}; register/verify the canonical original first"
                    )
                    continue
                video_delegations[str(item_id)] = originals.system_authorization(
                    "dataset_item", item_id, source
                )
            job_id = await _enqueue_seed_video_backfill(
                db,
                dataset_id=ds_id,
                actor_id=admin_id,
                accepted_before=accepted_before,
                video_delegations=video_delegations,
            )
            print(f"  media  enqueue 视频元信息回填 → {disp} job={job_id}")

    await engine.dispose()


async def _seed_online_training_profile(*, cache_dir, asset_dir, offline) -> None:
    from scripts.seed_online_training import (
        DATASET_ID,
        PROJECT_DISPLAY_ID,
        prepare_baseline,
        seed_online_training,
    )
    from app.workers.training_dataset import build_dataset

    try:
        assets = ensure_profile(
            "online-training",
            assets=select_profile(
                "online-training", required_ids={"coco8", "coco8-seg"}
            ),
            cache_dir=cache_dir,
            asset_dir=asset_dir,
            offline=offline,
        )
        async with Session() as db:
            users = await _ensure_seed_accounts(db)
            project, created = await seed_online_training(
                db,
                fixture=assets["coco8"].root,
                owner_id=users["pm"],
                annotator_id=users["anno"],
                reviewer_id=users["qa"],
                segmentation_fixture=assets["coco8-seg"].root,
            )
            await _ensure_demo_memberships(db, display_ids=(PROJECT_DISPLAY_ID,))
            versions = [
                await prepare_baseline(db, project, task=task)
                for task in ("detect", "segment")
            ]
            await db.commit()
            builds = [
                (v.id, v.async_job_id, v.attempt_id, v.status == "pending")
                for v in versions
            ]
        for version_id, job_id, attempt_id, pending in builds:
            if pending:
                await build_dataset(version_id, job_id, attempt_id, Session)
        async with Session() as db:
            from app.db.models.training_dataset import TrainingDatasetVersion

            for task, (version_id, _, _, _) in zip(
                ("detect", "segment"), builds, strict=True
            ):
                version = await db.get(TrainingDatasetVersion, version_id)
                if version.status != "ready":
                    raise SeedAssetError(
                        f"COCO8 {task} baseline status={version.status}: "
                        f"{version.error_message or '在训练数据页面检查、重试或创建新版本'}"
                    )
                print(
                    f"  {'add ' if created else 'keep'}  {PROJECT_DISPLAY_ID} "
                    f"task={task} baseline=ready version={version.id} images=8 split=4/4"
                )
        from yoao_assets_images.contract import ImageBackfillSelection
        from app.services.assets_images import image_policy
        from scripts.image_media_backfill import accept_image_backfill

        accepted = await accept_image_backfill(
            ImageBackfillSelection(
                owner_kind="dataset_item",
                dataset_id=DATASET_ID,
                purpose="thumbnail",
                only_missing=True,
                accepted_before=datetime.now(UTC),
            ),
            operation_id=uuid.uuid4(),
            policy=image_policy(),
        )
        print(f"  image  accepted thumbnail backfill → job={accepted.work.job_id}")
        print("  ready 检测与实例分割夹具；登录 pm，在训练数据按任务选择初始版本。")
        print(
            "  hint  检测试跑：epochs=1, batch=4, imgsz=320；"
            "实例分割：yolo11n-seg, epochs=3, batch=1, imgsz=640。"
        )
        print("  hint  训练与自动策略由页面明确启动。")
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("demo", "online-training"), default="demo"
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--asset-dir", type=Path)
    parser.add_argument("--video", type=Path, help="demo profile 的本地视频素材路径")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.video is not None and args.profile != "demo":
        parser.error("--video requires --profile demo")
    return args


async def main() -> None:
    args = parse_args()
    print("\n=== seed start ===")
    await seed(
        profile=args.profile,
        cache_dir=args.cache_dir,
        asset_dir=args.asset_dir,
        video=args.video,
        offline=args.offline,
    )
    print("=== seed done  ===\n")
    print("测试账号一览 (密码统一: 123456):")
    print("  admin    超级管理员   → AdminDashboard")
    print("  pm       项目管理员   → 项目总览")
    print("  qa       质检员       → ReviewerDashboard")
    print("  anno     标注员       → AnnotatorDashboard")
    print("  viewer   观察者       → ViewerDashboard")
    print("  anno2    标注员 (标注组A)")
    print("  anno3    标注员 (标注组B)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SeedAssetError as exc:
        print(f"[seed] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
