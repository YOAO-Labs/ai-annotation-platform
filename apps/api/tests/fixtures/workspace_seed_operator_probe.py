"""Run real point-cloud seed entrypoints inside the installed API image."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys


def _object_exists(key: str) -> bool:
    from app.services.storage import storage_service

    try:
        storage_service.client.head_object(
            Bucket=storage_service.datasets_bucket, Key=key
        )
    except Exception as exc:  # noqa: BLE001 - only S3's missing-key result is handled
        error = getattr(exc, "response", {}).get("Error", {})
        if error.get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def _emit(value: dict[str, object]) -> None:
    print("AAP_WORKSPACE_SEED_RESULT=" + json.dumps(value, sort_keys=True, default=str))


async def _run(operation: str, argument: str | None) -> None:
    from app.db.base import async_session
    from scripts import seed_pointcloud as seed

    async with async_session() as db:
        owner_id = await seed._ensure_admin(db)
        if operation == "directory":
            if argument is None or not argument.startswith("__aap_trusted_nuscenes__-"):
                raise ValueError("directory seed requires its owned S3 key prefix")
            result = await seed.seed_pointcloud(
                db,
                owner_id=owner_id,
                fixture=Path("/fixture/directory"),
                axis_convention="apollo",
                project_display_id="P-PC-MULTI",
                dataset_display_id="DS-PC-MULTI",
                dataset_name=argument,
                project_name="nuScenes 六相机环视（截图）",
            )
            await db.commit()
            _emit({"operation": operation, "owner_id": owner_id, "result": result})
            return

        if operation not in {"rollback", "replace", "replay"}:
            raise ValueError("unsupported seed operation")

        info, old_keys = await seed.seed_nuscenes_scene(
            db,
            owner_id=owner_id,
            cache_dir=Path("/fixture/nu-cache"),
            offline=True,
            create_batch=True,
        )
        old_key_checks = [_object_exists(key) for key in old_keys]
        if operation == "rollback":
            await db.rollback()
            _emit(
                {
                    "operation": operation,
                    "owner_id": owner_id,
                    "info": info,
                    "old_keys": old_keys,
                    "old_keys_before_rollback": old_key_checks,
                    "old_keys_after_rollback": [
                        _object_exists(key) for key in old_keys
                    ],
                    "cleanup_called": False,
                }
            )
            return

        await db.commit()
        old_keys_after_commit = [_object_exists(key) for key in old_keys]
        if operation == "replace":
            seed.cleanup_legacy_dev_seed_objects(old_keys)
            cleaned = [_object_exists(key) for key in old_keys]
        else:
            cleaned = old_keys_after_commit
        _emit(
            {
                "operation": operation,
                "owner_id": owner_id,
                "info": info,
                "old_keys": old_keys,
                "old_keys_before_commit": old_key_checks,
                "old_keys_after_commit_before_cleanup": old_keys_after_commit,
                "old_keys_after_cleanup": cleaned,
                "cleanup_called": operation == "replace",
            }
        )


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit("usage: workspace_seed_operator_probe.py OP [ARG]")
    asyncio.run(_run(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))


if __name__ == "__main__":
    main()
