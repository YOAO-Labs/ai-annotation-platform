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

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

# v0.13.11 · 点云夹具脚本与本文件同目录;PYTHONPATH 已含 apps/api,直接 import。
sys.path.insert(0, str(__file__.rsplit("/", 1)[0]))  # 让 `scripts/` 入 sys.path
from seed_pointcloud import seed_pointcloud, seed_nuscenes_scene  # noqa: E402
from seed_coco8 import seed_coco8  # noqa: E402  (依赖 sys.path 先扩)
from seed_video import seed_video  # noqa: E402  (开源视频 video-track 夹具)
from seed_ocr import seed_ocr  # noqa: E402  (OCR 截图夹具)
from seed_assets import (  # noqa: E402  (版本化网络素材)
    SeedAssetError,
    ensure_profile,
    select_profile,
)
from seed_screenshot_assets import (  # noqa: E402
    REQUIRED_SOURCE_IDS,
    ensure_screenshot_assets,
)
from seed_screenshot_profile import (  # noqa: E402
    finalize_screenshot_seed_timestamps,
    prepare_screenshot_seed,
    prepare_screenshot_seed_media,
    reconcile_screenshot_seed,
)

from app.config import settings
from app.core.security import hash_password
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.screenshot_seed_catalog import (
    ScreenshotSeedCatalogError,
    build_screenshot_seed_catalog,
)
from app.services.screenshot_seed_backends import (
    ScreenshotSeedBackendError,
    default_screenshot_stub_url,
    reconcile_screenshot_backends,
)
from app.services.screenshot_seed_spec import SEED_REVISION, parse_backend_requirements

# 生产保护栏：seed.py 仅用于 dev / staging
if settings.environment == "production":
    print("[seed] refusing to run with environment=production", file=sys.stderr)
    raise SystemExit(2)

