"""Create a reproducible large-image development project from pinned real assets.

The canonical asset manifest and downloader live under ``apps/web`` so browser
benchmarks, documentation screenshots, and this database seed all use the same
bytes:

    pnpm --filter @anno/web image:seeds

Run from ``apps/api`` after the files are present:

    PYTHONPATH=. uv run python scripts/seed_large_images.py \
      --enqueue-pyramids --wait-seconds 1800

The command is idempotent and refuses production. It only adopts the fixed
project/dataset IDs when the dataset carries this seed's ownership marker.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import time
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import func, or_, select

from app.config import settings
from app.db.base import async_session, engine
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.dataset import build_tasks_for_link
from app.services.image_pyramid import pyramid_eligible, pyramid_required
from app.services.storage import storage_service
from app.workers.image_pyramid import enqueue_image_pyramid


_PARENTS = Path(__file__).resolve().parents
REPO_ROOT = _PARENTS[3] if len(_PARENTS) > 3 else _PARENTS[-1]
DEFAULT_MANIFEST = REPO_ROOT / "apps/web/scripts/image-bench/fixtures.json"
DEFAULT_FIXTURE_DIR = REPO_ROOT / "test-results/image-seeds"

PROJECT_DISPLAY_ID = "P-LARGE-IMG"
DATASET_DISPLAY_ID = "DS-LARGE-IMG"
PROJECT_NAME = "真实超大图金字塔（dev）"
DATASET_NAME = "large-image-dev"
DATASET_FOLDER = "large-image-dev"
SEED_MANAGED_BY = "large-image-seed"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

TOOL_BINDINGS = {
    "region": {
        "enabled": True,
        "classes": [
            {"name": "区域", "order": 0},
            {"name": "前景", "order": 1},
        ],
    },
    "bbox": {
        "enabled": True,
        "classes": [{"name": "目标", "order": 0}],
    },
}


class LargeImageSeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class LargeImageFixture:
    id: str
    label: str
    role: str
    filename: str
    format: str
    width: int
    height: int
    pixel_count: int
    byte_size: int
    sha256: str
    source_page: str
    credit: str
    usage_policy: str
    usage_note: str

    @property
    def storage_key(self) -> str:
        return f"{DATASET_FOLDER}/{self.filename}"

    @property
    def content_type(self) -> str:
        return mimetypes.guess_type(self.filename)[0] or "application/octet-stream"


def _require_str(raw: dict[str, Any], key: str, fixture_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise LargeImageSeedError(f"{fixture_id}: {key} must be a non-empty string")
    return value


def _require_int(raw: dict[str, Any], key: str, fixture_id: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LargeImageSeedError(f"{fixture_id}: {key} must be a positive integer")
    return value


def load_fixture_manifest(
    path: Path = DEFAULT_MANIFEST,
) -> tuple[int, list[LargeImageFixture]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LargeImageSeedError(f"cannot read fixture manifest: {path}") from exc

    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise LargeImageSeedError("fixture manifest version must be a positive integer")
    raw_fixtures = payload.get("realLargeImages")
    if not isinstance(raw_fixtures, list) or not raw_fixtures:
        raise LargeImageSeedError("fixture manifest has no realLargeImages")

    fixtures: list[LargeImageFixture] = []
    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    for index, raw in enumerate(raw_fixtures):
        if not isinstance(raw, dict):
            raise LargeImageSeedError(f"realLargeImages[{index}] must be an object")
        fixture_id = _require_str(raw, "id", f"realLargeImages[{index}]")
        filename = _require_str(raw, "filename", fixture_id)
        width = _require_int(raw, "widthPx", fixture_id)
        height = _require_int(raw, "heightPx", fixture_id)
        pixel_count = _require_int(raw, "pixelCount", fixture_id)
        sha256 = _require_str(raw, "sha256", fixture_id)
        image_format = _require_str(raw, "format", fixture_id)
        if fixture_id in seen_ids:
            raise LargeImageSeedError(f"duplicate fixture id: {fixture_id}")
        if filename in seen_filenames:
            raise LargeImageSeedError(f"duplicate fixture filename: {filename}")
        if pixel_count != width * height:
            raise LargeImageSeedError(
                f"{fixture_id}: pixelCount does not match dimensions"
            )
        if SHA256_RE.fullmatch(sha256) is None:
            raise LargeImageSeedError(f"{fixture_id}: invalid sha256")
        if image_format not in {"jpeg", "png"}:
            raise LargeImageSeedError(
                f"{fixture_id}: unsupported format {image_format}"
            )

        fixtures.append(
            LargeImageFixture(
                id=fixture_id,
                label=_require_str(raw, "label", fixture_id),
                role=_require_str(raw, "role", fixture_id),
                filename=filename,
                format=image_format,
                width=width,
                height=height,
                pixel_count=pixel_count,
                byte_size=_require_int(raw, "byteSize", fixture_id),
                sha256=sha256,
                source_page=_require_str(raw, "sourcePage", fixture_id),
                credit=_require_str(raw, "credit", fixture_id),
                usage_policy=_require_str(raw, "usagePolicy", fixture_id),
                usage_note=_require_str(raw, "usageNote", fixture_id),
            )
        )
        seen_ids.add(fixture_id)
        seen_filenames.add(filename)
    return version, fixtures


def select_fixtures(
    fixtures: list[LargeImageFixture], fixture_ids: list[str]
) -> list[LargeImageFixture]:
    if not fixture_ids:
        return fixtures
    requested = set(fixture_ids)
    selected = [fixture for fixture in fixtures if fixture.id in requested]
    missing = [
        fixture_id
        for fixture_id in fixture_ids
        if fixture_id not in {f.id for f in selected}
    ]
    if missing:
        raise LargeImageSeedError(f"unknown fixture id: {', '.join(missing)}")
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_fixture_file(fixture: LargeImageFixture, path: Path) -> None:
    try:
        byte_size = path.stat().st_size
    except OSError as exc:
        raise LargeImageSeedError(
            f"{fixture.id}: fixture file missing: {path}"
        ) from exc
    if byte_size != fixture.byte_size:
        raise LargeImageSeedError(
            f"{fixture.id}: byte size mismatch ({byte_size} != {fixture.byte_size})"
        )
    digest = sha256_file(path)
    if digest != fixture.sha256:
        raise LargeImageSeedError(
            f"{fixture.id}: sha256 mismatch ({digest} != {fixture.sha256})"
        )


def _dataset_marker(manifest_version: int) -> dict[str, Any]:
    return {
        "seed": {
            "managed_by": SEED_MANAGED_BY,
            "manifest_version": manifest_version,
        }
    }


async def _ensure_seed_graph(
    *, owner_email: str, manifest_version: int
) -> tuple[Dataset, Project]:
    async with async_session() as db:
        owner = await db.scalar(select(User).where(User.email == owner_email))
        if owner is None:
            raise LargeImageSeedError(
                f"owner {owner_email!r} is missing; run scripts/seed.py first"
            )
        project = await db.scalar(
            select(Project).where(Project.display_id == PROJECT_DISPLAY_ID)
        )
        dataset = await db.scalar(
            select(Dataset).where(Dataset.display_id == DATASET_DISPLAY_ID)
        )
        if (project is None) != (dataset is None):
            raise LargeImageSeedError(
                "partial large-image seed exists; remove or repair the fixed project/dataset pair"
            )

        if project is None and dataset is None:
            project = Project(
                display_id=PROJECT_DISPLAY_ID,
                name=PROJECT_NAME,
                type_label="图像 · 实例分割",
                type_key="image-seg",
                data_type="image",
                owner_id=owner.id,
                ai_enabled=False,
                ai_interactive_enabled=False,
                tool_bindings=TOOL_BINDINGS,
            )
            dataset = Dataset(
                display_id=DATASET_DISPLAY_ID,
                name=DATASET_NAME,
                description="NASA 真实超大图开发、浏览器回归与文档截图夹具",
                data_type="image",
                created_by=owner.id,
                metadata_=_dataset_marker(manifest_version),
            )
            db.add_all([project, dataset])
            await db.flush()
        else:
            assert project is not None and dataset is not None
            marker = (dataset.metadata_ or {}).get("seed")
            expected_marker = _dataset_marker(manifest_version)["seed"]
            if marker != expected_marker:
                raise LargeImageSeedError(
                    f"{DATASET_DISPLAY_ID} is not owned by {SEED_MANAGED_BY}"
                )
            if (
                project.name != PROJECT_NAME
                or project.type_key != "image-seg"
                or project.data_type != "image"
                or project.owner_id != owner.id
                or dataset.name != DATASET_NAME
                or dataset.data_type != "image"
                or dataset.created_by != owner.id
            ):
                raise LargeImageSeedError(
                    "fixed large-image project/dataset was modified; refusing to overwrite it"
                )

        links = list(
            (
                await db.execute(
                    select(ProjectDataset).where(
                        or_(
                            ProjectDataset.project_id == project.id,
                            ProjectDataset.dataset_id == dataset.id,
                        )
                    )
                )
            ).scalars()
        )
        unexpected = [
            link
            for link in links
            if link.project_id != project.id or link.dataset_id != dataset.id
        ]
        if unexpected:
            raise LargeImageSeedError(
                "fixed large-image project/dataset has unexpected dataset links"
            )
        if not links:
            db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
        await db.commit()
        return dataset, project


def _fixture_metadata(fixture: LargeImageFixture) -> dict[str, Any]:
    return {
        "seed": {
            "managed_by": SEED_MANAGED_BY,
            "fixture_id": fixture.id,
            "role": fixture.role,
        },
        "source": {
            "page": fixture.source_page,
            "credit": fixture.credit,
            "usage_policy": fixture.usage_policy,
            "usage_note": fixture.usage_note,
        },
    }


def _ensure_source_object(fixture: LargeImageFixture, path: Path) -> bool:
    bucket = storage_service.datasets_bucket
    try:
        head = storage_service.client.head_object(
            Bucket=bucket, Key=fixture.storage_key
        )
        metadata = head.get("Metadata") or {}
        if (
            int(head.get("ContentLength") or 0) == fixture.byte_size
            and metadata.get("sha256") == fixture.sha256
        ):
            return False
    except ClientError:
        pass
    storage_service.client.upload_file(
        str(path),
        bucket,
        fixture.storage_key,
        ExtraArgs={
            "ContentType": fixture.content_type,
            "Metadata": {"sha256": fixture.sha256},
        },
    )
    head = storage_service.verify_upload(fixture.storage_key, bucket=bucket)
    if (
        head is None
        or int(head.get("ContentLength") or 0) != fixture.byte_size
        or (head.get("Metadata") or {}).get("sha256") != fixture.sha256
    ):
        raise LargeImageSeedError(f"{fixture.id}: uploaded object verification failed")
    return True


async def _ensure_fixture_item(
    dataset_id, fixture: LargeImageFixture, path: Path
) -> tuple[DatasetItem, bool]:
    from app.services import managed_original_reader as reader, storage_access
    from app.services.dataset_assets import (
        ensure_legacy_bindings,
        register_item_asset,
    )
    from app.services.image_pyramid_originals import Admission, physical
    from app.services.original_binary_response import close_stage

    def assert_fixture_item(item):
        if (
            item is None
            or (item.metadata_ or {}).get("seed") != _fixture_metadata(fixture)["seed"]
            or item.file_path != fixture.storage_key
            or item.file_type != "image"
            or item.file_size != fixture.byte_size
            or item.content_hash != fixture.sha256
            or item.width != fixture.width
            or item.height != fixture.height
        ):
            raise LargeImageSeedError(
                f"{fixture.id}: existing dataset item does not match the pinned fixture"
            )

    verify_fixture_file(fixture, path)
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(DatasetItem).where(
                        DatasetItem.dataset_id == dataset_id,
                        DatasetItem.file_name == fixture.filename,
                    )
                )
            ).scalars()
        )
        if len(rows) > 1:
            raise LargeImageSeedError(f"{fixture.id}: duplicate dataset items")
        if rows:
            item = rows[0]
            assert_fixture_item(item)
        binding = await ensure_legacy_bindings(db)
        # Publish only storage bootstrap metadata before independent admission.
        await db.commit()
        stage, failure = None, None
        admission = Admission()
        async with await storage_access.acquire(
            db, binding.location_id, "image_pyramid_seed_source"
        ) as permit:
            uploaded = await physical(_ensure_source_object, fixture, path)
            try:
                stage = await reader.capture(
                    reader.ObjectSpec(
                        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                        binding.bucket,
                        fixture.storage_key,
                        fixture.sha256,
                        fixture.byte_size,
                    ),
                    max_bytes=settings.image_pyramid_max_source_bytes,
                    timeout_seconds=settings.image_pyramid_job_timeout_seconds,
                    transport_timeout_seconds=30,
                    admission=admission,
                )
                etag = stage.observation["etag"]
            except BaseException as exc:
                failure = exc
                if getattr(exc, "storage_operation_unsettled", False):
                    admission.retained = True
                raise
            finally:
                if stage is not None:
                    try:
                        await close_stage(stage)
                    except BaseException as closing:
                        if getattr(failure, "storage_operation_unsettled", False):
                            storage_access.mark_unsettled(closing)
                        raise
        await storage_access.check(
            db, binding.location_id, expected_revision=permit.revision, lock=True
        )
        binding = await ensure_legacy_bindings(db)
        if rows:
            item = await db.scalar(
                select(DatasetItem)
                .where(DatasetItem.id == item.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            assert_fixture_item(item)
        if not rows:
            item = DatasetItem(
                dataset_id=dataset_id,
                file_name=fixture.filename,
                file_path=fixture.storage_key,
                file_type="image",
                file_size=fixture.byte_size,
                content_hash=fixture.sha256,
                width=fixture.width,
                height=fixture.height,
                metadata_=_fixture_metadata(fixture),
            )
            db.add(item)
            await db.flush()
        await register_item_asset(
            db,
            item,
            sha256=fixture.sha256,
            size_bytes=fixture.byte_size,
            etag=etag,
            binding=binding,
        )
        dataset = await db.get(Dataset, dataset_id)
        if dataset is not None:
            dataset.file_count = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(DatasetItem)
                        .where(DatasetItem.dataset_id == dataset_id)
                    )
                ).scalar_one()
            )
        await db.commit()
        return item, uploaded


async def _task_map(project_id, item_ids: list) -> dict:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.dataset_item_id.in_(item_ids),
                )
            )
        ).scalars()
        return {task.dataset_item_id: task for task in rows}


async def _pyramid_statuses(item_ids: list) -> dict[str, dict[str, Any]]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    ImagePyramidAsset.dataset_item_id,
                    ImagePyramidGeneration.generation,
                    ImagePyramidGeneration.status,
                    ImagePyramidGeneration.error_code,
                    ImagePyramidGeneration.tile_count,
                    ImagePyramidGeneration.retained_bytes,
                )
                .join(
                    ImagePyramidGeneration,
                    ImagePyramidGeneration.asset_id == ImagePyramidAsset.id,
                )
                .where(ImagePyramidAsset.dataset_item_id.in_(item_ids))
                .order_by(
                    ImagePyramidAsset.dataset_item_id,
                    ImagePyramidGeneration.generation.desc(),
                )
            )
        ).all()
    statuses: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.dataset_item_id)
        if key not in statuses:
            statuses[key] = {
                "generation": row.generation,
                "status": row.status,
                "error_code": row.error_code,
                "tile_count": row.tile_count,
                "retained_bytes": row.retained_bytes,
            }
    return statuses


async def _wait_for_pyramids(item_ids: list, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        statuses = await _pyramid_statuses(item_ids)
        if len(statuses) == len(item_ids) and all(
            status["status"] in {"ready", "failed"} for status in statuses.values()
        ):
            return statuses
        if time.monotonic() >= deadline:
            raise LargeImageSeedError(
                f"timed out waiting for {len(item_ids)} image pyramids"
            )
        await asyncio.sleep(2)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest_version, fixtures = load_fixture_manifest(args.manifest)
    selected = select_fixtures(fixtures, args.ids)
    paths = {fixture.id: args.fixture_dir / fixture.filename for fixture in selected}
    for fixture in selected:
        verify_fixture_file(fixture, paths[fixture.id])

    storage_service.ensure_bucket(storage_service.datasets_bucket)
    dataset, project = await _ensure_seed_graph(
        owner_email=args.owner_email,
        manifest_version=manifest_version,
    )

    item_rows: list[tuple[LargeImageFixture, DatasetItem, bool]] = []
    for fixture in selected:
        item, uploaded = await _ensure_fixture_item(
            dataset.id, fixture, paths[fixture.id]
        )
        item_rows.append((fixture, item, uploaded))

    async with async_session() as db:
        task_result = await build_tasks_for_link(
            db, dataset_id=dataset.id, project_id=project.id
        )
    tasks = await _task_map(project.id, [item.id for _, item, _ in item_rows])

    queued_item_ids = []
    celery_task_ids: dict[str, str] = {}
    if args.enqueue_pyramids:
        from app.services import image_pyramid_originals as originals

        async with async_session() as db:
            for fixture, item, _ in item_rows:
                if pyramid_eligible(fixture.width, fixture.height):
                    source = await originals.pin(db, "dataset_item", item.id)
                    celery_task_ids[fixture.id] = enqueue_image_pyramid(
                        "dataset_item",
                        item.id,
                        delegation=originals.system_authorization(
                            "dataset_item", item.id, source
                        ),
                    )
                    queued_item_ids.append(item.id)

    statuses: dict[str, dict[str, Any]] = {}
    if args.wait_seconds:
        if not queued_item_ids:
            raise LargeImageSeedError(
                "--wait-seconds requires at least one eligible enqueued fixture"
            )
        statuses = await _wait_for_pyramids(queued_item_ids, args.wait_seconds)
        failed = [
            fixture.id
            for fixture, item, _ in item_rows
            if statuses.get(str(item.id), {}).get("status") == "failed"
        ]
        if failed:
            raise LargeImageSeedError(
                f"image pyramid generation failed: {', '.join(failed)}"
            )

    fixture_output = []
    for fixture, item, uploaded in item_rows:
        task = tasks.get(item.id)
        if task is None:
            raise LargeImageSeedError(f"{fixture.id}: task was not created")
        fixture_output.append(
            {
                "id": fixture.id,
                "role": fixture.role,
                "dimensions": [fixture.width, fixture.height],
                "byte_size": fixture.byte_size,
                "item_id": str(item.id),
                "task_id": str(task.id),
                "task_display_id": task.display_id,
                "uploaded": uploaded,
                "pyramid_eligible": pyramid_eligible(fixture.width, fixture.height),
                "pyramid_required": pyramid_required(fixture.width, fixture.height),
                "celery_task_id": celery_task_ids.get(fixture.id),
                "pyramid": statuses.get(str(item.id)),
                "credit": fixture.credit,
            }
        )

    return {
        "project_display_id": project.display_id,
        "project_id": str(project.id),
        "dataset_display_id": dataset.display_id,
        "dataset_id": str(dataset.id),
        "tasks_created": task_result["created"],
        "workbench_base_url": f"/projects/{project.id}/annotate",
        "fixtures": fixture_output,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--id", dest="ids", action="append", default=[])
    parser.add_argument("--owner-email", default="admin")
    parser.add_argument("--enqueue-pyramids", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be >= 0")
    if args.wait_seconds and not args.enqueue_pyramids:
        parser.error("--wait-seconds requires --enqueue-pyramids")
    return args


def main() -> None:
    if settings.environment == "production":
        raise SystemExit("[seed-large-images] refusing to run in production")
    engine.echo = False
    try:
        output = asyncio.run(run(parse_args()))
    except LargeImageSeedError as exc:
        raise SystemExit(f"[seed-large-images] {exc}") from exc
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
