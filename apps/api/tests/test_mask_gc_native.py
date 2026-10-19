"""Database behavior for native Mask GC fencing and durable receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
import pytest

from app.db.models.annotation import Annotation
from tests.test_video_tracker_jobs_list import _make_video_task


ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql/0229_mask_gc.sql"


async def _native_call(db, operation: str, payload: dict):
    result = await db.execute(
        text(f"SELECT aap_labeling_v1.{operation}(CAST(:payload AS jsonb))"),
        {"payload": json.dumps(payload)},
    )
    return result.scalar_one()


def _key() -> str:
    digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"


def _annotation(task, user_id, key: str) -> Annotation:
    # Geometry remains a non-mask type so the historical annotation revision
    # trigger does not introduce a second independent root for this lifecycle test.
    return Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "mask_source": {"object_key": key}},
    )


async def _stage_one_old_candidate(db, key: str, *, expected_staged: int = 1):
    worker_id = uuid.uuid4()
    claim = await _native_call(
        db,
        "claim_mask_gc_sweep",
        {"worker_id": str(worker_id), "lease_seconds": 30},
    )
    assert claim["ok"] is True
    sweep = claim["data"]["sweep"]
    assert sweep is not None
    last_modified = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(
        timespec="microseconds"
    )
    staged = await _native_call(
        db,
        "stage_mask_gc_page",
        {
            "sweep_id": sweep["sweep_id"],
            "generation": sweep["generation"],
            "lease_token": sweep["lease_token"],
            "after_key": sweep["after_key"],
            "page": {
                "items": [
                    {
                        "object_key": key,
                        "etag": "gc-test-etag",
                        "last_modified": last_modified,
                        "byte_size": 123,
                    }
                ],
                "after_key": key,
                "complete": True,
            },
        },
    )
    assert staged["ok"] is True
    assert staged["data"]["staged"] == expected_staged
    return worker_id, last_modified


async def test_terminal_epoch_rejects_stale_root_and_allows_live_root_copy(
    db_session, super_admin
):
    user, _ = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    key = _key()
    source = _annotation(task, user.id, key)
    db_session.add(source)
    await db_session.flush()
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM public.mask_gc_roots WHERE object_key=:key"),
            {"key": key},
        )
    ).scalar_one() == 1

    await db_session.delete(source)
    await db_session.flush()
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM public.mask_gc_roots WHERE object_key=:key"),
            {"key": key},
        )
    ).scalar_one() == 0

    worker_id, last_modified = await _stage_one_old_candidate(db_session, key)
    claimed = await _native_call(
        db_session,
        "claim_mask_gc_deletes",
        {"worker_id": str(worker_id), "limit": 1, "lease_seconds": 30},
    )
    assert claimed["ok"] is True
    delete_claim = claimed["data"]["items"][0]
    request = {
        "operation_id": delete_claim["operation_id"],
        "object_key": key,
        "etag": "gc-test-etag",
        "last_modified": last_modified,
    }
    finished = await _native_call(
        db_session,
        "finish_mask_gc_delete",
        {
            "operation_id": delete_claim["operation_id"],
            "generation": delete_claim["generation"],
            "lease_token": delete_claim["lease_token"],
            "receipt": {
                "operation_id": delete_claim["operation_id"],
                "request": request,
                "state": "completed",
                "outcome": "missing",
            },
            "error_code": None,
        },
    )
    assert finished["ok"] is True
    assert finished["data"]["completed"] is True
    epoch, active = (
        await db_session.execute(
            text(
                "SELECT epoch, active_operation_id "
                "FROM public.mask_gc_key_epochs WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).one()
    assert epoch == 1
    assert active is None

    begun = await _native_call(
        db_session,
        "begin_mask_content_write",
        {"object_keys": [key]},
    )
    assert begun["ok"] is True
    assert begun["data"]["items"] == [{"object_key": key, "epoch": 1}]

    source = _annotation(task, user.id, key)
    db_session.add(source)
    await db_session.flush()
    await db_session.execute(
        text("SELECT set_config('aap.mask_gc_write_permit', '', true)")
    )
    copied = _annotation(task, user.id, key)
    db_session.add(copied)
    await db_session.flush()
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM public.mask_gc_roots WHERE object_key=:key"),
            {"key": key},
        )
    ).scalar_one() == 2

    await db_session.delete(copied)
    await db_session.delete(source)
    await db_session.flush()
    with pytest.raises(DBAPIError) as rejected:
        async with db_session.begin_nested():
            db_session.add(_annotation(task, user.id, key))
            await db_session.flush()
    assert getattr(rejected.value.orig, "sqlstate", None) == "AM409"


async def test_active_fence_rejects_root_and_preflight_before_payload_io(
    db_session, super_admin
):
    user, _ = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    key = _key()
    worker_id, _ = await _stage_one_old_candidate(db_session, key)

    epochs = await _native_call(
        db_session,
        "read_mask_write_epochs",
        {"object_keys": [key]},
    )
    assert epochs == {
        "schema": "aap-labeling-mask-gc/v1",
        "ok": False,
        "error": {"code": "content_deleting"},
    }

    with pytest.raises(DBAPIError) as rejected:
        async with db_session.begin_nested():
            db_session.add(_annotation(task, user.id, key))
            await db_session.flush()
    assert getattr(rejected.value.orig, "sqlstate", None) == "AM409"

    progress = await _native_call(db_session, "read_mask_gc_progress", {})
    assert progress["ok"] is True
    assert progress["data"]["active_intents"] == 1


async def test_begin_write_grace_protects_old_immutable_object_after_commit(
    db_session,
):
    key = _key()
    read = await _native_call(
        db_session, "read_mask_write_epochs", {"object_keys": [key]}
    )
    assert read["ok"] is True
    assert read["data"]["items"] == [{"object_key": key, "epoch": 0}]
    assert (
        await db_session.execute(
            text(
                "SELECT count(*) FROM public.mask_gc_key_epochs WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).scalar_one() == 0

    locked = await _native_call(
        db_session,
        "lock_mask_write_epochs",
        {"items": [{"object_key": key, "epoch": 0}]},
    )
    assert locked["ok"] is True
    assert (
        await db_session.execute(
            text(
                "SELECT count(*) FROM public.mask_gc_key_epochs WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).scalar_one() == 0

    begun = await _native_call(
        db_session, "begin_mask_content_write", {"object_keys": [key]}
    )
    assert begun["ok"] is True
    epoch, last_write_at = (
        await db_session.execute(
            text(
                "SELECT epoch, last_write_at FROM public.mask_gc_key_epochs "
                "WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).one()
    assert epoch == 0
    assert last_write_at is not None
    await db_session.execute(
        text(
            "UPDATE public.mask_gc_key_epochs "
            "SET last_write_at = clock_timestamp() - interval '25 hours' "
            "WHERE object_key=:key"
        ),
        {"key": key},
    )
    old_write_at = (
        await db_session.execute(
            text(
                "SELECT last_write_at FROM public.mask_gc_key_epochs "
                "WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).scalar_one()
    assert (
        await _native_call(db_session, "read_mask_write_epochs", {"object_keys": [key]})
    )["ok"] is True
    assert (
        await _native_call(
            db_session,
            "lock_mask_write_epochs",
            {"items": [{"object_key": key, "epoch": 0}]},
        )
    )["ok"] is True
    assert (
        await db_session.execute(
            text(
                "SELECT last_write_at FROM public.mask_gc_key_epochs "
                "WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).scalar_one() == old_write_at
    renewed = await _native_call(
        db_session, "begin_mask_content_write", {"object_keys": [key]}
    )
    assert renewed["ok"] is True
    last_write_at = (
        await db_session.execute(
            text(
                "SELECT last_write_at FROM public.mask_gc_key_epochs "
                "WHERE object_key=:key"
            ),
            {"key": key},
        )
    ).scalar_one()
    assert last_write_at > old_write_at

    # Ending this transaction mirrors Tracker's committed materialization
    # windows, where the final reviewable candidate root does not exist yet.
    await db_session.commit()
    worker_id, _ = await _stage_one_old_candidate(db_session, key, expected_staged=0)
    progress = await _native_call(db_session, "read_mask_gc_progress", {})
    assert progress["ok"] is True
    assert progress["data"]["active_intents"] == 0
    assert progress["data"]["indeterminate_intents"] == 0
    assert progress["data"]["completed_intents"] == 0
    assert (
        await db_session.execute(
            text(
                "SELECT count(*) FROM public.mask_gc_intents "
                "WHERE object_key=:key AND state <> 'completed'"
            ),
            {"key": key},
        )
    ).scalar_one() == 0


def test_native_roots_cover_retained_mask_sources_and_expiry_rules():
    sql = SQL.read_text(encoding="utf-8")
    for trigger in (
        "trg_mask_gc_roots_annotations",
        "trg_mask_gc_roots_predictions",
        "trg_mask_gc_roots_uploads",
        "trg_mask_gc_roots_tracker_jobs",
        "trg_mask_gc_roots_accept_decisions",
        "trg_mask_gc_roots_revisions",
        "trg_mask_gc_roots_qc_runs",
        "trg_mask_gc_roots_qc_issues",
        "trg_mask_gc_roots_review_scopes",
        "trg_mask_gc_roots_tracker_scopes",
        "trg_mask_gc_roots_repair_batches",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql
    assert "video_tracker_input_referenced" in sql
    assert "pending_review', 'partially_reviewed', 'cancelled" in sql
    assert "rollback_expires_at" in sql
    assert "expires_at" in sql
    assert "annotation_video_tracker_input_sources" in sql
    assert "trg_mask_gc_tracker_pins_retention" in sql


def test_epoch_gate_keeps_completed_epoch_and_advances_scan_after_skips():
    sql = SQL.read_text(encoding="utf-8")
    assert "active_operation_id = NULL" in sql
    assert "SET epoch = EXCLUDED.epoch" in sql
    assert "epoch = EXCLUDED.epoch" in sql
    assert "NOT aap_labeling_v1._mask_gc_has_live_root(v_ref.object_key, false)" in sql
    assert "aap_labeling_v1._mask_gc_has_live_root(v_key, true)" in sql
    assert "RAISE EXCEPTION USING ERRCODE = 'AM409'" in sql
    assert "upload.created_at >= v_sweep.cutoff" in sql
    assert "v_last_write_at >= v_sweep.cutoff" in sql
    assert "after_key = v_page_after_key" in sql
