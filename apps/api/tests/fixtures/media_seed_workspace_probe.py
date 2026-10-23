"""Run the installed media seed consumers with verifier-owned tiny fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys


_TAG = re.compile(r"[0-9a-f]{8}\Z")


def _emit(value: dict[str, object]) -> None:
    print(
        "AAP_MEDIA_SEED_WORKSPACE_RESULT="
        + json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    )


def _configure(tag: str):
    if not _TAG.fullmatch(tag):
        raise ValueError("fixture tag must be eight lowercase hexadecimal characters")

    from scripts import seed_coco8, seed_large_images, seed_ocr, seed_video

    suffix = tag.upper()
    for module, stem, folder in (
        (seed_coco8, "COCO8", "coco8"),
        (seed_video, "VIDEO", "video"),
        (seed_ocr, "OCR", "ocr"),
    ):
        module.PROJECT_DISPLAY_ID = f"P-MSC-{suffix}-{stem}"
        module.DATASET_DISPLAY_ID = f"D-MSC-{suffix}-{stem}"
        module.DATASET_FOLDER = f"media-seed/{tag}/{folder}"

    seed_large_images.PROJECT_DISPLAY_ID = f"P-MSC-{suffix}-LARGE"
    seed_large_images.DATASET_DISPLAY_ID = f"D-MSC-{suffix}-LARGE"
    seed_large_images.DATASET_NAME = f"large-image-seed-{tag}"
    return seed_coco8, seed_video, seed_ocr, seed_large_images


def _error(exc: Exception) -> dict[str, object]:
    original = getattr(exc, "orig", None) or exc
    return {
        "error_type": type(original).__name__,
        "error": str(original)[:500],
    }


async def _run_missing_create(tag: str) -> None:
    from app.db.base import async_session, engine

    seed_coco8, _seed_video, _seed_ocr, _seed_large_images = _configure(tag)
    outcome: dict[str, object] = {"operation": "missing_create", "tag": tag}
    try:
        async with async_session() as db:
            owner_id = await seed_coco8._ensure_admin(db)
            await db.commit()
            outcome["owner_id"] = str(owner_id)
            try:
                result = await seed_coco8.seed_coco8(
                    db,
                    owner_id=owner_id,
                    fixture=Path("/fixture/coco8"),
                    prediction_model_version="workspace-media-seed-cold",
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                outcome.update(_error(exc))
                _emit(outcome)
                raise
            outcome["unexpected_result"] = result
        _emit(outcome)
    finally:
        await engine.dispose()


async def _run_suite(tag: str, *, replay: bool) -> None:
    from app.db.base import async_session, engine

    seed_coco8, seed_video, seed_ocr, seed_large_images = _configure(tag)
    outcome: dict[str, object] = {
        "operation": "replay" if replay else "seed",
        "tag": tag,
    }
    try:
        async with async_session() as db:
            owner_id = await seed_coco8._ensure_admin(db)
            await db.commit()

        async with async_session() as db:
            coco8_result = await seed_coco8.seed_coco8(
                db,
                owner_id=owner_id,
                fixture=Path("/fixture/coco8"),
                prediction_model_version="workspace-media-seed-cold",
            )
            await db.commit()

        async with async_session() as db:
            video_result = await seed_video.seed_video(
                db,
                owner_id=owner_id,
                video=Path("/fixture/video/smart-point.webm"),
            )
            await db.commit()

        async with async_session() as db:
            ocr_result = await seed_ocr.seed_ocr(
                db,
                owner_id=owner_id,
                image=Path("/fixture/ocr/ocr.jpg"),
            )
            await db.commit()

        large_dataset, large_project = await seed_large_images._ensure_seed_graph(
            owner_email=seed_coco8.ADMIN_EMAIL,
            manifest_version=1,
        )
        outcome.update(
            {
                "owner_id": str(owner_id),
                "results": {
                    "coco8": coco8_result,
                    "video": video_result,
                    "ocr": ocr_result,
                },
                "large_graph": {
                    "project_id": str(large_project.id),
                    "project_display_id": large_project.display_id,
                    "dataset_id": str(large_dataset.id),
                    "dataset_display_id": large_dataset.display_id,
                },
            }
        )
        _emit(outcome)
    finally:
        await engine.dispose()


async def _run_coco_merge(tag: str) -> None:
    from app.db.base import async_session, engine

    seed_coco8, _seed_video, _seed_ocr, _seed_large_images = _configure(tag)
    outcome: dict[str, object] = {"operation": "coco_merge", "tag": tag}
    try:
        async with async_session() as db:
            owner_id = await seed_coco8._ensure_admin(db)
            outcome["owner_id"] = str(owner_id)
            first = await seed_coco8.seed_coco8(
                db,
                owner_id=owner_id,
                fixture=Path("/fixture/coco8"),
                prediction_model_version="workspace-media-seed-cold",
            )
            await db.commit()

        async with async_session() as db:
            owner_id = await seed_coco8._ensure_admin(db)
            second = await seed_coco8.seed_coco8(
                db,
                owner_id=owner_id,
                fixture=Path("/fixture/coco8"),
                prediction_model_version="workspace-media-seed-cold",
            )
            await db.commit()
        outcome["first"] = first
        outcome["second"] = second
        _emit(outcome)
    finally:
        await engine.dispose()


async def _run_screenshot() -> None:
    from app.db.base import async_session, engine
    from app.services.screenshot_seed_backends import reconcile_screenshot_backends
    from app.services.screenshot_seed_spec import PROJECT_SPECS
    from scripts.seed_screenshot_profile import (
        ScreenshotSeedPreparation,
        finalize_screenshot_seed_timestamps,
        reconcile_screenshot_seed,
    )

    digests = {key: "a" * 64 for key in PROJECT_SPECS}
    try:
        async with async_session() as db:
            report = await reconcile_screenshot_seed(
                db,
                preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
                asset_sha256=digests,
            )
            backend = await reconcile_screenshot_backends(
                db, mode="live", backend_requirements="none"
            )
            await finalize_screenshot_seed_timestamps(db, asset_sha256=digests)
        _emit({"operation": "screenshot", "report": report, "backend": backend})
    finally:
        await engine.dispose()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: media_seed_workspace_probe.py OP TAG")
    operation, tag = sys.argv[1:]
    if operation == "missing_create":
        asyncio.run(_run_missing_create(tag))
    elif operation == "seed":
        asyncio.run(_run_suite(tag, replay=False))
    elif operation == "replay":
        asyncio.run(_run_suite(tag, replay=True))
    elif operation == "coco_merge":
        asyncio.run(_run_coco_merge(tag))
    elif operation == "screenshot":
        asyncio.run(_run_screenshot())
    else:
        raise SystemExit("unsupported media-seed operation")


if __name__ == "__main__":
    main()
