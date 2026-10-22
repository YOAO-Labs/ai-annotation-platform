"""Run real media-seed Identity consumers in the installed API image."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid


def _emit(value: dict[str, object]) -> None:
    print("AAP_MEDIA_SEED_IDENTITY_RESULT=" + json.dumps(value, sort_keys=True))


def _error(value: dict[str, object], exc: Exception) -> dict[str, object]:
    database_error = getattr(exc, "orig", None)
    value["error_type"] = type(database_error or exc).__name__
    value["error"] = str(database_error or exc)[:500]
    return value


async def _run_admin(
    kind: str,
    email: str,
    *,
    pending_display_id: str | None = None,
    created_by: uuid.UUID | None = None,
    application_name: str | None = None,
) -> None:
    from sqlalchemy import event, text

    from app.db.base import async_session, engine
    from app.db.models.dataset import Dataset
    from scripts import seed_coco8, seed_ocr, seed_video

    modules = {"coco8": seed_coco8, "video": seed_video, "ocr": seed_ocr}
    if kind not in modules:
        raise SystemExit("unsupported media admin helper")
    module = modules[kind]
    original_email = module.ADMIN_EMAIL
    module.ADMIN_EMAIL = email
    commits = [0]
    pending = None
    result: dict[str, object] = {"operation": "admin", "kind": kind, "email": email}
    failure: Exception | None = None

    def track_commit(_session) -> None:
        commits[0] += 1

    try:
        async with async_session() as db:
            event.listen(db.sync_session, "before_commit", track_commit)
            try:
                if application_name is not None:
                    await db.execute(
                        text(
                            "SELECT pg_catalog.set_config('application_name', :tag, true)"
                        ),
                        {"tag": application_name},
                    )
                if pending_display_id is not None:
                    if created_by is None:
                        raise ValueError(
                            "pending Dataset requires an existing owner id"
                        )
                    pending = Dataset(
                        display_id=pending_display_id,
                        name="pending media seed transaction fixture",
                        created_by=created_by,
                    )
                    db.add(pending)
                owner_id = await module._ensure_admin(db)
                result["owner_id"] = str(owner_id)
                await db.commit()
                result["pending_id_materialized"] = (
                    pending is not None and pending.id is not None
                )
                if pending is not None:
                    result["pending_id"] = str(pending.id)
            finally:
                event.remove(db.sync_session, "before_commit", track_commit)
    except Exception as exc:
        failure = exc
        _error(result, exc)
        result["pending_id_materialized"] = (
            pending is not None and pending.id is not None
        )
        if pending is not None and pending.id is not None:
            result["pending_id"] = str(pending.id)
    finally:
        module.ADMIN_EMAIL = original_email
        await engine.dispose()

    result["commit_attempts"] = commits[0]
    _emit(result)
    if failure is not None:
        raise failure


async def _run_screenshot_users() -> None:
    from app.db.base import async_session, engine
    from scripts.seed_screenshot_profile import _seed_users

    result: dict[str, object] = {"operation": "screenshot_users"}
    failure: Exception | None = None
    try:
        async with async_session() as db:
            users = await _seed_users(db)
            result["user_ids"] = {key: str(user_id) for key, user_id in users.items()}
    except Exception as exc:
        failure = exc
        _error(result, exc)
    finally:
        await engine.dispose()

    _emit(result)
    if failure is not None:
        raise failure


async def _run_screenshot_catalog() -> None:
    from app.db.base import async_session, engine
    from app.services.screenshot_seed_catalog import (
        ScreenshotSeedCatalogError,
        build_screenshot_seed_catalog,
    )

    async with async_session() as db:
        try:
            catalog = await build_screenshot_seed_catalog(db)
        except ScreenshotSeedCatalogError as exc:
            result = {
                "operation": "screenshot_catalog",
                "ready": False,
                "issues": exc.issues,
            }
        else:
            result = {
                "operation": "screenshot_catalog",
                "ready": True,
                "user_ids": {
                    key: value["id"] for key, value in catalog["users"].items()
                },
                "project_keys": sorted(catalog["projects"]),
            }
    await engine.dispose()
    _emit(result)


async def _run_large_graph(owner_email: str, manifest_version: int) -> None:
    from app.db.base import engine
    from scripts import seed_large_images

    result: dict[str, object] = {
        "operation": "large_image_graph",
        "owner_email": owner_email,
    }
    failure: Exception | None = None
    try:
        dataset, project = await seed_large_images._ensure_seed_graph(
            owner_email=owner_email,
            manifest_version=manifest_version,
        )
        result["project"] = {
            "id": str(project.id),
            "display_id": project.display_id,
            "owner_id": str(project.owner_id),
        }
        result["dataset"] = {
            "id": str(dataset.id),
            "display_id": dataset.display_id,
            "created_by": str(dataset.created_by),
        }
    except Exception as exc:
        failure = exc
        _error(result, exc)
    finally:
        await engine.dispose()

    _emit(result)
    if failure is not None:
        raise failure


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: media_seed_identity_probe.py OP [KIND EMAIL [PENDING [OWNER [APP_NAME]]]]"
        )

    operation, *arguments = sys.argv[1:]
    if operation == "admin":
        if len(arguments) < 2:
            raise SystemExit("admin operation requires helper kind and email")
        pending_display_id = arguments[2] if len(arguments) >= 3 else None
        created_by = uuid.UUID(arguments[3]) if len(arguments) >= 4 else None
        application_name = arguments[4] if len(arguments) >= 5 else None
        asyncio.run(
            _run_admin(
                arguments[0],
                arguments[1],
                pending_display_id=pending_display_id,
                created_by=created_by,
                application_name=application_name,
            )
        )
    elif operation == "screenshot_users":
        asyncio.run(_run_screenshot_users())
    elif operation == "screenshot_catalog":
        asyncio.run(_run_screenshot_catalog())
    elif operation == "large_graph":
        if len(arguments) != 2:
            raise SystemExit("large_graph requires owner email and manifest version")
        asyncio.run(_run_large_graph(arguments[0], int(arguments[1])))
    else:
        raise SystemExit("unsupported media seed identity operation")


if __name__ == "__main__":
    main()
