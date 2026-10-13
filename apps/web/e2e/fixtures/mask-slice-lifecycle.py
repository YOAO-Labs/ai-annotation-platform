"""Explicit GC/version-loss injection in an owned disposable E2E environment."""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

_APP_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _APP_DIR.parent
sys.path.insert(0, str(_APP_DIR / "api"))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from worktree_runtime import WorktreeError, require_owner  # noqa: E402

OWNER_TAG = "aap-worktree-owner"


class GuardError(AssertionError):
    """Rejected bucket/resource identity before any GC work runs."""


def assert_owned_disposable_bucket(
    *,
    mode: str | None,
    resources: dict | None,
    database: str,
    bucket: str,
    owner_tag: str | None,
) -> None:
    """Fail closed unless ``bucket`` is an exclusively owned disposable test bucket.

    Worktree modes (``AAP_WORKTREE_MODE=test|e2e``) must declare this exact
    database and ``MINIO_BUCKET`` slot in the active resources manifest and
    carry the manifest owner tag (``scripts/worktree_runtime.require_owner``
    semantics: foreign owner raises, missing tag is rejected). Without a
    worktree mode only the legacy isolated-CI bucket naming is accepted, and an
    unrecognized ``aap-wt-*`` bucket is rejected instead of falling back to it.
    """
    if mode is not None:
        if mode not in {"test", "e2e"}:
            raise GuardError(f"worktree GC helper refuses mode {mode!r}")
        if not resources:
            raise GuardError("worktree mode requires the active resources manifest")
        if resources.get("mode") != mode:
            raise GuardError("resources manifest was created for another mode")
        if resources.get("root") != str(_REPO_ROOT):
            raise GuardError("resources manifest belongs to another checkout")
        owner = resources.get("owner")
        if not owner:
            raise GuardError("resources manifest has no owner tag")
        if database != resources.get("database"):
            raise GuardError("database does not match the active worktree resource")
        if bucket != (resources.get("buckets") or {}).get("MINIO_BUCKET"):
            raise GuardError("MINIO_BUCKET must be the active worktree resource slot")
        try:
            owned = require_owner(
                "MINIO_BUCKET",
                {"owner": owner_tag} if owner_tag is not None else None,
                str(owner),
            )
        except WorktreeError as exc:
            raise GuardError(str(exc)) from exc
        if not owned:
            raise GuardError("MINIO_BUCKET has no owner tag for this worktree")
        return
    if bucket.startswith("aap-wt-"):
        raise GuardError("aap-wt-* bucket requires an active worktree mode")
    if not bucket.endswith(("-e2e", "_e2e", "-test", "_test")):
        raise GuardError("legacy isolated CI bucket naming required")


def _bucket_owner_tag(storage: object, bucket: str) -> str | None:
    try:
        tags = storage.client.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except Exception:
        return None
    return next((tag["Value"] for tag in tags if tag["Key"] == OWNER_TAG), None)


async def main():
    action, task_value, operation_value = sys.argv[1:]
    task_id, operation_id = UUID(task_value), UUID(operation_value)
    url = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    assert url.database and url.database.endswith(("_e2e", "_test"))
    assert os.environ.get("E2E_SEED_ENABLED") == "true"
    assert os.environ.get("ENVIRONMENT") == "development"
    # A disposable DB alone is insufficient: GC must never scan a shared bucket.
    bucket = os.environ.get("MINIO_BUCKET", "")
    mode = os.environ.get("AAP_WORKTREE_MODE")
    resources = None
    if mode is not None:
        manifest = _REPO_ROOT / ".worktree" / mode / "resources.json"
        resources = json.loads(manifest.read_text())["resources"]
    from app.workers._db import task_session
    from app.services.storage import storage_service
    from app.services.raster_mask_storage import store_coco_rle
    from app.workers.cleanup import _purge_unreferenced_raster_masks_async

    assert storage_service.bucket == bucket
    assert_owned_disposable_bucket(
        mode=mode,
        resources=resources,
        database=url.database,
        bucket=bucket,
        owner_tag=_bucket_owner_tag(storage_service, bucket),
    )
    async with task_session() as db:
        operation = (
            (
                await db.execute(
                    text(
                        "SELECT report, response_json FROM annotation_operations WHERE id=:operation AND task_id=:task AND kind='slice_mask'"
                    ),
                    {"operation": operation_id, "task": task_id},
                )
            )
            .mappings()
            .one()
        )
        source_id = UUID(
            operation["response_json"]["slice_restore"]["source_annotation_id"]
        )
        source_version = operation["report"]["before"][str(source_id)][
            "annotation_version"
        ]
        if action in {"drop_revision", "expire_revision"}:
            sql = (
                "DELETE FROM mask_annotation_revisions"
                if action == "drop_revision"
                else "UPDATE mask_annotation_revisions SET expires_at=now()-interval '1 day'"
            )
            changed = await db.execute(
                text(
                    sql
                    + " WHERE task_id=:task AND annotation_id=:source AND annotation_version=:version"
                ),
                {"task": task_id, "source": source_id, "version": source_version},
            )
            assert changed.rowcount == 1
            await db.commit()
            print(json.dumps({"action": action, "changed": 1}))
            return
        assert action == "gc" and operation["report"]["current_side"] == "before"
        rows = (
            (
                await db.execute(
                    text("SELECT geometry FROM annotations WHERE task_id=:task"),
                    {"task": task_id},
                )
            )
            .scalars()
            .all()
        )
        protected = [
            row["mask"]["object_key"]
            for row in rows
            if row.get("type") == "raster_mask"
        ]

    # Add an actual unreferenced object to show that GC deletes eligible content.
    length = 128
    bits = uuid4().int
    counts, last = [0], False
    for index in range(length):
        value = bool(bits & (1 << index))
        if value != last:
            counts.append(0)
            last = value
        counts[-1] += 1
    orphan = await store_coco_rle(
        {"encoding": "coco_rle", "size": [1, length], "counts": counts}
    )
    original_list = storage_service.list_objects
    # Explicit age fixture bypasses the 24-hour grace while running the real GC.
    storage_service.list_objects = lambda *args, **kwargs: [
        {**item, "last_modified": datetime.now(timezone.utc) - timedelta(days=2)}
        for item in original_list(*args, **kwargs)
    ]
    try:
        result = await _purge_unreferenced_raster_masks_async(dry_run=False)
    finally:
        storage_service.list_objects = original_list
    remaining = {row["key"] for row in original_list("raster-masks/sha256/")}
    assert set(protected) <= remaining
    assert orphan["object_key"] not in remaining
    assert result["errors"] == 0 and result["deleted"] >= 1
    print(
        json.dumps(
            {
                "action": action,
                "result": result,
                "protected": protected,
                "orphan_deleted": True,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
