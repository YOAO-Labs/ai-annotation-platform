"""Explicit GC/version-loss injection in an owned disposable E2E environment."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

_APP_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _APP_DIR.parent
sys.path.insert(0, str(_APP_DIR / "api"))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from worktree_env import (  # noqa: E402
    load_identity,
    read_state,
    state_path,
    topology,
    worktree_mode,
)
from worktree_runtime import WorktreeError, require_owner  # noqa: E402

_RESOURCE_ROOT = Path(
    os.environ.get("AAP_E2E_RESOURCE_ROOT", str(_REPO_ROOT))
).resolve()
OWNER_TAG = "yoao-worktree-owner"


class GuardError(AssertionError):
    """Rejected bucket/resource identity before any GC work runs."""


def assert_owned_disposable_bucket(
    *,
    mode: str | None,
    resources: dict | None,
    database: str,
    bucket: str,
    owner_tag: str | None,
    resource_root: Path = _REPO_ROOT,
) -> None:
    """Fail closed unless ``bucket`` is an exclusively owned disposable test bucket.

    Worktree modes (``YOAO_WORKTREE_MODE=test|e2e``) must declare this exact
    database and ``MINIO_BUCKET`` slot in the active resources manifest and
    carry the manifest owner tag (``scripts/worktree_runtime.require_owner``
    semantics: foreign owner raises, missing tag is rejected). Without a
    worktree mode only the legacy isolated-CI bucket naming is accepted, and an
    unrecognized ``yoao-wt-*`` bucket is rejected instead of falling back to it.
    """
    if mode is not None:
        if mode not in {"test", "e2e"}:
            raise GuardError(f"worktree GC helper refuses mode {mode!r}")
        if not resources:
            raise GuardError("worktree mode requires the active resources manifest")
        if resources.get("mode") != mode:
            raise GuardError("resources manifest was created for another mode")
        if resources.get("root") != str(resource_root):
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
    if bucket.startswith(("yoao-wt-", "aap-wt-")):
        raise GuardError("worktree bucket requires an active worktree mode")
    if not bucket.endswith(("-e2e", "_e2e", "-test", "_test")):
        raise GuardError("legacy isolated CI bucket naming required")


def _bucket_owner_tag(storage: object, bucket: str) -> str | None:
    try:
        tags = storage.client.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except Exception:
        return None
    return next((tag["Value"] for tag in tags if tag["Key"] == OWNER_TAG), None)


async def require_fixture_annotations(
    db, task_id, source_id, created_id, run_id, suite_id
):
    owned = await db.scalar(
        text(
            "SELECT count(*) FROM public.annotations a "
            "JOIN public.tasks t ON t.id=a.task_id AND t.project_id=a.project_id "
            "JOIN yoao_fixture_v1.fixture_run_resources rt ON rt.resource_id=t.id "
            "  AND rt.kind='task' AND rt.logical_key='workbench-1' AND rt.state='published' "
            "JOIN yoao_fixture_v1.fixture_run_resources rp ON rp.run_id=rt.run_id "
            "  AND rp.resource_id=t.project_id AND rp.kind='project' "
            "  AND rp.logical_key='workbench' AND rp.state='published' "
            "JOIN yoao_fixture_v1.fixture_runs r ON r.id=rt.run_id "
            "JOIN yoao_fixture_v1.fixture_runs s ON s.id=r.parent_run_id "
            "WHERE t.id=:task AND a.id IN (:source, :created) AND r.id=:run AND s.id=:suite "
            "  AND r.family='owned' AND s.family='owned' AND s.parent_run_id IS NULL "
            "  AND r.state='open' AND s.state='open'"
        ),
        {
            "task": task_id,
            "run": run_id,
            "suite": suite_id,
            "source": source_id,
            "created": created_id,
        },
    )
    if source_id == created_id or owned != 2:
        raise GuardError("slice annotations do not belong to the selected fixture")


async def run(action: str, task_id: str, operation_id: str, run_id: str):
    mode = worktree_mode(os.environ)
    if action not in {"gc", "drop_revision", "expire_revision"}:
        raise GuardError("unknown mask lifecycle action")
    task_id, operation_id, run_id = UUID(task_id), UUID(operation_id), UUID(run_id)
    url = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    if not url.database or not url.database.endswith(("_e2e", "_test")):
        raise GuardError("mask lifecycle requires a disposable database")
    if (
        os.environ.get("E2E_SEED_ENABLED") != "true"
        or os.environ.get("ENVIRONMENT") != "development"
    ):
        raise GuardError("mask lifecycle requires E2E development mode")
    bucket = os.environ.get("MINIO_BUCKET", "")
    resources = None
    if mode is not None:
        if mode not in {"test", "e2e"} or not _RESOURCE_ROOT.is_relative_to(_REPO_ROOT):
            raise GuardError(
                "resource root must be inside this checkout in test or e2e mode"
            )
        manifest = read_state(state_path(_RESOURCE_ROOT, mode, "resources.json"))
        resources = manifest["resources"]
        if resources != topology(_RESOURCE_ROOT, load_identity(_RESOURCE_ROOT), mode):
            raise GuardError("resource manifest does not match its worktree identity")
    from app.workers._db import task_session
    from app.services.storage import storage_service
    from app.services import raster_mask_objects, raster_object_storage
    from app.services.raster_mask_storage import load_coco_rle, store_coco_rle
    from app.workers.cleanup import _purge_unreferenced_raster_masks_async

    assert storage_service.bucket == bucket
    assert_owned_disposable_bucket(
        mode=mode,
        resources=resources,
        database=url.database,
        bucket=bucket,
        owner_tag=_bucket_owner_tag(storage_service, bucket),
        resource_root=_RESOURCE_ROOT,
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
        restore = operation["response_json"]["slice_restore"]
        source_id = UUID(restore["source_annotation_id"])
        created_id = UUID(restore["created_annotation_id"])
        await require_fixture_annotations(
            db,
            task_id,
            source_id,
            created_id,
            run_id,
            UUID(os.environ["PLAYWRIGHT_FIXTURE_RUN_ID"]),
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
            return {"action": action, "changed": 1}
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
        references = [row["mask"] for row in rows if row.get("type") == "raster_mask"]
        protected = [reference["object_key"] for reference in references]
        before = [await load_coco_rle(reference) for reference in references]
        gc_time = datetime.now(timezone.utc) + timedelta(days=2)
        assert (
            datetime.fromisoformat(
                operation["response_json"]["slice_restore"]["restore_expires_at"]
            )
            > gc_time
        )

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
    async with task_session() as db:
        owner = await db.scalar(
            select(raster_mask_objects.Object).where(
                raster_mask_objects.Object.logical_key == orphan["object_key"]
            )
        )
        receipt = raster_object_storage.snapshot(owner)
        assert raster_object_storage.resolve(receipt).bucket == bucket
        target = await raster_object_storage.io_target(db, receipt)
    with patch.object(raster_mask_objects, "now", return_value=gc_time):
        retired = await _purge_unreferenced_raster_masks_async(dry_run=False)
        async with task_session() as db:
            owner = await db.get(raster_mask_objects.Object, receipt.id)
            assert owner.state == "retired"
        result = await _purge_unreferenced_raster_masks_async(dry_run=False)
    assert [await load_coco_rle(reference) for reference in references] == before
    assert (
        await asyncio.to_thread(
            raster_object_storage.run_io,
            target,
            storage_service.client,
            "read",
            receipt,
            missing_ok=True,
        )
        is None
    )
    assert retired["errors"] == result["errors"] == 0
    assert result["deleted"] >= 1
    return {
        "action": action,
        "result": result,
        "retirement": retired,
        "protected": protected,
        "orphan_deleted": True,
    }