engine = create_async_engine(settings.database_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ── 种子用户 ──────────────────────────────────────────────────────────────────

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

# Demo 夹具创建的固定项目: 非 owner 的 persona 通过显式项目职责获取访问权,
# 平台角色一律 employee / viewer (迁移 0174 后不再有 annotator / reviewer 平台身份).
DEMO_PROJECT_DISPLAY_IDS = (
    "P-COCO8",
    "P-VIDEO-DEV",
    "P-PC-DEV",
    "P-PC-MULTI",
    "P-OCR",
)
# (persona email, project duty) — 只授予 persona 声明用途所需的最小职责.
# anno/anno2/anno3 是 seed.py 自述的标注员 (标注组A/B), qa 是质检员, viewer 只读;
# pm / admin 通过项目负责人身份 (或超管) 管项目, 不写成员行.
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

    Users are re-read here instead of reusing the ORM instances created above:
    a fixture ``rollback()`` expires those instances, and touching an expired
    attribute inside an async session raises ``MissingGreenlet``.  ``members``
    and ``display_ids`` default to the seeded demo contract; tests pass their
    own values because persona emails are globally unique.
    """

    wanted_emails = {email for email, _ in members} | set(assigner_emails)
    by_email = {
        user.email: user
        for user in (
            await db.execute(select(User).where(User.email.in_(wanted_emails)))
        ).scalars()
    }
    assigner = next(
        (by_email.get(email) for email in assigner_emails if by_email.get(email)), None
    )
    if assigner is None:
        return 0
    projects = list(
        (
            await db.execute(select(Project).where(Project.display_id.in_(display_ids)))
        ).scalars()
    )
    changed = 0
    for project in projects:
        for email, role in members:
            user = by_email.get(email)
            if user is None:
                continue
            member = await db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == user.id,
                )
            )
            if member is None:
                db.add(
                    ProjectMember(
                        project_id=project.id,
                        user_id=user.id,
                        role=role,
                        assigned_by=assigner.id,
                    )
                )
                changed += 1
            elif member.role != role:
                member.role = role
                member.assigned_by = assigner.id
                changed += 1
    if changed:
        await db.commit()
    return changed


# ── 主逻辑 ────────────────────────────────────────────────────────────────────
# 示例项目不再造假数据:图片项目由 seed_coco8(真实 coco8) 单独建, 点云项目由
# seed_pointcloud / seed_nuscenes_scene 建, 均在 seed() 内按夹具可用性容错调用。


async def seed(
    *,
    profile: str = "demo",
    cache_dir: Path | None = None,
    asset_dir: Path | None = None,
    offline: bool = False,
    repair: bool = False,
    ml_backend_mode: str = "live",
    ml_backend_url: str | None = None,
    backend_requirements: str | None = None,
) -> None:
    parse_backend_requirements(backend_requirements)
    assets = {}
    generated_assets = None
    if profile == "screenshots":
        selected_assets = select_profile(
            "screenshots",
            required_ids={"rapidocr-image", *REQUIRED_SOURCE_IDS},
        )
        assets = ensure_profile(
            "screenshots",
            cache_dir=cache_dir,
            asset_dir=asset_dir,
            offline=offline,
            assets=selected_assets,
        )
        source_files = {
            asset_id: assets[asset_id].root.joinpath(
                *assets[asset_id].asset.required_files[0].parts
            )
            for asset_id in REQUIRED_SOURCE_IDS
        }
        generated_assets = ensure_screenshot_assets(
            source_files=source_files,
            cache_dir=cache_dir,
        )
    strict = profile == "screenshots"

    async with Session() as db:
        created_users: dict[str, User] = {}

        for data in USERS:
            existing = await db.scalar(select(User).where(User.email == data["email"]))
            if existing:
                print(f"  skip  {data['email']} (已存在)")
                created_users[data["email"]] = existing
                continue

            user = User(
                id=uuid.uuid4(),
                email=data["email"],
                name=data["name"],
                password_hash=hash_password(data["password"]),
                role=data["role"],
                group_name=data["group_name"],
                is_active=True,
            )
            db.add(user)
            await db.flush()  # 拿到 id，后续项目引用
            created_users[data["email"]] = user
            print(f"  add   {data['email']}  [{data['role']}]")

        owner = created_users.get("pm") or created_users.get("pm@test.com")
        await db.commit()

        # 固化 owner/admin 主键为本地 uuid:下面任一夹具失败都会 await db.rollback(),而
        # rollback 会 expire 掉 session 内所有 ORM 对象;之后再读 user.id 会触发同步
        # lazy-load(_load_expired → 同步 session.execute),在 async 上下文即抛 greenlet_spawn,
        # 连锁使其后的夹具全被误判“跳过”。提前取主键(commit 后仍有效)后全程传值规避。
        admin_user = created_users.get("admin")
        owner_id = owner.id if owner is not None else None
        admin_id = admin_user.id if admin_user is not None else None
        preparation = None
        if strict:
            preparation = await prepare_screenshot_seed(db, repair=repair)
            if preparation.purged_projects or preparation.purged_datasets:
                print(
                    "  repair screenshots "
                    f"projects={preparation.purged_projects} "
                    f"datasets={preparation.purged_datasets}"
                )

        # 图片标注项目:默认 demo 用 coco8;截图 profile 用校验后的真实道路照片派生集。
        img_owner_id = owner_id or admin_id
        if img_owner_id is not None:
            try:
                info = await seed_coco8(
                    db,
                    owner_id=img_owner_id,
                    fixture=generated_assets.image_root if strict else None,
                    prediction_model_version=(
                        f"screenshot-seed:{SEED_REVISION}" if strict else None
                    ),
                )
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
                if strict:
                    raise
                print(f"  WARN  coco8 夹具跳过: {e}")

            # 视频时序追踪项目:截图 profile 用真实城市交通片段的确定性转码。
            # 依赖 MinIO + 宿主 ffprobe, 缺失则跳过。幂等:P-VIDEO-DEV 已存在则跳过。
            try:
                info = await seed_video(
                    db,
                    owner_id=img_owner_id,
                    video=(generated_assets.video_path if strict else None),
                )
                await db.commit()
                if info is None:
                    print("  skip  video P-VIDEO-DEV (已存在)")
                else:
                    m = info["video_meta"]
                    print(
                        f"  add   video {info['project']}  "
                        f"{m['width']}x{m['height']} {m['fps']}fps "
                        f"frames={m['frame_count']} tasks={info['tasks']}"
                    )
            except Exception as e:  # noqa: BLE001 — 夹具/MinIO/ffprobe 不可用时不阻断 seed
                await db.rollback()
                if strict:
                    raise
                print(f"  WARN  video 夹具跳过: {e}")

            if strict:
                info = await seed_ocr(
                    db,
                    owner_id=img_owner_id,
                    image=assets["rapidocr-image"].root / "ch_en_num.jpg",
                )
                await db.commit()
                print(
                    f"  {'skip' if info and info.get('skipped') else 'add  '}  "
                    "ocr P-OCR"
                )

        # 点云开发夹具(owner=admin):脚本按需下载官方 nuScenes mini 到用户缓存，
        # P-PC-DEV 固定导入 scene-0061 的 39 个 key frame。--offline 只复用已有缓存。
        if admin_id is not None:
            try:
                nu = await seed_nuscenes_scene(
                    db,
                    owner_id=admin_id,
                    cache_dir=cache_dir,
                    offline=offline,
                    create_batch=not strict,
                )
                await db.commit()
                scenes = ", ".join(
                    f"{s['name']}({s['frames']}帧{'·已存在' if s.get('skipped') else ''})"
                    for s in nu["scenes"]
                )
                action = "replace" if nu.get("replaced_legacy") else "ready  "
                print(
                    f"  {action} nuscenes P-PC-DEV  items={nu['total_items']} "
                    f"batches={nu['batches']}  {scenes}"
                )
            except Exception as e:  # noqa: BLE001 — demo 模式下大型夹具可选
                await db.rollback()
                if strict:
                    raise
                print(f"  WARN  nuScenes mini 夹具跳过: {e}")

            if strict:
                try:
                    info = await seed_pointcloud(
                        db,
                        owner_id=admin_id,
                        fixture=generated_assets.multicamera_pointcloud_root,
                        axis_convention="apollo",
                        project_display_id="P-PC-MULTI",
                        dataset_display_id="DS-PC-MULTI",
                        dataset_name="pc-multicam-dev",
                        project_name="nuScenes 六相机环视（截图）",
                    )
                    await db.commit()
                    if info is None:
                        print("  skip  point-cloud P-PC-MULTI (已存在)")
                    else:
                        print(
                            f"  add   point-cloud {info['project']}  "
                            f"files={info['files']} tasks={info['tasks']}"
                        )
                except Exception as e:  # noqa: BLE001 — strict 分支会继续抛出
                    await db.rollback()
                    raise RuntimeError(
                        f"multi-camera point-cloud fixture failed: {e}"
                    ) from e

        member_count = await _ensure_demo_memberships(db)
        if member_count:
            print(f"  add   project memberships={member_count}")

        if strict:
            if preparation is None or generated_assets is None:
                raise RuntimeError("screenshots profile preparation is missing")
            screenshot_digests = {
                "image_demo": generated_assets.content_sha256,
                "video_demo": generated_assets.content_sha256,
                "pointcloud_demo": generated_assets.content_sha256,
                "pointcloud_multicam_demo": generated_assets.content_sha256,
                "ocr_demo": assets["rapidocr-image"].asset.sha256,
            }
            report = await reconcile_screenshot_seed(
                db,
                preparation=preparation,
                asset_sha256=screenshot_digests,
            )
            print(
                "  ready screenshots desired-state "
                f"projects={report['projects']} tasks={report['tasks']} "
                f"batches={report['batches']}"
            )
            media_report = await prepare_screenshot_seed_media(
                db, asset_sha256=screenshot_digests
            )
            print(
                "  ready screenshots media "
                f"datasets={media_report['datasets']} items={media_report['items']}"
            )
            backend_report = await reconcile_screenshot_backends(
                db,
                mode=ml_backend_mode,
                stub_url=ml_backend_url,
                backend_requirements=backend_requirements,
            )
            binding_names = ", ".join(
                f"{key}={value['backend_name']}"
                for key, value in backend_report["bindings"].items()
            )
            print(
                f"  ready screenshots ML binding mode={ml_backend_mode} {binding_names}"
            )
            await finalize_screenshot_seed_timestamps(
                db, asset_sha256=screenshot_digests
            )
            try:
                await build_screenshot_seed_catalog(
                    db, backend_requirements=backend_requirements
                )
            except ScreenshotSeedCatalogError as exc:
                raise RuntimeError(
                    "screenshots catalog preflight failed: " + "; ".join(exc.issues)
                ) from exc
            print("  ready screenshots catalog preflight")

    if not strict:
        # Demo keeps asynchronous backfill for all image/video datasets. The
        # screenshots profile has already verified its owned media synchronously.
        from app.db.models.dataset import Dataset
        from app.workers.media import backfill_media

        async with Session() as db:
            ds_rows = await db.execute(
                select(Dataset.id, Dataset.display_id).where(
                    Dataset.data_type.in_(["image", "video"])
                )
            )
            for ds_id, disp in ds_rows.all():
                backfill_media.delay(str(ds_id))
                print(f"  media  enqueue 缩略图回填 → {disp}")

    await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("demo", "screenshots"), default="demo")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--asset-dir", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="rebuild only screenshot-seed-owned fixed projects and datasets",
    )
    parser.add_argument(
        "--ml-backend-mode",
        choices=("live", "stub"),
        default="live",
        help="screenshots profile backend discovery mode",
    )
    parser.add_argument(
        "--ml-backend-url",
        help=(
            "stub URL reachable by the API and workers "
            f"(default: {default_screenshot_stub_url()})"
        ),
    )
    parser.add_argument(
        "--backend-requirements",
        help="comma-separated image_interactive,video_tracker,ocr; 'none' for manual capture; omitted means all",
    )
    args = parser.parse_args()
    try:
        parse_backend_requirements(args.backend_requirements)
    except ValueError as exc:
        parser.error(str(exc))
    if args.backend_requirements is not None and args.profile != "screenshots":
        parser.error("--backend-requirements requires --profile screenshots")
    return args


async def main() -> None:
    args = parse_args()
    print("\n=== seed start ===")
    await seed(
        profile=args.profile,
        cache_dir=args.cache_dir,
        asset_dir=args.asset_dir,
        offline=args.offline,
        repair=args.repair,
        ml_backend_mode=args.ml_backend_mode,
        ml_backend_url=args.ml_backend_url,
        backend_requirements=args.backend_requirements,
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
    except ScreenshotSeedBackendError as exc:
        print(f"[seed] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
